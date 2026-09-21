"""Ticker-only track tests: the watch CLI's auto path, fresh propagation, and
the shared report builder's out_dir/banner/fresh parameters.

The invariant under test: automation never erodes the thesis gate. A locked
thesis takes the journal track; a thesis-less print produces only a bannered
artifact in reports/auto/ that cannot be mistaken for a blind case.
"""

from __future__ import annotations

import importlib.util
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.watch.poller import Decision

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location("watch_cli", ROOT / "scripts" / "watch.py")
watch_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watch_cli)


def _poll_args(**over) -> Namespace:
    base = dict(
        ticker="NVDA", since=None, entry_day=None, interval=0.01, max_wait=1.0,
        once=True, dry_run=False, no_docs=False, no_auto=False, no_audit=False,
        no_brief=False, no_vintage=False,
    )
    base.update(over)
    return Namespace(**base)


class _FakeClient:
    def __init__(self, *a, **k):
        pass

    def resolve_cik(self, ticker):
        return "0000000000"

    def submissions_by_cik(self, cik):
        return {"filings": {"recent": {}}}


@pytest.fixture
def poll_env(monkeypatch, tmp_path):
    """Neutralize network + subprocess; record what the poll routed to."""
    calls = SimpleNamespace(generate=[], generate_auto=[], audit=[], marked=[], rearm=[])
    monkeypatch.setattr(watch_cli, "SecClient", _FakeClient)
    monkeypatch.setattr(watch_cli, "SWEEP_LOCK", tmp_path / "sweep.lock")  # never the real one
    monkeypatch.setattr(watch_cli, "BRIEF_PENDING", tmp_path / "pending")  # nor the real queue
    monkeypatch.setattr(watch_cli, "BRIEFS", tmp_path / "briefs")  # nor the real briefs
    calls.notified = []
    monkeypatch.setattr(watch_cli, "notify", lambda t, m: calls.notified.append((t, m)) or True)
    # Never let a test reach the real companyfacts archive under data/vintages/.
    calls.vintages = []
    monkeypatch.setattr(
        watch_cli, "capture_vintage",
        lambda client, ticker, now=None: calls.vintages.append(ticker) or SimpleNamespace(
            wrote=False, path=None, reason="unchanged"),
    )
    # Re-arming persists to the watchlist; never let a unit test touch the
    # real journal/watchlist.json.
    monkeypatch.setattr(
        watch_cli, "_rearm",
        lambda watch, decision, submissions:
        calls.rearm.append((watch.ticker, decision.action)) or True,
    )
    monkeypatch.setattr(
        watch_cli, "_find_watch",
        lambda t: watch_cli.wl.Watch(
            ticker=t, print_at=watch_cli._now("2026-08-26T20:20:00+00:00"),
            baseline_accession="0001045810-26-000052",
            expected_report_date=watch_cli.date(2026, 7, 26),
            thesis_entry="2026-08-26", thesis_sha256="ab" * 32,
        ),
    )
    monkeypatch.setattr(
        watch_cli, "_generate",
        lambda t, day, nd: calls.generate.append((t, day)) or 0,
    )
    monkeypatch.setattr(
        watch_cli, "_generate_auto",
        lambda t, nd: calls.generate_auto.append(t) or Path("/tmp/fake_auto.md"),
    )
    monkeypatch.setattr(
        watch_cli, "_run_audit",
        lambda p: calls.audit.append(p) or 0,
    )
    monkeypatch.setattr(
        watch_cli, "_mark_reported",
        lambda t, day: calls.marked.append((t, day, len(calls.audit))) or 0,
    )
    monkeypatch.setattr(
        watch_cli, "_latest_report", lambda t, d: Path("/tmp/fake_journal.md")
    )
    calls.brief = []
    monkeypatch.setattr(watch_cli, "_run_brief", lambda t, p: calls.brief.append((t, p)) or 0)
    return calls


def _force_decision(monkeypatch, action: str):
    monkeypatch.setattr(
        watch_cli, "decide",
        lambda watch, submissions, since=None, force=False:
        Decision(action, f"forced {action}"),
    )


class TestPollAutoTrack:
    def test_refuse_routes_to_auto_artifact_and_audit(self, poll_env, monkeypatch):
        _force_decision(monkeypatch, "refuse")
        rc = watch_cli.cmd_poll(_poll_args())
        assert rc == 0
        assert poll_env.generate_auto == ["NVDA"]
        assert poll_env.generate == []
        assert poll_env.audit == [Path("/tmp/fake_auto.md")]

    def test_no_auto_preserves_strict_refusal_exit_2(self, poll_env, monkeypatch):
        _force_decision(monkeypatch, "refuse")
        rc = watch_cli.cmd_poll(_poll_args(no_auto=True))
        assert rc == 2
        assert poll_env.generate_auto == []
        assert poll_env.audit == []

    def test_no_audit_skips_the_audit_only(self, poll_env, monkeypatch):
        _force_decision(monkeypatch, "refuse")
        rc = watch_cli.cmd_poll(_poll_args(no_audit=True))
        assert rc == 0
        assert poll_env.generate_auto == ["NVDA"]
        assert poll_env.audit == []

    def test_generate_takes_journal_track_then_audits(self, poll_env, monkeypatch):
        _force_decision(monkeypatch, "generate")
        rc = watch_cli.cmd_poll(_poll_args())
        assert rc == 0
        assert poll_env.generate == [("NVDA", "2026-08-26")]  # pinned day, not "latest"
        assert poll_env.generate_auto == []
        assert poll_env.audit == [Path("/tmp/fake_journal.md")]

    def test_reported_is_stamped_only_after_a_successful_audit(self, poll_env, monkeypatch):
        # Ordering regression: mark must come AFTER the audit completes. The
        # recorded audit-count-at-mark-time proves the sequence.
        _force_decision(monkeypatch, "generate")
        assert watch_cli.cmd_poll(_poll_args()) == 0
        assert poll_env.marked == [("NVDA", "2026-08-26", 1)]  # 1 audit already done

    def test_audit_failure_propagates_and_leaves_journal_retryable(self, poll_env, monkeypatch):
        # THE defect: the poll returned 0 while the audit had failed, so cron
        # saw a healthy run with no analysis. Now: exit 4, nothing marked.
        _force_decision(monkeypatch, "generate")
        monkeypatch.setattr(watch_cli, "_run_audit", lambda p: 7)
        rc = watch_cli.cmd_poll(_poll_args())
        assert rc == 4
        assert poll_env.marked == []  # entry stays retryable

    def test_no_audit_marks_immediately(self, poll_env, monkeypatch):
        _force_decision(monkeypatch, "generate")
        rc = watch_cli.cmd_poll(_poll_args(no_audit=True))
        assert rc == 0
        assert poll_env.audit == []
        assert poll_env.marked == [("NVDA", "2026-08-26", 0)]

    def test_auto_track_audit_failure_propagates(self, poll_env, monkeypatch):
        _force_decision(monkeypatch, "refuse")
        monkeypatch.setattr(watch_cli, "_run_audit", lambda p: 7)
        assert watch_cli.cmd_poll(_poll_args()) == 4

    def test_legacy_watch_without_identity_fails_closed(self, poll_env, monkeypatch):
        # decide() raising PollerError (no event identity) must exit 1, not
        # loop or pretend to wait.
        def boom(watch, submissions, since=None, force=False):
            raise watch_cli.PollerError("NVDA: watch has no event identity")
        monkeypatch.setattr(watch_cli, "decide", boom)
        assert watch_cli.cmd_poll(_poll_args()) == 1

    def test_auto_generation_failure_exits_1(self, poll_env, monkeypatch):
        _force_decision(monkeypatch, "refuse")
        monkeypatch.setattr(watch_cli, "_generate_auto", lambda t, nd: None)
        assert watch_cli.cmd_poll(_poll_args()) == 1
        assert poll_env.audit == []

    def test_dry_run_never_generates(self, poll_env, monkeypatch):
        _force_decision(monkeypatch, "refuse")
        rc = watch_cli.cmd_poll(_poll_args(dry_run=True))
        assert rc == 0
        assert poll_env.generate_auto == []


