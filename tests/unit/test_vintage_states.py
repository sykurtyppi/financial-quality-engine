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
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app.schemas.ledger import ValidationStatus
from app.services.ingestion import edgar_adapter
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
    # The window is named with its reason, never "compared X → Y: not
    # compared" (review of c131583, nit).
    assert rep.status_line() == (
        f"2024-12-29 → 2024-12-30 not compared as scored: the newer snapshot could not be "
        f"mapped (+{n} raw fact row(s), not scored changes)"
    )
    md = render_changes(rep.changes_since_previous, "2024-12-29", "2024-12-30")
    assert md.count(
        "(raw fact of a scored field's tag; not compared as the engine builds it)") == n
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
    # A bare capture does not map: the lock window is not compared as scored
    # and says so, not a note under a clean card (review of c131583, finding 1).
    assert rep.baseline is None and rep.baseline_note is None
    assert rep.baseline_unavailable == (
        "no snapshot before the pinned thesis day 2024-12-30 can be mapped (1 stored before "
        "it, none scored by a report)")
    assert rep.tier1_gap == ("since the pinned thesis: not compared as scored, no snapshot "
                             "before the thesis day could be mapped")
    assert (f"**Since the pinned thesis was locked:** Not compared as scored: "
            f"{rep.baseline_unavailable}.") in _silent_revisions_section(rep)
    # Nothing at all stored before the thesis day: a note, not a gap
    # (nothing can be back-filled).
    rep = report_diff(CIK, as_of=REPORT_DAY, baseline_day=date(2024, 12, 28), root=raw_only)
    assert rep.baseline_note == (
        "no snapshot before the pinned thesis day 2024-12-28; earliest is 2024-12-28")
    assert rep.baseline_unavailable is None and rep.tier1_gap is None
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


# --- the payload the report scored (review of 224b896, finding 1) --------------
#
# report_diff was never told WHICH payload the report scored. A live report
# whose own store_snapshot did not land (busy, failed, --no-vintage) after the
# sweep captured identical content compared an older pair and promoted a stale
# line; a replay scored the newest observation of any kind and then dropped
# that very payload as raw. The report's payload is now named by its digest:
# it is the newest state compared, or the section says it is not stored.


def _three_states() -> tuple[dict, dict, dict]:
    s1 = _every_field(composites=False)
    s2 = _bump(s1, "Assets", QUARTER_ENDS[-4], factor=1.5)
    return s1, s2, _bump(s2, "Assets", QUARTER_ENDS[-3], factor=1.2)


def test_the_reports_own_payload_captured_raw_is_the_newest_state(tmp_path):
    s1, s2, s3 = _three_states()
    store_snapshot(CIK, s1, now=D1, root=tmp_path)
    store_snapshot(CIK, s2, now=D2, root=tmp_path)
    capture(_Client(s3), "XYZ", now=D4, root=tmp_path)  # the sweep; the report's store did not land
    rep, tier1 = _tier1(tmp_path, s3)
    assert (rep.previous.captured, rep.newest.captured) == ("2024-12-29", "2024-12-31")
    assert [(c.field_name, c.key.end, c.scope) for c in rep.changes_since_previous] == [
        ("total_assets", QUARTER_ENDS[-3], "scored")]
    assert len(tier1) == 1 and "between snapshots 2024-12-29 and 2024-12-31" in tier1[0]
    assert rep.status_line() == "compared 2024-12-29 → 2024-12-31: 1 change(s)"


def test_a_payload_not_in_the_store_is_not_compared(tmp_path):
    s1, s2, s3 = _three_states()
    store_snapshot(CIK, s1, now=D1, root=tmp_path)
    store_snapshot(CIK, s2, now=D2, root=tmp_path)
    rep, tier1 = _tier1(tmp_path, s3)  # --no-vintage: S3 was never stored
    assert tier1 == [] and not rep.compared and rep.changes_since_previous == []
    assert rep.status_line() == (
        "the payload this report scored is not in the vintage store at or before "
        "2024-12-31 (see the Vintage snapshot line); not compared")
    assert rep.tier1_gap == "the report's payload is not in the vintage store"
    assert ledger._stream_state("vintage", True, {}, rep) == f"not compared: {rep.no_baseline_reason}"
    assert ledger._stream_state("offerings", True, {}, rep) == "checked"


def test_a_later_state_than_the_payload_is_not_compared(tmp_path):
    s1, s2, s3 = _three_states()
    store_snapshot(CIK, s1, now=D1, root=tmp_path)
    store_snapshot(CIK, s2, now=D2, root=tmp_path)
    store_snapshot(CIK, s3, now=D3, root=tmp_path)
    rep = report_diff(CIK, as_of=REPORT_DAY, root=tmp_path, scored_sha=v.digest_of(s2))
    assert (rep.previous.captured, rep.newest.captured) == ("2024-12-28", "2024-12-29")
    assert rep.status_line() == (
        "compared 2024-12-28 → 2024-12-29: 1 change(s); 1 later snapshot(s) after "
        "2024-12-29 not compared (not the payload this report scored)")


def test_the_replay_prefers_the_newest_scored_state_and_compares_it(tmp_path):
    s1, s2, s3 = _three_states()
    store_snapshot(CIK, s1, now=D1, root=tmp_path)
    store_snapshot(CIK, s2, now=D2, root=tmp_path)
    assert capture(_Client(s3), "XYZ", now=D3, root=tmp_path).wrote
    day = date(2024, 12, 30)
    snap, source = edgar_adapter.replay_snapshot(_Client(s3), "XYZ", day, root=tmp_path)
    assert snap.company_facts == s2
    assert source.startswith(
        f"the vintage snapshot captured 2024-12-29 (sha {v.digest_of(s2)[:12]}; scored by a "
        "report), cut to facts filed on or before 2024-12-30")
    assert source.endswith("; 1 newer stored snapshot(s) by then not used (1 raw capture(s))")
    _s, _e, tier1, errors, _t, _scan, rep = _collect_streams(
        _Client(snap.company_facts), "XYZ", day, company_facts=snap.company_facts,
        vintage_root=tmp_path)
    assert errors["vintage"] is None
    # The payload the replay scored is the newest state its section compares.
    assert (rep.previous.captured, rep.newest.captured) == ("2024-12-28", "2024-12-29")
    assert [c.field_name for c in rep.changes_since_previous] == ["total_assets"]


def test_the_replay_skips_a_capture_that_does_not_map(tmp_path):
    s1, _s2, _s3 = _three_states()
    store_snapshot(CIK, s1, now=D1, root=tmp_path)
    capture(_Client(_bare()), "XYZ", now=D2, root=tmp_path)
    snap, source = edgar_adapter.replay_snapshot(_Client(s1), "XYZ", D2.date(), root=tmp_path)
    assert snap.company_facts == s1 and "captured 2024-12-28" in source
    assert source.endswith("; 1 newer stored snapshot(s) by then not used (1 raw capture(s))")
    # A scored state the mapper cannot build (a legacy one, say) is skipped too.
    store_snapshot(CIK, _partial(s1), now=D3, root=tmp_path)
    snap, source = edgar_adapter.replay_snapshot(_Client(s1), "XYZ", D3.date(), root=tmp_path)
    assert snap.company_facts == s1 and source.endswith(
        "; 2 newer stored snapshot(s) by then not used (1 raw capture(s), 1 not mappable)")


def test_the_replay_falls_back_to_the_newest_mappable_capture(tmp_path):
    s1, s2, _s3 = _three_states()
    capture(_Client(s1), "XYZ", now=D1, root=tmp_path)
    capture(_Client(s2), "XYZ", now=D2, root=tmp_path)
    capture(_Client(_bare()), "XYZ", now=D3, root=tmp_path)
    snap, source = edgar_adapter.replay_snapshot(_Client(s1), "XYZ", D3.date(), root=tmp_path)
    assert snap.company_facts == s2
    assert "captured 2024-12-29" in source and (
        "a raw watch-sweep capture: no snapshot a report scored by then maps") in source
    assert source.endswith("; 1 newer stored snapshot(s) by then not used (1 not mappable)")


def test_a_replay_with_nothing_mappable_stored_cuts_todays_payload(tmp_path):
    s1, _s2, _s3 = _three_states()
    capture(_Client(_bare()), "XYZ", now=D1, root=tmp_path)
    snap, source = edgar_adapter.replay_snapshot(_Client(s1), "XYZ", D2.date(), root=tmp_path)
    assert snap.company_facts == s1
    assert source == (
        "today's companyfacts cut to facts filed on or before 2024-12-29 — no snapshot "
        "stored by then can be mapped (1 stored), so a value the filer revised in place "
        "since then shows as revised")
    _s, _e, _t1, errors, _t, _scan, rep = _collect_streams(
        _Client(s1), "XYZ", D2.date(), company_facts=s1, vintage_root=tmp_path)
    assert errors["vintage"] is None and not rep.compared
    assert rep.no_baseline_reason.startswith("the payload this report scored is not in the vintage store")


