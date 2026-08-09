"""Distress thermometer tests (P1-C). Verifies the AOM aggregation
(average-within-cluster, max-across-clusters) and additive regime dummies."""

from datetime import date

from app.schemas.financials import PeriodFinancials, PeriodType
from app.schemas.scoring import BlockScore, ComponentContribution, Confidence, Direction
from app.services.scoring.thermometer import compute_thermometer


def _component(name: str, concern: float | None) -> ComponentContribution:
    return ComponentContribution(
        metric_name=name,
        metric_value=1.0 if concern is not None else None,
        concern_score=concern,
        weight=1.0,
        anchors=[(0.0, 0.0), (1.0, 100.0)],
        status="ok" if concern is not None else "missing_data",
    )


def _block(name: str, concerns: dict[str, float | None]) -> BlockScore:
    return BlockScore(
        name=name,
        score=50.0,
        direction=Direction.MIXED,
        confidence=Confidence.MEDIUM,
        rationale="test",
        components=[_component(n, c) for n, c in concerns.items()],
        data_coverage=1.0,
    )


def _period(label: str, **kw) -> PeriodFinancials:
    return PeriodFinancials(
        period_end=kw.pop("period_end", date(2025, 12, 31)),
        period_type=PeriodType.QUARTER,
        fiscal_label=label,
        **kw,
    )


HEALTHY = [_period("FY2025Q4", net_income=100.0, ebit=120.0, depreciation_amortization=20.0)]


class TestAOMAggregation:
    def test_max_across_clusters_not_diluted_by_calm_clusters(self):
        # One screaming cluster (leverage 90), three calm ones (~10). Weighted
        # averaging would bury the 90; AOM must surface it.
        blocks = [
            _block("Balance Sheet Stress", {"net_debt_to_ebitda": 90.0, "interest_coverage": 90.0}),
            _block("Cash Conversion", {"cfo_to_net_income": 10.0, "fcf_margin": 10.0}),
            _block("Capital Integrity", {"issuance_pressure": 10.0}),
        ]
        t = compute_thermometer(blocks, HEALTHY)
        assert t.reading == 90.0  # max across clusters
        assert t.hottest_cluster.name == "Leverage & Coverage"

    def test_average_within_cluster(self):
        blocks = [_block("Cash Conversion", {"cfo_to_net_income": 80.0, "fcf_margin": 40.0})]
        t = compute_thermometer(blocks, HEALTHY)
        # single cluster: mean(80, 40) = 60
        assert t.reading == 60.0
        assert t.clusters[0].concern == 60.0

    def test_none_when_nothing_computable(self):
        blocks = [_block("Narrative Drift", {"adjustment_recurrence_ratio": 50.0})]
        # no distress-cluster metrics, healthy financials -> no reading
        t = compute_thermometer(blocks, HEALTHY)
        assert t.reading is None


class TestRegimeDummies:
    def test_negative_ebitda_adds_concern(self):
        blocks = [_block("Cash Conversion", {"cfo_to_net_income": 40.0})]
        distressed = [_period("FY2025Q4", net_income=50.0, ebit=-30.0, depreciation_amortization=10.0)]
        t = compute_thermometer(blocks, distressed)
        assert any(f.code == "EBITDA_NEGATIVE" for f in t.regime_flags)
        assert t.reading == 65.0  # base 40 + 25

    def test_two_quarter_loss_supersedes_single(self):
        two_q_loss = [
            _period("FY2025Q3", period_end=date(2025, 9, 30), net_income=-10.0),
            _period("FY2025Q4", net_income=-20.0),
        ]
        t = compute_thermometer([], two_q_loss)
        codes = {f.code for f in t.regime_flags}
        assert "NI_NEGATIVE_2Q" in codes
        assert "NI_NEGATIVE" not in codes

    def test_regime_dummy_alone_still_reads(self):
        # Negative EBITDA with no scored distress clusters must still read hot,
        # not vanish (the P0-9 inversion, at thermometer level).
        distressed = [_period("FY2025Q4", net_income=-5.0, ebit=-30.0, depreciation_amortization=10.0)]
        t = compute_thermometer([], distressed)
        assert t.reading is not None
        assert t.reading >= 25.0

    def test_reading_capped_at_100(self):
        blocks = [_block("Balance Sheet Stress", {"net_debt_to_ebitda": 95.0})]
        distressed = [
            _period("FY2025Q3", period_end=date(2025, 9, 30), net_income=-10.0),
            _period("FY2025Q4", net_income=-20.0, ebit=-30.0, depreciation_amortization=10.0),
        ]
        t = compute_thermometer(blocks, distressed)
        assert t.reading == 100.0  # 95 + 25 + 20 capped