class TestLinkAmbiguity:
    """`link` without --entry-day may resolve an entry only when there is
    exactly one candidate — with several unreported v2 entries it must refuse
    rather than silently pin the lexicographically latest one (the "latest
    entry wins" guess the pin mechanism exists to eliminate)."""

    def _entry(self, day):
        from datetime import date as d, datetime, timezone

        from app.services.journal.schema_v2 import (
            Assumption, BeforeBlock, EntryV2, lock_entry,
        )

        before = BeforeBlock(
            thesis="thesis for the event", conviction=3, intended_action="hold",
            assumptions=[Assumption(metric="revenue", comparator=">", threshold=1.0,
                                    window="Q", source="10-Q",
                                    resolve_by=d(2026, 12, 31))],
        )
        return lock_entry(EntryV2(
            ticker="NVDA", day=d.fromisoformat(day),
            opened=datetime(2026, 8, 1, tzinfo=timezone.utc), before=before,
        ))

    @pytest.fixture
    def link_env(self, monkeypatch, tmp_path):
        from app.services.journal import store as st

        monkeypatch.setattr(st, "ENTRIES", tmp_path)
        monkeypatch.setattr(
            watch_cli, "_find_watch",
            lambda t: watch_cli.wl.Watch(
                ticker=t, print_at=watch_cli._now("2026-11-18T21:20:00+00:00"),
                baseline_accession="acc-1",
                expected_report_date=watch_cli.date(2026, 10, 25),
            ),
        )
        pinned: list = []
        monkeypatch.setattr(
            watch_cli.wl, "update_entry",
            lambda t, updates, path=None: pinned.append((t, updates)) or None,
        )
        return SimpleNamespace(store=st, pinned=pinned)

    def test_two_unreported_entries_without_entry_day_refuse(self, link_env):
        link_env.store.save_v2(self._entry("2026-08-26"))
        link_env.store.save_v2(self._entry("2026-11-18"))
        rc = watch_cli.cmd_link(Namespace(ticker="NVDA", entry_day=None))
        assert rc == 1
        assert link_env.pinned == []  # nothing guessed, nothing pinned

    def test_single_unreported_entry_links_without_entry_day(self, link_env):
        entry = self._entry("2026-11-18")
        link_env.store.save_v2(entry)
        rc = watch_cli.cmd_link(Namespace(ticker="NVDA", entry_day=None))
        assert rc == 0
        assert link_env.pinned == [("NVDA", {
            "thesis_entry": "2026-11-18", "thesis_sha256": entry.before_sha256,
        })]

    def test_single_candidate_wins_over_later_spent_entry(self, link_env):
        # Review repro (HIGH): one unreported v2 entry plus a lexicographically
        # LATER already-reported one. The guard validated the single candidate
        # but resolution still went through find_entry(ticker, None), which
        # picked the later spent entry and failed with "already reported" —
        # breaking the routine close-one-case-open-the-next workflow. The
        # validated candidate must BE the resolution.
        from datetime import datetime, timezone

        from app.services.journal import store as st

        wanted = self._entry("2026-08-26")
        st.save_v2(wanted)
        spent = self._entry("2026-11-18")
        path = st.save_v2(spent)
        st.save_v2(spent.model_copy(update={"reported": datetime.now(timezone.utc)}),
                   path, allow_update=True)

        rc = watch_cli.cmd_link(Namespace(ticker="NVDA", entry_day=None))
        assert rc == 0
        assert link_env.pinned == [("NVDA", {
            "thesis_entry": "2026-08-26", "thesis_sha256": wanted.before_sha256,
        })]

    def test_explicit_entry_day_resolves_the_ambiguity(self, link_env):
        link_env.store.save_v2(self._entry("2026-08-26"))
        wanted = self._entry("2026-11-18")
        link_env.store.save_v2(wanted)
        rc = watch_cli.cmd_link(Namespace(ticker="NVDA", entry_day="2026-11-18"))
        assert rc == 0
        assert link_env.pinned == [("NVDA", {
            "thesis_entry": "2026-11-18", "thesis_sha256": wanted.before_sha256,
        })]


class TestFreshPropagation:
    def test_journal_generate_always_passes_fresh(self, monkeypatch):
        recorded = {}

        def fake_run(cmd, cwd=None):
            recorded["cmd"] = cmd
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(watch_cli.subprocess, "run", fake_run)
        assert watch_cli._generate("NVDA", "2026-08-26", no_docs=False) == 0
        assert "--fresh" in recorded["cmd"]
        assert "--defer-mark" in recorded["cmd"]  # mark happens only post-audit
        assert recorded["cmd"][recorded["cmd"].index("--date") + 1] == "2026-08-26"

    def test_build_report_constructs_fresh_client_and_banners(self, monkeypatch, tmp_path):
        from app.services.journal import reporting

        client_kwargs = {}

        class _Client:
            def __init__(self, *a, **k):
                client_kwargs.update(k)

            def submissions(self, ticker):
                return {}

        snapshot = SimpleNamespace(
            dataset=SimpleNamespace(documents=[]),
            diagnostics=SimpleNamespace(coverage=lambda: 0.9, warnings=[]),
            company_facts={},
        )
        monkeypatch.setattr(reporting, "SecClient", _Client)
        monkeypatch.setattr(reporting, "fetch_dataset_snapshot",
                            lambda t, n_quarters, client: snapshot)
        monkeypatch.setattr(
            reporting, "analyze", lambda ds: SimpleNamespace(overall=None)
        )
        monkeypatch.setattr(
            reporting, "build_full_report", lambda *a, **k: ("ENGINE REPORT BODY", None)
        )

        out, distress = reporting.build_report(
            "nvda", with_docs=False, fresh=True,
            out_dir=tmp_path / "auto", banner="> BANNER LINE",
        )
        assert client_kwargs.get("fresh") is True
        assert out.parent == tmp_path / "auto"
        assert out.name.startswith("NVDA_")
        assert out.read_text().startswith("> BANNER LINE\n\nENGINE REPORT BODY")
        # 4c: the composite is gone from the return — a distress summary string
        assert isinstance(distress, str)

    def test_build_report_defaults_unchanged(self, monkeypatch, tmp_path):
        """No banner, default dir logic, unfresh client — the journal path."""
        from app.services.journal import reporting

        client_kwargs = {}

        class _Client:
            def __init__(self, *a, **k):
                client_kwargs.update(k)

            def submissions(self, ticker):
                return {}

        snapshot = SimpleNamespace(
            dataset=SimpleNamespace(documents=[]),
            diagnostics=SimpleNamespace(coverage=lambda: 0.9, warnings=[]),
            company_facts={},
        )
        monkeypatch.setattr(reporting, "SecClient", _Client)
        monkeypatch.setattr(reporting, "fetch_dataset_snapshot",
                            lambda t, n_quarters, client: snapshot)
        monkeypatch.setattr(reporting, "analyze", lambda ds: SimpleNamespace(overall=None))
        monkeypatch.setattr(reporting, "build_full_report", lambda *a, **k: ("BODY", None))
        monkeypatch.setattr(reporting, "REPORTS", tmp_path)

        out, _ = reporting.build_report("nvda", with_docs=False)
        assert client_kwargs.get("fresh") is False
        assert out.parent == tmp_path
        assert out.read_text() == "BODY"


class TestPollRearm:
    """A completed event re-arms the row; anything retryable or hypothetical
    leaves the identity in place."""

    def test_journal_track_success_rearms(self, poll_env, monkeypatch):
        _force_decision(monkeypatch, "generate")
        assert watch_cli.cmd_poll(_poll_args()) == 0
        assert poll_env.rearm == [("NVDA", "generate")]

    def test_auto_track_success_rearms(self, poll_env, monkeypatch):
        _force_decision(monkeypatch, "refuse")
        assert watch_cli.cmd_poll(_poll_args()) == 0
        assert poll_env.rearm == [("NVDA", "refuse")]

    def test_skip_rearms(self, poll_env, monkeypatch):
        _force_decision(monkeypatch, "skip")
        assert watch_cli.cmd_poll(_poll_args()) == 0
        assert poll_env.rearm == [("NVDA", "skip")]

    def test_failed_audit_keeps_the_identity_for_retry(self, poll_env, monkeypatch):
        _force_decision(monkeypatch, "generate")
        monkeypatch.setattr(watch_cli, "_run_audit", lambda p: 7)
        assert watch_cli.cmd_poll(_poll_args()) == 4
        assert poll_env.rearm == []

    def test_failed_generation_returns_its_code_and_audits_nothing(self, poll_env, monkeypatch):
        _force_decision(monkeypatch, "generate")
        monkeypatch.setattr(watch_cli, "_generate", lambda t, day, nd: 1)
        assert watch_cli.cmd_poll(_poll_args()) == 1
        assert poll_env.audit == [] and poll_env.marked == [] and poll_env.rearm == []

    def test_rearm_failure_on_poll_is_exit_1(self, poll_env, monkeypatch, capsys):
        _force_decision(monkeypatch, "refuse")
        monkeypatch.setattr(watch_cli, "_rearm", lambda w, d, s: False)
        assert watch_cli.cmd_poll(_poll_args()) == 1
        assert poll_env.generate_auto == ["NVDA"]  # the case itself completed

    def test_completed_mapping(self):
        skip, gen, ref, wait = (Decision(a, "") for a in ("skip", "generate", "refuse", "wait"))
        assert watch_cli._completed(skip, 4)
        assert watch_cli._completed(gen, 0) and watch_cli._completed(ref, 0)
        assert not any(watch_cli._completed(d, rc) for d in (gen, ref) for rc in (1, 2, 4))
        assert not watch_cli._completed(wait, 0)

    def test_latest_report_never_returns_the_audit(self, tmp_path):
        import os, time as _t

        rep = tmp_path / "NVDA_2026-09-01.md"
        rep.write_text("# report")
        aud = tmp_path / "NVDA_2026-09-01_audit.md"
        aud.write_text("# audit")
        later = _t.time() + 10
        os.utime(aud, (later, later))  # the audit is the newer file
        assert watch_cli._latest_report("NVDA", tmp_path) == rep
        assert watch_cli._latest_report("AAPL", tmp_path) is None

    def test_strict_refusal_does_not_rearm(self, poll_env, monkeypatch):
        _force_decision(monkeypatch, "refuse")
        assert watch_cli.cmd_poll(_poll_args(no_auto=True)) == 2
        assert poll_env.rearm == []

    def test_dry_run_does_not_rearm(self, poll_env, monkeypatch):
        _force_decision(monkeypatch, "generate")
        assert watch_cli.cmd_poll(_poll_args(dry_run=True)) == 0
        assert poll_env.rearm == []

    def test_adhoc_poll_has_no_row_to_rearm(self, poll_env, monkeypatch):
        _force_decision(monkeypatch, "refuse")
        monkeypatch.setattr(watch_cli, "_find_watch", lambda t: None)
        assert watch_cli.cmd_poll(_poll_args(since="2026-08-26")) == 0
        assert poll_env.generate_auto == ["NVDA"]
        assert poll_env.rearm == []


_EMPTY_SUBS = {"filings": {"recent": {"form": [], "accessionNumber": [], "filingDate": []}}}