# --- an older state today's mapper cannot build (review of 224b896, finding 2) --


def _unmappable_older(s1: dict) -> dict:
    """A scored state the CURRENT mapper cannot build (the mapper changed
    since it was stored): one balance-sheet quarter, no revenue."""
    old = _strip(s1, REVENUE_TAGS)
    for rows in old["facts"]["us-gaap"]["Assets"]["units"].values():
        rows[:] = [r for r in rows if r["end"] == QUARTER_ENDS[-3].isoformat()]
    assert v._mapped(old) is None
    return old


def test_an_unmappable_older_side_is_raw_too():
    s1, s2 = _scenario_b()
    result = diff_scored(_unmappable_older(s1), s2)
    assert result.canonical_unavailable == (
        "scored values not compared as the engine builds them: the older snapshot could "
        "not be mapped (raw fact rows only, not scored changes)")
    assert [(c.field_name, c.scope) for c in result.changes] == [("total_assets", "raw")]


def test_an_unmappable_older_state_leaves_the_card_not_checked(tmp_path):
    from app.core.pipeline import analyze
    from app.services.reporting.report_builder import build_report
    from tests.fixtures.companies import stretch_dataset

    s1, s2 = _scenario_b()
    store_snapshot(CIK, _unmappable_older(s1), now=D2, root=tmp_path)
    store_snapshot(CIK, s2, now=D4, root=tmp_path)
    ds = stretch_dataset()
    report, _ = build_report(
        analyze(ds), ds, generated_on=REPORT_DAY.isoformat(), coverage=1.0, fetched_at="x",
        client=_Client(s2), ticker="XYZ", company_facts=s2, field_tags={}, vintage_root=tmp_path)
    # The card names the window not checked (review of c131583, nit).
    assert ("not checked this run: silent revisions (since the previous report: not compared "
            "as scored, a snapshot could not be mapped)") in report
    assert ("- Silent-revision check: 2024-12-29 → 2024-12-31 not compared as scored: the "
            "older snapshot could not be mapped (+1 raw fact row(s), not scored changes)\n"
            ) in report
    assert "compared 2024-12-29 → 2024-12-31:" not in report
    assert "0 change(s)" not in report
    assert ("1,211 (raw fact of a scored field's tag; not compared as the engine builds it)"
            in report)
    assert "Silent revision:" not in report


def test_a_raw_lock_window_does_not_suppress_a_promoted_line(tmp_path):
    s1, s2 = _scenario_b()
    store_snapshot(CIK, _unmappable_older(s1), now=datetime(2024, 12, 27, 12, tzinfo=UTC),
                   root=tmp_path)
    store_snapshot(CIK, s1, now=D2, root=tmp_path)
    store_snapshot(CIK, s2, now=D4, root=tmp_path)
    _s, _e, tier1, errors, _t, _scan, rep = _collect_streams(
        _Client(s2), "XYZ", REPORT_DAY, company_facts=s2, vintage_root=tmp_path,
        baseline_day=D1.date())
    assert errors["vintage"] is None
    assert {c.scope for c in rep.changes_since_baseline} == {"raw"}
    assert [line for line in tier1 if line.startswith("Silent revision")] == [
        f"Silent revision: total_assets for {QUARTER_ENDS[-3]} 1,009 → 1,211 (+20.0%) between "
        "snapshots 2024-12-29 and 2024-12-31 (detail in appendix; threshold hand-set, uncalibrated)"]
    assert rep.canonical_unavailable is None and rep.baseline_unavailable
    assert rep.status_line() == (
        "compared 2024-12-29 → 2024-12-31: 1 change(s); since pinned thesis 2024-12-27 not "
        "compared as scored: the older snapshot could not be mapped (+1 raw fact row(s), not "
        "scored changes)")
    assert rep.tier1_gap == (
        "since the pinned thesis: not compared as scored, a snapshot could not be mapped")
    # The ledger's stream state says the same, not a bare "checked".
    assert ledger._stream_state("vintage", True, {}, rep) == (
        "checked (incomplete: since the pinned thesis: not compared as scored, a snapshot "
        "could not be mapped)")
    clean = report_diff(CIK, as_of=REPORT_DAY, root=tmp_path, scored_sha=v.digest_of(s2))
    assert clean.tier1_gap is None and ledger._stream_state("vintage", True, {}, clean) == "checked"
    assert ledger._stream_state("events", True, {}, rep) == "checked"  # the vintage stream's alone


def test_a_lock_row_below_the_threshold_does_not_suppress_a_promoted_one(tmp_path):
    # Lock 1,009 -> previous 807 -> newest 1,049: +4% since the lock (listed,
    # not promoted), +30% since the previous report (promoted).
    s1 = _every_field(composites=False)
    end = QUARTER_ENDS[-3]
    store_snapshot(CIK, s1, now=D1, root=tmp_path)
    store_snapshot(CIK, _bump(s1, "Assets", end, factor=0.8), now=D3, root=tmp_path)
    s3 = _bump(s1, "Assets", end, factor=1.04)
    store_snapshot(CIK, s3, now=D4, root=tmp_path)
    _s, _e, tier1, errors, _t, _scan, rep = _collect_streams(
        _Client(s3), "XYZ", REPORT_DAY, company_facts=s3, vintage_root=tmp_path,
        baseline_day=D2.date())
    assert [(c.field_name, round(c.pct_change, 6)) for c in rep.changes_since_baseline] == [
        ("total_assets", 0.04)]
    lines = [line for line in tier1 if line.startswith("Silent revision")]
    assert len(lines) == 1 and "(+30.0%) between snapshots 2024-12-30 and 2024-12-31" in lines[0]


# --- the legacy walk (review of 224b896, finding 4) -----------------------------


def test_the_legacy_walk_passes_over_a_snapshot_it_cannot_read_or_map(tmp_path):
    s1, s2 = _scenario_b()
    odd = {"cik": CIK, "entityName": "x", "facts": {"us-gaap": []}}  # the mapper raises AttributeError
    store_snapshot(CIK, s1, now=datetime(2024, 12, 27, 12, tzinfo=UTC), root=tmp_path)
    store_snapshot(CIK, odd, now=D1, root=tmp_path)
    broken = store_snapshot(CIK, _bare(), now=D2, root=tmp_path)
    store_snapshot(CIK, s2, now=D3, root=tmp_path)
    _drop_kinds(tmp_path)
    broken.path.write_bytes(broken.path.read_bytes()[:-6])  # truncated gzip member
    _s, _e, tier1, errors, _t, _scan, rep = _collect_streams(
        _Client(s2), "XYZ", REPORT_DAY, company_facts=s2, vintage_root=tmp_path)
    assert errors["vintage"] is None
    assert (rep.previous.captured, rep.newest.captured) == ("2024-12-27", "2024-12-30")
    assert rep.status_line() == (
        "compared 2024-12-27 → 2024-12-30: 1 change(s); 2 raw capture(s) since 2024-12-27 "
        "not used as the baseline; 2 snapshot(s) passed over were unreadable or malformed")
    assert len(tier1) == 1


def test_the_legacy_walk_stops_at_the_first_mappable_state(tmp_path, monkeypatch):
    s1, s2 = _scenario_b()
    store_snapshot(CIK, s1, now=D1, root=tmp_path)
    store_snapshot(CIK, _bump(s1, "Assets", QUARTER_ENDS[-4], factor=1.5), now=D2, root=tmp_path)
    store_snapshot(CIK, s2, now=D3, root=tmp_path)
    _drop_kinds(tmp_path)
    loaded: list[str] = []
    real = v._load_for_diff
    monkeypatch.setattr(v, "_load_for_diff", lambda obs: loaded.append(obs.captured) or real(obs))
    rep = report_diff(CIK, as_of=REPORT_DAY, root=tmp_path)
    assert (rep.previous.captured, rep.newest.captured) == ("2024-12-29", "2024-12-30")
    assert "2024-12-28" not in loaded  # never mapped: the walk stopped at the 29th


