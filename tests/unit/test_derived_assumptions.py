"""Assumptions derived from a holding's own filed history.

The holder writes none, so these fill the brief's `Your assumptions` section.
That makes two things load-bearing: a rule must decline whenever the history
does not actually support a claim, and the rendered file must round-trip back
to exactly the claim texts (the brief contract pins each table row against the
re-parsed file, so a drift there fails the whole brief).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.schemas.financials import (
    CompanyDataset,
    CompanyProfile,
    PeriodFinancials,
    PeriodType,
)
from app.services.brief import derived as dv
from app.services.brief.assumptions import DERIVED, parse_assumptions, render_for_brief

FIRST_END = date(2024, 3, 31)
QUARTER = timedelta(days=91)


def series(gap_after: int | None = None, **fields: list) -> CompanyDataset:
    """A contiguous quarterly series, oldest first, one list per field.

    `gap_after` drops the quarter at that index's spacing by a full quarter,
    producing the hole a positional "four quarters back" must never step over.
    """
    lengths = {len(v) for v in fields.values()}
    assert len(lengths) == 1, "every field needs one value per quarter"
    n = lengths.pop()
    periods, end = [], FIRST_END
    for i in range(n):
        periods.append(PeriodFinancials(
            period_end=end,
            period_type=PeriodType.QUARTER,
            fiscal_label=f"Q{i}",
            **{k: v[i] for k, v in fields.items()},
        ))
        end += QUARTER * (2 if gap_after == i else 1)
    return CompanyDataset(profile=CompanyProfile(ticker="TST"), periods=periods)


def growing(base: float, rate: float, n: int = 8) -> list[float]:
    """n quarters where each is `rate` above the one four quarters earlier."""
    return [base * (1 + rate) ** (i // 4) * (1 + 0.01 * (i % 4)) for i in range(n)]


def only(dataset: CompanyDataset, key: str) -> dv.Derived | None:
    return next((d for d in dv.derive_assumptions(dataset) if d.key == key), None)


class TestRevenueGrowth:
    def test_claims_the_range_when_every_quarter_grew(self):
        d = only(series(revenue=growing(100.0, 0.20)), "revenue_growth")
        assert d is not None
        assert "Revenue keeps growing year over year" in d.text
        assert "20.0% to 20.0%" in d.text

    def test_declines_when_any_quarter_shrank(self):
        rev = growing(100.0, 0.20)
        rev[5] = rev[1] * 0.9  # one down quarter breaks the claim
        assert only(series(revenue=rev), "revenue_growth") is None

    def test_declines_on_a_hole_in_the_history(self):
        # Without the contiguity guard the missing quarter shifts every
        # comparison by one and the "year over year" figures are not annual.
        assert only(series(gap_after=3, revenue=growing(100.0, 0.20)), "revenue_growth") is None

    def test_declines_on_too_little_history(self):
        assert only(series(revenue=growing(100.0, 0.20, n=7)), "revenue_growth") is None

    def test_declines_on_a_zero_base(self):
        rev = growing(100.0, 0.20)
        rev[0] = 0.0
        assert only(series(revenue=rev), "revenue_growth") is None


class TestMarginFloor:
    def test_floor_is_the_trailing_low(self):
        rev = [100.0] * 4
        d = only(series(revenue=rev, cost_of_revenue=[30.0, 28.0, 26.0, 29.0]), "margin_floor")
        assert d is not None
        assert d.text.startswith("Gross margin stays at or above 70.0%")

    def test_declines_when_the_margin_swings_too_far(self):
        cost = [10.0, 60.0, 20.0, 30.0]  # 90% down to 40%: no meaningful floor
        assert only(series(revenue=[100.0] * 4, cost_of_revenue=cost), "margin_floor") is None

    def test_falls_back_to_operating_margin_without_cost_of_revenue(self):
        d = only(series(revenue=[100.0] * 4, operating_income=[12.0, 14.0, 13.0, 15.0]),
                 "margin_floor")
        assert d is not None
        assert d.text.startswith("Operating margin stays at or above 12.0%")

    def test_declines_when_the_margin_is_negative(self):
        assert only(series(revenue=[100.0] * 4, operating_income=[-5.0] * 4), "margin_floor") is None

    def test_a_floor_the_newest_quarter_just_set_is_not_called_a_floor(self):
        # "Stays at or above 17%" would promise a durability nothing has
        # tested: the quarter that set the bound is the one that just printed.
        d = only(series(revenue=[100.0] * 4, operating_income=[20.0, 19.0, 18.0, 17.0]),
                 "margin_floor")
        assert d is not None
        assert d.text == ("Operating margin does not fall further — it just set a "
                          "four-quarter low of 17.0%.")

    def test_a_tie_with_the_newest_quarter_counts_as_newly_set(self):
        # An older twin at the same value does not make the bound survived —
        # the latest quarter is sitting on it either way.
        d = only(series(revenue=[100.0] * 4, operating_income=[17.0, 19.0, 18.0, 17.0]),
                 "margin_floor")
        assert d is not None and d.text.startswith("Operating margin does not fall further")

    def test_names_an_older_quarter_plainly(self):
        d = only(series(revenue=[100.0] * 4, operating_income=[17.0, 18.0, 19.0, 20.0]),
                 "margin_floor")
        assert d is not None and "the most recent" not in d.text


class TestCashGeneration:
    def test_positive_run(self):
        d = only(series(cfo=[50.0] * 4, capex=[10.0] * 4), "cash_generation")
        assert d is not None and d.text.startswith("Free cash flow stays positive")

    def test_burn_gets_a_ceiling_on_the_burn(self):
        d = only(series(cfo=[1.0] * 4, capex=[11.0, 31.0, 21.0, 16.0]), "cash_generation")
        assert d is not None
        assert d.text.startswith("Quarterly free cash flow burn stays under $30")

    def test_declines_when_it_crossed_zero(self):
        assert only(series(cfo=[20.0] * 4, capex=[10.0, 10.0, 30.0, 10.0]),
                    "cash_generation") is None

    def test_declines_without_capex(self):
        assert only(series(cfo=[50.0] * 4, capex=[None] * 4), "cash_generation") is None


class TestDilution:
    def test_caps_growth_at_the_fastest_year(self):
        shares = [100.0, 100.0, 100.0, 100.0, 105.0, 110.0, 105.0, 105.0]
        d = only(series(shares_diluted=shares), "dilution")
        assert d is not None
        assert d.text.startswith("Diluted share count grows no more than 10.0% year over year")

    def test_states_no_growth_when_the_count_only_fell(self):
        shares = [100.0] * 4 + [99.0, 98.0, 97.0, 96.0]
        d = only(series(shares_diluted=shares), "dilution")
        assert d is not None and d.text.startswith("Diluted share count does not grow")

    def test_declines_on_a_split(self):
        # A 4-for-1 split restates the whole series; reading +300% off it as
        # dilution would put a fiction in front of the holder every quarter.
        shares = [100.0] * 4 + [400.0] * 4
        assert only(series(shares_diluted=shares), "dilution") is None

    def test_falls_back_to_shares_outstanding_when_diluted_has_the_q4_hole(self):
        # Diluted count is a weighted average the mapper cannot derive for a
        # fiscal Q4 backed out of the 10-K, so it is absent for EVERY filer in
        # at least one quarter of any eight-quarter window.
        diluted = [100.0, 100.0, 100.0, None, 110.0, 108.0, 106.0, None]
        outstanding = [100.0, 100.0, 100.0, 100.0, 110.0, 108.0, 106.0, 104.0]
        d = only(series(shares_diluted=diluted, shares_outstanding=outstanding), "dilution")
        assert d is not None
        assert d.text.startswith("Shares outstanding grow no more than 10.0%")

    def test_never_mixes_the_two_measures(self):
        # Half a diluted series plus half an outstanding series is not a
        # year-over-year move in anything; with neither measure complete the
        # rule must decline rather than splice them.
        diluted = [100.0, 100.0, 100.0, None, 110.0, 110.0, 110.0, 110.0]
        outstanding = [None, 100.0, 100.0, 100.0, 110.0, 110.0, 110.0, 110.0]
        assert only(series(shares_diluted=diluted, shares_outstanding=outstanding),
                    "dilution") is None

    def test_agrees_with_its_subject(self):
        d = only(series(shares_outstanding=[100.0] * 4 + [99.0] * 4), "dilution")
        assert d is not None and d.text.startswith("Shares outstanding do not grow")


class TestBalanceSheet:
    def test_net_cash_position(self):
        d = only(series(cash_and_equivalents=[50.0] * 4, total_debt=[10.0] * 4), "balance_sheet")
        assert d is not None
        assert d.text.startswith("Cash and equivalents stay above total debt")

    def test_debt_ceiling_when_debt_exceeds_cash(self):
        d = only(series(cash_and_equivalents=[5.0] * 4, total_debt=[10.0, 30.0, 20.0, 25.0]),
                 "balance_sheet")
        assert d is not None
        assert d.text.startswith("Total debt stays at or below $30")

    def test_cash_floor_when_no_debt_is_tagged(self):
        d = only(series(cash_and_equivalents=[80.0, 60.0, 70.0, 90.0], total_debt=[None] * 4),
                 "balance_sheet")
        assert d is not None
        assert d.text.startswith("Cash and equivalents stay at or above $60")

    def test_declines_without_cash(self):
        assert only(series(cash_and_equivalents=[None] * 4, total_debt=[1.0] * 4),
                    "balance_sheet") is None


class TestDeriveSet:
    def test_one_claim_per_dimension_in_order(self):
        n = 8
        items = dv.derive_assumptions(series(
            revenue=growing(100.0, 0.20),
            cost_of_revenue=[r * 0.3 for r in growing(100.0, 0.20)],
            cfo=[50.0] * n, capex=[10.0] * n,
            shares_outstanding=[100.0] * 4 + [99.0] * 4,
            cash_and_equivalents=[80.0] * n, total_debt=[10.0] * n,
        ))
        assert [d.key for d in items] == [
            "revenue_growth", "margin_floor", "cash_generation", "dilution", "balance_sheet"]
        assert len(items) <= dv.MAX_DERIVED

    def test_empty_history_derives_nothing(self):
        empty = CompanyDataset(profile=CompanyProfile(ticker="TST"), periods=[])
        assert dv.derive_assumptions(empty) == []

    def test_annual_periods_are_not_quarters(self):
        ds = series(revenue=growing(100.0, 0.20))
        ds.periods[-1].period_type = PeriodType.ANNUAL
        assert only(ds, "revenue_growth") is None

    @pytest.mark.parametrize("value,expected", [
        (33_370_000_000.0, "$33.37B"), (560_600_000.0, "$560.60M"),
        (-38_260_000.0, "-$38.26M"), (1_500.0, "$1.50K"), (42.0, "$42"),
    ])
    def test_money_reads_like_money(self, value, expected):
        assert dv._money(value) == expected


class TestWindowAndCitation:
    """The claims say "the last four quarters" and cite a quarter by name. Both
    are load-bearing sentences about a real company, and both are computed, not
    quoted — so both need holding down."""

    def test_reads_the_newest_quarters_not_the_oldest(self):
        # With every fixture sized to exactly the window a rule needs, taking
        # the FIRST n quarters and taking the LAST n are indistinguishable —
        # and production fetches 12 quarters for a 4- and 8-quarter window.
        stale = [10.0, 10.0, 10.0, 10.0]
        recent = [100.0] * 4 + [125.0, 126.0, 127.0, 128.0]
        d = only(series(revenue=stale + recent), "revenue_growth")
        assert d is not None
        assert "25.0% to 28.0%" in d.text, "computed off the oldest quarters, not the newest"

    def test_the_basis_line_pairs_each_value_with_its_own_quarter(self):
        # The basis line is what a holder checks the claim against; a shifted
        # pairing cites the right numbers for the wrong quarters, and the
        # brief contract cannot catch it (it never re-reads this line).
        d = only(series(revenue=[100.0] * 4, operating_income=[17.0, 18.0, 19.0, 20.0]),
                 "margin_floor")
        assert d is not None
        assert d.detail == "Q3 20.0%, Q2 19.0%, Q1 18.0%, Q0 17.0%", "newest first, in step"

    @pytest.mark.parametrize("key,fields,cited", [
        ("margin_floor",
         dict(revenue=[100.0] * 4, operating_income=[20.0, 17.0, 18.0, 19.0]), "Q1"),
        ("cash_generation",
         dict(cfo=[1.0] * 4, capex=[11.0, 31.0, 21.0, 16.0]), "Q1"),
        ("balance_sheet",
         dict(cash_and_equivalents=[5.0] * 4, total_debt=[10.0, 30.0, 20.0, 25.0]), "Q1"),
        ("dilution",
         dict(shares_outstanding=[100.0] * 4 + [104.0, 110.0, 106.0, 108.0]), "Q5"),
    ])
    def test_every_rule_cites_the_quarter_that_actually_set_its_bound(self, key, fields, cited):
        d = only(series(**fields), key)
        assert d is not None and f"({cited})" in d.text

    def test_a_cash_floor_cites_the_cash_series_not_the_debt_series(self):
        d = only(series(cash_and_equivalents=[80.0, 60.0, 70.0, 90.0], total_debt=[None] * 4),
                 "balance_sheet")
        assert d is not None and "(Q1)" in d.text


class TestWiring:
    def test_derive_for_ticker_calls_the_adapter_the_way_the_adapter_expects(self):
        # Nothing else exercises this call: sources.py swallows every exception
        # from it, so a signature drift would quietly return the whole feature
        # to printing UNAVAILABLE on every holding, with no test failing.
        import inspect

        from app.services.ingestion.edgar_adapter import fetch_dataset_snapshot

        bound = inspect.signature(fetch_dataset_snapshot).bind(
            "NVDA", n_quarters=dv.DERIVE_QUARTERS, client=None)
        assert bound.arguments["n_quarters"] == dv.DERIVE_QUARTERS

    def test_claims_too_long_to_round_trip_are_dropped_not_published(self, monkeypatch):
        from app.services.brief.assumptions import MAX_ASSUMPTION_CHARS

        long_one = dv.Derived("x", "y" * (MAX_ASSUMPTION_CHARS + 1), "basis")
        monkeypatch.setattr(dv, "RULES", (lambda q: long_one,))
        assert dv.derive_assumptions(series(revenue=[1.0] * 8)) == []

    def test_more_rules_than_max_derived_are_truncated(self, monkeypatch):
        made = [dv.Derived(f"k{i}", f"claim {i}", "basis") for i in range(dv.MAX_DERIVED + 2)]
        monkeypatch.setattr(dv, "RULES", tuple((lambda d: (lambda q: d))(d) for d in made))
        assert len(dv.derive_assumptions(series(revenue=[1.0] * 8))) == dv.MAX_DERIVED


class TestRenderRoundTrip:
    """The brief contract re-parses the written file to pin each table row, so
    anything the renderer adds must not come back as an assumption."""

    def test_only_the_claims_survive_reparsing(self):
        items = dv.derive_assumptions(series(
            revenue=growing(100.0, 0.20),
            shares_outstanding=[100.0] * 4 + [99.0] * 4,
        ))
        assert items
        text = render_for_brief(
            "TST", [d.text for d in items], origin=DERIVED, details=[d.detail for d in items])
        assert parse_assumptions(text) == [d.text for d in items]

    def test_the_file_declares_it_was_not_written_by_the_holder(self):
        text = render_for_brief("TST", ["Revenue keeps growing."], origin=DERIVED,
                                details=["Q1 +20.0% YoY"])
        assert "DERIVED BY THE ENGINE" in text
        assert "NOT holder-authored" in text

    def test_holder_rendering_is_unchanged_and_unlabelled(self):
        text = render_for_brief("TST", ["Revenue keeps growing."])
        assert "holder-authored" in text
        assert "DERIVED" not in text

    def test_a_claim_never_carries_a_pipe(self):
        # A pipe would split the brief's table cell and drop the row, failing
        # the brief for that holding on every print until someone noticed.
        items = dv.derive_assumptions(series(
            revenue=growing(100.0, 0.20), cash_and_equivalents=[8.0] * 8, total_debt=[1.0] * 8))
        assert items and all("|" not in d.text for d in items)