class TestRearmPersistence:
    def test_rearm_writes_the_next_identity_and_clears_the_pin(self, monkeypatch, tmp_path):
        import json

        p = tmp_path / "watchlist.json"
        p.write_text(json.dumps({"watchlist": [{
            "ticker": "NVDA", "label": "FQ3-27", "print_at": "2026-11-18T20:20:00+00:00",
            "baseline_accession": "q-1", "expected_report_date": "2026-10-25",
            "thesis_entry": "2026-11-17", "thesis_sha256": "ab" * 32,
        }]}))
        monkeypatch.setattr(watch_cli.wl, "WATCHLIST", p)
        watch = watch_cli.wl.load(p)[0]
        from app.services.watch.poller import Filing
        filing = Filing(form="10-Q", accession="q-0", filing_date=watch_cli.date(2026, 11, 18),
                        report_date=watch_cli.date(2026, 10, 25))
        watch_cli._rearm(watch, Decision("generate", "x", filing=filing), _EMPTY_SUBS)
        after = watch_cli.wl.load(p)[0]
        assert after.baseline_accession == "q-0"
        assert after.expected_report_date > watch.expected_report_date
        assert after.thesis_entry is None and after.label is None
        assert "re-armed" in after.note

    def test_rearm_malformed_payload_is_reported_not_raised(self, capsys):
        watch = watch_cli.wl.Watch(
            ticker="NVDA", print_at=watch_cli._now("2026-11-18T20:20:00+00:00"),
            baseline_accession="q-1", expected_report_date=watch_cli.date(2026, 10, 25),
        )
        watch_cli._rearm(watch, Decision("refuse", "x"), {"filings": {"recent": {}}})
        assert "re-arm FAILED" in capsys.readouterr().err

    def test_rearm_persistence_failure_is_reported_not_raised(self, monkeypatch, capsys):
        def boom(*a, **k):
            raise watch_cli.wl.WatchlistError("disk says no")
        monkeypatch.setattr(watch_cli.wl, "update_entry", boom)
        watch = watch_cli.wl.Watch(
            ticker="NVDA", print_at=watch_cli._now("2026-11-18T20:20:00+00:00"),
            baseline_accession="q-1", expected_report_date=watch_cli.date(2026, 10, 25),
        )
        watch_cli._rearm(watch, Decision("refuse", "x"), _EMPTY_SUBS)
        assert "re-arm FAILED" in capsys.readouterr().err


def _sweep_args(**over) -> Namespace:
    base = dict(portfolio=None, prune=False, dry_run=False, no_docs=False,
                no_auto=False, no_audit=False, no_brief=False, no_vintage=False,
                verbose=False)
    base.update(over)
    return Namespace(**base)


def _watch(ticker: str, **over) -> "watch_cli.wl.Watch":
    base = dict(
        ticker=ticker, print_at=watch_cli._now("2026-10-29T20:30:00+00:00"),
        baseline_accession="acc-1", expected_report_date=watch_cli.date(2026, 9, 26),
    )
    base.update(over)
    return watch_cli.wl.Watch(**base)


@pytest.fixture
def sweep_env(poll_env, monkeypatch, tmp_path):
    """poll_env's stubs, plus a three-name watchlist and a per-ticker
    decision table. The sweep lock lives in tmp so tests never contend with
    a real sweep."""
    monkeypatch.setattr(watch_cli, "SWEEP_LOCK", tmp_path / "sweep.lock")
    # Pin the sweep's clock before the fixture rows' print hint so "overdue"
    # can never depend on the day the tests happen to run.
    monkeypatch.setattr(watch_cli, "_utcnow", lambda: watch_cli._now("2026-10-01T00:00:00+00:00"))
    monkeypatch.setattr(
        watch_cli.wl, "load",
        lambda path=None: [_watch("AAPL"), _watch("MSFT"), _watch("NVDA")],
    )
    table: dict[str, object] = {}

    def decide(watch, submissions, since=None, force=False):
        what = table.get(watch.ticker, "wait")
        if isinstance(what, Exception):
            raise what
        return Decision(what, f"{watch.ticker}: forced {what}")

    monkeypatch.setattr(watch_cli, "decide", decide)
    poll_env.table = table
    return poll_env


