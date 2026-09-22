"""PR 1.1 — the silent-revision check reaches the report.

`diff_vintages` / `render_changes` (Tier-2 restatement detection: a filer
revising a number without re-presenting the original) were surfaced nowhere
but scripts/vintage.py. Now every client-backed report diffs the newest
snapshot taken at or before its date against the previous one — and, on the
journal track, against the snapshot at or before the pinned thesis day — and
promotes a revised scored figure of >=5% to the card's Tier 1. What could
not be compared is named; "no baseline yet" never reads as clean.
"""

from __future__ import annotations

import gzip
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.core.pipeline import analyze
from app.services.ingestion import vintages as v
from app.services.ingestion.vintages import (
    SILENT_REVISION_TIER1_PCT,
    FactKey,
    VintageChange,
    observation_at_or_before,
    report_diff,
    silent_revision_tier1_lines,
    store_snapshot,
)
from app.services.reporting.report_builder import _collect_streams, build_report
from tests.fixtures.companies import stretch_dataset

CIK = 1045810
D19, D20, D21 = (datetime(2026, 9, d, 12, 0, tzinfo=UTC) for d in (19, 20, 21))
AS_OF = date(2026, 9, 22)
FLOOR = date(2024, 9, 22)


def _facts(rows, tag="Assets"):
    """rows: (end, filed, val, form, accn)"""
    return {"cik": CIK, "facts": {"us-gaap": {tag: {"units": {"USD": [
        {"end": r[0], "filed": r[1], "val": r[2], "form": r[3], "accn": r[4]} for r in rows
    ]}}}}}


def _assets(val, end="2026-06-30", filed="2026-08-01", accn="a"):
    return _facts([(end, filed, val, "10-Q", accn)])


def _store(root, facts, at, force=False):
    cap = store_snapshot(CIK, facts, now=at, root=root, force=force)
    assert cap.wrote or force, cap.describe()
    return cap


class _Client:
    """Complete on purpose: a missing method reads as a stream failure."""

    def __init__(self, facts=None):
        self._facts = facts or {"facts": {}}

    def company_facts(self, ticker):
        return self._facts

    def company_facts_by_cik(self, cik):
        return self._facts

    def resolve_cik(self, ticker):
        return CIK

    def submissions(self, ticker):
        return {"filings": {"recent": {}}}

    def submissions_by_cik(self, cik):
        return {"filings": {"recent": {}}}


# --- observation selection ---------------------------------------------------


def test_at_or_before_keeps_the_day_and_excludes_the_next(tmp_path):
    for val, at in ((1000.0, D19), (1100.0, D20), (1200.0, D21)):
        _store(tmp_path, _assets(val), at)
    states = v.observed_vintages(CIK, tmp_path)
    assert [s.captured for s in states] == ["2026-09-19", "2026-09-20", "2026-09-21"]
    assert observation_at_or_before(states, date(2026, 9, 20)).captured == "2026-09-20"
    assert observation_at_or_before(states, date(2026, 9, 19)).captured == "2026-09-19"
    assert observation_at_or_before(states, date(2026, 9, 18)) is None
    assert observation_at_or_before(states, date(2026, 12, 31)).captured == "2026-09-21"


def test_same_day_observations_resolve_to_the_later_one(tmp_path):
    _store(tmp_path, _assets(1000.0), D19)
    later = _store(tmp_path, _assets(1100.0), D19, force=True)
    states = v.observed_vintages(CIK, tmp_path)
    assert len(states) == 2 and {s.captured for s in states} == {"2026-09-19"}
    assert observation_at_or_before(states, date(2026, 9, 19)).sha256 == later.sha256


def test_an_observation_after_the_report_date_is_invisible(tmp_path):
    _store(tmp_path, _assets(1000.0), D19)
    _store(tmp_path, _assets(1000.0, accn="b"), D20)  # same value, different bytes
    _store(tmp_path, _assets(1500.0), D21)  # the revision lands after as_of
    rep = report_diff(CIK, as_of=date(2026, 9, 20), root=tmp_path)
    assert (rep.previous.captured, rep.newest.captured) == ("2026-09-19", "2026-09-20")
    assert rep.changes_since_previous == []
    later = report_diff(CIK, as_of=date(2026, 9, 21), root=tmp_path)
    assert [c.new_value for c in later.changes_since_previous] == [1500.0]