def test_a_reverted_payload_is_compared_at_its_newest_observation(tmp_path):
    s1, s2 = _scenario_b()
    store_snapshot(CIK, s1, now=D1, root=tmp_path)
    store_snapshot(CIK, s2, now=D2, root=tmp_path)
    store_snapshot(CIK, s1, now=D3, root=tmp_path)  # reverted to S1's bytes
    rep = report_diff(CIK, as_of=REPORT_DAY, root=tmp_path, scored_sha=v.digest_of(s1))
    assert (rep.previous.captured, rep.newest.captured) == ("2024-12-29", "2024-12-30")
    assert [(c.field_name, c.scope) for c in rep.changes_since_previous] == [
        ("total_assets", "scored")]


def test_the_payload_captured_raw_before_the_lock_is_the_lock_baseline(tmp_path):
    # The sweep captured S3 before the thesis day; the report scoring S3 now
    # could not store it. S3 is scored for this comparison wherever it was
    # observed, so nothing moved since the lock — S2 -> S3 happened before it.
    _s1, s2, s3 = _three_states()
    store_snapshot(CIK, s2, now=datetime(2024, 12, 27, 12, tzinfo=UTC), root=tmp_path)
    capture(_Client(s3), "XYZ", now=D1, root=tmp_path)
    rep = report_diff(CIK, as_of=REPORT_DAY, baseline_day=D3.date(), root=tmp_path,
                      scored_sha=v.digest_of(s3))
    assert (rep.previous.captured, rep.newest.captured) == ("2024-12-27", "2024-12-28")
    assert rep.baseline.captured == "2024-12-28" and rep.changes_since_baseline is None
    assert rep.baseline_note == (
        "the pinned thesis snapshot (2024-12-28) is the newest snapshot; nothing to "
        "compare since the lock")


def test_the_replay_passes_over_odd_and_unreadable_legacy_snapshots(tmp_path):
    s1, _s2, _s3 = _three_states()
    store_snapshot(CIK, s1, now=D1, root=tmp_path)
    odd = {"cik": CIK, "entityName": "x", "facts": {"us-gaap": []}}  # AttributeError in the mapper
    store_snapshot(CIK, odd, now=D2, root=tmp_path)
    broken = store_snapshot(CIK, _bare(), now=D3, root=tmp_path)
    broken.path.write_bytes(broken.path.read_bytes()[:-6])  # EOFError on read
    _drop_kinds(tmp_path)
    snap, source = edgar_adapter.replay_snapshot(_Client(s1), "XYZ", D3.date(), root=tmp_path)
    assert snap.company_facts == s1
    assert source == (
        f"the vintage snapshot captured 2024-12-28 (sha {v.digest_of(s1)[:12]}; stored before "
        "kinds were recorded), cut to facts filed on or before 2024-12-30; 2 newer stored "
        "snapshot(s) by then not used (2 not mappable)")


def test_the_replay_tries_an_unmappable_content_once(tmp_path, monkeypatch):
    s1, _s2, _s3 = _three_states()
    capture(_Client(s1), "XYZ", now=D1, root=tmp_path)
    store_snapshot(CIK, _partial(s1), now=D2, root=tmp_path)
    capture(_Client(_bare()), "XYZ", now=D3, root=tmp_path)
    store_snapshot(CIK, _partial(s1), now=D4, root=tmp_path)  # the same bytes observed again
    built: list[int] = []
    real = edgar_adapter.build_dataset
    monkeypatch.setattr(edgar_adapter, "build_dataset",
                        lambda facts, **kw: built.append(1) or real(facts, **kw))
    snap, source = edgar_adapter.replay_snapshot(_Client(s1), "XYZ", D4.date(), root=tmp_path)
    assert snap.company_facts == s1 and "captured 2024-12-28" in source
    assert source.endswith("; 3 newer stored snapshot(s) by then not used (3 not mappable)")
    assert len(built) == 3  # the partial once, the bare capture, S1 — not the partial twice


# --- review of c131583 ----------------------------------------------------------
#
# 1. The thesis-lock window was silently dropped when only watch-sweep captures
#    predate the thesis day — the normal journal track, where the first scored
#    report is made ON that day: the card read clean and the ledger "checked".
# 2. The section printed the empty-diff sentence for a window not compared as
#    scored.
# 3. A mapper defect on a SCORED snapshot was passed over in silence.
# 4. A fact both windows promote was one card line but two VALIDATED items.

import logging  # noqa: E402

import pytest  # noqa: E402

D24, D25, D26, D27 = (datetime(2024, 12, d, 12, tzinfo=UTC) for d in (24, 25, 26, 27))
ODD = {"cik": CIK, "entityName": "x", "facts": {"us-gaap": []}}  # the mapper raises AttributeError
PERIOD = QUARTER_ENDS[-3]


def _report(tmp_path: Path, facts: dict, baseline_day: date | None = None) -> tuple[str, dict]:
    """The full report and its ledger document, over the store at tmp_path/v."""
    from app.core.pipeline import analyze
    from app.services.reporting.report_builder import build_report
    from tests.fixtures.companies import stretch_dataset

    ds = stretch_dataset()
    out = tmp_path / "ledger.json"
    report, _ = build_report(
        analyze(ds), ds, generated_on=REPORT_DAY.isoformat(), coverage=1.0, fetched_at="x",
        client=_Client(facts), ticker="XYZ", company_facts=facts, field_tags={},
        vintage_root=tmp_path / "v", baseline_day=baseline_day, ledger_out=out)
    return report, json.loads(out.read_text())


def _silent_items(doc: dict) -> list[tuple[str, str, str | None]]:
    return [(i["claim"], i["validation_status"], i.get("note"))
            for i in doc["items"] if i["kind"] == "silent_revision"]


def _lock_states() -> tuple[dict, dict, dict]:
    """S0 (the state the thesis was locked on) -> S1 (+20% total_assets, before
    the previous report) -> S2 (an unscored addition only)."""
    s0 = _every_field(composites=False)
    s1 = _bump(s0, "Assets", PERIOD, factor=1.2)
    s2 = copy.deepcopy(s1)
    _add(s2, "SomeUnrelatedConcept", [quarter(QUARTER_ENDS[-1], 1.0)])
    return s0, s1, s2


def test_a_mapped_sweep_capture_is_the_lock_baseline_when_no_report_predates_it(tmp_path):
    s0, s1, s2 = _lock_states()
    root = tmp_path / "v"
    assert capture(_Client(s0), "XYZ", now=D27, root=root).wrote
    store_snapshot(CIK, s1, now=D2, root=root)
    store_snapshot(CIK, s2, now=D4, root=root)
    report, doc = _report(tmp_path, s2, baseline_day=D1.date())
    # The card: the revision since the lock, as 424b0b4 showed it.
    assert (f"Silent revision: total_assets for {PERIOD} 1,009 → 1,211 (+20.0%) between "
            "snapshots 2024-12-27 and 2024-12-31 (detail in appendix; threshold hand-set, "
            "uncalibrated)") in report
    assert "not checked this run: silent revisions" not in report
    assert ("- Silent-revision check: compared 2024-12-29 → 2024-12-31: 0 change(s); since "
            "pinned thesis 2024-12-27, a sweep capture (mapped, complete): 1 change(s)\n") in report
    assert ("**Since the pinned thesis was locked** (2024-12-27, a sweep capture (mapped, "
            "complete)):" in report)
    # The ledger: the vintage stream checked, the one promoted fact VALIDATED.
    assert doc["streams"]["vintage"] == "checked"
    assert _silent_items(doc) == [
        (f"total_assets for {PERIOD}: 1,009 → 1,211 between snapshots 2024-12-27 and "
         "2024-12-31", "validated", None)]


def test_no_mappable_capture_before_the_thesis_day_leaves_the_lock_window_not_checked(tmp_path):
    _s0, s1, s2 = _lock_states()
    root = tmp_path / "v"
    assert capture(_Client(_bare()), "XYZ", now=D27, root=root).wrote
    store_snapshot(CIK, s1, now=D2, root=root)
    store_snapshot(CIK, s2, now=D4, root=root)
    report, doc = _report(tmp_path, s2, baseline_day=D1.date())
    gap = ("since the pinned thesis: not compared as scored, no snapshot before the thesis "
           "day could be mapped")
    why = ("no snapshot before the pinned thesis day 2024-12-28 can be mapped (1 stored "
           "before it, none scored by a report)")
    assert f"⚠ not checked this run: silent revisions ({gap})" in report
    assert doc["streams"]["vintage"] == f"checked (incomplete: {gap})"
    assert (f"- Silent-revision check: compared 2024-12-29 → 2024-12-31: 0 change(s); since the "
            f"pinned thesis not compared as scored: {why}; 1 capture(s) before the thesis day "
            "not usable as the lock baseline (1 unmappable)\n") in report
    assert f"**Since the pinned thesis was locked:** Not compared as scored: {why}." in report
    assert "Silent revision:" not in report and _silent_items(doc) == []


