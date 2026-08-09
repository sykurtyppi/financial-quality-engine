from datetime import date

import pytest

from app.schemas.financials import PeriodFinancials, PeriodType
from app.schemas.metrics import MetricStatus
from app.services.formulas import capital_structure as cs


def q(label: str = "Q4", **kw) -> PeriodFinancials:
    return PeriodFinancials(
        period_end=date(2025, 12, 31),
        period_type=PeriodType.QUARTER,
        fiscal_label=label,
        **kw,
    )


class TestIssuancePressureDistress:
    def test_negative_cfo_is_distress_signal(self):
        # P0-9: negative CFO means the firm cannot self-fund; issuance
        # dependence is total -> maximum concern, not a drop.
        m = cs.issuance_pressure(q(share_issuance_proceeds=100.0, cfo=-10.0))
        assert m.status is MetricStatus.NOT_MEANINGFUL
        assert m.distress_signal is True

    def test_positive_cfo_computes_normally(self):
        m = cs.issuance_pressure(q(share_issuance_proceeds=50.0, cfo=200.0))
        assert m.status is MetricStatus.OK
        assert m.value == pytest.approx(0.25)

    def test_zero_issuance_negative_cfo_is_not_distress(self):
        # Review finding 5: negative CFO with NO issuance is not "issuance
        # dependence" — the corroborating condition (material issuance) is absent.
        m = cs.issuance_pressure(q(share_issuance_proceeds=0.0, cfo=-10.0))
        assert m.distress_signal is False

    def test_material_issuance_negative_cfo_is_distress(self):
        m = cs.issuance_pressure(q(share_issuance_proceeds=100.0, cfo=-10.0))
        assert m.status is MetricStatus.NOT_MEANINGFUL
        assert m.distress_signal is True


class TestSbc:
    def test_sbc_to_revenue(self):
        assert cs.sbc_to_revenue(q(stock_based_compensation=80.0, revenue=1000.0)).value == pytest.approx(0.08)

    def test_sbc_to_cfo_negative_cfo_not_meaningful(self):
        m = cs.sbc_to_cfo(q(stock_based_compensation=80.0, cfo=-10.0))
        assert m.status is MetricStatus.NOT_MEANINGFUL

    def test_buyback_offset(self):
        m = cs.buyback_offset_ratio(q(buybacks=40.0, stock_based_compensation=80.0))
        assert m.value == pytest.approx(0.5)

    def test_buyback_offset_zero_sbc_guarded(self):
        assert (
            cs.buyback_offset_ratio(q(buybacks=40.0, stock_based_compensation=0.0)).status
            is MetricStatus.NOT_MEANINGFUL
        )


class TestDilution:
    def test_diluted_share_growth(self):
        prev = q("Q3", shares_diluted=100.0)
        cur = q("Q4", shares_diluted=103.0)
        assert cs.diluted_share_growth(cur, prev).value == pytest.approx(0.03)

    def test_net_share_count_change_buyback_shrink(self):
        prev = q("Q3", shares_outstanding=100.0)
        cur = q("Q4", shares_outstanding=98.0)
        assert cs.net_share_count_change(cur, prev).value == pytest.approx(-0.02)

    def test_missing_share_count_reported(self):
        prev = q("Q3", shares_diluted=None)
        cur = q("Q4", shares_diluted=103.0)
        m = cs.diluted_share_growth(cur, prev)
        assert m.status is MetricStatus.MISSING_DATA
        assert "shares_diluted_prior" in m.missing_fields

    def test_issuance_pressure(self):
        assert cs.issuance_pressure(q(share_issuance_proceeds=50.0, cfo=200.0)).value == pytest.approx(0.25)
