"""Every figure the engine scores is watched for silent revisions.

Hermes deep audit, finding 3: the snapshot comparison followed one tag per
field, taken from the single-tag candidate tables, so revisions to COMPOSED
figures — total debt, a composite SG&A or D&A — never reached the
silent-revision section or Tier-1. `diff_scored` compares those as the mapper
builds them, and the invariant below holds for every scored field: move the
facts it is built from and the diff reports it.
"""

from __future__ import annotations

import copy
from datetime import UTC, date, datetime

import pytest

from app.services.ingestion.companyfacts_mapper import build_dataset
from app.services.ingestion.fields import FIELDS, Kind
from app.services.ingestion.restatements import SPLIT_ADJUSTED_FIELDS
from app.services.ingestion.vintages import (
    COMPOSED,
    diff_scored,
    diff_vintages,
    render_changes,
    report_diff,
    silent_revision_tier1_lines,
    store_snapshot,
)
from tests.fixtures.selection_cases import (
    QUARTER_ENDS,
    Payload,
    annual,
    instant,
    quarter,
)

LAST = QUARTER_ENDS[-1]
FLOOR = date(2022, 1, 1)


def _every_field(*, composites: bool) -> dict:
    """A payload populating every field, each from one concept — or, with
    `composites`, SG&A and D&A from their components and total debt from
    the split. Values differ by field and quarter."""
    p = Payload("Every Field Co")
    for n, spec in enumerate(FIELDS):
        if spec.name == "total_debt":
            continue
        if composites and spec.name in ("sga_expense", "depreciation_amortization"):
            continue
        taxonomy, tag = spec.strategies[0].tags[0]
        unit = spec.unit if spec.unit != "shares" else "shares"
        base = 1_000.0 * (n + 1)
        if spec.kind is Kind.INSTANT:
            rows = [instant(q, base + i) for i, q in enumerate(QUARTER_ENDS)]
        else:
            rows = [quarter(q, base + i) for i, q in enumerate(QUARTER_ENDS)]
            if spec.name == "revenue":
                rows += [annual(y, 4 * base) for y in (2022, 2023, 2024)]
        p.add(tag, rows, taxonomy=taxonomy, unit=unit)
    if composites:
        p.add("SellingAndMarketingExpense", [quarter(q, 60.0 + i) for i, q in enumerate(QUARTER_ENDS)])
        p.add("GeneralAndAdministrativeExpense", [quarter(q, 30.0) for q in QUARTER_ENDS])
        p.add("Depreciation", [quarter(q, 20.0 + i) for i, q in enumerate(QUARTER_ENDS)])
        p.add("AmortizationOfIntangibleAssets", [quarter(q, 10.0) for q in QUARTER_ENDS])
    p.add("LongTermDebtNoncurrent", [instant(q, 180.0) for q in QUARTER_ENDS])
    p.add("LongTermDebtCurrent", [instant(q, 20.0) for q in QUARTER_ENDS])
    return p.data


def _bump(facts: dict, concept: str, end: date, factor: float = 1.5) -> dict:
    """The same payload with `concept`'s value at `end` revised (a later
    snapshot carrying a different number for an old period)."""
    out = copy.deepcopy(facts)
    for taxonomy in out["facts"].values():
        for unit_rows in taxonomy.get(concept, {}).get("units", {}).values():
            for row in unit_rows:
                if row["end"] == end.isoformat():
                    row["val"] *= factor
    return out


def _add(facts: dict, concept: str, rows: list[dict]) -> None:
    facts["facts"]["us-gaap"].setdefault(concept, {"units": {"USD": []}})["units"]["USD"].extend(rows)


def _first_component(facts: dict, field_name: str) -> str:
    _ds, diag = build_dataset(facts, "X")
    selected = diag.field_by_name(field_name).tag_used
    assert selected, field_name
    return selected.split("+")[0].split(":")[-1]


SCORED = [f.name for f in FIELDS if f.name not in SPLIT_ADJUSTED_FIELDS]


@pytest.mark.parametrize("composites", [False, True], ids=["single", "composed"])
@pytest.mark.parametrize("field_name", SCORED)
def test_moving_the_facts_behind_any_scored_field_is_reported(field_name, composites):
    older = _every_field(composites=composites)
    newer = _bump(older, _first_component(older, field_name), QUARTER_ENDS[-3])
    moved = {c.field_name for c in diff_scored(older, newer).changes if c.kind == "revised"}
    assert field_name in moved


def test_hermes_controlled_case_raw_diff_misses_it_scored_diff_does_not():
    older = _every_field(composites=True)
    # total debt 200 -> 300 (noncurrent 180 -> 280), SG&A 90+i -> 140+i.
    newer = copy.deepcopy(older)
    for concept, delta in (("LongTermDebtNoncurrent", 100.0), ("SellingAndMarketingExpense", 50.0)):
        for row in newer["facts"]["us-gaap"][concept]["units"]["USD"]:
            if row["end"] == QUARTER_ENDS[-2].isoformat():
                row["val"] += delta
    raw = {c.field_name for c in diff_vintages(older, newer, scored_only=True)}
    assert not raw & {"total_debt", "sga_expense"}  # the gap Hermes found
    changes = {c.field_name: c for c in diff_scored(older, newer).changes}
    assert (changes["total_debt"].old_value, changes["total_debt"].new_value) == (200.0, 300.0)
    sga = changes["sga_expense"]
    assert sga.new_value - sga.old_value == 50.0
    assert sga.key.taxonomy == COMPOSED
    assert sga.key.tag == "SellingAndMarketingExpense+GeneralAndAdministrativeExpense"