# --- the pinned-thesis baseline -------------------------------------------------


def test_a_baseline_equal_to_previous_is_not_compared_twice(tmp_path):
    _store(tmp_path, _assets(1000.0), D19)
    _store(tmp_path, _assets(1100.0), D20)
    rep = report_diff(CIK, as_of=AS_OF, baseline_day=date(2026, 9, 19), root=tmp_path)
    assert rep.changes_since_baseline is None
    assert rep.baseline.captured == "2026-09-19"
    assert "is the previous snapshot" in rep.baseline_note
    from app.services.reporting.report_builder import _silent_revisions_section

    assert _silent_revisions_section(rep).count("### Vintage diff") == 1
    assert "since pinned thesis" not in rep.status_line()
    assert "is the previous snapshot" in rep.status_line()


def test_a_baseline_equal_to_newest_says_nothing_to_compare(tmp_path):
    _store(tmp_path, _assets(1000.0), D19)
    _store(tmp_path, _assets(1100.0), D20)
    rep = report_diff(CIK, as_of=AS_OF, baseline_day=date(2026, 9, 21), root=tmp_path)
    assert rep.changes_since_baseline is None
    assert "is the newest snapshot" in rep.baseline_note


def test_a_baseline_older_than_previous_gets_its_own_block(tmp_path):
    """The revision happened between states 1 and 2; state 3 only re-tags
    provenance. previous->newest is clean, lock->newest carries it, and the
    Tier-1 line names the lock window."""
    _store(tmp_path, _assets(1000.0), D19)
    _store(tmp_path, _assets(1100.0), D20)
    _store(tmp_path, _assets(1100.0, accn="c"), D21)
    rep = report_diff(CIK, as_of=AS_OF, baseline_day=date(2026, 9, 19), root=tmp_path)
    assert rep.changes_since_previous == []
    assert [c.new_value for c in rep.changes_since_baseline] == [1100.0]
    assert rep.status_line() == (
        "compared 2026-09-20 → 2026-09-21: 0 change(s); since pinned thesis 2026-09-19: 1 change(s)"
    )
    from app.services.reporting.report_builder import _silent_revisions_section

    md = _silent_revisions_section(rep)
    assert md.count("### Vintage diff") == 2
    assert "**Since the pinned thesis was locked** (2026-09-19):" in md
    # Through the stream: the promoted line names the lock window, not previous->newest.
    _s, _e, tier1, errors, _t, _scan, diff = _collect_streams(
        _Client(_assets(1100.0)), "AAPL", AS_OF, company_facts=_assets(1100.0),
        baseline_day=date(2026, 9, 19), vintage_root=tmp_path,
    )
    assert errors["vintage"] is None
    assert [line for line in tier1 if line.startswith("Silent revision:")] == [
        "Silent revision: total_assets for 2026-06-30 1,000 → 1,100 (+10.0%) between "
        "snapshots 2026-09-19 and 2026-09-21 (detail in appendix; threshold hand-set, uncalibrated)"
    ]


def test_a_thesis_day_before_the_first_capture_is_named(tmp_path):
    _store(tmp_path, _assets(1000.0), D19)
    _store(tmp_path, _assets(1100.0), D20)
    rep = report_diff(CIK, as_of=AS_OF, baseline_day=date(2026, 9, 1), root=tmp_path)
    assert rep.baseline is None and rep.changes_since_baseline is None
    assert rep.baseline_note == (
        "no snapshot at or before the pinned thesis day 2026-09-01; earliest is 2026-09-19"
    )
    assert [c.new_value for c in rep.changes_since_previous] == [1100.0]


# --- nothing to compare is not "clean" -------------------------------------------


