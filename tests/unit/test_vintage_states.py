"""A raw capture never stands in for a scored state.

Hermes audit of 424b0b4, finding 2. The vintage store has two writers:
`store_snapshot` archives the payload a report SCORED (reached only after
`build_dataset` succeeded), `capture` archives whatever the watch sweep or
`scripts/vintage.py capture` fetched — never mapped, possibly partial (a bare
`{"cik", "entityName"}` passes the SEC shape check). Both were recorded as the
same kind of observation, the report took `visible[-2]` as its baseline
whatever wrote it, and `diff_scored` fell back to raw fact rows still labelled
`scope="scored"` when a side could not be mapped. Reproduced:

  A. S1 (scored) -> R (raw, unmappable) -> S2 (scored): S1 -> S2 has no scored
     change, yet R -> S2 promoted a Tier-1 "silent revision" of a single SG&A
     tag the engine does not score (it scores the composite).
  B. S1 -> bare capture -> S2: a real +20% total_assets revision was compared
     bare -> S2 and reported as 0 changes (the baseline was lost).
  C. S1 -> R as the newest state: 27 "scored" withdrawals from a partial fetch.

Each observation now records its kind, the report's baseline is the newest
SCORED state before its own, and rows read from raw facts carry
`scope="raw"`, which nothing promotes.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from app.schemas.ledger import ValidationStatus
from app.services.ingestion import vintages as v
from app.services.ingestion.companyfacts_mapper import build_dataset
from app.services.ingestion.vintages import (
    FactKey,
    VintageChange,
    capture,
    diff_scored,
    observed_vintages,
    read_manifest,
    render_changes,
    report_diff,
    silent_revision_tier1_lines,
    store_snapshot,
)
from app.services.reporting import ledger
from app.services.reporting.report_builder import (
    _collect_streams,
    _silent_revisions_section,
)
from tests.fixtures.selection_cases import QUARTER_ENDS, quarter
from tests.unit.test_vintage_composed import _add, _bump, _every_field

CIK = 1045810
REPORT_DAY = date(2024, 12, 31)  # Tier-1 floor 2022-12-28
D1, D2, D3, D4 = (datetime(2024, 12, d, 12, tzinfo=UTC) for d in (28, 29, 30, 31))
REVENUE_TAGS = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax"]
REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"


class _Client:
    def __init__(self, facts: dict, cik: int = CIK):
        self._facts, self._cik = facts, cik

    def resolve_cik(self, ticker):
        return self._cik

    def company_facts(self, ticker):
        return self._facts

    def company_facts_by_cik(self, cik):
        return self._facts

    def submissions(self, ticker):
        return {"filings": {"recent": {}}}

    def submissions_by_cik(self, cik):
        return {"filings": {"recent": {}}}


def _strip(facts: dict, concepts: list[str]) -> dict:
    """A partial response: the same document with concepts missing, which
    the mapper cannot build (no revenue, no assets)."""
    out = copy.deepcopy(facts)
    for c in concepts:
        out["facts"]["us-gaap"].pop(c, None)
    return out


def _partial(facts: dict) -> dict:
    return _strip(facts, ["Assets", *REVENUE_TAGS])


def _bare() -> dict:
    return {"cik": CIK, "entityName": "Every Field Co"}  # no "facts": passes the shape check


def _tier1(root: Path, facts: dict) -> tuple:
    _s, _e, tier1, errors, _t, _scan, rep = _collect_streams(
        _Client(facts), "XYZ", REPORT_DAY, company_facts=facts, vintage_root=root)
    assert errors["vintage"] is None, errors
    return rep, [line for line in tier1 if line.startswith("Silent revision")]


def _scenario_a() -> tuple[dict, dict]:
    s1 = _every_field(composites=True)
    _add(s1, "SellingGeneralAndAdministrativeExpense",
         [quarter(q, 400.0) for q in QUARTER_ENDS[:4]])
    return s1, _bump(s1, "SellingGeneralAndAdministrativeExpense", QUARTER_ENDS[3])


def _scenario_b() -> tuple[dict, dict]:
    s1 = _every_field(composites=False)
    return s1, _bump(s1, "Assets", QUARTER_ENDS[-3], factor=1.2)


# --- the reproduced scenarios -------------------------------------------------


def test_a_raw_capture_between_two_reports_is_not_the_baseline(tmp_path):
    s1, s2 = _scenario_a()
    assert diff_scored(s1, s2).changes == []  # the truth: nothing scored moved
    store_snapshot(CIK, s1, now=D2, root=tmp_path)
    assert capture(_Client(_partial(s1)), "XYZ", now=D3, root=tmp_path).wrote
    store_snapshot(CIK, s2, now=D4, root=tmp_path)
    rep, tier1 = _tier1(tmp_path, s2)
    assert tier1 == []
    assert (rep.previous.captured, rep.newest.captured) == ("2024-12-29", "2024-12-31")
    assert rep.changes_since_previous == [] and rep.canonical_unavailable is None
    assert rep.status_line() == (
        "compared 2024-12-29 → 2024-12-31: 0 change(s); 1 raw capture(s) since "
        "2024-12-29 not used as the baseline"
    )
    assert "_1 raw capture(s) since 2024-12-29 not used as the baseline._" in (
        _silent_revisions_section(rep))


def test_b_a_bare_capture_does_not_hide_a_real_revision(tmp_path):
    s1, s2 = _scenario_b()
    store_snapshot(CIK, s1, now=D2, root=tmp_path)
    assert capture(_Client(_bare()), "XYZ", now=D3, root=tmp_path).wrote
    store_snapshot(CIK, s2, now=D4, root=tmp_path)
    rep, tier1 = _tier1(tmp_path, s2)
    assert rep.previous.captured == "2024-12-29"
    [c] = rep.changes_since_previous
    assert (c.kind, c.field_name, c.key.end, c.scope) == (
        "revised", "total_assets", QUARTER_ENDS[-3], "scored")
    assert round(c.pct_change, 6) == 0.2
    assert len(tier1) == 1 and tier1[0].startswith("Silent revision: total_assets")


def test_b_on_the_real_aapl_fixture(tmp_path):
    cik = 320193
    s1 = json.loads((REAL / "companyfacts_AAPL_trimmed.json").read_text())
    ds, _ = build_dataset(s1, "AAPL")
    target = ds.periods[-3].period_end
    s2 = copy.deepcopy(s1)
    for rows in s2["facts"]["us-gaap"]["Assets"]["units"].values():
        for row in rows:
            if row["end"] == target.isoformat():
                row["val"] *= 1.2
    at = [datetime(2026, 9, d, 12, tzinfo=UTC) for d in (19, 20, 21)]
    store_snapshot(cik, s1, now=at[0], root=tmp_path)
    bare = {"cik": cik, "entityName": "Apple Inc."}
    assert capture(_Client(bare, cik), "AAPL", now=at[1], root=tmp_path).wrote
    store_snapshot(cik, s2, now=at[2], root=tmp_path)
    rep = report_diff(cik, as_of=date(2026, 9, 21), root=tmp_path)
    assert rep.previous.captured == "2026-09-19"
    assert [(c.kind, c.field_name, c.key.end, c.scope) for c in rep.changes_since_previous] == [
        ("revised", "total_assets", target, "scored")]
    assert silent_revision_tier1_lines(
        rep.changes_since_previous, "2026-09-19", "2026-09-21", period_since=date(2024, 9, 21))


def test_c_a_raw_newest_state_is_not_compared(tmp_path):
    s1 = _every_field(composites=True)
    store_snapshot(CIK, s1, now=D2, root=tmp_path)
    assert capture(_Client(_partial(s1)), "XYZ", now=D3, root=tmp_path).wrote
    rep = report_diff(CIK, as_of=date(2024, 12, 30), root=tmp_path)
    assert rep.newest.kind == "scored" and rep.newest.captured == "2024-12-29"
    assert not rep.compared and rep.changes_since_previous == []
    assert rep.status_line() == (
        "no earlier scored snapshot to diff the newest (2024-12-29) against; "
        "1 raw capture(s) after 2024-12-29 not compared"
    )


def test_c_raw_captures_after_the_newest_scored_state_are_named(tmp_path):
    s1, s2 = _scenario_b()
    store_snapshot(CIK, s1, now=D1, root=tmp_path)
    store_snapshot(CIK, s2, now=D2, root=tmp_path)
    capture(_Client(_partial(s2)), "XYZ", now=D3, root=tmp_path)
    capture(_Client(_bare()), "XYZ", now=D4, root=tmp_path)
    rep = report_diff(CIK, as_of=REPORT_DAY, root=tmp_path)
    assert (rep.previous.captured, rep.newest.captured) == ("2024-12-28", "2024-12-29")
    assert [(c.field_name, c.scope) for c in rep.changes_since_previous] == [
        ("total_assets", "scored")]
    assert rep.status_line() == (
        "compared 2024-12-28 → 2024-12-29: 1 change(s); 2 raw capture(s) after "
        "2024-12-29 not compared"
    )


def test_no_scored_state_at_all_is_not_compared(tmp_path):
    s1, s2 = _scenario_b()
    capture(_Client(s1), "XYZ", now=D1, root=tmp_path)
    capture(_Client(s2), "XYZ", now=D2, root=tmp_path)
    rep = report_diff(CIK, as_of=REPORT_DAY, root=tmp_path)
    assert not rep.compared and rep.previous is None
    assert rep.status_line() == (
        "no scored snapshot at or before 2024-12-31; 2 raw capture(s) not used")


def test_raw_rows_are_shown_but_never_promoted_or_validated(tmp_path):
    # A scored state the mapper cannot build (only a test stores one; the
    # report stores a payload it has built) still reaches the raw fallback.
    s1 = _every_field(composites=True)
    store_snapshot(CIK, s1, now=D2, root=tmp_path)
    store_snapshot(CIK, _partial(s1), now=D3, root=tmp_path)
    rep = report_diff(CIK, as_of=REPORT_DAY, root=tmp_path)
    assert rep.changes_since_previous, "the raw rows stay visible in the appendix"
    assert {c.scope for c in rep.changes_since_previous} == {"raw"}
    assert silent_revision_tier1_lines(
        rep.changes_since_previous, "a", "b", period_since=date(2000, 1, 1)) == []
    n = len(rep.changes_since_previous)
    assert rep.status_line() == (
        f"compared 2024-12-29 → 2024-12-30: 0 change(s) (+{n} raw fact row(s), not "
        "scored changes); scored values not compared as the engine builds them: the "
        "newer snapshot could not be mapped (raw fact rows only, not scored changes)"
    )
    md = render_changes(rep.changes_since_previous, "2024-12-29", "2024-12-30")
    assert md.count("(raw fact; not a scored figure)") == n
    b = ledger._Builder()
    ledger._vintage_items(b, rep, date(2000, 1, 1))
    assert b.items and all(i.validation_status is ValidationStatus.DIRECTIONAL for i in b.items)
    assert {i.note for i in b.items} == {
        "raw fact (a snapshot could not be mapped): not a scored change"}
    _rep, tier1 = _tier1(tmp_path, s1)
    assert tier1 == []


def test_c_through_the_report_stream_promotes_nothing(tmp_path):
    s1 = _every_field(composites=True)
    store_snapshot(CIK, s1, now=D2, root=tmp_path)
    s2 = _bump(s1, "Assets", QUARTER_ENDS[-3], factor=1.2)
    assert capture(_Client(_strip(s2, REVENUE_TAGS)), "XYZ", now=D4, root=tmp_path).wrote
    _rep, tier1 = _tier1(tmp_path, s1)
    assert tier1 == []


# --- the unmappable fallback --------------------------------------------------


def test_an_unmappable_side_returns_raw_scoped_rows():
    s1, s2 = _scenario_b()
    # Unmappable (one balance-sheet quarter, no revenue), but it still
    # carries the revised Assets fact the raw diff compares.
    newer = _strip(s2, REVENUE_TAGS)
    for rows in newer["facts"]["us-gaap"]["Assets"]["units"].values():
        rows[:] = [r for r in rows if r["end"] == QUARTER_ENDS[-3].isoformat()]
    result = diff_scored(s1, newer)
    assert result.canonical_unavailable
    revised = [c for c in result.changes if c.kind == "revised"]
    assert [c.field_name for c in revised] == ["total_assets"]
    assert {c.scope for c in result.changes} == {"raw"}
    assert silent_revision_tier1_lines(result.changes, "a", "b", period_since=date(2000, 1, 1)) == []


# --- the recorded kind --------------------------------------------------------


def test_each_writer_records_its_kind(tmp_path):
    s1, s2 = _scenario_b()
    store_snapshot(CIK, s1, now=D1, root=tmp_path)
    capture(_Client(s2), "XYZ", now=D2, root=tmp_path)
    assert [o["kind"] for o in read_manifest(CIK, tmp_path)["observations"]] == ["scored", "raw"]
    assert [o.kind for o in observed_vintages(CIK, tmp_path)] == ["scored", "raw"]


def test_the_same_content_stored_as_scored_is_upgraded(tmp_path):
    s1, s2 = _scenario_b()
    store_snapshot(CIK, s1, now=D1, root=tmp_path)
    # The sweep fetched S2 first; a report then scored the identical payload
    # the same day (the observation is deduplicated, its kind upgraded) ...
    capture(_Client(s2), "XYZ", now=D2, root=tmp_path)
    assert observed_vintages(CIK, tmp_path)[-1].kind == "raw"
    store_snapshot(CIK, s2, now=D2, root=tmp_path)
    obs = read_manifest(CIK, tmp_path)["observations"]
    assert [o["kind"] for o in obs] == ["scored", "scored"]
    assert [o.kind for o in observed_vintages(CIK, tmp_path)] == ["scored", "scored"]


def test_scored_content_is_scored_wherever_it_was_observed(tmp_path):
    s1, s2 = _scenario_b()
    capture(_Client(s1), "XYZ", now=D1, root=tmp_path)
    capture(_Client(s2), "XYZ", now=D2, root=tmp_path)
    # ... and on a later day: every observation of that content is scored.
    store_snapshot(CIK, s1, now=D3, root=tmp_path)
    assert [o.kind for o in observed_vintages(CIK, tmp_path)] == ["scored", "raw", "scored"]
    # Never downgraded: the sweep fetching scored content again leaves it scored.
    capture(_Client(s2), "XYZ", now=D4, root=tmp_path)
    store_snapshot(CIK, s2, now=D4, root=tmp_path)
    capture(_Client(s2), "XYZ", now=D4, root=tmp_path, force=True)
    assert [o.kind for o in observed_vintages(CIK, tmp_path)] == ["scored"] * 4


def test_a_raw_state_between_the_same_content_leaves_nothing_to_compare(tmp_path):
    s1, _s2 = _scenario_b()
    store_snapshot(CIK, s1, now=D1, root=tmp_path)
    capture(_Client(_bare()), "XYZ", now=D2, root=tmp_path)
    store_snapshot(CIK, s1, now=D3, root=tmp_path)
    rep = report_diff(CIK, as_of=REPORT_DAY, root=tmp_path)
    assert not rep.compared and rep.previous is None
    assert rep.status_line() == (
        "no earlier scored snapshot to diff the newest (2024-12-30) against; "
        "1 raw capture(s) not used as the baseline"
    )


# --- stores written before kinds existed --------------------------------------


def _drop_kinds(root: Path) -> None:
    path = v._manifest_path(CIK, root)
    man = json.loads(path.read_text())
    for o in man["observations"]:
        o.pop("kind")
    path.write_text(json.dumps(man))


def test_an_old_manifest_is_classified_by_whether_the_snapshot_maps(tmp_path):
    s1, s2 = _scenario_b()
    store_snapshot(CIK, s1, now=D1, root=tmp_path)
    store_snapshot(CIK, _partial(s1), now=D2, root=tmp_path)  # a legacy unmappable state
    store_snapshot(CIK, s2, now=D3, root=tmp_path)
    _drop_kinds(tmp_path)
    assert [o.kind for o in observed_vintages(CIK, tmp_path)] == [None, None, None]
    rep = report_diff(CIK, as_of=REPORT_DAY, root=tmp_path)
    assert rep.previous.captured == "2024-12-28"
    assert [(c.field_name, c.scope) for c in rep.changes_since_previous] == [
        ("total_assets", "scored")]
    assert "1 raw capture(s) since 2024-12-28 not used as the baseline" in rep.status_line()


def test_a_lost_manifest_falls_back_the_same_way(tmp_path):
    s1, s2 = _scenario_b()
    store_snapshot(CIK, s1, now=D1, root=tmp_path)
    capture(_Client(_bare()), "XYZ", now=D2, root=tmp_path)
    store_snapshot(CIK, s2, now=D3, root=tmp_path)
    v._manifest_path(CIK, tmp_path).unlink()
    rep = report_diff(CIK, as_of=REPORT_DAY, root=tmp_path)
    assert rep.previous.captured == "2024-12-28"
    assert [c.field_name for c in rep.changes_since_previous] == ["total_assets"]


def test_a_legacy_mappable_state_is_a_baseline(tmp_path):
    s1, s2 = _scenario_b()
    store_snapshot(CIK, s1, now=D1, root=tmp_path)
    store_snapshot(CIK, s2, now=D2, root=tmp_path)
    _drop_kinds(tmp_path)
    rep = report_diff(CIK, as_of=REPORT_DAY, root=tmp_path)
    assert (rep.previous.captured, rep.newest.captured) == ("2024-12-28", "2024-12-29")
    assert [c.field_name for c in rep.changes_since_previous] == ["total_assets"]
    assert "raw capture" not in rep.status_line()


# --- the pinned-thesis baseline ------------------------------------------------


def test_the_thesis_baseline_is_a_scored_state_too(tmp_path):
    s1, s2 = _scenario_b()
    s3 = copy.deepcopy(s2)
    _add(s3, "SomeUnrelatedConcept", [quarter(QUARTER_ENDS[-1], 1.0)])  # an addition only
    store_snapshot(CIK, s1, now=D1, root=tmp_path)
    capture(_Client(_bare()), "XYZ", now=D2, root=tmp_path)
    store_snapshot(CIK, s2, now=D3, root=tmp_path)
    store_snapshot(CIK, s3, now=D4, root=tmp_path)
    rep = report_diff(CIK, as_of=REPORT_DAY, baseline_day=date(2024, 12, 30), root=tmp_path)
    assert (rep.previous.captured, rep.newest.captured) == ("2024-12-30", "2024-12-31")
    assert rep.changes_since_previous == []
    assert rep.baseline.captured == "2024-12-28"  # not the bare capture on the 29th
    assert [c.field_name for c in rep.changes_since_baseline] == ["total_assets"]


def test_a_raw_state_before_the_thesis_day_is_not_the_lock_baseline(tmp_path):
    s1, s2 = _scenario_b()
    capture(_Client(s1), "XYZ", now=D1, root=tmp_path)
    store_snapshot(CIK, s1, now=D3, root=tmp_path)
    store_snapshot(CIK, s2, now=D4, root=tmp_path)
    rep = report_diff(CIK, as_of=REPORT_DAY, baseline_day=date(2024, 12, 30), root=tmp_path)
    # S1's content was scored on the 30th, so its capture on the 28th is
    # scored too: the lock baseline is that observation.
    assert rep.baseline.captured == "2024-12-28"
    raw_only = tmp_path / "raw_only"
    capture(_Client(_bare()), "XYZ", now=D1, root=raw_only)
    store_snapshot(CIK, s1, now=D3, root=raw_only)
    store_snapshot(CIK, s2, now=D4, root=raw_only)
    rep = report_diff(CIK, as_of=REPORT_DAY, baseline_day=date(2024, 12, 30), root=raw_only)
    assert rep.baseline is None
    assert rep.baseline_note == (
        "no scored snapshot before the pinned thesis day 2024-12-30; earliest is 2024-12-28")
    assert f"_{rep.baseline_note}._" in _silent_revisions_section(rep)


def test_a_legacy_observation_keeps_its_content_unknown_beside_a_raw_one(tmp_path):
    # A report may have written the legacy observation: a later raw fetch of
    # the same content must not make it raw.
    s1, s2 = _scenario_b()
    store_snapshot(CIK, s1, now=D1, root=tmp_path)
    store_snapshot(CIK, s2, now=D2, root=tmp_path)
    _drop_kinds(tmp_path)
    capture(_Client(s1), "XYZ", now=D3, root=tmp_path)
    assert [o.kind for o in observed_vintages(CIK, tmp_path)] == [None, None, None]
    rep = report_diff(CIK, as_of=REPORT_DAY, root=tmp_path)
    assert (rep.previous.captured, rep.newest.captured) == ("2024-12-29", "2024-12-30")


def test_raw_rows_are_counted_apart_even_when_a_filing_explains_them():
    key = FactKey("us-gaap", "Assets", "USD", None, QUARTER_ENDS[-1])
    amended = VintageChange("revised", "total_assets", key, 1.0, None, "a", "10-Q", 2.0,
                            None, "b", "10-Q/A", 1.0, original_retained=True)
    assert amended.explained_by_filing
    assert v._count([amended]) == "0 change(s) (+1 moved with a later filing, not silent)"
    assert v._count([replace(amended, scope="raw")]) == (
        "0 change(s) (+1 raw fact row(s), not scored changes)")