class TestSweep:
    def test_visits_every_name_and_waiting_is_not_failure(self, sweep_env, capsys):
        assert watch_cli.cmd_sweep(_sweep_args()) == 0
        assert sweep_env.generate == [] and sweep_env.generate_auto == []
        assert "3 watched, 3 waiting" in capsys.readouterr().out

    def test_acts_on_landed_filings_and_rearms_each(self, sweep_env):
        sweep_env.table.update({"AAPL": "refuse", "NVDA": "generate"})
        assert watch_cli.cmd_sweep(_sweep_args()) == 0
        assert sweep_env.generate_auto == ["AAPL"]
        assert sweep_env.generate == [("NVDA", None)]
        assert sorted(sweep_env.rearm) == [("AAPL", "refuse"), ("NVDA", "generate")]

    def test_one_failure_does_not_stop_the_sweep(self, sweep_env, capsys):
        sweep_env.table.update({
            "AAPL": watch_cli.PollerError("AAPL: watch has no event identity"),
            "NVDA": "refuse",
        })
        assert watch_cli.cmd_sweep(_sweep_args()) == 1
        assert sweep_env.generate_auto == ["NVDA"]  # reached despite AAPL failing
        assert "no event identity" in capsys.readouterr().err

    def test_worst_code_wins_and_failed_audit_is_not_rearmed(self, sweep_env, monkeypatch):
        sweep_env.table.update({"AAPL": "refuse", "MSFT": "generate"})
        monkeypatch.setattr(watch_cli, "_run_audit",
                            lambda p: 7 if p == Path("/tmp/fake_journal.md") else 0)
        assert watch_cli.cmd_sweep(_sweep_args()) == 4
        assert sweep_env.rearm == [("AAPL", "refuse")]

    def test_strict_mode_surfaces_refusals(self, sweep_env):
        sweep_env.table["AAPL"] = "refuse"
        assert watch_cli.cmd_sweep(_sweep_args(no_auto=True)) == 2
        assert sweep_env.generate_auto == [] and sweep_env.rearm == []

    def test_dry_run_neither_generates_nor_rearms(self, sweep_env):
        sweep_env.table.update({"AAPL": "refuse", "NVDA": "generate"})
        assert watch_cli.cmd_sweep(_sweep_args(dry_run=True)) == 0
        assert sweep_env.generate == [] and sweep_env.generate_auto == []
        assert sweep_env.rearm == []

    def test_concurrent_sweep_yields(self, sweep_env, capsys):
        import fcntl

        with open(watch_cli.SWEEP_LOCK, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            assert watch_cli.cmd_sweep(_sweep_args()) == 0
        assert "another sweep or poll is acting" in capsys.readouterr().out

    def test_syncs_the_portfolio_first(self, sweep_env, monkeypatch, tmp_path):
        synced = []
        monkeypatch.setattr(watch_cli, "_sync",
                            lambda client, path, prune, dry_run=False:
                            synced.append((path, prune, dry_run)) or 0)
        pf = tmp_path / "portfolio.txt"
        pf.write_text("AAPL\n")
        assert watch_cli.cmd_sweep(_sweep_args(portfolio=str(pf), prune=True)) == 0
        assert synced == [(pf, True, False)]

    def test_sync_failure_is_reported_in_the_exit_code(self, sweep_env, monkeypatch):
        monkeypatch.setattr(watch_cli, "_sync", lambda client, path, prune, dry_run=False: 1)
        assert watch_cli.cmd_sweep(_sweep_args(portfolio="x.txt")) == 1

    def test_sync_failure_survives_names_that_acted(self, sweep_env, monkeypatch):
        # Worst code across BOTH the sync and the per-name results: a name
        # completing cleanly (0) must not mask a failed sync (1), and a failed
        # audit (4) still outranks it.
        monkeypatch.setattr(watch_cli, "_sync", lambda client, path, prune, dry_run=False: 1)
        sweep_env.table["AAPL"] = "refuse"
        assert watch_cli.cmd_sweep(_sweep_args(portfolio="x.txt")) == 1
        # ...and a setup failure (1) is ranked above a failed audit (4).
        monkeypatch.setattr(watch_cli, "_run_audit", lambda p: 7)
        assert watch_cli.cmd_sweep(_sweep_args(portfolio="x.txt")) == 1

    def test_rearm_failure_is_exit_1_after_a_completed_case(self, sweep_env, monkeypatch, capsys):
        # The report and audit exist, but the row still names the consumed
        # event and will never fire again: a scheduler must not see 0.
        sweep_env.table["AAPL"] = "refuse"

        def boom(watch, decision, submissions):
            raise OSError("disk full")

        monkeypatch.setattr(watch_cli, "_rearm", boom)
        assert watch_cli.cmd_sweep(_sweep_args()) == 1
        assert sweep_env.generate_auto == ["AAPL"]
        assert "re-arm FAILED for AAPL" in capsys.readouterr().err

    def test_overdue_waiting_name_is_named_without_verbose(self, sweep_env, monkeypatch, capsys):
        # Rows print 2026-10-29; 30 days later with nothing filed the name is
        # called out on stderr — a mis-armed row must not hide behind "waiting".
        monkeypatch.setattr(
            watch_cli, "_utcnow", lambda: watch_cli._now("2026-11-29T00:00:00+00:00"))
        assert watch_cli.cmd_sweep(_sweep_args()) == 0
        err = capsys.readouterr().err
        assert "AAPL: still waiting 30d past its print hint" in err
        assert "expected period 2026-09-26" in err

    def test_waiting_before_the_print_is_quiet(self, sweep_env, capsys):
        assert watch_cli.cmd_sweep(_sweep_args()) == 0
        assert "still waiting" not in capsys.readouterr().err


class TestPortfolioSync:
    def test_read_portfolio_tolerates_csv_and_comments(self, tmp_path):
        pf = tmp_path / "p.csv"
        pf.write_text('# holdings\n"NVDA",10\naapl 5  # dup below\nNVDA\n\nmxl\n')
        assert watch_cli.read_portfolio(pf) == ["NVDA", "AAPL", "MXL"]

    def test_missing_portfolio_is_an_error(self, tmp_path):
        with pytest.raises(watch_cli.wl.WatchlistError, match="not found"):
            watch_cli.read_portfolio(tmp_path / "nope.txt")

    @pytest.fixture
    def sync_env(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            watch_cli.wl, "load",
            lambda path=None: [_watch("AAPL"), _watch("MXL", thesis_entry="2026-10-01",
                                                       thesis_sha256="ab" * 32),
                               _watch("GLW")],
        )
        armed, removed = [], []
        monkeypatch.setattr(watch_cli, "_arm",
                            lambda t, subs, **k: armed.append(t) or _watch(t))
        monkeypatch.setattr(watch_cli.wl, "remove_entry",
                            lambda t, path=None: removed.append(t))
        pf = tmp_path / "portfolio.txt"
        pf.write_text("AAPL\nNVDA\nAMKR\n")
        return SimpleNamespace(armed=armed, removed=removed, portfolio=pf)

    def test_adds_only_the_missing_holdings(self, sync_env, capsys):
        rc = watch_cli._sync(_FakeClient(), sync_env.portfolio, prune=False)
        assert rc == 0
        assert sync_env.armed == ["NVDA", "AMKR"]
        assert sync_env.removed == []
        out = capsys.readouterr().out
        assert "MXL is watched but not in portfolio.txt (keep" in out

    def test_prune_removes_unpinned_only(self, sync_env, capsys):
        rc = watch_cli._sync(_FakeClient(), sync_env.portfolio, prune=True)
        assert rc == 0
        assert sync_env.removed == ["GLW"]  # MXL has a thesis in flight
        assert "MXL not in portfolio.txt but has a pinned thesis — kept" in capsys.readouterr().out

    def test_one_unarmable_name_is_reported_and_skipped(self, sync_env, monkeypatch, capsys):
        def arm(t, subs, **k):
            if t == "AMKR":
                raise watch_cli.wl.WatchlistError("AMKR: cannot infer the print date")
            sync_env.armed.append(t)
            return _watch(t)
        monkeypatch.setattr(watch_cli, "_arm", arm)
        rc = watch_cli._sync(_FakeClient(), sync_env.portfolio, prune=False)
        assert rc == 1
        assert sync_env.armed == ["NVDA"]
        assert "AMKR NOT added" in capsys.readouterr().err


class TestBriefHook:
    def test_brief_runs_after_a_successful_audit_on_both_tracks(self, poll_env, monkeypatch):
        _force_decision(monkeypatch, "generate")
        assert watch_cli.cmd_poll(_poll_args()) == 0
        assert poll_env.brief == [("NVDA", Path("/tmp/fake_journal.md"))]
        _force_decision(monkeypatch, "refuse")
        assert watch_cli.cmd_poll(_poll_args()) == 0
        assert poll_env.brief[-1] == ("NVDA", Path("/tmp/fake_auto.md"))

    def test_brief_skipped_without_audit_or_with_no_brief(self, poll_env, monkeypatch):
        _force_decision(monkeypatch, "generate")
        assert watch_cli.cmd_poll(_poll_args(no_audit=True)) == 0
        assert watch_cli.cmd_poll(_poll_args(no_brief=True)) == 0
        assert poll_env.brief == []

    def test_brief_not_run_after_a_failed_audit(self, poll_env, monkeypatch):
        _force_decision(monkeypatch, "refuse")
        monkeypatch.setattr(watch_cli, "_run_audit", lambda p: 7)
        assert watch_cli.cmd_poll(_poll_args()) == 4
        assert poll_env.brief == []

    def test_brief_failure_is_exit_5_but_the_case_still_completes(self, poll_env, monkeypatch):
        # Report, audit, mark and re-arm all happen; only the brief is queued.
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, p: 2)
        _force_decision(monkeypatch, "generate")
        assert watch_cli.cmd_poll(_poll_args()) == 5
        assert poll_env.marked and poll_env.rearm == [("NVDA", "generate")]
        _force_decision(monkeypatch, "refuse")
        assert watch_cli.cmd_poll(_poll_args()) == 5
        assert poll_env.rearm[-1] == ("NVDA", "refuse")

    def test_mark_failure_outranks_a_queued_brief(self, poll_env, monkeypatch):
        _force_decision(monkeypatch, "generate")
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, p: 2)
        monkeypatch.setattr(watch_cli, "_mark_reported", lambda t, day: 1)
        assert watch_cli.cmd_poll(_poll_args()) == 1
        assert poll_env.rearm == []  # not completed

    def test_run_brief_queues_on_failure_and_clears_on_success(self, monkeypatch, tmp_path, capsys):
        from types import SimpleNamespace

        monkeypatch.setattr(watch_cli, "BRIEF_PENDING", tmp_path / "pending")
        report = tmp_path / "NVDA_2026-09-01.md"
        report.write_text("# r")
        rcs = iter([2, 0])
        seen = []
        monkeypatch.setattr(watch_cli.subprocess, "run",
                            lambda cmd, **k: seen.append(cmd) or SimpleNamespace(returncode=next(rcs)))
        assert watch_cli._run_brief("NVDA", report) == 2
        marker = tmp_path / "pending" / "NVDA"
        assert watch_cli._queue_read("NVDA") == (str(report), 1, "")
        assert "queued at" in capsys.readouterr().err
        assert seen[0][1:] == [str(watch_cli.ROOT / "scripts" / "earnings_brief.py"),
                               "build", "NVDA", "--report", str(report)]
        assert watch_cli._run_brief("NVDA", report) == 0
        assert not marker.exists()

    def test_retry_pending_brief(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(watch_cli, "BRIEF_PENDING", tmp_path / "pending")
        assert watch_cli._retry_pending_brief("NVDA") == 0  # nothing queued
        report = tmp_path / "NVDA_2026-09-01.md"
        report.write_text("# r")
        (tmp_path / "pending").mkdir()
        marker = tmp_path / "pending" / "NVDA"
        marker.write_text(f"{report}\n")
        ran = []
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, p: ran.append((t, p)) or 2)
        assert watch_cli._retry_pending_brief("NVDA") == 5
        assert ran == [("NVDA", report)]
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, p: 0)
        assert watch_cli._retry_pending_brief("NVDA") == 0
        # A queue entry whose report vanished is dropped, not retried forever.
        marker.write_text(str(tmp_path / "gone.md"))
        assert watch_cli._retry_pending_brief("NVDA") == 0
        assert not marker.exists()
        assert "no longer exists" in capsys.readouterr().err

    def test_sweep_retries_queued_briefs_before_deciding(self, sweep_env, monkeypatch, tmp_path, capsys):
        report = tmp_path / "AAPL_2026-09-01.md"
        report.write_text("# r")
        watch_cli.BRIEF_PENDING.mkdir()
        (watch_cli.BRIEF_PENDING / "AAPL").write_text(str(report))
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, p: 2)  # still failing
        assert watch_cli.cmd_sweep(_sweep_args()) == 5  # AAPL waiting, but its brief is still queued
        assert "AAPL -> 5" in capsys.readouterr().out
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, p: 0)  # fixed (login restored)
        assert watch_cli.cmd_sweep(_sweep_args()) == 0

    def test_queue_is_not_retried_in_the_pass_that_rebuilds_anyway(self, sweep_env, monkeypatch, tmp_path):
        # A queued print-night brief ("-") and the 10-Q landing in the same
        # pass: one build, with the report — never a --no-report retry first.
        watch_cli.BRIEF_PENDING.mkdir()
        (watch_cli.BRIEF_PENDING / "AAPL").write_text("-\n")
        built = []
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, r: built.append((t, r)) or 0)
        sweep_env.table["AAPL"] = "refuse"
        assert watch_cli.cmd_sweep(_sweep_args()) == 0
        assert built == [("AAPL", Path("/tmp/fake_auto.md"))]

    def test_sweep_dry_run_and_no_brief_leave_the_queue_alone(self, sweep_env, monkeypatch, tmp_path):
        report = tmp_path / "AAPL_2026-09-01.md"
        report.write_text("# r")
        watch_cli.BRIEF_PENDING.mkdir()
        (watch_cli.BRIEF_PENDING / "AAPL").write_text(str(report))
        monkeypatch.setattr(watch_cli, "_run_brief",
                            lambda t, p: pytest.fail("must not retry"))
        assert watch_cli.cmd_sweep(_sweep_args(dry_run=True)) == 0
        assert watch_cli.cmd_sweep(_sweep_args(no_brief=True)) == 0

    def test_queued_brief_retry_crash_is_contained(self, sweep_env, monkeypatch, tmp_path, capsys):
        # The retry runs before the per-name try block; a disk error there
        # must not abort the pass for every name after it.
        report = tmp_path / "AAPL_2026-09-01.md"
        report.write_text("# r")
        watch_cli.BRIEF_PENDING.mkdir()
        (watch_cli.BRIEF_PENDING / "AAPL").write_text(str(report))

        def boom(t, p):
            if t == "AAPL":  # the queued retry crashes; NVDA's own brief is fine
                raise OSError("Disk quota exceeded")
            return 0
        monkeypatch.setattr(watch_cli, "_run_brief", boom)
        sweep_env.table["NVDA"] = "refuse"
        assert watch_cli.cmd_sweep(_sweep_args()) == 5
        assert sweep_env.generate_auto == ["NVDA"]  # the pass reached NVDA
        assert "queued brief retry crashed" in capsys.readouterr().err

    def test_sweep_aggregate_ranks_by_severity_not_number(self, sweep_env, monkeypatch, tmp_path):
        # AAPL's audit fails (4) while MSFT's brief is queued (5): the pass
        # says 4 — an audit failure is the louder problem.
        sweep_env.table.update({"AAPL": "refuse", "MSFT": "refuse"})
        audits = iter([7, 0])  # AAPL's audit fails, MSFT's succeeds
        monkeypatch.setattr(watch_cli, "_run_audit", lambda p: next(audits))
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, p: 2)
        assert watch_cli.cmd_sweep(_sweep_args()) == 4
        assert watch_cli._worst([0, 5]) == 5 and watch_cli._worst([5, 2]) == 2
        assert watch_cli._worst([2, 4, 5]) == 4 and watch_cli._worst([4, 1]) == 1
        assert watch_cli._worst([3, 0]) == 0 and watch_cli._worst([]) == 0

    def test_queued_brief_does_not_mask_a_failed_audit(self, sweep_env, monkeypatch, tmp_path):
        report = tmp_path / "AAPL_2026-09-01.md"
        report.write_text("# r")
        watch_cli.BRIEF_PENDING.mkdir()
        (watch_cli.BRIEF_PENDING / "AAPL").write_text(str(report))
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, p: 2)
        monkeypatch.setattr(watch_cli, "_run_audit", lambda p: 7)
        sweep_env.table["AAPL"] = "refuse"
        assert watch_cli.cmd_sweep(_sweep_args()) == 4