def test_the_lock_baseline_capture_is_the_newest_that_maps(tmp_path, caplog):
    s0, s1, s2 = _lock_states()
    root = tmp_path / "v"
    capture(_Client(_bump(s0, "Assets", PERIOD, factor=0.5)), "XYZ", now=D24, root=root)
    capture(_Client(s0), "XYZ", now=D25, root=root)
    odd = capture(_Client(ODD), "XYZ", now=D26, root=root)
    capture(_Client(_bare()), "XYZ", now=D27, root=root)
    store_snapshot(CIK, s1, now=D2, root=root)
    store_snapshot(CIK, s2, now=D4, root=root)
    with caplog.at_level(logging.WARNING, logger=v.__name__):
        rep = report_diff(CIK, as_of=REPORT_DAY, baseline_day=D1.date(), root=root,
                          scored_sha=v.digest_of(s2))
    assert rep.baseline.captured == "2024-12-25"
    assert rep.baseline_source == "a sweep capture (mapped, complete)"
    assert [(c.field_name, c.scope, round(c.pct_change, 6))
            for c in rep.changes_since_baseline] == [("total_assets", "scored", 0.2)]
    assert rep.tier1_gap is None and rep.baseline_unavailable is None
    # The odd-shaped capture was passed over, and said so.
    assert odd.path is not None
    assert any("AttributeError" in r.getMessage() and odd.path.name in r.getMessage()
               for r in caplog.records)


def test_a_legacy_state_the_walk_rejected_is_not_mapped_again_for_the_lock(tmp_path, caplog):
    # The lock fallback reads raw captures only: a legacy state before the
    # thesis day was already mapped (and rejected) by the walk, and is
    # passed over once — one load, one warning.
    _s0, s1, s2 = _lock_states()
    root = tmp_path / "v"
    odd = store_snapshot(CIK, ODD, now=D26, root=root)
    _drop_kinds(root)
    capture(_Client(_bare()), "XYZ", now=D27, root=root)
    store_snapshot(CIK, s1, now=D2, root=root)
    store_snapshot(CIK, s2, now=D4, root=root)
    with caplog.at_level(logging.WARNING, logger=v.__name__):
        rep = report_diff(CIK, as_of=REPORT_DAY, baseline_day=D1.date(), root=root,
                          scored_sha=v.digest_of(s2))
    assert rep.baseline is None and rep.baseline_unavailable == (
        "no snapshot before the pinned thesis day 2024-12-28 can be mapped (2 stored before "
        "it, none scored by a report)")
    assert odd.path is not None
    assert sum(odd.path.name in r.getMessage() for r in caplog.records) == 1


def test_the_section_says_the_previous_window_was_not_compared_as_scored(tmp_path):
    s1, _s2 = _scenario_b()
    new = copy.deepcopy(s1)
    _add(new, "SomeUnrelatedConcept", [quarter(QUARTER_ENDS[-1], 1.0)])
    store_snapshot(CIK, _unmappable_older(s1), now=D2, root=tmp_path)
    store_snapshot(CIK, new, now=D4, root=tmp_path)
    rep, _tier = _tier1(tmp_path, new)
    assert rep.changes_since_previous == [] and rep.canonical_unavailable
    section = _silent_revisions_section(rep)
    assert ("### Vintage diff — 2024-12-29 → 2024-12-31\n\nNot compared as scored: the older "
            "snapshot could not be mapped.\n") in section
    assert "No prior-period figure changed" not in section


def test_the_section_says_the_lock_window_was_not_compared_as_scored(tmp_path):
    s1, _s2 = _scenario_b()
    new = copy.deepcopy(s1)
    _add(new, "SomeUnrelatedConcept", [quarter(QUARTER_ENDS[-1], 1.0)])
    store_snapshot(CIK, _unmappable_older(s1), now=D27, root=tmp_path)
    store_snapshot(CIK, s1, now=D2, root=tmp_path)
    store_snapshot(CIK, new, now=D4, root=tmp_path)
    rep = report_diff(CIK, as_of=REPORT_DAY, baseline_day=D1.date(), root=tmp_path,
                      scored_sha=v.digest_of(new))
    assert rep.changes_since_baseline == [] and rep.baseline_unavailable
    section = _silent_revisions_section(rep)
    assert ("**Since the pinned thesis was locked** (2024-12-27):\n\n### Vintage diff — "
            "2024-12-27 → 2024-12-31\n\nNot compared as scored: the older snapshot could not "
            "be mapped.\n") in section
    # The previous -> newest window WAS compared as scored, and found nothing.
    assert section.count("No prior-period figure changed or disappeared") == 1


def test_an_unavailable_window_lists_its_raw_rows_after_saying_so():
    key = FactKey("us-gaap", "Assets", "USD", None, QUARTER_ENDS[-1])
    amended = VintageChange("revised", "total_assets", key, 1.0, None, "a", "10-Q", 2.0,
                            None, "b", "10-Q/A", 1.0, original_retained=True, scope="raw")
    reason = ("scored values not compared as the engine builds them: the newer snapshot could "
              "not be mapped (raw fact rows only, not scored changes)")
    md = render_changes([amended], "a", "b", unavailable=reason)
    assert md.startswith("### Vintage diff — a → b\n\nNot compared as scored: the newer snapshot "
                         "could not be mapped.\n")
    assert "**Moved with a later filing (not silent).**" in md
    assert "changed silently" not in md  # no "nothing silent" claim over raw rows
    assert render_changes([], "a", "b") == (
        "### Vintage diff — a → b\n\nNo prior-period figure changed or disappeared between "
        "these snapshots.\n")


def test_a_mapper_defect_on_a_scored_snapshot_surfaces_in_the_replay(tmp_path, monkeypatch):
    s1, s2, _s3 = _three_states()
    store_snapshot(CIK, s1, now=D1, root=tmp_path)
    store_snapshot(CIK, s2, now=D2, root=tmp_path)
    real = edgar_adapter.build_dataset

    def defect(facts, **kw):
        if v.digest_of(facts) == v.digest_of(s2):
            raise TypeError("unsupported operand")
        return real(facts, **kw)

    monkeypatch.setattr(edgar_adapter, "build_dataset", defect)
    with pytest.raises(edgar_adapter.ScoredSnapshotUnmappable, match="unsupported operand"):
        edgar_adapter.replay_snapshot(_Client(s2), "XYZ", D3.date(), root=tmp_path)
    # A legacy state (nothing says a report scored it) is still passed over — logged.
    _drop_kinds(tmp_path)
    snap, source = edgar_adapter.replay_snapshot(_Client(s2), "XYZ", D3.date(), root=tmp_path)
    assert snap.company_facts == s1 and source.endswith("(1 not mappable)")


def test_every_snapshot_passed_over_is_logged(tmp_path, caplog):
    s1, _s2, _s3 = _three_states()
    store_snapshot(CIK, s1, now=D1, root=tmp_path)
    partial = store_snapshot(CIK, _partial(s1), now=D2, root=tmp_path)  # scored, unmappable
    bare = capture(_Client(_bare()), "XYZ", now=D3, root=tmp_path)
    assert partial.path is not None and bare.path is not None
    with caplog.at_level(logging.WARNING):
        snap, _source = edgar_adapter.replay_snapshot(_Client(s1), "XYZ", D3.date(), root=tmp_path)
    assert snap.company_facts == s1
    [record] = [r for r in caplog.records if partial.path.name in r.getMessage()]
    assert record.name == edgar_adapter.__name__ and "ValueError" in record.getMessage()
    assert "kind scored" in record.getMessage()
    # Tried last (scored states first) and never reached: S1 mapped.
    assert not any(bare.path.name in r.getMessage() for r in caplog.records)
    # The report's legacy walk too.
    caplog.clear()
    legacy = tmp_path / "legacy"
    sa, sb = _scenario_b()
    store_snapshot(CIK, sa, now=D1, root=legacy)
    odd = store_snapshot(CIK, ODD, now=D2, root=legacy)
    store_snapshot(CIK, sb, now=D3, root=legacy)
    _drop_kinds(legacy)
    with caplog.at_level(logging.WARNING, logger=v.__name__):
        rep = report_diff(CIK, as_of=REPORT_DAY, root=legacy)
    assert rep.previous.captured == "2024-12-28"
    assert odd.path is not None
    assert any("AttributeError" in r.getMessage() and odd.path.name in r.getMessage()
               for r in caplog.records)


