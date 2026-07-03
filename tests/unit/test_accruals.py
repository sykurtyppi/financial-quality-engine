from datetime import date

import pytest

from app.schemas.financials import PeriodFinancials, PeriodType
from app.schemas.metrics import MetricStatus
from app.services.formulas import accruals


def make_period(**overrides) -> PeriodFinancials:
    base = dict(
        period_end=date(2025, 12, 31),
        period_type=PeriodType.QUARTER,
        fiscal_label="FY2025Q4",
        net_income=100.0,
        cfo=120.0,
        capex=30.0,
        revenue=1000.0,
        total_assets=2000.0,
    )
    base.update(overrides)
    return PeriodFinancials(**base)


class TestTotalAccruals:
    def test_computes_expected_value(self):
        cur = make_period(net_income=100.0, cfo=80.0, total_assets=2100.0)
        prev = make_period(fiscal_label="FY2025Q3", total_assets=1900.0)
        m = accruals.total_accruals(cur, prev)
        # (100 - 80) / avg(2100, 1900) = 20 / 2000 = 0.01
        assert m.status is MetricStatus.OK
        assert m.value == pytest.approx(0.01)
        assert m.inputs["net_income"] == 100.0

    def test_missing_cfo_reported_not_dropped(self):
        cur = make_period(cfo=None)
        prev = make_period(fiscal_label="FY2025Q3")
        m = accruals.total_accruals(cur, prev)
        assert m.status is MetricStatus.MISSING_DATA
        assert "cfo" in m.missing_fields
        assert m.value is None


class TestCfoToNetIncome:
    def test_positive_earnings(self):
        m = accruals.cfo_to_net_income(make_period(cfo=120.0, net_income=100.0))
        assert m.status is MetricStatus.OK
        assert m.value == pytest.approx(1.2)

    def test_negative_net_income_not_meaningful(self):
        m = accruals.cfo_to_net_income(make_period(net_income=-50.0))
        assert m.status is MetricStatus.NOT_MEANINGFUL
        assert m.value is None
        assert "non-positive" in (m.note or "").lower()

    def test_zero_net_income_not_meaningful(self):
        m = accruals.cfo_to_net_income(make_period(net_income=0.0))
        assert m.status is MetricStatus.NOT_MEANINGFUL


class TestFcfMetrics:
    def test_fcf_to_net_income(self):
        m = accruals.fcf_to_net_income(make_period(cfo=120.0, capex=30.0, net_income=100.0))
        assert m.value == pytest.approx(0.9)

    def test_fcf_margin_zero_revenue_not_meaningful(self):
        m = accruals.fcf_margin(make_period(revenue=0.0))
        assert m.status is MetricStatus.NOT_MEANINGFUL

    def test_fcf_margin_negative_revenue_not_meaningful(self):
        m = accruals.fcf_margin(make_period(revenue=-10.0))
        assert m.status is MetricStatus.NOT_MEANINGFUL


class TestAccrualTrend:
    def _series(self, values):
        out = []
        for i, v in enumerate(values):
            cur = make_period(
                fiscal_label=f"P{i}",
                net_income=100.0 + v * 2000.0,
                cfo=100.0,
                total_assets=2000.0,
            )
            prev = make_period(total_assets=2000.0)
            out.append(accruals.total_accruals(cur, prev))
        return out

    def test_requires_three_periods(self):
        m = accruals.accrual_trend(self._series([0.01, 0.02]))
        assert m.status is MetricStatus.MISSING_DATA

    def test_detects_deterioration(self):
        m = accruals.accrual_trend(self._series([0.01, 0.01, 0.01, 0.05]))
        assert m.status is MetricStatus.OK
        assert m.value == pytest.approx(0.04, abs=1e-9)
