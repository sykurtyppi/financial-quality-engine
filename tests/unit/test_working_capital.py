from datetime import date

import pytest

from app.schemas.financials import PeriodFinancials, PeriodType
from app.schemas.metrics import MetricStatus
from app.services.formulas import working_capital as wc


def q(label: str, **kw) -> PeriodFinancials:
    return PeriodFinancials(
        period_end=date(2025, 12, 31),
        period_type=PeriodType.QUARTER,
        fiscal_label=label,
        **kw,
    )


class TestGrowthSpreads:
    def test_receivables_outpacing_revenue(self):
        prev = q("Q3", receivables=100.0, revenue=1000.0)
        cur = q("Q4", receivables=138.0, revenue=1110.0)
        m = wc.receivables_growth_spread(cur, prev)
        # receivables +38%, revenue +11% -> spread 27%
        assert m.value == pytest.approx(0.27)

    def test_zero_prior_revenue_guarded(self):
        prev = q("Q3", receivables=100.0, revenue=0.0)
        cur = q("Q4", receivables=120.0, revenue=100.0)
        assert wc.receivables_growth_spread(cur, prev).status is MetricStatus.NOT_MEANINGFUL

    def test_missing_inventory_reported(self):
        prev = q("Q3", inventory=None, revenue=1000.0)
        cur = q("Q4", inventory=100.0, revenue=1100.0)
        m = wc.inventory_growth_spread(cur, prev)
        assert m.status is MetricStatus.MISSING_DATA
        assert "inventory_prior" in m.missing_fields


class TestDayCounts:
    def test_dso_quarterly_uses_91_days(self):
        m = wc.dso(q("Q4", receivables=100.0, revenue=910.0))
        assert m.value == pytest.approx(10.0)

    def test_dso_annual_uses_365_days(self):
        p = PeriodFinancials(
            period_end=date(2025, 12, 31),
            period_type=PeriodType.ANNUAL,
            fiscal_label="FY2025",
            receivables=100.0,
            revenue=3650.0,
        )
        assert wc.dso(p).value == pytest.approx(10.0)

    def test_dio_and_dpo(self):
        p = q("Q4", inventory=182.0, accounts_payable=91.0, cost_of_revenue=910.0)
        assert wc.dio(p).value == pytest.approx(18.2)
        assert wc.dpo(p).value == pytest.approx(9.1)

    def test_dio_zero_cogs_not_meaningful(self):
        assert wc.dio(q("Q4", inventory=10.0, cost_of_revenue=0.0)).status is MetricStatus.NOT_MEANINGFUL


class TestWorkingCapitalSwing:
    def test_swing_relative_to_income(self):
        prev = q("Q3", receivables=100.0, inventory=50.0, accounts_payable=30.0)
        cur = q(
            "Q4",
            receivables=140.0,
            inventory=60.0,
            accounts_payable=30.0,
            net_income=100.0,
        )
        m = wc.working_capital_swing_to_income(cur, prev)
        # |(140+60-30) - (100+50-30)| / 100 = 50/100
        assert m.value == pytest.approx(0.5)

    def test_zero_net_income_guarded(self):
        prev = q("Q3", receivables=1.0, inventory=1.0, accounts_payable=1.0)
        cur = q("Q4", receivables=2.0, inventory=1.0, accounts_payable=1.0, net_income=0.0)
        assert wc.working_capital_swing_to_income(cur, prev).status is MetricStatus.NOT_MEANINGFUL

    def test_negative_net_income_still_meaningful(self):
        prev = q("Q3", receivables=100.0, inventory=50.0, accounts_payable=30.0)
        cur = q("Q4", receivables=150.0, inventory=50.0, accounts_payable=30.0, net_income=-25.0)
        m = wc.working_capital_swing_to_income(cur, prev)
        assert m.status is MetricStatus.OK
        assert m.value == pytest.approx(2.0)
