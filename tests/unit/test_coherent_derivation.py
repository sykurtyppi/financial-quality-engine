"""Derived quarters are rebuilt from one filing date (Hermes audit round 4, finding 1).

A derived quarter subtracts earlier periods from a longer figure filed at
some date. Subtracting those periods as they stand TODAY mixes vintages: a
Q1 amended 100 -> 150 after the 10-K reported 400 for the year made Q4
400 - 150 - 100 - 100 = 50, though the 400 embeds the old Q1 and Q4 is 100.
The earlier periods are now taken as they stood when the longer figure was
filed; where no filing date has both, the latest values are used and the
quarter is noted.
"""

from __future__ import annotations

from datetime import date, timedelta

from hypothesis import given
from hypothesis import strategies as st

from app.services.ingestion.companyfacts_mapper import (
    RawFact,
    _FlowSeries,
    build_dataset,
)
from tests.fixtures.selection_cases import (
    QUARTER_ENDS,
    _base,
    mixed_vintages,
    quarter,
    ytd,
)

Q1, Q2, Q3, Q4 = date(2024, 3, 31), date(2024, 6, 30), date(2024, 9, 30), date(2024, 12, 31)
ENDS = [Q1, Q2, Q3, Q4]
Y0 = date(2024, 1, 1)


def _fact(start, end, val, filed, form="10-Q", accn="a"):
    return RawFact(start=start, end=end, val=val, filed=filed, form=form, accn=accn)


def _q(end, val, filed, form="10-Q"):
    return _fact(date(end.year, end.month - 2, 1), end, val, filed, form)


class TestAnnualLessThreeQuarters:
    def test_hermes_chronology(self):
        facts = [
            _q(Q1, 100.0, date(2024, 5, 1)),
            _q(Q2, 100.0, date(2024, 8, 1)),
            _q(Q3, 100.0, date(2024, 11, 1)),
            _fact(Y0, Q4, 400.0, date(2025, 2, 20), "10-K"),
            _q(Q1, 150.0, date(2025, 3, 10), "10-Q/A"),  # after the 10-K
        ]
        s = _FlowSeries(facts)
        values, methods = s.quarterly(ENDS)
        assert values[Q1] == 150.0  # Q1 itself stands at its amended value
        assert (values[Q4], methods[Q4]) == (100.0, "fy_minus_3q")
        assert s.mixed == set()

    def test_an_amendment_the_10k_already_reflects_changes_nothing(self):
        facts = [
            _q(Q1, 100.0, date(2024, 5, 1)),
            _q(Q1, 150.0, date(2024, 6, 1), "10-Q/A"),  # before the 10-K
            _q(Q2, 100.0, date(2024, 8, 1)),
            _q(Q3, 100.0, date(2024, 11, 1)),
            _fact(Y0, Q4, 450.0, date(2025, 2, 20), "10-K"),
        ]
        values, _ = _FlowSeries(facts).quarterly(ENDS)
        assert values[Q4] == 100.0


    def test_a_quarter_filed_after_the_10k_is_noted(self):
        """Q3 first appears after the 10-K: no single filing date has the
        year and all three quarters, so the latest values are used and the
        quarter is noted."""
        facts = [
            _q(Q1, 100.0, date(2024, 5, 1)),
            _q(Q2, 100.0, date(2024, 8, 1)),
            _fact(Y0, Q4, 400.0, date(2025, 2, 20), "10-K"),
            _q(Q3, 100.0, date(2025, 3, 1)),
        ]
        s = _FlowSeries(facts)
        values, _ = s.quarterly(ENDS)
        assert values[Q4] == 100.0 and s.mixed == {Q4}