def test_a_composed_revision_is_promoted_to_tier_1_and_rendered():
    older = _every_field(composites=True)
    newer = _bump(older, "LongTermDebtNoncurrent", QUARTER_ENDS[-2], factor=2.0)
    changes = diff_scored(older, newer).changes
    lines = silent_revision_tier1_lines(changes, "2026-09-01", "2026-09-02", period_since=FLOOR)
    assert any(line.startswith("Silent revision: total_debt for ") for line in lines)
    table = render_changes(changes, "2026-09-01", "2026-09-02")
    assert "composed from LongTermDebtNoncurrent+LongTermDebtCurrent" in table


def test_a_change_of_composition_is_reported_but_never_promoted():
    older = _every_field(composites=True)
    newer = copy.deepcopy(older)
    # The newer payload also files a single SG&A tag covering every quarter,
    # which now wins: the figure is BUILT differently, not revised.
    # (The composite no longer covers strictly more quarters.)
    _add(newer, "SellingGeneralAndAdministrativeExpense", [quarter(q, 500.0) for q in QUARTER_ENDS])
    changes = [c for c in diff_scored(older, newer).changes if c.field_name == "sga_expense"]
    assert changes and all(c.moved_tag for c in changes)
    assert not silent_revision_tier1_lines(changes, "a", "b", period_since=FLOOR)


def test_a_quarter_the_newer_snapshot_still_covers_can_be_withdrawn_one_that_rolled_off_cannot():
    older = _every_field(composites=True)
    newer = copy.deepcopy(older)
    rows = newer["facts"]["us-gaap"]["LongTermDebtNoncurrent"]["units"]["USD"]
    gone = QUARTER_ENDS[-2].isoformat()
    newer["facts"]["us-gaap"]["LongTermDebtNoncurrent"]["units"]["USD"] = [
        r for r in rows if r["end"] != gone
    ]
    withdrawn = [c for c in diff_scored(older, newer).changes
                 if c.field_name == "total_debt" and c.kind == "withdrawn"]
    assert [c.key.end for c in withdrawn] == [QUARTER_ENDS[-2]]

    # A newer snapshot one quarter on: the oldest reported quarter rolls out
    # of its window, and that is not a withdrawal.
    later = copy.deepcopy(older)
    q_next = date(2025, 3, 31)
    for concept in list(later["facts"]["us-gaap"]):
        for unit_rows in later["facts"]["us-gaap"][concept]["units"].values():
            template = next((r for r in unit_rows if r["end"] == LAST.isoformat()), None)
            if template is not None:
                row = dict(template, end=q_next.isoformat(), filed="2025-05-10")
                if "start" in row:
                    row["start"] = "2025-01-01"
                unit_rows.append(row)
    assert not [c for c in diff_scored(older, later).changes if c.kind == "withdrawn"]


def test_raw_rows_for_a_composed_field_are_not_reported():
    # A single SG&A tag the filer used only before the reported window is
    # revised; the engine scores the composite in every reported quarter, so
    # that is not a revision of the scored figure.
    older = _every_field(composites=True)
    _add(older, "SellingGeneralAndAdministrativeExpense", [quarter(q, 400.0) for q in QUARTER_ENDS[:4]])
    newer = _bump(older, "SellingGeneralAndAdministrativeExpense", QUARTER_ENDS[3])
    assert [c for c in diff_vintages(older, newer) if c.field_name == "sga_expense"]
    assert not [c for c in diff_scored(older, newer).changes if c.field_name == "sga_expense"]


def test_an_unmappable_snapshot_says_composed_fields_were_not_compared():
    older = {"facts": {"us-gaap": {"Assets": {"units": {"USD": [instant(LAST, 1.0)]}}}}}
    result = diff_scored(older, _every_field(composites=False))
    assert result.composed_unavailable == (
        "composed fields (total debt, composite SG&A/D&A) not compared: the older "
        "snapshot could not be mapped"
    )


def test_the_report_shows_a_total_debt_revision(tmp_path):
    cik = 1045810
    older = _every_field(composites=True)
    newer = _bump(older, "LongTermDebtNoncurrent", QUARTER_ENDS[-2], factor=2.0)
    store_snapshot(cik, older, now=datetime(2026, 9, 19, 12, tzinfo=UTC), root=tmp_path)
    store_snapshot(cik, newer, now=datetime(2026, 9, 20, 12, tzinfo=UTC), root=tmp_path)
    rep = report_diff(cik, as_of=date(2026, 9, 21), root=tmp_path)
    assert rep.composed_unavailable is None
    assert [c.field_name for c in rep.changes_since_previous] == ["total_debt"]
    from app.services.reporting.report_builder import _silent_revisions_section

    assert "composed from LongTermDebtNoncurrent+LongTermDebtCurrent" in _silent_revisions_section(rep)
