"""Forward-outcome anchoring and contiguity guards (round-16 F1).

The measured defect: the signal anchors on the last quarter FILED by as_of
(the PIT slice's newest period) while forward_outcomes re-derived its base as
the last quarter ENDED by as_of — one quarter ahead whenever a quarter had
ended but not yet been filed. The positional idx+4 lookahead also assumed
contiguous fiscal quarters that nothing enforced.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.schemas.financials import (
    CompanyDataset,
    CompanyProfile,
    PeriodFinancials,
    PeriodType,
)
from app.services.backtesting.outcomes import forward_outcomes

_START = date(2022, 3, 31)


def _quarter(i: int, *, op_margin: float = 0.10, end: date | None = None) -> PeriodFinancials:
    rev = 1000.0
    return PeriodFinancials(
        period_end=end if end is not None else _START + timedelta(days=91 * i),
        period_type=PeriodType.QUARTER,
        fiscal_label=f"FY{2022 + i // 4}Q{i % 4 + 1}",
        revenue=rev,
        operating_income=rev * op_margin,
        net_income=100.0 + i,
        cfo=150.0,
        capex=50.0,
    )


def _dataset(periods: list[PeriodFinancials]) -> CompanyDataset:
    return CompanyDataset(profile=CompanyProfile(ticker="TEST"), periods=periods)


def _ramp(n: int) -> list[PeriodFinancials]:
    """n contiguous quarters whose op margin rises 1pp per quarter."""
    return [_quarter(i, op_margin=0.10 + 0.01 * i) for i in range(n)]


class TestAnchor:
    def test_anchor_selects_the_signal_quarter_not_the_last_ended(self):
        periods = _ramp(12)
        # Quarter 7 has ended by as_of but was not yet filed; the signal was
        # scored on quarter 6. Legacy derivation picks 7; the anchor picks 6.
        as_of = periods[7].period_end + timedelta(days=10)
        anchored = forward_outcomes(_dataset(periods), as_of, anchor=periods[6].period_end)
        legacy = forward_outcomes(_dataset(periods), as_of)
        assert anchored["op_margin_chg_4q"] is not None
        assert legacy["op_margin_chg_4q"] is not None
        # margin ramp is 1pp/q, so both deltas are 4pp — but measured from
        # different quarters: q10 vs q6 (anchored) and q11 vs q7 (legacy).
        m = [0.10 + 0.01 * i for i in range(12)]
        assert abs(anchored["op_margin_chg_4q"] - (m[10] - m[6])) < 1e-12
        assert abs(legacy["op_margin_chg_4q"] - (m[11] - m[7])) < 1e-12

    def test_anchor_not_in_full_dataset_returns_none(self):
        periods = _ramp(12)
        out = forward_outcomes(
            _dataset(periods), periods[-1].period_end, anchor=date(2019, 1, 1)
        )
        assert all(v is None for v in out.values())

    def test_no_anchor_preserves_legacy_selection(self):
        periods = _ramp(12)
        as_of = periods[5].period_end  # inclusive boundary
        out = forward_outcomes(_dataset(periods), as_of)
        m = [0.10 + 0.01 * i for i in range(12)]
        assert abs(out["op_margin_chg_4q"] - (m[9] - m[5])) < 1e-12


class TestContiguityGuard:
    def test_missing_quarter_in_forward_window_returns_none(self):
        periods = _ramp(12)
        del periods[8]  # gap between anchor (6) and t+4
        out = forward_outcomes(
            _dataset(periods), periods[-1].period_end, anchor=periods[6].period_end
        )
        assert all(v is None for v in out.values())

    def test_annual_period_in_window_returns_none(self):
        periods = _ramp(12)
        periods[8] = periods[8].model_copy(update={"period_type": PeriodType.ANNUAL})
        out = forward_outcomes(
            _dataset(periods), periods[-1].period_end, anchor=periods[6].period_end
        )
        assert all(v is None for v in out.values())

    def test_gap_in_trailing_window_kills_mean_based_outcomes_only(self):
        periods = _ramp(12)
        # Break contiguity in the trailing window (before the anchor at 6)
        # but leave anchor..anchor+4 intact: op-margin delta survives, the
        # trailing-mean outcomes refuse.
        del periods[4]
        out = forward_outcomes(
            _dataset(periods), periods[-1].period_end, anchor=_START + timedelta(days=91 * 6)
        )
        assert out["op_margin_chg_4q"] is not None
        assert out["fcf_margin_chg_4q"] is None
        assert out["ni_growth_fwd_4q"] is None

    def test_clean_history_produces_all_outcomes(self):
        periods = _ramp(12)
        out = forward_outcomes(
            _dataset(periods), periods[-1].period_end, anchor=periods[4].period_end
        )
        assert out["op_margin_chg_4q"] is not None
        assert out["fcf_margin_chg_4q"] is not None
        assert out["ni_growth_fwd_4q"] is not None

    def test_insufficient_forward_history_returns_none(self):
        periods = _ramp(8)
        out = forward_outcomes(
            _dataset(periods), periods[-1].period_end, anchor=periods[6].period_end
        )
        assert all(v is None for v in out.values())