class TestPrintNightBrief:
    """The earnings 8-K fires a brief-only pass; the 10-Q track is untouched."""

    @staticmethod
    def _k(monkeypatch, filed="2026-10-13", acc="k-1"):
        from app.services.watch.poller import Filing

        monkeypatch.setattr(
            watch_cli, "latest_earnings_8k",
            lambda subs: Filing("8-K", acc, watch_cli.date.fromisoformat(filed), items="2.02,9.01"))

    def test_fires_once_on_a_fresh_8k_while_the_10q_is_awaited(self, sweep_env, monkeypatch, capsys):
        self._k(monkeypatch)
        monkeypatch.setattr(watch_cli, "_utcnow", lambda: watch_cli._now("2026-10-13T22:05:00+00:00"))
        built = []

        def run_brief(t, report):
            built.append((t, report))
            (watch_cli.BRIEFS / f"{t}_2026-10-13.md").parent.mkdir(parents=True, exist_ok=True)
            (watch_cli.BRIEFS / f"{t}_2026-10-13.md").write_text("# brief")
            return 0
        monkeypatch.setattr(watch_cli, "_run_brief", run_brief)
        assert watch_cli.cmd_sweep(_sweep_args()) == 0
        assert built == [("AAPL", None), ("MSFT", None), ("NVDA", None)]  # every name is "waiting"
        assert "print-night brief" in capsys.readouterr().out
        assert watch_cli.cmd_sweep(_sweep_args()) == 0
        assert len(built) == 3  # the brief on disk is the idempotency key
        assert sweep_env.rearm == []  # the 10-Q track did not move

    def test_stale_8k_is_last_quarters_news(self, sweep_env, monkeypatch):
        self._k(monkeypatch, filed="2026-08-26")
        monkeypatch.setattr(watch_cli, "_utcnow", lambda: watch_cli._now("2026-10-01T00:00:00+00:00"))
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, r: pytest.fail("must not build"))
        assert watch_cli.cmd_sweep(_sweep_args()) == 0

    def test_failure_is_queued_as_5_and_not_rebuilt_while_queued(self, sweep_env, monkeypatch):
        self._k(monkeypatch)
        monkeypatch.setattr(watch_cli, "_utcnow", lambda: watch_cli._now("2026-10-13T22:05:00+00:00"))
        calls = []

        def failing(t, report):
            calls.append(t)
            watch_cli.BRIEF_PENDING.mkdir(parents=True, exist_ok=True)
            (watch_cli.BRIEF_PENDING / t).write_text(f"{watch_cli.NO_REPORT_MARK}\n")
            return 2
        monkeypatch.setattr(watch_cli, "_run_brief", failing)
        assert watch_cli.cmd_sweep(_sweep_args()) == 5
        assert calls == ["AAPL", "MSFT", "NVDA"]
        # next pass: the queue retry runs it (once per name), the trigger does not add a second
        assert watch_cli.cmd_sweep(_sweep_args()) == 5
        assert calls == ["AAPL", "MSFT", "NVDA"] * 2

    def test_retry_of_a_queued_print_night_brief_passes_no_report(self, monkeypatch, tmp_path):
        monkeypatch.setattr(watch_cli, "BRIEF_PENDING", tmp_path / "pending")
        (tmp_path / "pending").mkdir()
        (tmp_path / "pending" / "NVDA").write_text("-\n")
        ran = []
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, r: ran.append((t, r)) or 0)
        assert watch_cli._retry_pending_brief("NVDA") == 0
        assert ran == [("NVDA", None)]

    def test_run_brief_without_report_uses_no_report_and_queues_a_dash(self, monkeypatch, tmp_path):
        from types import SimpleNamespace

        monkeypatch.setattr(watch_cli, "BRIEF_PENDING", tmp_path / "pending")
        seen = []
        monkeypatch.setattr(watch_cli.subprocess, "run",
                            lambda cmd, **k: seen.append(cmd) or SimpleNamespace(returncode=2))
        assert watch_cli._run_brief("NVDA", None) == 2
        assert seen[0][-3:] == ["build", "NVDA", "--no-report"]
        assert watch_cli._queue_read("NVDA") == ("-", 1, "")
        assert watch_cli._run_brief("NVDA", None) == 2  # same target: attempts climb
        assert watch_cli._queue_read("NVDA") == ("-", 2, "")

    def test_retry_cap_drops_a_hopeless_print_night_brief(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(watch_cli, "BRIEF_PENDING", tmp_path / "pending")
        watch_cli._queue_write("NVDA", "-", watch_cli.PRINT_BRIEF_MAX_ATTEMPTS)
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, r: pytest.fail("must not run"))
        assert watch_cli._retry_pending_brief("NVDA") == 5  # says so once
        assert "giving up" in capsys.readouterr().err
        assert watch_cli._queue_read("NVDA") is None
        assert watch_cli._retry_pending_brief("NVDA") == 0
        # A queued FULL brief past the cap is kept and reported, never re-run
        # (see TestBriefRetryCap); below the cap it still retries.
        report = tmp_path / "NVDA_2026-09-01.md"
        report.write_text("# r")
        watch_cli._queue_write("NVDA", str(report), 1)
        ran = []
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, r: ran.append(r) or 0)
        assert watch_cli._retry_pending_brief("NVDA") == 0 and ran == [report]

    def test_crash_in_the_trigger_leaves_an_honest_queue_entry(self, sweep_env, monkeypatch, capsys):
        self._k(monkeypatch)
        monkeypatch.setattr(watch_cli, "_utcnow", lambda: watch_cli._now("2026-10-13T22:05:00+00:00"))

        def boom(t, r):
            raise OSError("disk")
        monkeypatch.setattr(watch_cli, "_run_brief", boom)
        assert watch_cli.cmd_sweep(_sweep_args()) == 5
        assert watch_cli._queue_read("AAPL") == ("-", 1, "")
        assert "print-night brief crashed" in capsys.readouterr().err

    def test_window_boundary_is_inclusive_at_14_days(self, sweep_env, monkeypatch):
        self._k(monkeypatch, filed="2026-10-01")
        built = []
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, r: built.append(t) or 0)
        monkeypatch.setattr(watch_cli, "_utcnow", lambda: watch_cli._now("2026-10-15T23:59:00+00:00"))
        assert watch_cli.cmd_sweep(_sweep_args()) == 0 and built == ["AAPL", "MSFT", "NVDA"]
        built.clear()
        monkeypatch.setattr(watch_cli, "_utcnow", lambda: watch_cli._now("2026-10-16T00:01:00+00:00"))
        assert watch_cli.cmd_sweep(_sweep_args()) == 0 and built == []

    def test_strict_no_auto_mode_builds_no_brief_either(self, sweep_env, monkeypatch):
        self._k(monkeypatch)
        monkeypatch.setattr(watch_cli, "_utcnow", lambda: watch_cli._now("2026-10-13T22:05:00+00:00"))
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, r: pytest.fail("must not build"))
        assert watch_cli.cmd_sweep(_sweep_args(no_auto=True)) == 0

    def test_failed_audit_still_gets_a_print_night_brief_and_stays_4(self, sweep_env, monkeypatch):
        self._k(monkeypatch)
        monkeypatch.setattr(watch_cli, "_utcnow", lambda: watch_cli._now("2026-10-13T22:05:00+00:00"))
        built = []
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, r: built.append((t, r)) or 0)
        monkeypatch.setattr(watch_cli, "_run_audit", lambda p: 7)
        sweep_env.table["NVDA"] = "refuse"
        assert watch_cli.cmd_sweep(_sweep_args()) == 4
        assert ("NVDA", None) in built  # the release is news tonight; the audit retries

    def test_act_crash_skips_the_trigger(self, sweep_env, monkeypatch):
        self._k(monkeypatch)
        monkeypatch.setattr(watch_cli, "_utcnow", lambda: watch_cli._now("2026-10-13T22:05:00+00:00"))
        built = []
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, r: built.append(t) or 0)

        def crash(*a, **k):
            raise RuntimeError("x")
        monkeypatch.setattr(watch_cli, "_act", crash)
        sweep_env.table["NVDA"] = "refuse"
        assert watch_cli.cmd_sweep(_sweep_args()) == 1
        assert "NVDA" not in built and built == ["AAPL", "MSFT"]

    def test_same_day_second_8k_rebuilds_a_print_night_brief_but_never_a_full_one(
            self, sweep_env, monkeypatch, capsys):
        import json

        monkeypatch.setattr(watch_cli, "_utcnow", lambda: watch_cli._now("2026-10-13T23:05:00+00:00"))
        built = []
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, r: built.append(t) or 0)
        for t in ("AAPL", "MSFT", "NVDA"):
            (watch_cli.BRIEFS / t / "2026-10-13").mkdir(parents=True)
            (watch_cli.BRIEFS / f"{t}_2026-10-13.md").write_text("# brief")
        # AAPL: print-night from the preliminary accession; MSFT: full; NVDA: no record
        (watch_cli.BRIEFS / "AAPL" / "2026-10-13" / "built.json").write_text(
            json.dumps({"kind": "print-night", "accession": "k-prelim"}))
        (watch_cli.BRIEFS / "MSFT" / "2026-10-13" / "built.json").write_text(
            json.dumps({"kind": "full", "accession": "k-prelim"}))
        self._k(monkeypatch, acc="k-final")
        assert watch_cli.cmd_sweep(_sweep_args()) == 0
        assert built == ["AAPL"]
        assert "superseded k-prelim" in capsys.readouterr().out
        self._k(monkeypatch, acc="k-prelim")  # same accession as before: nothing
        built.clear()
        assert watch_cli.cmd_sweep(_sweep_args()) == 0 and built == []
        # A queued marker wins over the supersede: the queue's own retry runs
        # (once), the trigger does not add a second, and nothing claims
        # "rebuilding" for a rebuild that did not happen.
        self._k(monkeypatch, acc="k-final2")
        watch_cli._queue_write("AAPL", "-", 1)
        capsys.readouterr()
        assert watch_cli.cmd_sweep(_sweep_args()) == 0 and built == ["AAPL"]
        assert "superseded" not in capsys.readouterr().out

    def test_real_payload_reaches_the_trigger_end_to_end(self, sweep_env, monkeypatch):
        # No _k patch: the sweep's own submissions flow through the real
        # latest_earnings_8k (8-K/A excluded, newest 2.02 wins).
        rec = {
            "form": ["8-K/A", "8-K", "10-Q", "8-K"],
            "accessionNumber": ["k-a", "k-new", "q-1", "k-old"],
            "filingDate": ["2026-10-14", "2026-10-13", "2026-08-05", "2026-07-30"],
            "reportDate": ["2026-10-13", "2026-10-13", "2026-06-27", "2026-07-30"],
            "items": ["2.02,9.01", "2.02,9.01", None, "2.02,9.01"],
            "acceptanceDateTime": [None] * 4, "primaryDocument": [None] * 4,
        }

        class Client(_FakeClient):
            def submissions_by_cik(self, cik):
                return {"filings": {"recent": rec}}
        monkeypatch.setattr(watch_cli, "SecClient", Client)
        monkeypatch.setattr(watch_cli, "_utcnow", lambda: watch_cli._now("2026-10-14T22:05:00+00:00"))
        built = []
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, r: built.append(t) or 0)
        assert watch_cli.cmd_sweep(_sweep_args(verbose=True)) == 0
        assert built == ["AAPL", "MSFT", "NVDA"]
        # the key is the ORIGINAL 8-K's date, not the amendment's
        for t in built:
            assert not (watch_cli.BRIEFS / f"{t}_2026-10-14.md").exists()

    def test_dry_run_reports_but_does_not_build(self, sweep_env, monkeypatch, capsys):
        self._k(monkeypatch)
        monkeypatch.setattr(watch_cli, "_utcnow", lambda: watch_cli._now("2026-10-13T22:05:00+00:00"))
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, r: pytest.fail("must not build"))
        assert watch_cli.cmd_sweep(_sweep_args(dry_run=True)) == 0
        assert "dry run — not building" in capsys.readouterr().out

    def test_no_brief_flag_and_a_crash_are_contained(self, sweep_env, monkeypatch, capsys):
        self._k(monkeypatch)
        monkeypatch.setattr(watch_cli, "_utcnow", lambda: watch_cli._now("2026-10-13T22:05:00+00:00"))
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, r: pytest.fail("must not build"))
        assert watch_cli.cmd_sweep(_sweep_args(no_brief=True)) == 0

        def boom(subs):
            raise OSError("disk")
        monkeypatch.setattr(watch_cli, "latest_earnings_8k", boom)
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, r: 0)  # NVDA's own brief is fine
        sweep_env.table["NVDA"] = "refuse"
        assert watch_cli.cmd_sweep(_sweep_args()) == 5
        assert sweep_env.generate_auto == ["NVDA"]  # the pass still acted
        assert "print-night brief crashed" in capsys.readouterr().err

    def test_same_pass_as_the_10q_builds_the_brief_once(self, sweep_env, monkeypatch):
        # The audit hook writes the brief; the print-night trigger then finds it.
        self._k(monkeypatch)
        monkeypatch.setattr(watch_cli, "_utcnow", lambda: watch_cli._now("2026-10-13T22:05:00+00:00"))
        built = []

        def run_brief(t, report):
            built.append((t, report))
            watch_cli.BRIEFS.mkdir(parents=True, exist_ok=True)
            (watch_cli.BRIEFS / f"{t}_2026-10-13.md").write_text("# brief")
            return 0
        monkeypatch.setattr(watch_cli, "_run_brief", run_brief)
        sweep_env.table["NVDA"] = "refuse"
        assert watch_cli.cmd_sweep(_sweep_args()) == 0
        assert built.count(("NVDA", Path("/tmp/fake_auto.md"))) == 1
        assert ("NVDA", None) not in built


