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
    assert rep.status_line() == (
        f"compared 2024-12-29 → 2024-12-30: not compared as scored (+{n} raw fact row(s), not "
        "scored changes); scored values not compared as the engine builds them: the "
        "newer snapshot could not be mapped (raw fact rows only, not scored changes)"
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
    assert ("not checked this run: silent revisions (not compared as scored: a snapshot "
            "could not be mapped)") in report
    assert ("- Silent-revision check: compared 2024-12-29 → 2024-12-31: not compared as "
            "scored (+1 raw fact row(s), not scored changes); scored values not compared as "
            "the engine builds them: the older snapshot could not be mapped") in report
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
        "compared 2024-12-29 → 2024-12-31: 1 change(s); since pinned thesis 2024-12-27: not "
        "compared as scored (+1 raw fact row(s), not scored changes); since the pinned thesis: "
        "scored values not compared as the engine builds them: the older snapshot could not be "
        "mapped (raw fact rows only, not scored changes)")
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
