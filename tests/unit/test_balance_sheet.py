from datetime import date

import pytest

from app.schemas.financials import PeriodFinancials, PeriodType
from app.schemas.metrics import MetricStatus
from app.services.formulas import balance_sheet as bs


def q(label: str = "Q4", **kw) -> PeriodFinancials:
    return PeriodFinancials(
        period_end=date(2025, 12, 31),
        period_type=PeriodType.QUARTER,
        fiscal_label=label,
        **kw,
    )


class TestLeverage:
    def test_net_debt_to_ebitda(self):
        m = bs.net_debt_to_ebitda(
            q(total_debt=500.0, cash_and_equivalents=100.0, ebit=80.0, depreciation_amortization=20.0)
        )
        assert m.value == pytest.approx(4.0)

    def test_negative_ebitda_not_meaningful(self):
        m = bs.net_debt_to_ebitda(
            q(total_debt=500.0, cash_and_equivalents=100.0, ebit=-30.0, depreciation_amortization=20.0)
        )
        assert m.status is MetricStatus.NOT_MEANINGFUL

    def test_negative_ebitda_with_net_debt_is_distress_signal(self):
        # ebitda = -10, net_debt = 500-100 = 400 > 0 -> unambiguous distress (P0-9)
        m = bs.net_debt_to_ebitda(
            q(total_debt=500.0, cash_and_equivalents=100.0, ebit=-30.0, depreciation_amortization=20.0)
        )
        assert m.status is MetricStatus.NOT_MEANINGFUL
        assert m.distress_signal is True

    def test_negative_ebitda_with_net_cash_is_benign_drop(self):
        # ebitda = -10 but net cash (50 debt vs 500 cash): not a leverage concern.
        m = bs.net_debt_to_ebitda(
            q(total_debt=50.0, cash_and_equivalents=500.0, ebit=-30.0, depreciation_amortization=20.0)
        )
        assert m.status is MetricStatus.NOT_MEANINGFUL
        assert m.distress_signal is False

    def test_interest_coverage(self):
        assert bs.interest_coverage(q(ebit=80.0, interest_expense=10.0)).value == pytest.approx(8.0)

    def test_zero_interest_guarded(self):
        assert bs.interest_coverage(q(ebit=80.0, interest_expense=0.0)).status is MetricStatus.NOT_MEANINGFUL

    def test_leverage_change(self):
        prev = q("Q3", total_debt=200.0, total_assets=1000.0)
        cur = q("Q4", total_debt=300.0, total_assets=1000.0)
        assert bs.leverage_change(cur, prev).value == pytest.approx(0.1)


class TestAssetQuality:
    def test_asset_quality_proxy(self):
        m = bs.asset_quality_proxy(q(current_assets=400.0, ppe_net=300.0, total_assets=1000.0))
        assert m.value == pytest.approx(0.3)

    def test_intangibles_to_assets(self):
        m = bs.intangibles_to_assets(q(intangible_assets=100.0, goodwill=200.0, total_assets=1000.0))
        assert m.value == pytest.approx(0.3)

    def test_goodwill_growth_zero_prior_guarded(self):
        prev = q("Q3", goodwill=0.0)
        cur = q("Q4", goodwill=100.0)
        assert bs.goodwill_growth(cur, prev).status is MetricStatus.NOT_MEANINGFUL

    def test_current_ratio(self):
        assert bs.current_ratio(q(current_assets=300.0, current_liabilities=200.0)).value == pytest.approx(1.5)