class TestSweepNotifications:
    def test_clean_pass_is_silent_and_problems_are_one_notification(self, sweep_env, monkeypatch):
        sweep_env.table["AAPL"] = "refuse"
        assert watch_cli.cmd_sweep(_sweep_args()) == 0
        assert sweep_env.notified == []
        audits = iter([7])
        monkeypatch.setattr(watch_cli, "_run_audit", lambda p: next(audits, 0))
        monkeypatch.setattr(watch_cli, "_sync", lambda client, path, prune, dry_run=False: 1)
        assert watch_cli.cmd_sweep(_sweep_args(portfolio="x.txt")) == 1
        assert sweep_env.notified == [("FQE sweep needs attention",
                                       "portfolio sync: error; AAPL: audit FAILED")]

    def test_wording_for_refusal_and_queued_brief(self, sweep_env, monkeypatch):
        sweep_env.table["AAPL"] = "refuse"
        assert watch_cli.cmd_sweep(_sweep_args(no_auto=True)) == 2
        assert sweep_env.notified[-1][1] == "AAPL: refused (no thesis)"
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, p: 2)
        assert watch_cli.cmd_sweep(_sweep_args()) == 5
        assert sweep_env.notified[-1][1] == "AAPL: brief queued"

    def test_undelivered_notification_is_logged(self, sweep_env, monkeypatch, capsys):
        monkeypatch.setattr(watch_cli, "notify", lambda t, m: False)
        sweep_env.table["AAPL"] = watch_cli.PollerError("x")
        assert watch_cli.cmd_sweep(_sweep_args()) == 1
        assert "notification NOT delivered" in capsys.readouterr().err

    def test_dry_run_never_notifies(self, sweep_env, monkeypatch):
        sweep_env.table["AAPL"] = watch_cli.PollerError("x")
        assert watch_cli.cmd_sweep(_sweep_args(dry_run=True)) == 1
        assert sweep_env.notified == []