def test_card_and_ledger_agree_on_a_fact_both_windows_promote(tmp_path):
    s0 = _every_field(composites=False)
    s1 = _bump(s0, "Assets", PERIOD, factor=1.2)
    s2 = _bump(s1, "Assets", PERIOD, factor=1.2)
    root = tmp_path / "v"
    for s, d in ((s0, D27), (s1, D2), (s2, D4)):
        store_snapshot(CIK, s, now=d, root=root)
    report, doc = _report(tmp_path, s2, baseline_day=D1.date())
    assert report.count(f"Silent revision: total_assets for {PERIOD}") == 1
    assert "1,009 → 1,453 (+44.0%) between snapshots 2024-12-27 and 2024-12-31" in report
    items = _silent_items(doc)
    assert [i for i in items if i[1] == "validated"] == [
        (f"total_assets for {PERIOD}: 1,009 → 1,453 between snapshots 2024-12-27 and "
         "2024-12-31", "validated", None)]
    assert (f"total_assets for {PERIOD}: 1,211 → 1,453 between snapshots 2024-12-29 and "
            "2024-12-31", "directional",
            "also promoted via snapshots 2024-12-27 → 2024-12-31 (the card lists each fact "
            "once)") in items
    assert len(items) == 2


def test_the_card_names_the_window_not_checked():
    from app.services.ingestion.vintages import VintageDiffReport, VintageObservation

    def obs(day: str, sha: str) -> VintageObservation:
        return VintageObservation(day, sha, Path(f"{day}.json.gz"), "scored")

    why = ("scored values not compared as the engine builds them: the older snapshot could "
           "not be mapped (raw fact rows only, not scored changes)")
    base = VintageDiffReport(REPORT_DAY, obs("2024-12-31", "c"), obs("2024-12-29", "b"), [],
                             obs("2024-12-27", "a"), [])
    previous_only = replace(base, canonical_unavailable=why)
    assert previous_only.tier1_gap == (
        "since the previous report: not compared as scored, a snapshot could not be mapped")
    assert previous_only.status_line() == (
        "2024-12-29 → 2024-12-31 not compared as scored: the older snapshot could not be "
        "mapped; since pinned thesis 2024-12-27: 0 change(s)")
    both = replace(previous_only, baseline_unavailable=why)
    assert both.tier1_gap == "not compared as scored in either window: a snapshot could not be mapped"
    assert both.status_line() == (
        "2024-12-29 → 2024-12-31 not compared as scored: the older snapshot could not be "
        "mapped; since pinned thesis 2024-12-27 not compared as scored: the older snapshot "
        "could not be mapped")
    assert replace(base, baseline_unavailable=why).tier1_gap == (
        "since the pinned thesis: not compared as scored, a snapshot could not be mapped")
    assert base.tier1_gap is None


def test_no_vintage_help_says_what_the_check_then_shows(monkeypatch, capsys):
    from scripts import generate_report

    monkeypatch.setattr(generate_report.sys, "argv", ["generate_report.py", "--help"])
    with pytest.raises(SystemExit):
        generate_report.main()
    text = " ".join(capsys.readouterr().out.split())
    assert ("--no-vintage do not archive the scored companyfacts payload to data/vintages/ "
            "(the silent-revision check then reads 'not compared' unless the store already "
            "holds identical content)") in text


# --- review of 626ca1b ------------------------------------------------------------
#
# 1. A capture that MAPS but is incomplete was accepted as the lock baseline: one
#    missing Assets, or the latest quarters, hid a real +20% revision and read
#    clean; a newer partial capture beat an older complete one; one missing a
#    D&A component made 8 recomposed "changes" and false "reads a revised
#    figure" card notes. A capture is now the lock baseline only if it covers
#    the scored comparison.
# 2. A mapper defect on a scored snapshot ended the replay in a traceback that
#    did not name the snapshot.
# 3. An old scored state beat a covering capture the day before the lock, so a
#    pre-lock revision read as one since the lock.
# 4. The lock walk mapped every capture before the day, uncached.
# 5. Captures the walk skipped because they do not map were neither logged
#    nor counted.

D20, D21, D22 = (datetime(2024, 12, d, 12, tzinfo=UTC) for d in (20, 21, 22))
LOCK = date(2024, 12, 28)


def _with(facts: dict, concept: str) -> dict:
    """The same payload plus an unscored concept: new content, no scored change."""
    out = copy.deepcopy(facts)
    out["facts"]["us-gaap"][concept] = {"units": {"USD": [
        {"end": "2024-06-30", "val": 1, "fy": 2024, "fp": "Q2", "form": "10-Q",
         "filed": "2024-08-09", "accn": "x"}]}}
    return out


def _drop_quarters(facts: dict, ends) -> dict:
    """A fetch cut short: every concept's rows for `ends` missing."""
    out = copy.deepcopy(facts)
    iso = {e.isoformat() for e in ends}
    for tags in out["facts"].values():
        for concept in tags.values():
            for rows in concept["units"].values():
                rows[:] = [r for r in rows if r["end"] not in iso]
    return out


def _lock_run(root: Path, pre: list[tuple[datetime, dict]], s1: dict, s2: dict):
    """Captures `pre` before the thesis day, S1 scored on the 29th, S2 on the
    31st; the report streams for S2 with the thesis locked on LOCK."""
    for at, facts in pre:
        capture(_Client(facts), "XYZ", now=at, root=root, force=True)
    store_snapshot(CIK, s1, now=D2, root=root)
    store_snapshot(CIK, s2, now=D4, root=root)
    _s, _e, tier1, errors, _t, _scan, rep = _collect_streams(
        _Client(s2), "XYZ", REPORT_DAY, company_facts=s2, vintage_root=root, baseline_day=LOCK)
    assert errors["vintage"] is None, errors
    return rep, [line for line in tier1 if line.startswith("Silent revision")]


INCOMPLETE_GAP = "since the pinned thesis: not compared as scored, incomplete sweep capture(s) only"


def _assert_not_checked(rep, stored: int = 1) -> None:
    assert rep.baseline is None and rep.changes_since_baseline is None
    assert rep.baseline_unavailable == (
        "no snapshot before the pinned thesis day 2024-12-28 covers the scored comparison: "
        f"incomplete sweep capture(s) only ({stored} stored before it, none scored by a report)")
    assert rep.tier1_gap == INCOMPLETE_GAP
    assert ledger._stream_state("vintage", True, {}, rep) == f"checked (incomplete: {INCOMPLETE_GAP})"


@pytest.mark.parametrize("partial", [
    pytest.param(lambda s: _strip(s, ["Assets"]), id="missing-assets"),
    pytest.param(lambda s: _drop_quarters(s, QUARTER_ENDS[-3:]), id="missing-recent-quarters"),
])
def test_an_incomplete_capture_is_not_the_lock_baseline(tmp_path, partial, caplog):
    s0 = _every_field(composites=True)
    bumped = _bump(s0, "Assets", PERIOD, factor=1.2)  # the truth: +20% since the lock
    cut = partial(s0)
    assert v._mapped(cut) is not None  # it maps: 626ca1b took it as the baseline
    with caplog.at_level(logging.WARNING, logger=v.__name__):
        rep, tier1 = _lock_run(tmp_path, [(D20, cut)], bumped, _with(bumped, "U"))
    _assert_not_checked(rep)
    assert tier1 == []  # nothing to promote: the lock window was not compared
    assert rep.raw_note == (
        "1 capture(s) before the thesis day not usable as the lock baseline (1 incomplete)")
    assert "0 change(s); since the pinned thesis not compared as scored" in rep.status_line()
    assert any("passed over for the thesis-lock baseline: incomplete" in r.getMessage()
               for r in caplog.records)


def test_an_older_complete_capture_beats_a_newer_partial_one(tmp_path):
    s0 = _every_field(composites=True)
    bumped = _bump(s0, "Assets", PERIOD, factor=1.2)
    rep, tier1 = _lock_run(tmp_path, [(D20, s0), (D21, _strip(s0, ["Assets"]))],
                           bumped, _with(bumped, "U"))
    assert rep.baseline.captured == "2024-12-20"
    assert rep.baseline_source == "a sweep capture (mapped, complete)"
    assert [(c.field_name, c.key.end, round(c.pct_change, 6))
            for c in rep.changes_since_baseline] == [("total_assets", PERIOD, 0.2)]
    assert len(tier1) == 1 and "between snapshots 2024-12-20 and 2024-12-31" in tier1[0]
    assert rep.tier1_gap is None
    assert rep.raw_note == (
        "1 capture(s) before the thesis day not usable as the lock baseline (1 incomplete)")