@pytest.mark.parametrize("snapshots", [0, 1], ids=["none", "one"])
def test_no_baseline_wording(tmp_path, snapshots):
    if snapshots:
        _store(tmp_path, _assets(1000.0), D19)
    rep = report_diff(CIK, as_of=AS_OF, root=tmp_path)
    assert not rep.compared and rep.changes_since_previous == []
    expected = (
        "no baseline yet (first capture this run)" if snapshots
        else "no snapshot at or before 2026-09-22 (capture disabled or failed — see the Vintage snapshot line)"
    )
    assert rep.no_baseline_reason == expected and rep.status_line() == expected
    ds = stretch_dataset()
    report, _ = build_report(
        analyze(ds), ds, generated_on=AS_OF.isoformat(), coverage=1.0, fetched_at="x",
        client=_Client(), ticker="AAPL", company_facts={"facts": {}}, field_tags={},
        vintage_root=tmp_path,
    )
    assert f"## Silent Revisions Between Snapshots (evidence — not scored)\n\nNot checked: {expected}." in report
    assert f"- Silent-revision check: {expected}" in report
    assert "silent revisions (no vintage baseline yet)" in report
    assert "not checked this run:" in report
    assert "Silent-revision appendix UNAVAILABLE" not in report
    assert "No prior-period figure changed" not in report


def test_an_unreadable_snapshot_is_a_stream_failure_not_a_report_abort(tmp_path):
    _store(tmp_path, _assets(1000.0), D19)
    newest = _store(tmp_path, _assets(1100.0), D20)
    newest.path.write_bytes(gzip.compress(b'{"facts": {}')[:-6])  # truncated member
    with pytest.raises(v.UNREADABLE):
        report_diff(CIK, as_of=AS_OF, root=tmp_path)
    ds = stretch_dataset()
    report, _ = build_report(
        analyze(ds), ds, generated_on=AS_OF.isoformat(), coverage=1.0, fetched_at="x",
        client=_Client(), ticker="AAPL", company_facts={"facts": {}}, field_tags={},
        vintage_root=tmp_path,
    )
    assert "**Silent-revision appendix UNAVAILABLE**" in report
    assert "not evidence of no silent revisions" in report
    assert "not checked this run: silent revisions (vintage diff)" in report
    assert "## Silent Revisions Between Snapshots" not in report
    assert "Silent-revision check:" not in report


def test_a_defect_in_the_vintage_stream_propagates(monkeypatch):
    def broken(*a, **k):
        raise NameError("name 'baseline_day' is not defined")

    monkeypatch.setattr(v, "report_diff", broken)
    with pytest.raises(NameError):
        _collect_streams(_Client(), "AAPL", AS_OF, company_facts={"facts": {}})


# --- the Tier-1 rule, conjunct by conjunct --------------------------------------


def _change(kind="revised", field="total_assets", tag="Assets", end=date(2026, 6, 30),
            old=1000.0, new=1050.0, pct=0.05, new_tag="Assets"):
    return VintageChange(
        kind, field, FactKey("us-gaap", tag, "USD", None, end), old, date(2026, 8, 1), "a", "10-Q",
        new if kind == "revised" else None, date(2026, 9, 1) if kind == "revised" else None,
        "b" if kind == "revised" else "", "10-Q" if kind == "revised" else "",
        pct if kind == "revised" else None, new_tag if kind == "revised" else "",
    )


@pytest.mark.parametrize("change, promoted", [
    (_change(), True),
    (_change(new=1049.0, pct=0.049), False),
    (_change(kind="withdrawn"), False),
    (_change(new_tag="AssetsNet"), False),
    (_change(end=date(2024, 9, 21)), False),
    (_change(end=date(2024, 9, 22)), True),
    (_change(field="us-gaap:MadeUpTag", tag="MadeUpTag"), False),
    (_change(field="shares_diluted", tag="WeightedAverageNumberOfDilutedSharesOutstanding"), False),
    (_change(old=0.0, new=5.0, pct=None), False),
    (_change(new=900.0, pct=0.10), True),
], ids=["at-threshold", "below-threshold", "withdrawn", "tag-move", "old-period",
        "at-floor", "unscored-field", "split-field", "no-ratio", "down-10pct"])
def test_tier1_rule_each_conjunct(change, promoted):
    assert SILENT_REVISION_TIER1_PCT == 0.05
    lines = silent_revision_tier1_lines([change], "2026-09-19", "2026-09-20", period_since=FLOOR)
    assert bool(lines) is promoted
    if promoted:
        signed = "+5.0%" if change.new_value > change.old_value else "-10.0%"
        assert lines == [
            f"Silent revision: total_assets for {change.key.end} 1,000 → {change.new_value:,.0f} "
            f"({signed}) between snapshots 2026-09-19 and 2026-09-20 "
            "(detail in appendix; threshold hand-set, uncalibrated)"
        ]