class TestPollSweepExclusion:
    """One activity lock across poll and sweep: a manual poll cannot
    duplicate a sweep's generate+audit of the same landed filing."""

    def test_poll_rechecks_under_the_lock_and_yields_if_consumed(self, poll_env, monkeypatch):
        # First decide (unlocked) says generate; the re-decide under the lock
        # — after a concurrent sweep consumed and re-armed the event — says
        # wait. Nothing generated, exit 0.
        answers = iter(["generate", "wait"])
        monkeypatch.setattr(
            watch_cli, "decide",
            lambda watch, submissions, since=None, force=False:
            Decision(next(answers), "x"),
        )
        assert watch_cli.cmd_poll(_poll_args()) == 0
        assert poll_env.generate == [] and poll_env.rearm == []

    def test_poll_rereads_the_row_before_acting(self, poll_env, monkeypatch):
        # Same event identity, but a `link` pinned a thesis while we waited
        # for the lock: the re-decide must see the pinned row.
        seen = []
        original = watch_cli._find_watch("NVDA")
        pinned = watch_cli.wl.Watch(
            ticker="NVDA", print_at=original.print_at,
            baseline_accession=original.baseline_accession,
            expected_report_date=original.expected_report_date,
            thesis_entry="2026-08-25", thesis_sha256="cd" * 32,
        )
        finds = iter([original, pinned])
        monkeypatch.setattr(watch_cli, "_find_watch", lambda t: next(finds))
        monkeypatch.setattr(
            watch_cli, "decide",
            lambda watch, submissions, since=None, force=False:
            seen.append(watch.thesis_entry) or Decision("generate", "x"),
        )
        assert watch_cli.cmd_poll(_poll_args()) == 0
        assert seen == ["2026-08-26", "2026-08-25"]
        assert poll_env.generate == [("NVDA", "2026-08-25")]  # acted on the re-read row

    def test_poll_treats_a_rearmed_row_as_consumed(self, poll_env, monkeypatch):
        original = watch_cli._find_watch("NVDA")
        rearmed = watch_cli.wl.Watch(
            ticker="NVDA", print_at=watch_cli._now("2027-02-10T20:20:00+00:00"),
            baseline_accession="new", expected_report_date=watch_cli.date(2027, 1, 24),
        )
        finds = iter([original, rearmed])
        monkeypatch.setattr(watch_cli, "_find_watch", lambda t: next(finds))
        _force_decision(monkeypatch, "refuse")
        assert watch_cli.cmd_poll(_poll_args()) == 0
        assert poll_env.generate_auto == [] and poll_env.rearm == []

    def test_poll_waits_for_a_running_sweep(self, poll_env, monkeypatch):
        import fcntl
        import threading

        _force_decision(monkeypatch, "refuse")
        fh = open(watch_cli.SWEEP_LOCK, "w")
        fcntl.flock(fh, fcntl.LOCK_EX)
        result = {}
        th = threading.Thread(target=lambda: result.update(rc=watch_cli.cmd_poll(_poll_args())))
        th.start()
        th.join(0.3)
        assert th.is_alive() and poll_env.generate_auto == []  # blocked, not skipped
        fcntl.flock(fh, fcntl.LOCK_UN)
        th.join(5)
        assert result["rc"] == 0 and poll_env.generate_auto == ["NVDA"]

    def test_poll_gives_up_on_the_lock_at_max_wait(self, poll_env, monkeypatch, capsys):
        import fcntl

        _force_decision(monkeypatch, "refuse")
        with open(watch_cli.SWEEP_LOCK, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            assert watch_cli.cmd_poll(_poll_args(max_wait=0.3)) == 1
        assert poll_env.generate_auto == []
        assert "held the activity lock" in capsys.readouterr().err

    def test_since_poll_racing_a_rearm_exits_0_not_1(self, poll_env, monkeypatch):
        # The row's identity moved while we waited for the lock: the
        # concurrent run consumed the event. With --since the re-decide
        # would raise the since/event mismatch (exit 1); the identity
        # change must be recognised first.
        original = watch_cli._find_watch("NVDA")
        rearmed = watch_cli.wl.Watch(
            ticker="NVDA", print_at=original.print_at,
            baseline_accession="0001045810-26-000075",
            expected_report_date=watch_cli.date(2026, 10, 25),
        )
        finds = iter([original, rearmed])
        monkeypatch.setattr(watch_cli, "_find_watch", lambda t: next(finds))
        calls = []

        def decide(watch, submissions, since=None, force=False):
            calls.append(watch.baseline_accession)
            if len(calls) > 1:
                raise watch_cli.PollerError("since/event mismatch")
            return Decision("refuse", "x")
        monkeypatch.setattr(watch_cli, "decide", decide)
        assert watch_cli.cmd_poll(_poll_args(since="2026-08-26")) == 0
        assert calls == ["0001045810-26-000052"] and poll_env.generate_auto == []

    def test_poll_rearm_crash_is_reported_not_raised(self, poll_env, monkeypatch, capsys):
        _force_decision(monkeypatch, "generate")

        def boom(watch, decision, submissions):
            raise OSError("disk full")
        monkeypatch.setattr(watch_cli, "_rearm", boom)
        # No traceback, the case itself completed — but the row still names
        # the consumed event, so the exit code says 1, not "all fine".
        assert watch_cli.cmd_poll(_poll_args()) == 1
        assert poll_env.marked
        assert "re-arm FAILED for NVDA" in capsys.readouterr().err

    def test_poll_treats_a_pruned_row_as_nothing_to_do(self, poll_env, monkeypatch):
        finds = iter([watch_cli._find_watch("NVDA"), None])
        monkeypatch.setattr(watch_cli, "_find_watch", lambda t: next(finds))
        _force_decision(monkeypatch, "refuse")
        assert watch_cli.cmd_poll(_poll_args()) == 0
        assert poll_env.generate_auto == [] and poll_env.rearm == []

    def test_dry_run_poll_never_takes_the_lock(self, poll_env, monkeypatch):
        import fcntl

        _force_decision(monkeypatch, "refuse")
        with open(watch_cli.SWEEP_LOCK, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            assert watch_cli.cmd_poll(_poll_args(dry_run=True)) == 0


class TestSweepRearmIsolation:
    def test_rearm_crash_does_not_stop_the_pass(self, sweep_env, monkeypatch, capsys):
        sweep_env.table.update({"AAPL": "refuse", "NVDA": "refuse"})

        def rearm(watch, decision, submissions):
            if watch.ticker == "AAPL":
                raise OSError("disk full")
            sweep_env.rearm.append((watch.ticker, decision.action))
            return True
        monkeypatch.setattr(watch_cli, "_rearm", rearm)
        # The pass reaches NVDA regardless; AAPL's stuck row makes the pass a 1.
        assert watch_cli.cmd_sweep(_sweep_args()) == 1
        assert sweep_env.generate_auto == ["AAPL", "NVDA"]
        assert sweep_env.rearm == [("NVDA", "refuse")]
        assert "re-arm FAILED for AAPL" in capsys.readouterr().err


class TestSyncDryRun:
    def test_dry_run_reports_and_writes_nothing(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(watch_cli.wl, "load", lambda path=None: [_watch("AAPL"), _watch("GLW")])
        monkeypatch.setattr(watch_cli, "_arm", lambda *a, **k: pytest.fail("must not arm"))
        monkeypatch.setattr(watch_cli.wl, "remove_entry",
                            lambda *a, **k: pytest.fail("must not remove"))
        pf = tmp_path / "portfolio.txt"
        pf.write_text("AAPL\nNVDA\n")
        rc = watch_cli._sync(_FakeClient(), pf, prune=True, dry_run=True)
        out = capsys.readouterr().out
        assert rc == 0
        assert "would add NVDA" in out and "would remove GLW" in out
        assert "sync (dry run)" in out

    def test_sweep_dry_run_makes_the_sync_dry(self, sweep_env, monkeypatch, tmp_path):
        synced = []
        monkeypatch.setattr(watch_cli, "_sync",
                            lambda client, path, prune, dry_run=False: synced.append(dry_run) or 0)
        pf = tmp_path / "p.txt"
        pf.write_text("AAPL\n")
        assert watch_cli.cmd_sweep(_sweep_args(portfolio=str(pf), dry_run=True)) == 0
        assert synced == [True]


class TestEventRoundTrip:
    """Invariant: once an event completes and the row is re-armed, the SAME
    submissions payload must decide `wait` — the consumed filing can never
    trigger again on the next pass."""

    def test_consumed_filing_cannot_retrigger(self, monkeypatch, tmp_path):
        import json

        from app.services.journal import store
        from app.services.watch.poller import decide as real_decide

        monkeypatch.setattr(store, "ENTRIES", tmp_path / "entries")
        (tmp_path / "entries").mkdir()
        p = tmp_path / "watchlist.json"
        p.write_text(json.dumps({"watchlist": [{
            "ticker": "NVDA", "print_at": "2026-11-18T20:20:00+00:00",
            "baseline_accession": "q-1", "expected_report_date": "2026-10-25",
        }]}))
        monkeypatch.setattr(watch_cli.wl, "WATCHLIST", p)
        subs = {"filings": {"recent": {
            "form": ["10-Q", "8-K", "10-Q"],
            "accessionNumber": ["q-0", "k-0", "q-1"],
            "filingDate": ["2026-11-18", "2026-11-18", "2026-08-27"],
            "reportDate": ["2026-10-25", "2026-11-18", "2026-07-26"],
            "items": [None, "2.02,9.01", None],
            "acceptanceDateTime": [None, "2026-11-18T21:20:00.000Z", None],
            "primaryDocument": [None] * 3,
        }}}
        watch = watch_cli.wl.load(p)[0]
        first = real_decide(watch, subs)
        assert first.action == "refuse" and first.filing.accession == "q-0"
        watch_cli._rearm(watch, first, subs)  # auto track completed (rc 0)
        again = watch_cli.wl.load(p)[0]
        assert again.baseline_accession == "q-0"
        assert real_decide(again, subs).action == "wait"


class TestBriefRetryCap:
    """A repeated brief failure is deterministic (a contract the model keeps
    missing, a source that cannot be built): stop paying for retries, keep
    saying so."""

    def test_full_brief_stops_retrying_but_keeps_the_queue_entry(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(watch_cli, "BRIEF_PENDING", tmp_path / "pending")
        report = tmp_path / "NVDA_2026-09-01.md"
        report.write_text("# r")
        watch_cli._queue_write("NVDA", str(report), watch_cli.BRIEF_MAX_ATTEMPTS)
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, r: pytest.fail("must not run"))
        monkeypatch.setattr(watch_cli, "_utcnow",
                            lambda: watch_cli._now("2026-10-13T09:00:00+00:00"))
        assert watch_cli._retry_pending_brief("NVDA") == 5  # alerts once
        assert "no longer retrying" in capsys.readouterr().err
        # Later passes the same day still LOG it and still spend nothing, but
        # do not alert again: an hourly notification about a state that cannot
        # change on its own is how a real alert gets ignored.
        monkeypatch.setattr(watch_cli, "_utcnow",
                            lambda: watch_cli._now("2026-10-13T23:00:00+00:00"))
        assert watch_cli._retry_pending_brief("NVDA") == 0
        assert "no longer retrying" in capsys.readouterr().err
        # ...and it reminds you once a day, every day, until you act.
        monkeypatch.setattr(watch_cli, "_utcnow",
                            lambda: watch_cli._now("2026-10-14T00:30:00+00:00"))
        assert watch_cli._retry_pending_brief("NVDA") == 5
        assert watch_cli._queue_read("NVDA")[:2] == (str(report), watch_cli.BRIEF_MAX_ATTEMPTS)

    def test_a_new_failure_rearms_the_alert(self, monkeypatch, tmp_path):
        monkeypatch.setattr(watch_cli, "BRIEF_PENDING", tmp_path / "pending")
        report = tmp_path / "NVDA_2026-09-01.md"
        report.write_text("# r")
        watch_cli._queue_write("NVDA", str(report), 2, alerted="2026-10-13")
        from types import SimpleNamespace

        monkeypatch.setattr(watch_cli.subprocess, "run",
                            lambda cmd, **k: SimpleNamespace(returncode=2))
        assert watch_cli._run_brief("NVDA", report) == 2
        assert watch_cli._queue_read("NVDA") == (str(report), 3, "")

    def test_below_the_cap_a_full_brief_still_retries(self, monkeypatch, tmp_path):
        monkeypatch.setattr(watch_cli, "BRIEF_PENDING", tmp_path / "pending")
        report = tmp_path / "NVDA_2026-09-01.md"
        report.write_text("# r")
        watch_cli._queue_write("NVDA", str(report), watch_cli.BRIEF_MAX_ATTEMPTS - 1)
        ran = []
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, r: ran.append(r) or 0)
        assert watch_cli._retry_pending_brief("NVDA") == 0 and ran == [report]


class TestVintageCapture:
    """A quarter that goes uncaptured is gone, so the snapshot runs from every
    pass — and can never cost a print."""

    def test_captured_on_every_pass_and_reported_when_it_changed(self, sweep_env, monkeypatch, capsys):
        monkeypatch.setattr(
            watch_cli, "capture_vintage",
            lambda client, ticker, now=None: SimpleNamespace(
                wrote=True, reason="captured",
                path=SimpleNamespace(name=f"{ticker}.json.gz", stat=lambda: SimpleNamespace(st_size=3072))))
        assert watch_cli.cmd_sweep(_sweep_args()) == 0
        out = capsys.readouterr().out
        assert out.count("companyfacts changed") == 3

    def test_a_failed_capture_is_a_warning_not_a_failed_pass(self, sweep_env, monkeypatch, capsys):
        def boom(client, ticker, now=None):
            raise OSError("disk full")
        monkeypatch.setattr(watch_cli, "capture_vintage", boom)
        sweep_env.table["NVDA"] = "refuse"
        assert watch_cli.cmd_sweep(_sweep_args()) == 0        # the print still happened
        assert sweep_env.generate_auto == ["NVDA"]
        assert "vintage capture failed" in capsys.readouterr().err

    def test_dry_run_and_opt_out_capture_nothing(self, sweep_env, monkeypatch):
        monkeypatch.setattr(watch_cli, "capture_vintage",
                            lambda *a, **k: pytest.fail("must not capture"))
        assert watch_cli.cmd_sweep(_sweep_args(dry_run=True)) == 0
        assert watch_cli.cmd_sweep(_sweep_args(no_vintage=True)) == 0


class TestVintageEscalation:
    """A capture failing for one day is noise; failing for days running means
    the archive is not being written and nobody has noticed."""

    def _failing(self, monkeypatch, days: int):
        def boom(client, ticker, now=None):
            raise OSError("disk full")
        monkeypatch.setattr(watch_cli, "capture_vintage", boom)
        monkeypatch.setattr(
            watch_cli, "_vintage_rc",
            lambda t, c: watch_cli.VINTAGE_STALE_RC if days >= watch_cli.VINTAGE_STALE_DAYS else 0)

    def test_one_bad_day_does_not_wake_anyone(self, sweep_env, monkeypatch, capsys):
        self._failing(monkeypatch, days=1)
        assert watch_cli.cmd_sweep(_sweep_args()) == 0
        assert sweep_env.notified == []
        assert "vintage capture failed" in capsys.readouterr().err

    def test_a_stalled_archive_reaches_the_exit_code_and_the_notification(
            self, sweep_env, monkeypatch):
        self._failing(monkeypatch, days=watch_cli.VINTAGE_STALE_DAYS)
        assert watch_cli.cmd_sweep(_sweep_args()) == watch_cli.VINTAGE_STALE_RC
        assert sweep_env.notified and "vintage capture stalled" in sweep_env.notified[-1][1]

    def test_a_stalled_archive_never_masks_a_failed_audit(self, sweep_env, monkeypatch):
        self._failing(monkeypatch, days=watch_cli.VINTAGE_STALE_DAYS)
        monkeypatch.setattr(watch_cli, "_run_audit", lambda p: 7)
        sweep_env.table["AAPL"] = "refuse"
        assert watch_cli.cmd_sweep(_sweep_args()) == 4   # the audit is the louder problem

    def test_the_rc_helper_uses_the_pass_client_and_never_builds_its_own(self, monkeypatch):
        # Building one here would ignore the caller's identity and cache and
        # could block on a live request inside an error path.
        monkeypatch.setattr(watch_cli, "SecClient",
                            lambda *a, **k: pytest.fail("must not construct a client"))
        asked = []

        class C:
            def resolve_cik(self, t):
                asked.append(t)
                return 1045810

        monkeypatch.setattr("app.services.ingestion.vintages.read_manifest",
                            lambda cik, root=None: {"problem_days": 9})
        assert watch_cli._vintage_rc("NVDA", C()) == watch_cli.VINTAGE_STALE_RC
        assert asked == ["NVDA"]


class TestSeasonCriticalFixes:
    """Three defects found auditing the merged season stack as one system.

    Each could lose a print on a night nobody is watching, and each was
    invisible to the per-PR reviews that passed the code they live in.
    """

    def _audit_env(self, poll_env, monkeypatch, tmp_path, codes):
        """Route the report to tmp (the attempts marker lands beside it) and
        make the audit return `codes` in turn, then its last value forever."""
        report = tmp_path / "NVDA_2026-09-01.md"
        report.write_text("# report")
        monkeypatch.setattr(watch_cli, "_generate_auto", lambda t, nd: report)
        monkeypatch.setattr(watch_cli, "_latest_report", lambda t, d: report)
        seq = list(codes)
        monkeypatch.setattr(
            watch_cli, "_run_audit",
            lambda p: poll_env.audit.append(p) or (seq.pop(0) if len(seq) > 1 else seq[0]),
        )
        return report

    # --- a re-arm that failed must never read as the routine queued brief ---

    def test_a_failed_rearm_outranks_a_queued_brief(self, poll_env, monkeypatch):
        # Both fail together under the same resource pressure. Plain numeric
        # max() reports 5 ("brief queued", self-healing); the row actually
        # still names the consumed event and will never fire again.
        _force_decision(monkeypatch, "refuse")
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, p: 1)
        monkeypatch.setattr(watch_cli, "_rearm", lambda w, d, s: False)
        assert watch_cli.cmd_poll(_poll_args()) == 1

    def test_severity_order_is_what_decides_it(self):
        assert watch_cli._worst([watch_cli.BRIEF_PENDING_RC, 1]) == 1
        assert max(watch_cli.BRIEF_PENDING_RC, 1) == 5, "the bug this pins is arithmetic"

    # --- the audit cannot bill forever for a failure that never changes -----

    def test_a_failing_audit_is_retryable_at_first(self, poll_env, monkeypatch, tmp_path):
        _force_decision(monkeypatch, "refuse")
        self._audit_env(poll_env, monkeypatch, tmp_path, [4])
        assert watch_cli.cmd_poll(_poll_args()) == 4
        assert poll_env.rearm == [], "a retryable audit failure must not consume the event"

    def test_it_is_abandoned_after_the_cap_so_the_row_re_arms(
            self, poll_env, monkeypatch, tmp_path):
        _force_decision(monkeypatch, "refuse")
        self._audit_env(poll_env, monkeypatch, tmp_path, [4])
        for _ in range(watch_cli.AUDIT_MAX_ATTEMPTS - 1):
            assert watch_cli.cmd_poll(_poll_args()) == 4
        rc = watch_cli.cmd_poll(_poll_args())
        assert rc == watch_cli.AUDIT_ABANDONED_RC
        assert len(poll_env.audit) == watch_cli.AUDIT_MAX_ATTEMPTS
        # Re-arming is what actually stops the hourly loop.
        assert poll_env.rearm == [("NVDA", "refuse")]
        # And the print still lands: the brief is built without the audit.
        assert [t for t, _ in poll_env.brief] == ["NVDA"]

    def test_an_abandoned_audit_never_spends_another_run(
            self, poll_env, monkeypatch, tmp_path):
        _force_decision(monkeypatch, "refuse")
        self._audit_env(poll_env, monkeypatch, tmp_path, [4])
        for _ in range(watch_cli.AUDIT_MAX_ATTEMPTS):
            watch_cli.cmd_poll(_poll_args())
        spent = len(poll_env.audit)
        watch_cli.cmd_poll(_poll_args())
        assert len(poll_env.audit) == spent, "paid a headless run after giving up"

    def test_a_passing_audit_clears_the_count(self, poll_env, monkeypatch, tmp_path):
        _force_decision(monkeypatch, "refuse")
        report = self._audit_env(poll_env, monkeypatch, tmp_path, [4, 0])
        assert watch_cli.cmd_poll(_poll_args()) == 4
        assert watch_cli._audit_attempts_path(report).exists()
        assert watch_cli.cmd_poll(_poll_args()) == 0
        assert not watch_cli._audit_attempts_path(report).exists()

    def test_an_abandoned_audit_completes_the_case(self):
        ref = Decision("refuse", "")
        assert watch_cli._completed(ref, watch_cli.AUDIT_ABANDONED_RC)
        assert not watch_cli._completed(ref, 4)

    # --- a row that has silently dropped out of the season must be said ------

    def test_an_overdue_row_reaches_the_notification(self, sweep_env, monkeypatch, tmp_path):
        # Overdue names return 3 ("waiting"), which _sweep_locked filters out
        # before notifying — so the only safety net for a mis-armed row used
        # to be a stderr line in a log full of routine passes.
        monkeypatch.setattr(watch_cli, "OVERDUE_ALERTS", tmp_path / "alerted.json")
        monkeypatch.setattr(
            watch_cli.wl, "load",
            lambda path=None: [_watch("AAPL", print_at=watch_cli._now("2026-01-01T00:00:00+00:00"))],
        )
        assert watch_cli.cmd_sweep(_sweep_args()) == 0  # still "waiting", not an error
        assert any("AAPL: overdue" in msg for _t, msg in sweep_env.notified)

    def test_it_is_said_once_a_day_not_once_an_hour(self, sweep_env, monkeypatch, tmp_path):
        # An hourly reminder about a row drifting for three weeks is exactly
        # how an alert channel gets trained into noise.
        monkeypatch.setattr(watch_cli, "OVERDUE_ALERTS", tmp_path / "alerted.json")
        monkeypatch.setattr(
            watch_cli.wl, "load",
            lambda path=None: [_watch("AAPL", print_at=watch_cli._now("2026-01-01T00:00:00+00:00"))],
        )
        for _ in range(4):
            watch_cli.cmd_sweep(_sweep_args())
        assert sum("overdue" in msg for _t, msg in sweep_env.notified) == 1

    def test_a_row_inside_its_window_says_nothing(self, sweep_env, monkeypatch, tmp_path):
        monkeypatch.setattr(watch_cli, "OVERDUE_ALERTS", tmp_path / "alerted.json")
        watch_cli.cmd_sweep(_sweep_args())
        assert not any("overdue" in msg for _t, msg in sweep_env.notified)

    def test_a_dry_run_never_writes_the_alert_state(self, sweep_env, monkeypatch, tmp_path):
        state = tmp_path / "alerted.json"
        monkeypatch.setattr(watch_cli, "OVERDUE_ALERTS", state)
        monkeypatch.setattr(
            watch_cli.wl, "load",
            lambda path=None: [_watch("AAPL", print_at=watch_cli._now("2026-01-01T00:00:00+00:00"))],
        )
        watch_cli.cmd_sweep(_sweep_args(dry_run=True))
        assert not state.exists()