def test_a_capture_missing_a_component_is_not_the_lock_baseline(tmp_path):
    # No figure moved: the capture lacks one D&A component, so the mapper
    # built D&A from depreciation alone there. 626ca1b compared it and read
    # eight "changes" and "reads a revised figure" card notes.
    from app.services.reporting.revised_inputs import revision_index

    s0 = _every_field(composites=True)
    cut = _strip(s0, ["AmortizationOfIntangibleAssets"])
    assert v._mapped(cut) is not None
    rep, tier1 = _lock_run(tmp_path, [(D20, cut)], s0, _with(s0, "U"))
    _assert_not_checked(rep)
    assert tier1 == [] and not revision_index(None, rep)


def test_a_capture_taken_before_a_quarter_was_filed_covers_without_it(tmp_path):
    # Coverage is judged against what the scored state held the day before
    # the capture (`build_dataset(as_of=)`): a quarter filed later is not a
    # gap. The 2024-09-30 quarter is filed 2024-11-09.
    s0 = _every_field(composites=True)
    bumped = _bump(s0, "Assets", PERIOD, factor=1.2)
    early = datetime(2024, 10, 1, 12, tzinfo=UTC)
    rep, tier1 = _lock_run(tmp_path, [(early, _drop_quarters(s0, QUARTER_ENDS[-2:]))],
                           bumped, _with(bumped, "U"))
    assert rep.baseline.captured == "2024-10-01" and rep.raw_note is None
    assert [(c.field_name, c.key.end) for c in rep.changes_since_baseline] == [
        ("total_assets", PERIOD)]
    assert len(tier1) == 1


def test_the_nearest_state_before_the_lock_is_the_baseline(tmp_path):
    # The +20% was public before the lock: the sweep captured it on the 27th.
    # The scored state from June is older; the capture is nearer and covers
    # the comparison, so nothing moved since the lock.
    s0 = _every_field(composites=False)
    pre = _bump(s0, "Assets", PERIOD, factor=1.2)
    root = tmp_path
    store_snapshot(CIK, s0, now=datetime(2024, 6, 1, 12, tzinfo=UTC), root=root)
    capture(_Client(pre), "XYZ", now=D27, root=root)
    s1, s2 = _with(pre, "V"), _with(pre, "U")
    store_snapshot(CIK, s1, now=D2, root=root)
    store_snapshot(CIK, s2, now=D4, root=root)
    _s, _e, tier1, errors, _t, _scan, rep = _collect_streams(
        _Client(s2), "XYZ", REPORT_DAY, company_facts=s2, vintage_root=root, baseline_day=LOCK)
    assert rep.baseline.captured == "2024-12-27"
    assert rep.baseline_source == "a sweep capture (mapped, complete)"
    assert rep.changes_since_baseline == []
    assert [line for line in tier1 if line.startswith("Silent revision")] == []
    assert rep.status_line() == (
        "compared 2024-12-29 → 2024-12-31: 0 change(s); since pinned thesis 2024-12-27, a "
        "sweep capture (mapped, complete): 0 change(s)")
    # A scored state nearer than any covering capture is the baseline, unlabelled.
    root2 = tmp_path / "scored_nearer"
    capture(_Client(pre), "XYZ", now=D20, root=root2)
    store_snapshot(CIK, s0, now=D27, root=root2)
    store_snapshot(CIK, s1, now=D2, root=root2)
    store_snapshot(CIK, s2, now=D4, root=root2)
    rep = report_diff(CIK, as_of=REPORT_DAY, baseline_day=LOCK, root=root2,
                      scored_sha=v.digest_of(s2))
    assert rep.baseline.captured == "2024-12-27" and rep.baseline_source is None
    assert [c.field_name for c in rep.changes_since_baseline] == ["total_assets"]


def _counting(monkeypatch) -> list[int]:
    built: list[int] = []
    real = v.build_dataset
    monkeypatch.setattr(v, "build_dataset", lambda *a, **k: built.append(1) or real(*a, **k))
    return built


def test_the_lock_walk_maps_each_content_once(tmp_path, monkeypatch, caplog):
    # 40 captures alternating between two unmappable contents, then the one
    # complete capture: each content is mapped once, not once per capture.
    s0 = _every_field(composites=True)
    t0 = datetime(2024, 10, 1, 12, tzinfo=UTC)
    capture(_Client(s0), "XYZ", now=t0, root=tmp_path)
    for i in range(1, 41):
        facts = _partial(s0) if i % 2 else dict(_partial(s0), entityName="y")
        capture(_Client(facts), "XYZ", now=t0 + timedelta(days=i), root=tmp_path, force=True)
    s1, s2 = _with(s0, "V"), _with(s0, "U")
    store_snapshot(CIK, s1, now=D2, root=tmp_path)
    store_snapshot(CIK, s2, now=D4, root=tmp_path)
    built = _counting(monkeypatch)
    with caplog.at_level(logging.WARNING, logger=v.__name__):
        rep = report_diff(CIK, as_of=REPORT_DAY, baseline_day=LOCK, root=tmp_path,
                          scored_sha=v.digest_of(s2))
    assert rep.baseline.captured == "2024-10-01"
    assert rep.raw_note == (
        "40 capture(s) before the thesis day not usable as the lock baseline (40 unmappable)")
    # Two unmappable contents, the complete capture, the scored state as of
    # its day, and the two diffs' four builds.
    assert len(built) == 8
    # Every capture passed over is logged, a memo hit too.
    assert sum("passed over for the thesis-lock baseline" in r.getMessage()
               for r in caplog.records) == 40


def test_the_lock_walk_is_bounded(tmp_path, monkeypatch):
    s0 = _every_field(composites=True)
    t0 = datetime(2024, 10, 1, 12, tzinfo=UTC)
    capture(_Client(s0), "XYZ", now=t0, root=tmp_path)
    for i in range(1, 21):  # twenty distinct contents, none of which maps
        capture(_Client(dict(_partial(s0), entityName=f"x{i}")), "XYZ",
                now=t0 + timedelta(days=i), root=tmp_path)
    s1, s2 = _with(s0, "V"), _with(s0, "U")
    store_snapshot(CIK, s1, now=D2, root=tmp_path)
    store_snapshot(CIK, s2, now=D4, root=tmp_path)
    built = _counting(monkeypatch)
    rep = report_diff(CIK, as_of=REPORT_DAY, baseline_day=LOCK, root=tmp_path,
                      scored_sha=v.digest_of(s2))
    tries = v.LOCK_CAPTURES_EXAMINED
    assert len(built) == tries + 2  # the previous -> newest diff's two builds
    assert rep.baseline is None and rep.baseline_unavailable == (
        f"no snapshot before the pinned thesis day 2024-12-28 was usable among the {tries} "
        "examined (21 stored before it, none scored by a report)")
    assert rep.raw_note == (
        f"{tries} capture(s) before the thesis day not usable as the lock baseline ({tries} "
        f"unmappable); {21 - tries} older capture(s) before the thesis day not examined (at "
        f"most {tries} per report)")
    assert rep.tier1_gap == (
        "since the pinned thesis: not compared as scored, no snapshot examined before the "
        "thesis day could be used")


def test_an_unmappable_capture_before_the_lock_is_logged_and_counted(tmp_path, caplog):
    s0 = _every_field(composites=False)
    s1 = _with(s0, "V")
    older = _bump(s0, "Assets", PERIOD, factor=0.8)
    root = tmp_path
    capture(_Client(older), "XYZ", now=D20, root=root)
    partial = capture(_Client(_partial(s0)), "XYZ", now=D22, root=root)  # ValueError: None
    bare = capture(_Client(_bare()), "XYZ", now=D24, root=root)
    store_snapshot(CIK, s0, now=D2, root=root)
    store_snapshot(CIK, s1, now=D4, root=root)
    with caplog.at_level(logging.WARNING, logger=v.__name__):
        rep = report_diff(CIK, as_of=REPORT_DAY, baseline_day=LOCK, root=root,
                          scored_sha=v.digest_of(s1))
    assert rep.baseline.captured == "2024-12-20"
    assert rep.raw_note == (
        "2 capture(s) before the thesis day not usable as the lock baseline (2 unmappable)")
    assert rep.status_line().endswith(rep.raw_note)
    for c in (partial, bare):
        assert c.path is not None
        assert any(c.path.name in r.getMessage() and "does not map" in r.getMessage()
                   for r in caplog.records)