# --- end to end through the real builder ------------------------------------------


def _real_report(tmp_path, old, new):
    _store(tmp_path, _assets(old), D19)
    facts = _assets(new)
    _store(tmp_path, facts, D20)
    ds = stretch_dataset()
    report, _ = build_report(
        analyze(ds), ds, generated_on=AS_OF.isoformat(), coverage=1.0, fetched_at="x",
        client=_Client(facts), ticker="AAPL", company_facts=facts, field_tags={},
        vintage_root=tmp_path,
    )
    return report


def test_the_real_report_promotes_a_silent_revision(tmp_path):
    report = _real_report(tmp_path, 1000.0, 1100.0)
    assert "## Silent Revisions Between Snapshots (evidence — not scored)" in report
    assert "### Vintage diff — 2026-09-19 → 2026-09-20" in report
    assert "| total_assets | 2026-06-30 | 1,000 | 1,100 | 10.0% |" in report
    tier1 = report.split("**Tier 1 — validated (low false-positive):**")[1].split("**Tier 2")[0]
    assert (
        "- Silent revision: total_assets for 2026-06-30 1,000 → 1,100 (+10.0%) between "
        "snapshots 2026-09-19 and 2026-09-20 (detail in appendix; threshold hand-set, uncalibrated)"
    ) in tier1
    assert "- Silent-revision check: compared 2026-09-19 → 2026-09-20: 1 change(s)" in report
    # Every stream ran: nothing is "not checked", and nothing is unavailable.
    assert "not checked this run" not in report
    assert "Silent-revision appendix UNAVAILABLE" not in report


def test_a_small_move_stays_in_the_appendix(tmp_path):
    report = _real_report(tmp_path, 1000.0, 1020.0)
    assert "| total_assets | 2026-06-30 | 1,000 | 1,020 | 2.0% |" in report
    assert "Silent revision:" not in report
    assert "- Silent-revision check: compared 2026-09-19 → 2026-09-20: 1 change(s)" in report


def test_the_client_less_report_lists_silent_revisions_as_not_checked():
    ds = stretch_dataset()
    report, _ = build_report(analyze(ds), ds, generated_on=AS_OF.isoformat(), coverage=1.0)
    assert "silent revisions (vintage diff)" in report
    assert "## Silent Revisions Between Snapshots" not in report


# --- entry points ------------------------------------------------------------------


def test_journal_threads_the_entry_day_and_the_cli_passes_nothing(monkeypatch, tmp_path):
    import importlib
    import sys

    from app.services.journal import reporting as journal_reporting

    seen: dict[str, object] = {}

    class _Diag:
        warnings: list[str] = []

        def coverage(self):
            return 1.0

        def selected_tags(self):
            return {}

        def field_notes(self):
            return []

    class _Snap:
        dataset = stretch_dataset()
        diagnostics = _Diag()
        company_facts = {"facts": {}}

    def fake_build(result, dataset, **kw):
        seen[kw["ticker"]] = kw.get("baseline_day", "absent")
        from app.services.scoring.thermometer import compute_thermometer
        return "report", compute_thermometer(result.block_scores, dataset.periods)

    monkeypatch.setattr(journal_reporting, "fetch_dataset_snapshot", lambda *a, **k: _Snap())
    monkeypatch.setattr(journal_reporting, "fetch_submissions_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(journal_reporting, "store_vintage_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(journal_reporting, "SecClient", lambda **k: object())
    monkeypatch.setattr(journal_reporting, "build_full_report", fake_build)
    journal_reporting.build_report("JRN", with_docs=False, out_dir=tmp_path, report_day="2026-09-10")
    assert seen["JRN"] == date(2026, 9, 10)
    journal_reporting.build_report("JR2", with_docs=False, out_dir=tmp_path)
    assert seen["JR2"] is None

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    cli = importlib.import_module("generate_report")
    monkeypatch.setattr(cli, "fetch_dataset_snapshot", lambda *a, **k: _Snap())
    monkeypatch.setattr(cli, "fetch_submissions_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(cli, "store_vintage_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(cli, "SecClient", lambda **k: object())
    monkeypatch.setattr(cli, "build_report", fake_build)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["generate_report.py", "CLI", "--no-docs", "--no-vintage"])
    assert cli.main() == 0
    assert seen["CLI"] == "absent"
