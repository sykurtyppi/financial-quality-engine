"""Every figure the engine scores is watched for silent revisions.

Hermes deep audit, finding 3: the snapshot comparison followed one tag per
field, taken from the single-tag candidate tables, so revisions to COMPOSED
figures — total debt, a composite SG&A or D&A — never reached the
silent-revision section or Tier-1. `diff_scored` compares those as the mapper
builds them, and the invariant below holds for every scored field: move the
facts it is built from and the diff reports it.

Hermes re-audit: that still skipped D&A standing on depreciation alone, and
the raw diff behind single-concept fields followed `_active_tag`, which can
pick a tag the mapper does not score. Every scored field is now compared as
the mapper builds it; raw facts are provenance and pre-window context only.
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
    duration,
    instant,
    quarter,
)

LAST = QUARTER_ENDS[-1]
FLOOR = date(2022, 1, 1)


def _every_field(*, composites: bool, da_partial: bool = False) -> dict:
    """A payload populating every field, each from one concept — or, with
    `composites`, SG&A and D&A from their components and total debt from
    the split; with `da_partial`, D&A from depreciation alone (the partial
    fallback). Values differ by field and quarter."""
    p = Payload("Every Field Co")
    for n, spec in enumerate(FIELDS):
        if spec.name == "total_debt":
            continue
        if composites and spec.name in ("sga_expense", "depreciation_amortization"):
            continue
        if da_partial and spec.name == "depreciation_amortization":
            p.add("Depreciation", [quarter(q, 20.0 + i) for i, q in enumerate(QUARTER_ENDS)])
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


@pytest.mark.parametrize(
    "form", ["single", "composed", "partial"], ids=["single", "composed", "partial-da"]
)
@pytest.mark.parametrize("field_name", SCORED)
def test_moving_the_facts_behind_any_scored_field_is_reported(field_name, form):
    older = _every_field(composites=form == "composed", da_partial=form == "partial")
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
    assert "built from LongTermDebtNoncurrent+LongTermDebtCurrent" in table


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
    assert result.canonical_unavailable == (
        "scored values not compared as the engine builds them: the older snapshot "
        "could not be mapped (raw facts only)"
    )


def test_the_report_shows_a_total_debt_revision(tmp_path):
    cik = 1045810
    older = _every_field(composites=True)
    newer = _bump(older, "LongTermDebtNoncurrent", QUARTER_ENDS[-2], factor=2.0)
    store_snapshot(cik, older, now=datetime(2026, 9, 19, 12, tzinfo=UTC), root=tmp_path)
    store_snapshot(cik, newer, now=datetime(2026, 9, 20, 12, tzinfo=UTC), root=tmp_path)
    rep = report_diff(cik, as_of=date(2026, 9, 21), root=tmp_path)
    assert rep.canonical_unavailable is None
    assert [c.field_name for c in rep.changes_since_previous] == ["total_debt"]
    from app.services.reporting.report_builder import _silent_revisions_section

    assert "built from LongTermDebtNoncurrent+LongTermDebtCurrent" in _silent_revisions_section(rep)


# --- one resolver: the vintage diff compares what the mapper scores ----------


def test_a_revision_behind_the_partial_depreciation_fallback_is_reported_and_promoted():
    # Hermes re-audit: canonical D&A 20 -> 40 on depreciation alone (no
    # amortization reported) went unreported — neither the raw diff (which
    # followed the aggregate candidates) nor the composed comparison (which
    # skipped a qualified selection) looked at it.
    older = _every_field(composites=False, da_partial=True)
    q = QUARTER_ENDS[-2]
    newer = _bump(older, "Depreciation", q, factor=2.0)
    [c] = [c for c in diff_scored(older, newer).changes if c.field_name == "depreciation_amortization"]
    assert (c.key.end, c.old_value, c.new_value) == (q, 30.0, 60.0)
    assert c.key.taxonomy == COMPOSED and c.key.tag == "Depreciation (partial)"
    lines = silent_revision_tier1_lines([c], "a", "b", period_since=FLOOR)
    assert lines and lines[0].startswith("Silent revision: depreciation_amortization ")


def _cash_tags_disagree() -> dict:
    """The mapper scores CashAndCashEquivalentsAtCarryingValue (it covers
    every reported quarter); `_active_tag` — the raw diff's approximation,
    counting distinct periods over all history — picks the restricted-cash
    tag, which the filer reported at many dates long ago."""
    older = _every_field(composites=False)
    del older["facts"]["us-gaap"]["CashAndCashEquivalentsAtCarryingValue"]
    _add(older, "CashAndCashEquivalentsAtCarryingValue", [instant(q, 500.0 + i) for i, q in enumerate(QUARTER_ENDS[4:])])
    old_dates = [date(2015 + k // 12, k % 12 + 1, 28) for k in range(40)]
    _add(older, "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
         [instant(d, 1.0) for d in old_dates] + [instant(q, 520.0) for q in QUARTER_ENDS[:6]])
    return older


def test_the_raw_diffs_tag_approximation_does_not_decide_what_is_reported():
    older = _cash_tags_disagree()
    q = QUARTER_ENDS[5]  # a reported quarter
    _ds, diag = build_dataset(older, "X")
    assert diag.field_by_name("cash_and_equivalents").tag_used == (
        "us-gaap:CashAndCashEquivalentsAtCarryingValue"
    )
    # The unscored tag moves: the raw diff reports it as cash; nothing scored moved.
    noise = _bump(older, "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", q)
    assert [c for c in diff_vintages(older, noise) if c.field_name == "cash_and_equivalents"]
    assert not [c for c in diff_scored(older, noise).changes if c.field_name == "cash_and_equivalents"]
    # The scored tag moves: the raw diff cannot see it; the scored diff
    # reports it, with the filing of the fact behind it.
    real = _bump(older, "CashAndCashEquivalentsAtCarryingValue", q)
    assert not [c for c in diff_vintages(older, real) if c.field_name == "cash_and_equivalents"]
    [c] = [c for c in diff_scored(older, real).changes if c.field_name == "cash_and_equivalents"]
    assert c.key.tag == "CashAndCashEquivalentsAtCarryingValue" and c.old_accession
    assert (c.old_value, c.new_value) == (501.0, 751.5)


def test_a_single_concept_change_names_the_filing_behind_it():
    older = _every_field(composites=False)
    newer = _bump(older, "InventoryNet", QUARTER_ENDS[-3])
    [c] = [c for c in diff_scored(older, newer).changes if c.field_name == "inventory"]
    assert c.key.taxonomy == "us-gaap" and c.key.tag == "InventoryNet"
    assert c.old_filed is not None and c.old_accession
    assert "built from" not in render_changes([c], "a", "b")


def test_an_old_period_revision_of_a_scored_tag_is_context_never_promoted():
    older = _every_field(composites=False)
    old_quarter = QUARTER_ENDS[1]  # before the reported window
    newer = _bump(older, "InventoryNet", old_quarter)
    changes = [c for c in diff_scored(older, newer).changes if c.field_name == "inventory"]
    assert [(c.key.end, c.scope) for c in changes] == [(old_quarter, "context")]
    assert not silent_revision_tier1_lines(changes, "a", "b", period_since=date(2000, 1, 1))
    assert "(before the scored window)" in render_changes(changes, "a", "b")


def test_provenance_names_the_quarters_own_fact_not_the_year_to_date_one():
    # An ordinary 10-Q carries the quarter and the year to date, both ending
    # on the quarter end. The scored value is the quarter's; so is its filing.
    older = _every_field(composites=False)
    q = QUARTER_ENDS[-3]  # 2024-06-30
    tag = "RevenueFromContractWithCustomerExcludingAssessedTax"
    ytd = duration(date(q.year, 1, 1), q, 9_999.0, filed=date(2024, 8, 30))
    older["facts"]["us-gaap"][tag]["units"]["USD"].append(ytd)
    newer = _bump(older, tag, q)
    [c] = [c for c in diff_scored(older, newer).changes if c.field_name == "revenue"]
    assert c.key.start == date(2024, 4, 1)
    assert c.old_accession != ytd["accn"]


def test_a_quarter_ending_on_the_window_start_is_compared():
    """Mutation backlog (Hermes audit round 5): `since` is inclusive — a
    quarter ending exactly on it is in the window."""
    from datetime import timedelta

    older = _every_field(composites=False)
    q = QUARTER_ENDS[-3]
    newer = _bump(older, _first_component(older, "revenue"), q)

    def revised(since):
        return {c.field_name for c in diff_scored(older, newer, since=since).changes
                if c.kind == "revised"}

    assert "revenue" in revised(q)
    assert "revenue" not in revised(q + timedelta(days=1))


# Survivors of the full mutation census (Hermes audit round 6), each run
# against the whole suite first: each test fails under the mutant named.

WINDOW_START = QUARTER_ENDS[4]  # the oldest of the 8 reported quarters


def _set(facts: dict, concept: str, end: date, val: float) -> dict:
    out = copy.deepcopy(facts)
    for taxonomy in out["facts"].values():
        for unit_rows in taxonomy.get(concept, {}).get("units", {}).values():
            for row in unit_rows:
                if row["end"] == end.isoformat():
                    row["val"] = val
    return out


def _inventory(older_val: float, newer_val: float, end: date = QUARTER_ENDS[-3]):
    base = _every_field(composites=False)
    changes = diff_scored(_set(base, "InventoryNet", end, older_val),
                          _set(base, "InventoryNet", end, newer_val)).changes
    return [c for c in changes if c.field_name == "inventory"]


def test_the_scored_diff_applies_the_materiality_floor_inclusively():
    # `pct < materiality_pct` -> `<=` dropped exactly 1%; the `continue`
    # -> `pass` reported a move below it.
    assert [c.kind for c in _inventory(100.0, 101.0)] == ["revised"]
    assert _inventory(100.0, 100.9) == []


def test_a_scored_zero_that_stays_zero_is_not_a_revision():
    # `if new == old: continue` -> `pass`: 0 -> 0 has no percentage.
    assert _inventory(0.0, 0.0) == []
    assert [c.kind for c in _inventory(0.0, 7.0)] == ["revised"]


def test_a_share_count_move_is_not_a_scored_revision():
    # The split-adjusted `continue` -> `pass` compared share counts, where a
    # split is not a restatement.
    older = _every_field(composites=False)
    concept = _first_component(older, "shares_outstanding")
    newer = _bump(older, concept, QUARTER_ENDS[-3], factor=4.0)
    assert not [c for c in diff_scored(older, newer).changes
                if c.field_name in SPLIT_ADJUSTED_FIELDS]


def test_the_oldest_reported_quarter_can_be_withdrawn():
    # `end >= window_start` -> `>`: a figure gone from the first quarter the
    # newer snapshot still reports was not called withdrawn.
    older = _every_field(composites=True)
    newer = copy.deepcopy(older)
    rows = newer["facts"]["us-gaap"]["LongTermDebtNoncurrent"]["units"]["USD"]
    newer["facts"]["us-gaap"]["LongTermDebtNoncurrent"]["units"]["USD"] = [
        r for r in rows if r["end"] != WINDOW_START.isoformat()
    ]
    withdrawn = [c.key.end for c in diff_scored(older, newer).changes
                 if c.field_name == "total_debt" and c.kind == "withdrawn"]
    assert withdrawn == [WINDOW_START]


def test_a_revision_in_the_oldest_reported_quarter_is_not_also_context():
    # `c.key.end < window_start` -> `<=` listed it twice: once compared,
    # once as a pre-window context row.
    changes = _inventory(100.0, 150.0, end=WINDOW_START)
    assert [(c.key.end, c.scope) for c in changes] == [(WINDOW_START, "scored")]
