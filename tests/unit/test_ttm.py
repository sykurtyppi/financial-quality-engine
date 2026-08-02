"""P0-A: TTM annualization and basis discipline.

The measured failure this guards against: net_debt_to_ebitda computed on a
single quarter's EBITDA overstated leverage ~4x (GLW 7.39x vs ~1.85x true).
"""

from __future__ import annotations

from datetime import date

from app.schemas.financials import CompanyDataset, CompanyProfile, PeriodFinancials, PeriodType
from app.schemas.metrics import MetricStatus
from app.services.formulas import registry, ttm


def _q(end: date, label: str, **kwargs) -> PeriodFinancials:
    return PeriodFinancials(
        period_end=end, period_type=PeriodType.QUARTER, fiscal_label=label, **kwargs
    )


def _four_quarters(**overrides) -> list[PeriodFinancials]:
    base = dict(
        revenue=100.0,
        net_income=10.0,
        cfo=12.0,
        capex=5.0,
        ebit=15.0,
        depreciation_amortization=8.0,
        total_assets=400.0,
        total_debt=200.0,
        cash_and_equivalents=50.0,
    )
    base.update(overrides)
    ends = [date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31)]
    labels = ["FY2025Q1", "FY2025Q2", "FY2025Q3", "FY2025Q4"]
    return [_q(e, lbl, **base) for e, lbl in zip(ends, labels)]


class TestAnnualize:
    def test_sums_flows_keeps_instants(self):
        periods = _four_quarters()
        result = ttm.annualize(periods, 3)
        assert result is not None
        assert result.revenue == 400.0  # flow: summed
        assert result.ebit == 60.0
        assert result.depreciation_amortization == 32.0
        assert result.total_debt == 200.0  # instant: ending quarter
        assert result.total_assets == 400.0
        assert result.fiscal_label == "TTM FY2025Q4"

    def test_requires_four_quarters(self):
        periods = _four_quarters()
        assert ttm.annualize(periods, 2) is None
        assert ttm.annualize(periods, -1) is None
        assert ttm.annualize(periods, 4) is None

    def test_rejects_gap_in_window(self):
        periods = _four_quarters()
        # Skip a quarter: Q3 end jumps 6 months after Q2.
        periods[2] = periods[2].model_copy(update={"period_end": date(2025, 12, 30)})
        assert ttm.annualize(periods, 3) is None

    def test_accepts_53_week_quarter(self):
        periods = _four_quarters()
        # 14-week quarter: 98-day gap.
        periods[3] = periods[3].model_copy(update={"period_end": date(2026, 1, 6)})
        assert ttm.annualize(periods, 3) is not None

    def test_missing_flow_in_any_quarter_nulls_the_field(self):
        periods = _four_quarters()
        periods[1] = periods[1].model_copy(update={"cfo": None})
        result = ttm.annualize(periods, 3)
        assert result is not None
        assert result.cfo is None  # partial sums are forbidden
        assert result.revenue == 400.0


class TestRegistryTtmWiring:
    def _dataset(self, quarters: list[PeriodFinancials]) -> CompanyDataset:
        return CompanyDataset(profile=CompanyProfile(ticker="TEST"), periods=quarters)

    def _eight_quarters(self) -> list[PeriodFinancials]:
        q_2024 = _four_quarters()
        shifted = []
        for p in q_2024:
            e = p.period_end
            shifted.append(
                p.model_copy(
                    update={
                        "period_end": date(e.year - 1, e.month, e.day),
                        "fiscal_label": p.fiscal_label.replace("2025", "2024"),
                    }
                )
            )
        return shifted + _four_quarters()

    def test_net_debt_to_ebitda_uses_ttm_denominator(self):
        """The GLW-class fix: (200-50)/(60+32) ≈ 1.63, not (200-50)/23 ≈ 6.5."""
        bundle = registry.compute_metrics(self._dataset(self._eight_quarters()))
        m = bundle.get_latest("net_debt_to_ebitda")
        assert m is not None and m.status is MetricStatus.OK
        assert abs(m.value - (200.0 - 50.0) / (60.0 + 32.0)) < 1e-9
        assert m.fiscal_label.startswith("TTM ")

    def test_beneish_is_yoy_ttm(self):
        bundle = registry.compute_metrics(self._dataset(self._eight_quarters()))
        sgi = bundle.get_latest("beneish_sgi")
        assert sgi is not None and sgi.status is MetricStatus.OK
        assert abs(sgi.value - 1.0) < 1e-9  # flat revenue year over year

    def test_total_accruals_is_annual_scale(self):
        """TTM (NI - CFO) / avg assets: (40-48)/400 = -0.02 (annual vocabulary)."""
        bundle = registry.compute_metrics(self._dataset(self._eight_quarters()))
        m = bundle.get_latest("total_accruals")
        assert m is not None and m.status is MetricStatus.OK
        assert abs(m.value - (40.0 - 48.0) / 400.0) < 1e-9

    def test_short_history_degrades_to_explicit_window_gap(self):
        bundle = registry.compute_metrics(self._dataset(_four_quarters()[:3]))
        m = bundle.get_latest("net_debt_to_ebitda")
        assert m is not None and m.status is MetricStatus.MISSING_DATA
        assert m.missing_fields == [ttm.TTM_WINDOW_MISSING]

    def test_basis_lint_no_ttm_metric_computed_quarterly(self):
        """Every TTM-mandated metric that computes OK must carry a TTM label —
        the lint that prevents the quarterly-denominator bug class returning."""
        bundle = registry.compute_metrics(self._dataset(self._eight_quarters()))
        for name in registry.TTM_BASIS_METRICS:
            m = bundle.get_latest(name)
            assert m is not None, f"{name} not computed at all"
            if m.status is MetricStatus.OK:
                assert m.fiscal_label.startswith("TTM "), f"{name} computed on non-TTM basis"
