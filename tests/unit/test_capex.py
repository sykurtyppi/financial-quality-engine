from datetime import date

import pytest

from app.schemas.financials import PeriodFinancials, PeriodType
from app.schemas.metrics import MetricStatus
from app.services.formulas import capex


def q(label: str, revenue: float | None = 1000.0, capex_v: float | None = 50.0, **kw) -> PeriodFinancials:
    return PeriodFinancials(
        period_end=date(2025, 12, 31),
        period_type=PeriodType.QUARTER,
        fiscal_label=label,
        revenue=revenue,
        capex=capex_v,
        **kw,
    )


class TestCapexRatios:
    def test_capex_to_revenue(self):
        assert capex.capex_to_revenue(q("Q4")).value == pytest.approx(0.05)

    def test_capex_growth_spread(self):
        prev = q("Q3", revenue=1000.0, capex_v=50.0)
        cur = q("Q4", revenue=1100.0, capex_v=75.0)
        # capex +50%, revenue +10% -> 40%
        assert capex.capex_growth_spread(cur, prev).value == pytest.approx(0.4)

    def test_capex_to_da(self):
        m = capex.capex_to_da(q("Q4", depreciation_amortization=25.0))
        assert m.value == pytest.approx(2.0)

    def test_capex_growth_spread_negative_prior_guarded(self):
        # Round-15 finding 6 (same class as R14 F1/F2): a negative prior capex
        # used to pass `== 0` and produce a spurious 3.0 growth spread via
        # `(20 - (-10)) / abs(-10) = 3.0`.
        prev = q("Q3", revenue=1000.0, capex_v=-10.0)
        cur = q("Q4", revenue=1100.0, capex_v=20.0)
        m = capex.capex_growth_spread(cur, prev)
        assert m.status is MetricStatus.NOT_MEANINGFUL
        assert "Non-positive" in (m.note or "")

    def test_capex_growth_spread_zero_prior_guarded(self):
        prev = q("Q3", revenue=1000.0, capex_v=0.0)
        cur = q("Q4", revenue=1100.0, capex_v=20.0)
        assert capex.capex_growth_spread(cur, prev).status is MetricStatus.NOT_MEANINGFUL

    def test_capex_to_da_negative_da_guarded(self):
        # Round-15 finding 6: negative D&A used to slip through the `== 0`
        # guard and produce sign-flipped ratios like capex=20, D&A=-10 → -2.0
        # as an OK value.
        m = capex.capex_to_da(q("Q4", depreciation_amortization=-10.0))
        assert m.status is MetricStatus.NOT_MEANINGFUL
        assert "Non-positive" in (m.note or "")

    def test_capex_to_da_zero_da_guarded(self):
        m = capex.capex_to_da(q("Q4", depreciation_amortization=0.0))
        assert m.status is MetricStatus.NOT_MEANINGFUL


class TestRegimeShift:
    def test_needs_history(self):
        series = [q(f"Q{i}") for i in range(4)]
        assert capex.capex_intensity_regime_shift(series).status is MetricStatus.MISSING_DATA

    def test_detects_step_up(self):
        # 4 quarters at 5% intensity, then 4 quarters at 10%
        series = [q(f"P{i}", revenue=1000.0, capex_v=50.0) for i in range(4)]
        series += [q(f"P{i + 4}", revenue=1000.0, capex_v=100.0) for i in range(4)]
        m = capex.capex_intensity_regime_shift(series)
        assert m.status is MetricStatus.OK
        assert m.value == pytest.approx(0.05)

    def test_skips_zero_revenue_periods(self):
        series = [q(f"P{i}", revenue=1000.0, capex_v=50.0) for i in range(6)]
        series.insert(3, q("BAD", revenue=0.0, capex_v=50.0))
        m = capex.capex_intensity_regime_shift(series)
        assert m.status is MetricStatus.OK


class TestIncrementalRevenuePerCapex:
    def test_computes(self):
        series = [q(f"P{i}", revenue=1000.0 + 50.0 * i, capex_v=100.0) for i in range(5)]
        m = capex.incremental_revenue_per_capex(series)
        # (1200 - 1000) / 400 = 0.5
        assert m.value == pytest.approx(0.5)

    def test_missing_capex_reported(self):
        series = [q(f"P{i}", revenue=1000.0 + 50.0 * i, capex_v=None) for i in range(5)]
        m = capex.incremental_revenue_per_capex(series)
        assert m.status is MetricStatus.MISSING_DATA
        assert "capex" in m.missing_fields