class TestYearToDate:
    def test_q2_subtracts_q1_as_the_h1_filing_saw_it(self):
        facts = [
            _q(Q1, 100.0, date(2024, 5, 1)),
            _fact(Y0, Q2, 101.0, date(2024, 8, 1)),  # H1
            _q(Q1, 100.5, date(2024, 9, 1), "10-Q/A"),  # Q1 amended after H1
        ]
        values, methods = _FlowSeries(facts).quarterly(ENDS[:2])
        assert (values[Q2], methods[Q2]) == (1.0, "ytd_diff")  # not 0.5

    def test_a_chain_of_year_to_date_figures(self):
        facts = [
            _fact(Y0, Q1, 100.0, date(2024, 5, 1)),
            _fact(Y0, Q2, 210.0, date(2024, 8, 1)),
            _fact(Y0, Q3, 330.0, date(2024, 11, 1)),
            _fact(Y0, Q2, 260.0, date(2024, 12, 1), "10-Q/A"),  # H1 amended after 9M
        ]
        values, _ = _FlowSeries(facts).quarterly(ENDS[:3])
        assert values[Q2] == 160.0  # H1 as it stands (260) less Q1 (100)
        assert values[Q3] == 120.0  # 9M less H1 as the 9M filing saw it (210), not 70

    def test_no_single_filing_date_has_both_is_noted(self):
        facts = [
            _fact(Y0, Q2, 130.0, date(2024, 8, 1)),  # H1 first
            _q(Q1, 60.0, date(2024, 9, 1)),  # Q1 only filed later
        ]
        s = _FlowSeries(facts)
        values, _ = s.quarterly(ENDS[:2])
        assert values[Q2] == 70.0 and s.mixed == {Q2}
        # Each run reports its own quarters only.
        s.quarterly(ENDS[:1])
        assert s.mixed == set()


def test_the_mapper_notes_the_quarter_it_could_not_rebuild():
    ds, diag = build_dataset(mixed_vintages(), "MV")
    by_end = {p.period_end: p for p in ds.periods}
    assert by_end[QUARTER_ENDS[11]].operating_income == 100.0
    assert diag.field_by_name("operating_income").notes == []
    (note,) = diag.field_by_name("cfo").notes
    assert note.startswith("Derived from filings of different dates at FY2024Q2:")


def test_a_composed_field_is_noted_where_a_component_could_not_be_rebuilt():
    """SG&A composed from S&M + G&A per quarter: the note reaches the field
    when a COMPONENT's quarter could not be rebuilt at one filing date (G&A
    Q2 from an H1 filed before its Q1 was), and only that quarter."""
    p = _base("Composite Vintages Co")
    q = QUARTER_ENDS
    p.add("SellingAndMarketingExpense", [quarter(e, 40.0) for e in q])
    ga = [quarter(e, 20.0) for e in q if e not in (q[8], q[9])]
    ga.append(quarter(q[8], 25.0, filed=date(2024, 9, 1)))
    ga.append(ytd(q[9], 55.0, filed=date(2024, 8, 1)))
    p.add("GeneralAndAdministrativeExpense", ga)
    ds, diag = build_dataset(p.data, "CV")
    by_end = {x.period_end: x for x in ds.periods}
    assert by_end[q[9]].sga_expense == 40.0 + (55.0 - 25.0)
    notes = diag.field_by_name("sga_expense").notes
    assert any(n.startswith("Derived from filings of different dates at FY2024Q2:") for n in notes)
    assert not any("FY2024Q1" in n and "different dates" in n for n in notes)


@st.composite
def _chronological_series(draw):
    """One fact per period, filed in period order, never revised: every
    filing date sees everything filed before it, so rebuilding as of a
    filing date must change nothing."""
    facts: list[RawFact] = []
    filed = date(2024, 4, 15)
    for end in ENDS:
        filed += timedelta(days=draw(st.integers(20, 60)))
        kind = draw(st.sampled_from(["quarter", "ytd", "none"]))
        val = float(draw(st.integers(-500, 500)))
        if kind == "quarter":
            facts.append(_q(end, val, filed))
        elif kind == "ytd":
            facts.append(_fact(Y0, end, val, filed))
    if draw(st.booleans()):
        facts.append(_fact(Y0, Q4, float(draw(st.integers(-2000, 2000))),
                           filed + timedelta(days=30), "10-K"))
    return facts


@given(facts=_chronological_series())
def test_nothing_revised_later_means_nothing_changes(facts):
    s = _FlowSeries(facts)
    values, _ = s.quarterly(ENDS)
    assert s.mixed == set()
    # The same series with no cut at all: identical values, bit for bit.
    uncut = _FlowSeries(facts)
    uncut._as_of = lambda cutoff: uncut  # type: ignore[method-assign]
    assert uncut.quarterly(ENDS)[0] == values
