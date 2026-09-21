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
    # Composites resolve to NOTHING — see the next test for why.
    ("LongTermDebtNoncurrent+LongTermDebtCurrent+CommercialPaper", []),
    ("SellingAndMarketingExpense+GeneralAndAdministrativeExpense", []),
])
def test_resolve_tags_reads_every_shape_the_mapper_records(selected, expected):
    assert _resolve_tags({}, "f", REV, "USD", None, {"f": selected}) == expected


def test_a_composite_component_is_never_reported_as_the_derived_field():
    """Changed deliberately after review. Expanding a composite into its
    components and reporting each as a revision of the DERIVED field states a
    number that was never scored.

    Here SG&A = S&M 1000 + G&A 10. G&A moves 10 -> 11: a 10% revision that
    clears the 1% materiality bar. The sga_expense the engine actually scored
    went 1010 -> 1011 — 0.099%, nowhere near it. Reporting
    `sga_expense: 10 -> 11` is a plausible, wrong, material-looking finding in
    a section whose entire value is that its numbers are checkable.
    """
    payload = _facts(
        SellingAndMarketingExpense={"units": {"USD": [
            _row("2024-01-01", "2024-03-31", 1000.0, "2024-05-01", "s1"),
        ]}},
        GeneralAndAdministrativeExpense={"units": {"USD": [
            _row("2024-01-01", "2024-03-31", 10.0, "2024-05-01", "g1"),
            _row("2024-01-01", "2024-03-31", 11.0, "2024-08-01", "g2"),
        ]}},
    )
    composite = {"sga_expense": "SellingAndMarketingExpense+GeneralAndAdministrativeExpense"}
    assert _found(payload, selected_tags=composite) == []


def test_an_unchecked_composite_is_disclosed_not_silently_omitted():
    """A field that was never examined must not read as a field with no
    revisions — that is the difference between a gap and an all-clear."""
    from app.services.ingestion.restatements import (
        render_restatements_section, unchecked_composites,
    )
    tags = {"sga_expense": "SellingAndMarketingExpense+GeneralAndAdministrativeExpense",
            "revenue": "us-gaap:Revenues"}
    assert unchecked_composites(tags) == ["sga_expense"]

    section = render_restatements_section([], unchecked_composites(tags))
    assert "NOT CHECKED: sga_expense" in section
    assert "Silence here means unexamined, not unrevised." in section


def test_a_single_tag_field_is_not_called_unchecked():
    from app.services.ingestion.restatements import unchecked_composites
    assert unchecked_composites({"revenue": "us-gaap:Revenues"}) == []
    assert unchecked_composites({"revenue": None}) == []
    assert unchecked_composites(None) == []


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
        if "+" in qualified:
            # A composite is deliberately unresolvable, and disclosed instead.
            assert resolved == []
            continue
        assert resolved, f"{field_name}: mapper recorded {qualified!r}, resolver found no series"
        for taxonomy, tag in resolved:
            assert taxonomy and tag and ":" not in tag


# --- the wiring ------------------------------------------------------------

def test_build_report_hands_the_detector_the_mappers_selection(monkeypatch):
    """Threading the selection only helps if the report actually passes it.
    Without this, reverting the wiring leaves every unit test above green
    while the shipped report goes back to guessing."""
    from types import SimpleNamespace

    from app.core.pipeline import analyze
    from app.services.ingestion import restatements as restatements_mod
    from app.services.reporting.report_builder import build_report
    from tests.fixtures.companies import stretch_dataset

    seen: dict = {}

    def spy(facts_json, **kw):
        seen.update(kw)
        return []

    monkeypatch.setattr(restatements_mod, "detect_restatements", spy)

    class _Client:
        def company_facts(self, ticker):
            return {"facts": {}}

        def submissions(self, ticker):
            raise RuntimeError("not under test")

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