def _recomposed(tmp_path: Path):
    """Between two SCORED states the filer dropped a D&A component: D&A is
    built from depreciation alone now — a change of composition."""
    s0 = _every_field(composites=True)
    s1 = _strip(s0, ["AmortizationOfIntangibleAssets"])
    store_snapshot(CIK, s0, now=D2, root=tmp_path)
    store_snapshot(CIK, s1, now=D4, root=tmp_path)
    rep = report_diff(CIK, as_of=REPORT_DAY, root=tmp_path, scored_sha=v.digest_of(s1))
    moved = [c for c in rep.changes_since_previous if c.moved_tag]
    assert moved and len(moved) == len(rep.changes_since_previous)
    return rep, moved


def test_a_recomposed_figure_is_not_counted_as_a_change(tmp_path):
    # Listed with its new composition, never "N change(s)".
    rep, moved = _recomposed(tmp_path)
    assert rep.status_line() == (
        f"compared 2024-12-29 → 2024-12-31: 0 change(s) (+{len(moved)} built from other "
        "concepts: a change of composition, not a revision)")


def test_a_recomposed_figure_is_not_a_revised_input(tmp_path):
    # The card's "reads a revised figure" notes skip it, as Tier 1 does.
    from app.services.reporting.revised_inputs import revision_index

    rep, _moved = _recomposed(tmp_path)
    assert not revision_index(None, rep)


def _defect_on(monkeypatch, facts: dict) -> None:
    real = edgar_adapter.build_dataset

    def defect(f, **kw):
        if v.digest_of(f) == v.digest_of(facts):
            raise TypeError("unsupported operand type(s) for +: 'NoneType' and 'float'")
        return real(f, **kw)

    monkeypatch.setattr(edgar_adapter, "build_dataset", defect)


def test_a_scored_snapshot_the_replay_cannot_map_is_named(tmp_path, monkeypatch):
    s1, s2, _s3 = _three_states()
    store_snapshot(CIK, s1, now=D1, root=tmp_path)
    stored = store_snapshot(CIK, s2, now=D2, root=tmp_path)
    _defect_on(monkeypatch, s2)
    with pytest.raises(edgar_adapter.ScoredSnapshotUnmappable) as caught:
        edgar_adapter.replay_snapshot(_Client(s2), "XYZ", D3.date(), root=tmp_path)
    e = caught.value
    assert stored.path is not None
    assert (e.path, e.captured, e.kind) == (stored.path, "2024-12-29", "scored")
    assert isinstance(e.__cause__, TypeError) and not isinstance(e, ValueError)
    assert str(e) == (
        f"mapper defect on a snapshot a report scored: {stored.path} (captured 2024-12-29, "
        "kind scored): TypeError: unsupported operand type(s) for +: 'NoneType' and 'float'; "
        "move it aside to replay from an older state")


def test_generate_report_as_of_names_the_scored_snapshot_and_exits_4(tmp_path, monkeypatch,
                                                                      capsys):
    from app.services.journal import reporting as journal_reporting
    from scripts import generate_report

    s1, s2, _s3 = _three_states()
    store_snapshot(CIK, s1, now=D1, root=tmp_path)
    stored = store_snapshot(CIK, s2, now=D2, root=tmp_path)
    _defect_on(monkeypatch, s2)
    monkeypatch.setattr(v, "VINTAGES", tmp_path)
    monkeypatch.setattr(journal_reporting, "SecClient", lambda fresh=False: _Client(s2))
    monkeypatch.setattr(generate_report, "ROOT", tmp_path)
    monkeypatch.setattr(generate_report.sys, "argv",
                        ["generate_report.py", "xyz", "--as-of", "2024-12-30", "--no-docs"])
    assert generate_report._main() == generate_report.EXIT_SCORED_SNAPSHOT == 4
    err = capsys.readouterr().err
    assert stored.path is not None
    assert err == (
        f"error: XYZ: mapper defect on a snapshot a report scored: {stored.path} (captured "
        "2024-12-29, kind scored): TypeError: unsupported operand type(s) for +: 'NoneType' "
        "and 'float'; move it aside to replay from an older state\n")


def test_journal_replay_names_the_scored_snapshot(tmp_path, monkeypatch, capsys):
    import argparse
    from types import SimpleNamespace

    from scripts import journal

    e = edgar_adapter.ScoredSnapshotUnmappable(
        tmp_path / "2024-12-29-abc.json.gz", "2024-12-29", "scored", TypeError("boom"))
    monkeypatch.setattr(journal.store, "find_entry", lambda t, d: tmp_path / "XYZ_x.md")
    monkeypatch.setattr(journal.store, "is_v2", lambda p: True)
    monkeypatch.setattr(journal.store, "load_v2",
                        lambda p: SimpleNamespace(ticker="XYZ", day=REPORT_DAY))
    monkeypatch.setattr(journal, "verify_lock", lambda entry: True)

    def raises(*a, **k):
        raise e

    monkeypatch.setattr(journal, "build_report", raises)
    args = argparse.Namespace(ticker="XYZ", date=None, no_docs=True, replay=True, fresh=False)
    assert journal.cmd_report(args) == 1
    assert capsys.readouterr().err == f"Replay failed: {e}\n"
    assert str(e).startswith("mapper defect on a snapshot a report scored: ")


def test_the_not_checked_text_states_the_capture_rule(tmp_path):
    rep = report_diff(CIK, as_of=REPORT_DAY, root=tmp_path)  # nothing stored
    section = " ".join(_silent_revisions_section(rep).split())
    assert "never compared" not in section
    assert ("(a raw watch-sweep capture never stands in for either; one is compared only as "
            "the thesis-lock baseline, when it is the nearest state before the thesis day and "
            "holds every figure the scored comparison reads)") in section


def test_a_capture_fetched_after_a_filing_that_day_covers(tmp_path):
    # KO's 10-K filed 2025-02-20 moves interest expense to another concept
    # for three quarters: a sweep fetch that day, after the filing, is
    # complete, yet differs from the scored state as of the day before. The
    # fetch time is known only to the day, so the day itself counts too.
    from app.services.ingestion.companyfacts_mapper import _visible_as_of

    ko = json.loads((REAL / "companyfacts_KO_trimmed.json").read_text())
    fetched = _visible_as_of(ko, date(2025, 2, 20))
    ref = v._mapped(ko, as_of=date(2025, 2, 19))
    assert v._coverage_gaps(v._mapped(fetched), ref)  # the day before alone rejects it
    at = [datetime(2025, 2, d, 12, tzinfo=UTC) for d in (20, 23, 24)]
    capture(_Client(fetched, 21344), "KO", now=at[0], root=tmp_path)
    store_snapshot(21344, _with(ko, "V"), now=at[1], root=tmp_path)
    store_snapshot(21344, _with(ko, "U"), now=at[2], root=tmp_path)
    rep = report_diff(21344, as_of=date(2025, 2, 24), baseline_day=date(2025, 2, 22),
                      root=tmp_path, scored_sha=v.digest_of(_with(ko, "U")))
    assert rep.baseline.captured == "2025-02-20" and rep.baseline_source == v.SWEEP_BASELINE
    assert rep.raw_note is None and rep.baseline_unavailable is None


def test_share_counts_are_not_part_of_coverage(tmp_path):
    # Split-adjusted fields are never compared (`diff_scored` skips them), so
    # a capture without them misses nothing the lock window reads.
    s0 = _every_field(composites=True)
    bumped = _bump(s0, "Assets", PERIOD, factor=1.2)
    cut = _strip(s0, ["WeightedAverageNumberOfDilutedSharesOutstanding"])
    rep, tier1 = _lock_run(tmp_path, [(D20, cut)], bumped, _with(bumped, "U"))
    assert rep.baseline.captured == "2024-12-20" and rep.raw_note is None
    assert len(tier1) == 1


def test_a_quarter_older_than_the_compared_span_is_not_a_gap(tmp_path):
    # The capture lacks 2023-06-30 altogether; the report compares quarters
    # from `since` on only, so that quarter is outside the comparison.
    s0 = _every_field(composites=True)
    bumped = _bump(s0, "Assets", PERIOD, factor=1.2)
    capture(_Client(_drop_quarters(s0, [QUARTER_ENDS[5]])), "XYZ", now=D20, root=tmp_path)
    store_snapshot(CIK, bumped, now=D2, root=tmp_path)
    store_snapshot(CIK, _with(bumped, "U"), now=D4, root=tmp_path)

    def lock(since):
        return report_diff(CIK, as_of=REPORT_DAY, baseline_day=LOCK, root=tmp_path,
                           since=since, scored_sha=v.digest_of(_with(bumped, "U")))

    assert lock(None).baseline is None  # compared from the capture's first quarter: a gap
    assert lock(QUARTER_ENDS[5]).baseline is None  # a quarter ending on `since` is compared
    rep = lock(date(2023, 7, 1))
    assert rep.baseline.captured == "2024-12-20"
    assert [c.field_name for c in rep.changes_since_baseline] == ["total_assets"]


