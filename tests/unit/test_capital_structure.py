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


class TestIssuancePressure:
    """Review finding 1 (round 4): the negative-CFO distress SCORE was removed
    (unvalidated threshold on frozen scoring). Negative/zero CFO now drops the
    metric benignly — NOT_MEANINGFUL, never a distress signal."""

    def test_positive_cfo_computes_normally(self):
        m = cs.issuance_pressure(q(share_issuance_proceeds=50.0, cfo=200.0))
        assert m.status is MetricStatus.OK
        assert m.value == pytest.approx(0.25)

    def test_negative_cfo_is_not_meaningful_not_distress(self):
        m = cs.issuance_pressure(q(share_issuance_proceeds=100.0, cfo=-10.0))
        assert m.status is MetricStatus.NOT_MEANINGFUL
        assert m.distress_signal is False

    def test_zero_cfo_is_not_meaningful_not_distress(self):
        # No distress from breakeven, and no zero/zero threshold artifact.
        m = cs.issuance_pressure(q(share_issuance_proceeds=0.0, cfo=0.0))
        assert m.status is MetricStatus.NOT_MEANINGFUL
        assert m.distress_signal is False

    def test_no_issuance_negative_cfo_is_not_distress(self):
        m = cs.issuance_pressure(q(share_issuance_proceeds=0.0, cfo=-10.0))
        assert m.distress_signal is False


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


# Both share-change metrics, by the field each reads. A filed share count of
# 0 or below reaches the formulas (r17: refused where used; the schema is a
# carrier), and until r38 only the prior count was guarded: a bad CURRENT
# count read as an OK change of -100% or beyond, which interpolates to
# diluted_share_growth's lowest concern anchor, i.e. "no dilution concern".
SHARE_CHANGES = [
    pytest.param(cs.diluted_share_growth, "shares_diluted", id="diluted_share_growth"),
    pytest.param(cs.net_share_count_change, "shares_outstanding", id="net_share_count_change"),
]


@pytest.mark.parametrize(("metric", "field"), SHARE_CHANGES)
class TestShareCountGuards:
    @pytest.mark.parametrize("current", [0.0, -1.0])
    def test_a_non_positive_current_count_is_not_meaningful(self, metric, field, current):
        m = metric(q("Q4", **{field: current}), q("Q3", **{field: 100.0}))
        assert m.status is MetricStatus.NOT_MEANINGFUL
        assert m.value is None and m.distress_signal is False
        assert m.note == "Non-positive current share count"

    @pytest.mark.parametrize("current", [103.0, 0.0, -1.0])
    @pytest.mark.parametrize("prior", [0.0, -1.0])
    def test_a_non_positive_prior_count_keeps_its_message(self, metric, field, prior, current):
        """The prior's message is checked first and is unchanged, whatever
        the current count."""
        m = metric(q("Q4", **{field: current}), q("Q3", **{field: prior}))
        assert m.status is MetricStatus.NOT_MEANINGFUL
        assert m.value is None and m.distress_signal is False
        assert m.note == "Non-positive prior share count"

    @pytest.mark.parametrize(("current", "prior", "change"), [
        (103.0, 100.0, 0.03), (98.0, 100.0, -0.02), (100.0, 100.0, 0.0), (1e-9, 100.0, -1.0 + 1e-11),
    ])
    def test_two_positive_counts_compute_the_change(self, metric, field, current, prior, change):
        m = metric(q("Q4", **{field: current}), q("Q3", **{field: prior}))
        assert m.status is MetricStatus.OK and m.note is None
        assert m.value == pytest.approx(change, rel=1e-12, abs=1e-15)
