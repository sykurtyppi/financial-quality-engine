from datetime import date

import pytest

from app.core.pipeline import analyze
from app.schemas.financials import (
    CompanyDataset,
    CompanyProfile,
    PeriodFinancials,
    PeriodType,
)
from app.schemas.scoring import Direction
from tests.fixtures.companies import clean_dataset, stretch_dataset

QUARTER_END = [(3, 31), (6, 30), (9, 30), (12, 31)]


def quarter_end(i: int, start_year: int = 2024) -> date:
    month, day = QUARTER_END[i % 4]
    return date(start_year + i // 4, month, day)


class TestStretchCo:
    @staticmethod
    @pytest.fixture(scope="class")
    def result():
        return analyze(stretch_dataset())

    def test_overall_score_computed(self, result):
        assert result.overall is not None
        assert result.overall.score is not None

    def test_red_flags_present_with_evidence(self, result):
        assert result.red_flags
        for flag in result.red_flags:
            assert flag.evidence_metrics
            assert "review" in flag.detail.lower()

    def test_cash_flow_gap_is_flagged_on_ttm_basis(self, result):
        """P0-A: the cash-conversion flag must fire on the TTM value, not the
        collapsing single-quarter value. Exact rank among this fixture's
        deliberately extreme values is not a design contract."""
        flag = next(
            f
            for f in result.red_flags
            if f.title == "Operating cash flow lagging reported earnings"
        )
        assert flag.fiscal_label.startswith("TTM ")

    def test_receivables_divergence_carries_elevated_concern(self, result):
        rq = next(b for b in result.block_scores if b.name == "Revenue Quality")
        comp = next(c for c in rq.components if c.metric_name == "receivables_growth_spread")
        assert comp.concern_score is not None and comp.concern_score >= 70

    def test_narrative_findings_present(self, result):
        kinds = {nf.kind for nf in result.narrative_findings}
        assert "adjustment_recurrence" in kinds
        assert "kpi_removed" in kinds

    def test_evidence_ledger_populated(self, result):
        assert len(result.evidence) > 15
        entry = result.evidence[0]
        assert entry.formula
        assert entry.inputs

    def test_no_accusatory_language(self, result):
        banned = ("fraud", "manipulat", "deceptive", "cooking")
        for flag in result.red_flags:
            text = (flag.title + flag.detail).lower()
            assert not any(b in text for b in banned)
        for q in result.analyst_questions:
            assert not any(b in q.lower() for b in banned)


class TestCleanCo:
    @staticmethod
    @pytest.fixture(scope="class")
    def result():
        return analyze(clean_dataset())

    def test_scores_lower_than_stretch(self, result):
        stretch = analyze(stretch_dataset())
        assert result.overall.score < stretch.overall.score

    def test_green_flags_present(self, result):
        assert result.green_flags

    def test_clean_direction_not_negative(self, result):
        assert result.overall.direction in (Direction.POSITIVE, Direction.MIXED)


class TestExclusionsAndEdgeCases:
    def test_financial_institution_excluded(self):
        ds = stretch_dataset()
        ds.profile.is_financial_institution = True
        result = analyze(ds)
        assert result.excluded
        assert result.metrics == []
        assert "excluded" in (result.exclusion_reason or "").lower()

    def test_single_period_rejected(self):
        ds = stretch_dataset()
        ds.periods = ds.periods[:1]
        with pytest.raises(ValueError, match="at least 2 periods"):
            analyze(ds)

    def test_high_growth_saas_gets_caveat_not_just_flags(self):
        """A hypergrowth company with scale-up working capital build must carry
        the high-growth caveat on growth-sensitive blocks (false-positive control)."""
        periods = []
        for i in range(8):
            rev = 100.0 * (1.5**i)  # 50% q/q growth
            periods.append(
                PeriodFinancials(
                    period_end=quarter_end(i),
                    period_type=PeriodType.QUARTER,
                    fiscal_label=f"P{i}",
                    revenue=rev,
                    cost_of_revenue=rev * 0.3,
                    net_income=rev * 0.05,
                    cfo=rev * 0.10,
                    capex=rev * 0.15 * (1 + 0.1 * i),
                    receivables=rev * 0.6 * (1 + 0.02 * i),
                    inventory=rev * 0.1,
                    accounts_payable=rev * 0.2,
                    total_assets=rev * 5,
                    current_assets=rev * 2,
                    ppe_net=rev * 1.5,
                    stock_based_compensation=rev * 0.15,
                )
            )
        ds = CompanyDataset(
            profile=CompanyProfile(ticker="GROWCO", sector="Technology"),
            periods=periods,
        )
        result = analyze(ds)
        growth_blocks = [b for b in result.block_scores if b.name == "Capex Discipline"]
        assert any("High-growth profile" in c for c in growth_blocks[0].caveats)

    def test_missing_data_company_reports_gaps_not_scores(self):
        """A dataset with only revenue/net income must not fabricate an overall score."""
        periods = [
            PeriodFinancials(
                period_end=date(2025, 3, 31),
                period_type=PeriodType.QUARTER,
                fiscal_label="Q1",
                revenue=100.0,
                net_income=10.0,
            ),
            PeriodFinancials(
                period_end=date(2025, 6, 30),
                period_type=PeriodType.QUARTER,
                fiscal_label="Q2",
                revenue=110.0,
                net_income=11.0,
            ),
        ]
        ds = CompanyDataset(profile=CompanyProfile(ticker="SPARSE"), periods=periods)
        result = analyze(ds)
        assert result.overall.score is None
        missing = [m for m in result.metrics if m.status.value == "missing_data"]
        assert missing

    def test_negative_earnings_company_handled(self):
        """Loss-making company: earnings-ratio metrics report not_meaningful,
        pipeline still completes. Eight quarters so the TTM accrual metric has
        a date-valid year-ago asset base (PR #2 review finding 1: no silent
        asset substitution on short histories)."""
        periods = []
        for i in range(8):
            periods.append(
                PeriodFinancials(
                    period_end=quarter_end(i, start_year=2025),
                    period_type=PeriodType.QUARTER,
                    fiscal_label=f"Q{i + 1}",
                    revenue=100.0,
                    cost_of_revenue=60.0,
                    net_income=-20.0,
                    cfo=-5.0,
                    capex=10.0,
                    total_assets=500.0,
                    receivables=50.0,
                    inventory=20.0,
                    accounts_payable=15.0,
                )
            )
        ds = CompanyDataset(profile=CompanyProfile(ticker="LOSSCO"), periods=periods)
        result = analyze(ds)
        by_name = {m.name: m for m in result.metrics}
        assert by_name["cfo_to_net_income"].status.value == "not_meaningful"
        assert by_name["total_accruals"].status.value == "ok"