def test_the_reference_is_the_nearest_scored_state_after_the_lock(tmp_path):
    # A raw capture on the thesis day itself is not what the pre-lock capture
    # is checked against: the scored state of the 29th is.
    s0 = _every_field(composites=True)
    bumped = _bump(s0, "Assets", PERIOD, factor=1.2)
    rep, tier1 = _lock_run(tmp_path, [(D20, s0), (D1, _bare())], bumped, _with(bumped, "U"))
    assert rep.baseline.captured == "2024-12-20" and rep.baseline_source == v.SWEEP_BASELINE
    assert len(tier1) == 1


def test_the_scored_state_is_built_once_per_day_checked(tmp_path, monkeypatch):
    s0 = _every_field(composites=True)
    bumped = _bump(s0, "Assets", PERIOD, factor=1.2)
    capture(_Client(s0), "XYZ", now=D20, root=tmp_path)
    for concept in ("Assets", "Goodwill", "InventoryNet"):  # three partial fetches on the 22nd
        capture(_Client(_strip(s0, [concept])), "XYZ", now=D22, root=tmp_path, force=True)
    s2 = _with(bumped, "U")
    store_snapshot(CIK, bumped, now=D2, root=tmp_path)
    store_snapshot(CIK, s2, now=D4, root=tmp_path)
    built = _counting(monkeypatch)
    rep = report_diff(CIK, as_of=REPORT_DAY, baseline_day=LOCK, root=tmp_path,
                      scored_sha=v.digest_of(s2))
    assert rep.baseline.captured == "2024-12-20"
    assert rep.raw_note == (
        "3 capture(s) before the thesis day not usable as the lock baseline (3 incomplete)")
    # Four captures; the scored state as of the 21st, the 22nd and the 19th;
    # the two diffs' four builds.
    assert len(built) == 4 + 3 + 4


def test_a_capture_fetched_before_that_days_filing_is_checked_once(tmp_path, monkeypatch):
    # The day before is checked first: a fetch that preceded the day's
    # filing matches it, and the same-day state is never built.
    from app.services.ingestion.companyfacts_mapper import _visible_as_of

    ko = json.loads((REAL / "companyfacts_KO_trimmed.json").read_text())
    capture(_Client(_visible_as_of(ko, date(2025, 2, 19)), 21344), "KO",
            now=datetime(2025, 2, 20, 12, tzinfo=UTC), root=tmp_path)
    s1, s2 = _with(ko, "V"), _with(ko, "U")
    store_snapshot(21344, s1, now=datetime(2025, 2, 23, 12, tzinfo=UTC), root=tmp_path)
    store_snapshot(21344, s2, now=datetime(2025, 2, 24, 12, tzinfo=UTC), root=tmp_path)
    built = _counting(monkeypatch)
    rep = report_diff(21344, as_of=date(2025, 2, 24), baseline_day=date(2025, 2, 22),
                      root=tmp_path, scored_sha=v.digest_of(s2))
    assert rep.baseline.captured == "2025-02-20" and rep.raw_note is None
    assert len(built) == 1 + 1 + 4  # the capture, the state as of the 19th, the two diffs


def test_a_legacy_state_passed_over_by_both_walks_is_logged_once(tmp_path, caplog):
    s1, s2 = _scenario_b()
    store_snapshot(CIK, s1, now=D24, root=tmp_path)
    odd = store_snapshot(CIK, ODD, now=D26, root=tmp_path)
    store_snapshot(CIK, s2, now=D2, root=tmp_path)
    _drop_kinds(tmp_path)
    with caplog.at_level(logging.WARNING, logger=v.__name__):
        rep = report_diff(CIK, as_of=REPORT_DAY, baseline_day=LOCK, root=tmp_path)
    assert (rep.previous.captured, rep.baseline.captured) == ("2024-12-24", "2024-12-24")
    assert odd.path is not None
    assert sum(odd.path.name in r.getMessage() for r in caplog.records) == 1


def test_a_capture_missing_its_own_first_quarter_of_a_field_is_incomplete(tmp_path):
    s0 = _every_field(composites=True)
    bumped = _bump(s0, "Assets", PERIOD, factor=1.2)
    cut = copy.deepcopy(s0)
    first = v._mapped(s0).window[0]
    for rows in cut["facts"]["us-gaap"]["Goodwill"]["units"].values():
        rows[:] = [r for r in rows if r["end"] != first.isoformat()]
    mapped = v._mapped(cut)
    assert mapped.window[0] == first and first not in mapped.values["goodwill"]
    rep, tier1 = _lock_run(tmp_path, [(D20, cut)], bumped, _with(bumped, "U"))
    _assert_not_checked(rep)


def test_coverage_is_judged_against_the_state_after_the_lock_not_the_newest(tmp_path):
    # The filer dropped a D&A component after the scored state of the 29th:
    # the newest builds D&A from depreciation alone. The capture matches the
    # state nearest after the lock, so it covers; its D&A rows against the
    # newest are a change of composition, not counted, and the revision is
    # still found.
    s0 = _every_field(composites=True)
    bumped = _bump(s0, "Assets", PERIOD, factor=1.2)
    newest = _strip(bumped, ["AmortizationOfIntangibleAssets"])
    rep, tier1 = _lock_run(tmp_path, [(D20, s0)], bumped, newest)
    assert rep.baseline.captured == "2024-12-20" and rep.baseline_source == v.SWEEP_BASELINE
    assert len(tier1) == 1 and "total_assets" in tier1[0]
    assert "since pinned thesis 2024-12-20, a sweep capture (mapped, complete): 1 change(s) (+" in (
        rep.status_line())


def test_a_capture_is_not_checked_against_a_scored_state_of_its_own_day(tmp_path):
    # The 27th holds a scored state (D&A from depreciation alone, as the
    # filer then reported it) and, observed after it, a complete sweep
    # capture: the nearer state is the capture, checked against the scored
    # state after the thesis day — not the earlier one of its own day.
    s0 = _every_field(composites=True)
    root = tmp_path
    store_snapshot(CIK, _strip(s0, ["AmortizationOfIntangibleAssets"]), now=D27, root=root)
    capture(_Client(s0), "XYZ", now=D27, root=root, force=True)
    s1, s2 = _with(s0, "V"), _with(s0, "U")
    store_snapshot(CIK, s1, now=D2, root=root)
    store_snapshot(CIK, s2, now=D4, root=root)
    rep = report_diff(CIK, as_of=REPORT_DAY, baseline_day=LOCK, root=root,
                      scored_sha=v.digest_of(s2))
    assert rep.baseline.captured == "2024-12-27" and rep.baseline_source == v.SWEEP_BASELINE
    assert rep.changes_since_baseline == []


def test_a_scored_state_past_the_bound_is_still_the_baseline(tmp_path):
    # The bound limits mapper builds, not the walk: an older scored state
    # costs nothing to accept, and the captures not examined are counted.
    s0 = _every_field(composites=False)
    t0 = datetime(2024, 10, 1, 12, tzinfo=UTC)
    store_snapshot(CIK, s0, now=t0, root=tmp_path)
    for i in range(1, 13):
        capture(_Client(dict(_partial(s0), entityName=f"x{i}")), "XYZ",
                now=t0 + timedelta(days=i), root=tmp_path)
    s2 = _bump(s0, "Assets", PERIOD, factor=1.2)
    store_snapshot(CIK, _with(s2, "V"), now=D2, root=tmp_path)
    store_snapshot(CIK, s2, now=D4, root=tmp_path)
    rep = report_diff(CIK, as_of=REPORT_DAY, baseline_day=LOCK, root=tmp_path,
                      scored_sha=v.digest_of(s2))
    tries = v.LOCK_CAPTURES_EXAMINED
    assert rep.baseline.captured == "2024-10-01" and rep.baseline_source is None
    assert [c.field_name for c in rep.changes_since_baseline] == ["total_assets"]
    assert rep.raw_note.endswith(
        f"{tries} capture(s) before the thesis day not usable as the lock baseline ({tries} "
        f"unmappable); {12 - tries} older capture(s) before the thesis day not examined (at "
        f"most {tries} per report)")
