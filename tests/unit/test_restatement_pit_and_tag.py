"""Point-in-time and tag-selection integrity of the restatement detector.

Two defects, one root cause: the detector chose WHICH series to inspect by
re-deriving the mapper's choice from the raw payload, before any `as_of`
filter had been applied.

  * Future leakage — coverage was counted over the whole payload, so facts
    filed years later could decide which tag a historical report inspected.
    A dated report is supposed to reconstruct what was knowable then; instead
    it silently changed whenever new filings arrived.

  * Tag divergence — the re-derivation counted every distinct period in a
    tag's history, while the mapper (`_best_series`) scores coverage of the
    report's own quarter ends after period reconstruction. A legacy tag with a
    long irrelevant history could win here and lose there, so the report could
    show a revision on a series the engine never scored.

Evidence-only output, so no score moves. What moves is whether the evidence
describes the same company-quarter the score describes.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.ingestion.companyfacts_mapper import build_dataset
from app.services.ingestion.restatements import (
    _active_tag,
    _resolve_tags,
    detect_restatements,
)

REV = (("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
       ("us-gaap", "Revenues"),
       ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"),
       ("us-gaap", "SalesRevenueNet"))


def _row(start, end, val, filed, accn, form="10-Q"):
    return {"start": start, "end": end, "val": val, "filed": filed,
            "accn": accn, "form": form, "fy": 2024, "fp": "Q1"}


def _revised_history() -> dict:
    """One Q1-2024 figure, reported at 100 and later revised to 130."""
    return {"units": {"USD": [
        _row("2024-01-01", "2024-03-31", 100.0, "2024-05-01", "0000-24-001"),
        _row("2024-01-01", "2024-03-31", 130.0, "2024-08-01", "0000-24-002"),
    ]}}


def _facts(**tags) -> dict:
    return {"facts": {"us-gaap": tags}}


def _found(payload, **kw):
    return [(f.tag, str(f.period_end), f.original_value, f.current_value)
            for f in detect_restatements(payload, **kw)]


# --- future leakage --------------------------------------------------------

def test_post_as_of_facts_cannot_change_a_historical_result():
    """The metamorphic property: adding only facts FILED after `as_of` must
    leave every earlier as-of result byte-identical."""
    base = _facts(Revenues=_revised_history())
    later = [_row(f"{2025 + i // 4}-01-01", f"{2025 + i // 4}-0{i % 4 + 1}-28",
                  200.0 + i, "2026-05-01", f"0000-26-{i:03d}") for i in range(6)]
    with_future = _facts(Revenues=_revised_history(),
                         SalesRevenueNet={"units": {"USD": later}})

    as_of = date(2024, 12, 31)
    assert _found(base, as_of=as_of) == _found(with_future, as_of=as_of)
    # and the amendment is actually there to be lost
    assert _found(base, as_of=as_of) == [("us-gaap:Revenues", "2024-03-31", 100.0, 130.0)]


def test_active_tag_ignores_coverage_a_dated_report_could_not_see():
    later = [_row(f"2025-0{i + 1}-01", f"2025-0{i + 1}-28", 1.0, "2026-05-01", f"x{i}")
             for i in range(8)]
    payload = _facts(Revenues=_revised_history(),
                     SalesRevenueNet={"units": {"USD": later}})
    # Unfiltered, the newer series wins on raw period count.
    assert _active_tag(payload, REV, "USD") == ("us-gaap", "SalesRevenueNet")
    # As of 2024 it did not exist, so it cannot win.
    assert _active_tag(payload, REV, "USD", date(2024, 12, 31)) == ("us-gaap", "Revenues")


def test_a_fact_without_a_usable_filed_date_is_not_dated_in():
    undated = {"units": {"USD": [
        {"start": "2024-01-01", "end": "2024-03-31", "val": 1.0, "accn": "n"},
    ]}}
    payload = _facts(Revenues=_revised_history(), SalesRevenueNet=undated)
    assert _active_tag(payload, REV, "USD", date(2024, 12, 31)) == ("us-gaap", "Revenues")


# --- tag divergence --------------------------------------------------------

def test_the_mappers_selection_wins_over_the_approximation():
    """The regression: a legacy tag with a longer raw history beats the tag
    the mapper actually scored, unless the mapper's choice is supplied."""
    legacy = {"units": {"USD": [
        _row(f"20{y}-01-01", f"20{y}-03-31", 50.0, f"20{y}-05-01", f"L{y}")
        for y in range(10, 24)
    ] + _revised_history()["units"]["USD"]}}
    current = {"units": {"USD": [
        _row("2024-01-01", "2024-03-31", 90.0, "2024-05-01", "C1"),
    ]}}
    payload = _facts(Revenues=legacy, RevenueFromContractWithCustomerExcludingAssessedTax=current)

    # Left to its own devices the detector picks the long legacy series.
    assert _active_tag(payload, REV, "USD") == ("us-gaap", "Revenues")
    assert _found(payload) == [("us-gaap:Revenues", "2024-03-31", 100.0, 130.0)]

    # Told what the mapper scored, it inspects that series instead — and
    # reports nothing, because that series carries no revision.
    scored = {"revenue": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"}
    assert _found(payload, selected_tags=scored) == []


def test_a_field_the_mapper_could_not_map_reports_nothing():
    """`tag_used=None` means no series backed the field. Falling back to the
    approximation there would report a revision on a series that contributed
    nothing to the score — the exact mismatch the argument prevents."""
    payload = _facts(Revenues=_revised_history())
    assert _found(payload) == [("us-gaap:Revenues", "2024-03-31", 100.0, 130.0)]
    assert _found(payload, selected_tags={"revenue": None}) == []


def test_a_field_absent_from_the_selection_falls_back():
    # Only fields the mapper reported on are authoritative; others still use
    # the approximation rather than silently disappearing.
    payload = _facts(Revenues=_revised_history())
    assert _found(payload, selected_tags={"net_income": "us-gaap:NetIncomeLoss"}) == [
        ("us-gaap:Revenues", "2024-03-31", 100.0, 130.0)
    ]


@pytest.mark.parametrize("selected,expected", [
    ("us-gaap:Revenues", [("us-gaap", "Revenues")]),
    ("dei:EntityCommonStockSharesOutstanding",
     [("dei", "EntityCommonStockSharesOutstanding")]),
    # Composites expand to every component; `none` marks one the mapper could
    # not fill. The components are then SUMMED, never reported individually.
    ("LongTermDebtNoncurrent+LongTermDebtCurrent+CommercialPaper",
     [("us-gaap", "LongTermDebtNoncurrent"), ("us-gaap", "LongTermDebtCurrent"),
      ("us-gaap", "CommercialPaper")]),
    ("LongTermDebtNoncurrent+LongTermDebtCurrent+none",
     [("us-gaap", "LongTermDebtNoncurrent"), ("us-gaap", "LongTermDebtCurrent")]),
])
def test_resolve_tags_reads_every_shape_the_mapper_records(selected, expected):
    assert _resolve_tags({}, "f", REV, "USD", None, {"f": selected}) == expected


SGA = {"sga_expense": "SellingAndMarketingExpense+GeneralAndAdministrativeExpense"}


def _sga(sm_rows, ga_rows) -> dict:
    return _facts(SellingAndMarketingExpense={"units": {"USD": sm_rows}},
                  GeneralAndAdministrativeExpense={"units": {"USD": ga_rows}})


def test_materiality_applies_to_the_sum_not_a_component():
    """The defect this replaces. SG&A = S&M 1000 + G&A 10; G&A moves 10 -> 11.
    Reported per-component that is a 10% revision clearing the 1% bar. The
    sga_expense the engine scored went 1010 -> 1011 — 0.099%, nowhere near
    it, so there is nothing to report."""
    payload = _sga(
        [_row("2024-01-01", "2024-03-31", 1000.0, "2024-05-01", "s1")],
        [_row("2024-01-01", "2024-03-31", 10.0, "2024-05-01", "g1"),
         _row("2024-01-01", "2024-03-31", 11.0, "2024-08-01", "g2")],
    )
    assert _found(payload, selected_tags=SGA) == []


def test_a_material_move_is_reported_as_the_summed_figure():
    payload = _sga(
        [_row("2024-01-01", "2024-03-31", 1000.0, "2024-05-01", "s1"),
         _row("2024-01-01", "2024-03-31", 1100.0, "2024-08-01", "s2", "10-Q/A")],
        [_row("2024-01-01", "2024-03-31", 10.0, "2024-05-01", "g1")],
    )
    found = detect_restatements(payload, selected_tags=SGA)
    assert len(found) == 1
    fp = found[0]
    # The SUM, not the component that moved — and one footprint, not one per tag.
    assert (fp.field_name, fp.original_value, fp.current_value) == ("sga_expense", 1010.0, 1110.0)
    assert "+" in fp.tag  # the composite is disclosed as such


def test_a_component_not_re_reported_carries_forward():
    """The amendment re-states G&A only. S&M's earlier value still stands, and
    is what the mapper scores, so the aggregate must include it."""
    payload = _sga(
        [_row("2024-01-01", "2024-03-31", 1000.0, "2024-05-01", "s1")],
        [_row("2024-01-01", "2024-03-31", 10.0, "2024-05-01", "g1"),
         _row("2024-01-01", "2024-03-31", 200.0, "2024-08-01", "g2")],
    )
    found = detect_restatements(payload, selected_tags=SGA)
    assert (found[0].original_value, found[0].current_value) == (1010.0, 1200.0)


def test_two_sub_threshold_moves_that_sum_past_the_bar_are_caught():
    """Only the aggregate view sees this: S&M +0.6% and G&A +60% of a tiny
    base are individually the wrong measurement, but the figure that was
    scored moved 1.19% — over the bar."""
    payload = _sga(
        [_row("2024-01-01", "2024-03-31", 1000.0, "2024-05-01", "s1"),
         _row("2024-01-01", "2024-03-31", 1006.0, "2024-08-01", "s2")],
        [_row("2024-01-01", "2024-03-31", 10.0, "2024-05-01", "g1"),
         _row("2024-01-01", "2024-03-31", 16.0, "2024-08-01", "g2")],
    )
    found = detect_restatements(payload, selected_tags=SGA)
    assert (found[0].original_value, found[0].current_value) == (1010.0, 1022.0)


def test_a_component_appearing_later_is_composition_not_revision():
    """A filer adopting a tag it had not used changes how the figure is
    COMPOSED. Comparing across that boundary would manufacture a restatement
    out of a taxonomy change — exactly what this module refuses to do for
    single-tag switches."""
    payload = _sga(
        [_row("2024-01-01", "2024-03-31", 1000.0, "2024-05-01", "s1")],
        [_row("2024-01-01", "2024-03-31", 500.0, "2024-08-01", "g1")],
    )
    assert _found(payload, selected_tags=SGA) == []


# --- the two halves agree on real mapper output ----------------------------

def test_selected_tags_round_trips_from_the_mapper():
    """`IngestionDiagnostics.selected_tags()` must produce exactly the
    qualified form `_resolve_tag` parses — the contract between the two."""
    import json
    from pathlib import Path

    facts = json.loads((Path(__file__).parent.parent / "fixtures" / "real"
                        / "companyfacts_AAPL_trimmed.json").read_text())
    _, diag = build_dataset(facts, "AAPL", n_quarters=8)
    tags = diag.selected_tags()
    assert tags, "mapper reported no field selections"
    for field_name, qualified in tags.items():
        if qualified is None:
            continue
        resolved = _resolve_tags({}, field_name, (), "USD", None, tags)
        assert resolved, f"{field_name}: mapper recorded {qualified!r}, resolver found no series"
        for taxonomy, tag in resolved:
            assert taxonomy and tag and ":" not in tag


# --- the wiring ------------------------------------------------------------

def test_build_report_hands_the_detector_the_mappers_selection(monkeypatch):
    """Threading the selection only helps if the report actually passes it.
    Without this, reverting the wiring leaves every unit test above green
    while the shipped report goes back to guessing."""

    from app.core.pipeline import analyze
    from app.services.ingestion import restatements as restatements_mod
    from app.services.reporting.report_builder import build_report
    from tests.fixtures.companies import stretch_dataset

    seen: dict = {}
    # Bound before patching: calling the module attribute from inside the spy
    # recursed forever, and the old catch-all filed the RecursionError as a
    # restatement data gap, so this test passed on a stream that never ran.
    real_scan = restatements_mod.scan_restatements

    def spy(facts_json, **kw):
        seen.update(kw)
        return real_scan({"facts": {}}, **kw)

    monkeypatch.setattr(restatements_mod, "scan_restatements", spy)

    class _Client:
        """Complete on purpose. A stub missing a method raises AttributeError
        from inside an evidence stream, which reads as a stream failure rather
        than as the test's own gap — and once defects stop being disguised as
        data gaps, it fails the test outright instead."""

        def company_facts(self, ticker):
            return {"facts": {}}

        def company_facts_by_cik(self, cik):
            return {"facts": {}}

        def resolve_cik(self, ticker):
            return 320193

        def submissions(self, ticker):
            return {"filings": {"recent": {}}}

        def submissions_by_cik(self, cik):
            return {"filings": {"recent": {}}}

    ds = stretch_dataset()
    tags = {"revenue": "us-gaap:Revenues"}
    build_report(
        analyze(ds), ds,
        generated_on="2026-09-21",
        coverage=1.0,
        client=_Client(),
        ticker="AAPL",
        company_facts={"facts": {}},
        field_tags=tags,
    )
    assert seen.get("selected_tags") == tags, (
        "build_report did not pass the mapper's tag selection to the detector"
    )


def test_both_entry_points_supply_the_selection():
    """`diag.selected_tags()` must reach `build_report` from the CLI and the
    journal alike — the two paths that actually produce reports."""
    import inspect
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for rel in ("scripts/generate_report.py", "app/services/journal/reporting.py"):
        src = (root / rel).read_text()
        assert "field_tags=diag.selected_tags()" in src, f"{rel} does not supply field_tags"
    # and the parameter still exists to receive it
    from app.services.reporting.report_builder import build_report
    assert "field_tags" in inspect.signature(build_report).parameters


# --- the as-of boundary itself -------------------------------------------
# Mutation testing found both of these unpinned: the comparator could flip to
# `<` and undated facts could be admitted, with the whole suite still green.
# `pit.py` makes exactly these two choices for the backtest; the restatement
# trail has to make them the same way or a dated report and a dated backtest
# row disagree about what was knowable.

def test_a_fact_filed_on_the_as_of_date_is_visible():
    """`filed <= as_of`, not `<`. A report dated the day a filing lands must
    see it — that filing is precisely the news of the day, and an off-by-one
    here hides every same-day amendment from its own report."""
    from app.services.ingestion.restatements import _eligible_rows

    payload = _facts(Revenues=_revised_history())  # filed 2024-05-01 and 2024-08-01
    on_the_day = _eligible_rows(payload, "us-gaap", "Revenues", "USD", date(2024, 8, 1))
    assert [r["filed"] for r in on_the_day] == ["2024-05-01", "2024-08-01"]

    day_before = _eligible_rows(payload, "us-gaap", "Revenues", "USD", date(2024, 7, 31))
    assert [r["filed"] for r in day_before] == ["2024-05-01"]


def test_the_boundary_reaches_the_footprints_not_just_the_rows():
    payload = _facts(Revenues=_revised_history())
    assert _found(payload, as_of=date(2024, 8, 1)) == [
        ("us-gaap:Revenues", "2024-03-31", 100.0, 130.0)
    ]
    # One day earlier only the original exists, so there is no revision yet.
    assert _found(payload, as_of=date(2024, 7, 31)) == []


def test_a_fact_with_no_filed_date_is_dropped_in_pit_mode():
    """A fact that cannot be dated cannot be shown to have been knowable.
    `pit.py::filter_as_of` drops these for the same reason; admitting them
    here would let an undatable value into a dated report."""
    from app.services.ingestion.restatements import _eligible_rows

    payload = _facts(Revenues={"units": {"USD": [
        {"start": "2024-01-01", "end": "2024-03-31", "val": 1.0, "accn": "undated"},
        _row("2024-01-01", "2024-03-31", 2.0, "2024-05-01", "dated"),
    ]}})
    dated = _eligible_rows(payload, "us-gaap", "Revenues", "USD", date(2024, 12, 31))
    assert [r["accn"] for r in dated] == ["dated"]


def test_undated_facts_are_kept_when_no_as_of_is_requested():
    """A live report is not reconstructing a date, so an undated fact is just
    a fact. Dropping it there would silently narrow live coverage."""
    from app.services.ingestion.restatements import _eligible_rows

    payload = _facts(Revenues={"units": {"USD": [
        {"start": "2024-01-01", "end": "2024-03-31", "val": 1.0, "accn": "undated"},
        _row("2024-01-01", "2024-03-31", 2.0, "2024-05-01", "dated"),
    ]}})
    live = _eligible_rows(payload, "us-gaap", "Revenues", "USD", None)
    assert [r["accn"] for r in live] == ["undated", "dated"]


def test_an_undated_fact_cannot_win_tag_selection_in_a_dated_report():
    """The survivor that motivated this: undated facts inflating a candidate's
    coverage would let an undatable series decide which tag a dated report
    inspects — the leakage this branch exists to close, by another route."""
    undated = {"units": {"USD": [
        {"start": f"2025-0{i + 1}-01", "end": f"2025-0{i + 1}-28", "val": 1.0, "accn": f"u{i}"}
        for i in range(8)
    ]}}
    payload = _facts(Revenues=_revised_history(), SalesRevenueNet=undated)
    assert _active_tag(payload, REV, "USD", date(2024, 12, 31)) == ("us-gaap", "Revenues")
def test_composite_components_are_all_us_gaap():
    """`_parse_selection` qualifies bare composite components as us-gaap,
    because that is how the mapper records them. If it ever composes from
    another taxonomy, that guess would inspect the wrong series (or none) —
    fail here rather than silently going blind on a summed field."""
    from app.services.ingestion.companyfacts_mapper import (
        DA_COMPONENTS,
        DEBT_CURRENT,
        DEBT_NONCURRENT,
        DEBT_SHORT,
        DEBT_TOTAL,
        FINANCE_LEASE_CURRENT,
        FINANCE_LEASE_NONCURRENT,
        SGA_COMPONENTS,
    )
    assert all(tax == "us-gaap" for tax, _ in SGA_COMPONENTS + DA_COMPONENTS)
    for group in (DEBT_CURRENT, DEBT_NONCURRENT, DEBT_SHORT, DEBT_TOTAL,
                  FINANCE_LEASE_CURRENT, FINANCE_LEASE_NONCURRENT):
        assert all(isinstance(t, str) and ":" not in t for t in group)


def test_a_single_tag_field_is_unaffected_by_the_composite_path():
    payload = _facts(Revenues=_revised_history())
    assert _found(payload, selected_tags={"revenue": "us-gaap:Revenues"}) == [
        ("us-gaap:Revenues", "2024-03-31", 100.0, 130.0)
    ]


# --- adversarial composite cases (round-13 review) ------------------------
# Three defects that a green suite did not catch, because the cases were not
# in it. All three suppress or misclassify EVIDENCE: the engine scores a
# revised figure while the appendix reports nothing, or reports it as routine.

def _debt(noncurrent, current=None):
    facts = {"LongTermDebtNoncurrent": {"units": {"USD": noncurrent}}}
    if current is not None:
        facts["LongTermDebtCurrent"] = {"units": {"USD": current}}
    return {"facts": {"us-gaap": facts}}


def _instant(end, val, filed, accn, form="10-Q"):
    return {"end": end, "val": val, "filed": filed, "accn": accn, "form": form}


def test_total_debt_is_inspected_although_it_is_in_no_candidate_table():
    """`total_debt` is assembled by `_total_debt_series` and recorded like any
    other field, but lives in neither INSTANT_FIELDS nor FLOW_FIELDS. Iterating
    those tables alone meant the engine could score a revised debt total while
    the appendix said nothing — silence reading as "no revisions" for one of
    the most consequential figures on the balance sheet."""
    payload = _debt([
        _instant("2024-03-31", 100.0, "2024-05-01", "d1"),
        _instant("2024-03-31", 150.0, "2024-08-01", "d2", "10-Q/A"),
    ])
    found = detect_restatements(payload, selected_tags={"total_debt": "LongTermDebtNoncurrent"})
    assert [(f.field_name, f.original_value, f.current_value) for f in found] == [
        ("total_debt", 100.0, 150.0)
    ]
    assert found[0].is_amendment


def test_a_composed_total_debt_is_compared_as_the_sum():
    payload = _debt(
        [_instant("2024-03-31", 1000.0, "2024-05-01", "a1"),
         _instant("2024-03-31", 1000.0, "2024-08-01", "a2")],
        [_instant("2024-03-31", 50.0, "2024-05-01", "b1"),
         _instant("2024-03-31", 300.0, "2024-08-01", "bA", "10-Q/A")],
    )
    found = detect_restatements(
        payload, selected_tags={"total_debt": "LongTermDebtNoncurrent+LongTermDebtCurrent+none"})
    assert [(f.original_value, f.current_value) for f in found] == [(1050.0, 1300.0)]


def test_a_field_with_no_mapper_selection_is_not_invented():
    payload = _debt([_instant("2024-03-31", 100.0, "2024-05-01", "d1"),
                     _instant("2024-03-31", 150.0, "2024-08-01", "d2")])
    assert detect_restatements(payload, selected_tags={"total_debt": None}) == []
    assert detect_restatements(payload, selected_tags={}) == []


def test_an_amendment_after_a_component_is_adopted_is_not_suppressed():
    """Refusing to compare across a composition change must not discard the
    revisions that follow it. Adopt G&A (1000 -> 1500), then amend it by
    10-Q/A (1500 -> 1700): the earliest and latest vintages have different
    component sets, and dropping the period on that basis silently lost a
    genuine 13% amendment. The comparison belongs inside the stable segment."""
    payload = _sga(
        [_row("2024-01-01", "2024-03-31", 1000.0, "2024-05-01", "s1")],
        [_row("2024-01-01", "2024-03-31", 500.0, "2024-08-01", "g1"),
         _row("2024-01-01", "2024-03-31", 700.0, "2024-11-01", "g2", "10-Q/A")],
    )
    found = detect_restatements(payload, selected_tags=SGA)
    assert [(f.original_value, f.current_value) for f in found] == [(1500.0, 1700.0)]
    assert found[0].is_amendment


def test_adoption_alone_is_still_not_a_revision():
    """The guard the segment logic must not undo."""
    payload = _sga(
        [_row("2024-01-01", "2024-03-31", 1000.0, "2024-05-01", "s1")],
        [_row("2024-01-01", "2024-03-31", 500.0, "2024-08-01", "g1")],
    )
    assert _found(payload, selected_tags=SGA) == []


def test_same_day_provenance_keeps_the_amendment_that_moved_the_figure():
    """Several components can be refiled on one date through different
    filings. Attributing the aggregate to whichever was iterated first
    dropped the /A that actually moved it, downgrading a high-confidence
    amendment to a routine comparative revision."""
    # Everything about this fixture is arranged so that ONLY a rule which
    # actually prefers the amendment can pass. The /A sits on
    # SellingAndMarketingExpense, which sorts AFTER
    # GeneralAndAdministrativeExpense, so component iteration order reaches
    # the ordinary filing first; and its accession sorts last, so the
    # accession tiebreak also picks the ordinary one. Two earlier versions of
    # this test passed by luck — first on accession order, then on tag order
    # after components began being iterated sorted — and proved nothing.
    payload = _sga(
        [_row("2024-01-01", "2024-03-31", 1000.0, "2024-05-01", "s1"),
         _row("2024-01-01", "2024-03-31", 1300.0, "2024-08-01", "zzz-amended", "10-Q/A")],
        [_row("2024-01-01", "2024-03-31", 10.0, "2024-05-01", "g1"),
         _row("2024-01-01", "2024-03-31", 10.0, "2024-08-01", "aaa-ordinary", "10-Q")],
    )
    found = detect_restatements(payload, selected_tags=SGA)
    assert len(found) == 1
    assert found[0].amendment_form == "10-Q/A"
    assert found[0].amendment_accession == "zzz-amended"
    assert found[0].is_amendment


def test_same_day_provenance_is_deterministic_without_an_amendment():
    """No /A on the date: the choice still must not depend on dict order."""
    payload = _sga(
        [_row("2024-01-01", "2024-03-31", 1000.0, "2024-05-01", "s1"),
         _row("2024-01-01", "2024-03-31", 1400.0, "2024-08-01", "zzz")],
        [_row("2024-01-01", "2024-03-31", 10.0, "2024-05-01", "g1"),
         _row("2024-01-01", "2024-03-31", 20.0, "2024-08-01", "aaa")],
    )
    accessions = {detect_restatements(payload, selected_tags=SGA)[0].current_accession
                  for _ in range(5)}
    assert accessions == {"aaa"}  # lowest accession among equals


def test_several_moved_components_still_produce_one_footprint():
    payload = _sga(
        [_row("2024-01-01", "2024-03-31", 1000.0, "2024-05-01", "s1"),
         _row("2024-01-01", "2024-03-31", 1200.0, "2024-08-01", "s2")],
        [_row("2024-01-01", "2024-03-31", 10.0, "2024-05-01", "g1"),
         _row("2024-01-01", "2024-03-31", 90.0, "2024-08-01", "g2")],
    )
    found = detect_restatements(payload, selected_tags=SGA)
    assert len(found) == 1
    assert (found[0].original_value, found[0].current_value) == (1010.0, 1290.0)


def test_component_order_cannot_change_a_composite_result():
    """Reproducibility, stated exactly rather than within a tolerance.

    Float addition is not associative, so summing components in the order a
    selection string happened to list them moved aggregates by ~1e-13 — never
    enough to flip a materiality decision, but enough that the same
    point-in-time report did not reproduce byte-identically, which is the one
    property such an artifact is supposed to have.

    The values matter. Arbitrary decimals mostly sum identically whatever the
    order, so a test built on them passes regardless of the code; these are
    chosen because (414.18 + 261.51) + 20.32 != (20.32 + 261.51) + 414.18.

    This test was written during the audit that produced the fix, then lost in
    a branch split — the fix reached main with nothing holding it there, and
    the mutation sweep caught it on the merged result.
    """
    def fact(val, filed, accn):
        return _row("2024-01-01", "2024-03-31", val, filed, accn)

    parts = {
        "A": [fact(414.18, "2024-05-01", "a1"), fact(900.00, "2024-08-01", "a2")],
        "B": [fact(261.51, "2024-05-01", "b1")],
        "C": [fact(20.32, "2024-05-01", "c1")],
    }

    def run(order):
        payload = {"facts": {"us-gaap": {k: {"units": {"USD": parts[k]}} for k in order}}}
        return [(f.original_value, f.current_value, f.is_amendment, f.current_accession)
                for f in detect_restatements(payload, selected_tags={"f": "+".join(order)})]

    assert run(["A", "B", "C"]) == run(["C", "B", "A"]) == run(["B", "A", "C"])
