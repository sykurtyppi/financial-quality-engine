"""Ticker-only track tests: the watch CLI's auto path, fresh propagation, and
the shared report builder's out_dir/banner/fresh parameters.

The invariant under test: automation never erodes the thesis gate. A locked
thesis takes the journal track; a thesis-less print produces only a bannered
artifact in reports/auto/ that cannot be mistaken for a blind case.
"""

from __future__ import annotations

import importlib.util
from argparse import Namespace
from datetime import UTC, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.watch.poller import Decision
from tests.fixtures.staged import without_generation, write_ledger

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location("watch_cli", ROOT / "scripts" / "watch.py")
watch_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watch_cli)
_REAL_RUN_BRIEF = watch_cli._run_brief  # before any fixture stubs it
_REAL_GENERATE_AUTO = watch_cli._generate_auto  # likewise


def _poll_args(**over) -> Namespace:
    base = dict(
        ticker="NVDA", since=None, entry_day=None, interval=0.01, max_wait=1.0,
        once=True, dry_run=False, no_docs=False, no_auto=False, no_audit=False,
        no_brief=False, no_vintage=False,
    )
    base.update(over)
    return Namespace(**base)


def _fake_generated(ticker: str, day: str | None):
    """The run a stubbed `_generate`'s journal child said it published."""
    return watch_cli.Generated(Path("/tmp/fake_journal.md"), "0" * 32, ticker,
                               day or "2026-08-26", None)


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
    # The fake reports live at /tmp/fake_*.md, and the audit retry counter is
    # written beside the report: it went to the SHARED /tmp and accumulated
    # across runs until every later run read the audit as abandoned (exit 7).
    # Each test's counters live in its own tmp_path instead.
    real_attempts = watch_cli._audit_attempts_path
    monkeypatch.setattr(
        watch_cli, "_audit_attempts_path",
        lambda report: tmp_path / real_attempts(report).name
        if str(report).startswith("/tmp/fake_") else real_attempts(report),
    )
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
    # The journal child's result: the run it published, which is audited
    # and stamped (Hermes re-audit of 84e65b0, finding 3).
    monkeypatch.setattr(
        watch_cli, "_generate",
        lambda t, day, nd: calls.generate.append((t, day)) or (0, _fake_generated(t, day)),
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
        lambda t, day, gen: calls.marked.append((t, day, len(calls.audit))) or 0,
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


def test_the_fake_reports_audit_counters_stay_in_the_tests_tmp_path(poll_env, tmp_path):
    """A counter left in the shared /tmp by one run made the next runs'
    audits read as abandoned (exit 7) until the file was deleted by hand."""
    assert watch_cli._audit_attempts_path(Path("/tmp/fake_auto.md")).parent == tmp_path


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
        from datetime import date as d
        from datetime import datetime

        from app.services.journal.schema_v2 import (
            Assumption,
            BeforeBlock,
            EntryV2,
            lock_entry,
        )

        before = BeforeBlock(
            thesis="thesis for the event", conviction=3, intended_action="hold",
            assumptions=[Assumption(metric="revenue", comparator=">", threshold=1.0,
                                    window="FY2027Q3", source="10-Q",
                                    resolve_by=d(2026, 12, 31))],
        )
        return lock_entry(EntryV2(
            ticker="NVDA", day=d.fromisoformat(day),
            opened=datetime(2026, 8, 1, tzinfo=UTC), before=before,
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
        from datetime import datetime

        from app.services.journal import store as st

        wanted = self._entry("2026-08-26")
        st.save_v2(wanted)
        spent = self._entry("2026-11-18")
        path = st.save_v2(spent)
        st.save_v2(spent.model_copy(update={"reported": datetime.now(UTC)}),
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

        def fake_run(cmd, cwd=None, env=None):
            recorded["cmd"] = cmd
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(watch_cli.subprocess, "run", fake_run)
        # Exit 0 with no result file written: which run is not known.
        assert watch_cli._generate("NVDA", "2026-08-26", no_docs=False) == (0, None)
        assert "--fresh" in recorded["cmd"]
        assert "--no-docs" not in recorded["cmd"]
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
            diagnostics=SimpleNamespace(coverage=lambda: 0.9, warnings=[], selected_tags=lambda: {}, selected_series=lambda: {}, field_notes=lambda: []),
            company_facts={},
        )
        monkeypatch.setattr(reporting, "SecClient", _Client)
        monkeypatch.setattr(reporting, "fetch_dataset_snapshot",
                            lambda t, n_quarters, client: snapshot)
        monkeypatch.setattr(
            reporting, "analyze", lambda ds: SimpleNamespace(overall=None)
        )
        monkeypatch.setattr(
            reporting, "build_full_report",
            lambda *a, **k: (write_ledger(k), "ENGINE REPORT BODY", None)[1:]
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
            diagnostics=SimpleNamespace(coverage=lambda: 0.9, warnings=[], selected_tags=lambda: {}, selected_series=lambda: {}, field_notes=lambda: []),
            company_facts={},
        )
        monkeypatch.setattr(reporting, "SecClient", _Client)
        monkeypatch.setattr(reporting, "fetch_dataset_snapshot",
                            lambda t, n_quarters, client: snapshot)
        monkeypatch.setattr(reporting, "analyze", lambda ds: SimpleNamespace(overall=None))
        monkeypatch.setattr(reporting, "build_full_report",
                            lambda *a, **k: (write_ledger(k), "BODY", None)[1:])
        monkeypatch.setattr(reporting, "REPORTS", tmp_path)

        out, _ = reporting.build_report("nvda", with_docs=False)
        assert client_kwargs.get("fresh") is False
        assert out.parent == tmp_path
        assert without_generation(out.read_text()) == "BODY"


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
        monkeypatch.setattr(watch_cli, "_generate", lambda t, day, nd: (1, None))
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


def _watch(ticker: str, **over) -> watch_cli.wl.Watch:
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

    @pytest.mark.parametrize("timeout, slept", [(1.25, [0.5, 0.5, 0.25]), (0, [])])
    def test_the_lock_wait_is_paced_and_ends_at_the_deadline(self, tmp_path, monkeypatch,
                                                            timeout, slept):
        # The activity lock on a fake clock: held elsewhere, it is retried
        # every LOCK_RETRY_S, the last sleep cut to what is left, and reaching
        # the deadline yields False at once — the sweep's timeout 0 never
        # sleeps at all. (Before the tests below that hold the lock on the
        # real clock: a wait that never ends hangs them rather than failing.)
        import fcntl

        clock, naps = [100.0], []

        def sleep(s):
            naps.append(s)
            clock[0] += s
            assert len(naps) < 10, "the wait never reached its deadline"

        monkeypatch.setattr(watch_cli, "SWEEP_LOCK", tmp_path / "sweep.lock")
        monkeypatch.setattr(watch_cli, "LOCK_RETRY_S", 0.5)
        monkeypatch.setattr(watch_cli, "time", SimpleNamespace(monotonic=lambda: clock[0],
                                                               sleep=sleep))
        with open(watch_cli.SWEEP_LOCK, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            with watch_cli._activity_lock(timeout=timeout) as held:
                assert held is False
        assert naps == slept

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

    @pytest.mark.parametrize("days, named", [(watch_cli.OVERDUE_DAYS, False),
                                             (watch_cli.OVERDUE_DAYS + 1, True)])
    def test_overdue_means_more_than_overdue_days(self, sweep_env, monkeypatch, capsys,
                                                  days, named):
        # Rows print 2026-10-29 20:30Z: exactly OVERDUE_DAYS later is still
        # patience, a day more is a mis-armed row.
        at = watch_cli._now("2026-10-29T20:30:00+00:00") + timedelta(days=days)
        monkeypatch.setattr(watch_cli, "_utcnow", lambda: at)
        assert watch_cli.cmd_sweep(_sweep_args()) == 0
        assert (f"AAPL: still waiting {days}d past its print hint"
                in capsys.readouterr().err) is named

    def test_verbose_says_each_wait(self, sweep_env, capsys):
        assert watch_cli.cmd_sweep(_sweep_args()) == 0
        assert "AAPL: forced wait" not in capsys.readouterr().out
        assert watch_cli.cmd_sweep(_sweep_args(verbose=True)) == 0
        assert "AAPL: forced wait" in capsys.readouterr().out


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
        monkeypatch.setattr(watch_cli, "_mark_reported", lambda t, day, gen: 1)
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
        marker = tmp_path / "pending" / "NVDA__NVDA_2026-09-01"  # one marker per event
        assert marker.is_file()
        assert watch_cli._queue_read("NVDA", str(report)) == (str(report), 1, "")
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
        err = capsys.readouterr().err
        assert "queued brief retry crashed" in err and "still queued" in err
        assert (watch_cli.BRIEF_PENDING / "AAPL").is_file()  # as it says

    def test_an_unusable_queue_is_not_said_to_hold_the_brief(
            self, sweep_env, monkeypatch, tmp_path, capsys):
        """With ``.pending`` a link (refused), the retry raises and the pass
        said "— still queued." when nothing could be queued or retried there
        (review of b17cc08, finding 5)."""
        away = tmp_path / "away"
        away.mkdir()
        watch_cli.BRIEF_PENDING.symlink_to(away)
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, p: pytest.fail("nothing to run"))
        assert watch_cli.cmd_sweep(_sweep_args()) == 5
        err = capsys.readouterr().err
        assert "still queued" not in err
        assert err.count("queued brief retry crashed") == 3
        assert f"the brief queue {watch_cli.BRIEF_PENDING} cannot be used" in err
        assert "nothing queued for AAPL is being retried" in err

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
        assert watch_cli._queue_read("NVDA", "-") == ("-", 1, "")
        assert watch_cli._run_brief("NVDA", None) == 2  # same target: attempts climb
        assert watch_cli._queue_read("NVDA", "-") == ("-", 2, "")

    def test_retry_cap_gives_up_on_a_hopeless_print_night_brief(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(watch_cli, "BRIEF_PENDING", tmp_path / "pending")
        watch_cli._queue_write("NVDA", "-", watch_cli.PRINT_BRIEF_MAX_ATTEMPTS)
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, r: pytest.fail("must not run"))
        assert watch_cli._retry_pending_brief("NVDA") == 5  # says so once
        assert "giving up" in capsys.readouterr().err
        # Kept, marked as given up, so the trigger does not start it over
        # (TestBriefQueuePerEvent); never retried, never said again.
        assert watch_cli._queue_read("NVDA", "-") == ("-", watch_cli.PRINT_BRIEF_MAX_ATTEMPTS, "")
        assert watch_cli._retry_pending_brief("NVDA") == 0
        assert "giving up" not in capsys.readouterr().err
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
        assert watch_cli._queue_read("AAPL", "-") == ("-", 1, "")
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
        import hashlib
        import json

        monkeypatch.setattr(watch_cli, "_utcnow", lambda: watch_cli._now("2026-10-13T23:05:00+00:00"))
        built = []
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, r: built.append(t) or 0)
        for t in ("AAPL", "MSFT", "NVDA"):
            (watch_cli.BRIEFS / t / "2026-10-13").mkdir(parents=True)
            (watch_cli.BRIEFS / f"{t}_2026-10-13.md").write_text("# brief")
        # AAPL: print-night from the preliminary accession; MSFT: full; NVDA:
        # no record. A record vouches only for the brief whose hash it names.
        digest = hashlib.sha256(b"# brief").hexdigest()
        (watch_cli.BRIEFS / "AAPL" / "2026-10-13" / "built.json").write_text(
            json.dumps({"kind": "print-night", "accession": "k-prelim", "brief_sha256": digest}))
        (watch_cli.BRIEFS / "MSFT" / "2026-10-13" / "built.json").write_text(
            json.dumps({"kind": "full", "accession": "k-prelim", "brief_sha256": digest}))
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
        assert watch_cli._queue_read("NVDA", str(report))[:2] == (str(report), watch_cli.BRIEF_MAX_ATTEMPTS)

    def test_a_new_failure_rearms_the_alert(self, monkeypatch, tmp_path):
        monkeypatch.setattr(watch_cli, "BRIEF_PENDING", tmp_path / "pending")
        report = tmp_path / "NVDA_2026-09-01.md"
        report.write_text("# r")
        watch_cli._queue_write("NVDA", str(report), 2, alerted="2026-10-13")
        from types import SimpleNamespace

        monkeypatch.setattr(watch_cli.subprocess, "run",
                            lambda cmd, **k: SimpleNamespace(returncode=2))
        assert watch_cli._run_brief("NVDA", report) == 2
        assert watch_cli._queue_read("NVDA", str(report)) == (str(report), 3, "")

    def test_below_the_cap_a_full_brief_still_retries(self, monkeypatch, tmp_path):
        monkeypatch.setattr(watch_cli, "BRIEF_PENDING", tmp_path / "pending")
        report = tmp_path / "NVDA_2026-09-01.md"
        report.write_text("# r")
        watch_cli._queue_write("NVDA", str(report), watch_cli.BRIEF_MAX_ATTEMPTS - 1)
        ran = []
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, r: ran.append(r) or 0)
        assert watch_cli._retry_pending_brief("NVDA") == 0 and ran == [report]


class TestBriefQueuePerEvent:
    """The brief queue under reports/briefs/.pending/ was keyed by TICKER: one
    file, written in place, that any brief success deleted and any failure
    overwrote. Three defects followed (round-24 audit):

    - a torn or empty marker parsed as "nothing queued", so the retry did
      nothing, yet the print-night trigger saw the FILE exist and stayed
      quiet: the print went brief-less, silently, for as long as it sat there;
    - quarter A's exhausted full brief is kept on purpose (nothing else
      rebuilds it) and alerts daily, but that same entry suppressed quarter
      B's print-night brief, and B's 10-Q brief succeeding then deleted A's
      entry without a word;
    - a failure for target B overwrote A's entry, losing it the same way.

    The queue is now one marker per event, written atomically, and a marker
    that does not parse is logged, reported and removed."""

    DAY = "2026-10-13"

    @pytest.fixture
    def briefs(self, sweep_env, monkeypatch):
        """The real `_run_brief` over a fake earnings_brief.py: `rcs[ticker]`
        (default 0) is its exit code; a success writes the brief file, as
        the real build does. `runs` records (ticker, report) per build."""
        from app.services.watch.poller import Filing

        monkeypatch.setattr(watch_cli, "_run_brief", _REAL_RUN_BRIEF)
        monkeypatch.setattr(
            watch_cli, "latest_earnings_8k",
            lambda subs: Filing("8-K", "k-b", watch_cli.date.fromisoformat(self.DAY),
                                items="2.02,9.01"))
        self._at(monkeypatch, f"{self.DAY}T22:05:00+00:00")
        env = SimpleNamespace(rcs={}, runs=[])

        def run(cmd, **k):
            ticker = cmd[3]
            report = None if cmd[-1] == "--no-report" else Path(cmd[-1])
            env.runs.append((ticker, report))
            rc = env.rcs.get(ticker, 0)
            if rc == 0:
                watch_cli.BRIEFS.mkdir(parents=True, exist_ok=True)
                (watch_cli.BRIEFS / f"{ticker}_{self.DAY}.md").write_text("# brief")
            return SimpleNamespace(returncode=rc)
        monkeypatch.setattr(watch_cli.subprocess, "run", run)
        return env

    @staticmethod
    def _at(monkeypatch, when: str) -> None:
        monkeypatch.setattr(watch_cli, "_utcnow", lambda: watch_cli._now(when))

    @staticmethod
    def _queued() -> dict[str, str]:
        """Every marker on disk, by file name -> contents."""
        pending = watch_cli.BRIEF_PENDING
        return {} if not pending.is_dir() else {
            p.name: p.read_text() for p in sorted(pending.iterdir())}

    @staticmethod
    def _report(tmp_path, name: str) -> Path:
        report = tmp_path / name
        report.write_text("# r")
        return report

    @pytest.mark.parametrize("name", ["AAPL", "AAPL__print-night"])
    @pytest.mark.parametrize("body", ["", "\n", "-\nattempts=", "/reports/AAPL_2026-10-0"])
    def test_an_unreadable_marker_never_suppresses_the_print_night_brief(
            self, briefs, capsys, name, body):
        # A marker torn mid-write (the old writer was a plain write_text) or
        # truncated by hand: before, the retry read "nothing queued" and the
        # trigger read "queued", and the print sat brief-less indefinitely.
        watch_cli.BRIEF_PENDING.mkdir(parents=True)
        (watch_cli.BRIEF_PENDING / name).write_text(body)
        assert watch_cli.cmd_sweep(_sweep_args()) == 5  # the discarded job is reported
        out, err = capsys.readouterr()
        assert ("AAPL", None) in briefs.runs  # the print-night brief went out this pass
        assert (watch_cli.BRIEFS / f"AAPL_{self.DAY}.md").exists()
        assert "unreadable queue marker" in err and name in err
        assert "AAPL -> 5" in out
        assert self._queued() == {}  # removed, never left to block anything
        # ...and said once: the next pass is clean.
        assert watch_cli.cmd_sweep(_sweep_args()) == 0

    def test_an_exhausted_quarter_neither_blocks_nor_is_erased_by_the_next(
            self, briefs, monkeypatch, tmp_path, capsys):
        # Quarter A's full brief failed BRIEF_MAX_ATTEMPTS times: its entry is
        # kept (nothing else rebuilds it) and alerts daily. Quarter B's 8-K
        # lands: B's print-night brief must go out, and neither B's print-night
        # nor B's 10-Q brief may delete A's entry.
        report_a = self._report(tmp_path, "AAPL_2026-07-31.md")
        watch_cli._queue_write("AAPL", str(report_a), watch_cli.BRIEF_MAX_ATTEMPTS)
        assert watch_cli.cmd_sweep(_sweep_args()) == 5  # A alerts (first time today)
        assert ("AAPL", None) in briefs.runs  # B's print-night was not suppressed by A
        assert ("AAPL", report_a) not in briefs.runs  # A is not retried: it is exhausted
        assert "no longer retrying" in capsys.readouterr().err
        kept = [body for body in self._queued().values() if str(report_a) in body]
        assert len(kept) == 1 and f"attempts={watch_cli.BRIEF_MAX_ATTEMPTS}" in kept[0]
        # B's 10-Q lands and its full brief succeeds: A's entry is untouched...
        report_b = self._report(tmp_path, f"AAPL_{self.DAY}.md")
        assert watch_cli._run_brief("AAPL", report_b) == 0
        assert [str(report_a) in body for body in self._queued().values()] == [True]
        # ...and it keeps alerting, once a day, until someone acts on it.
        self._at(monkeypatch, "2026-10-14T09:00:00+00:00")
        assert watch_cli._retry_pending_brief("AAPL") == 5
        assert "no longer retrying" in capsys.readouterr().err

    def test_a_failure_for_another_target_never_overwrites_an_entry(self, briefs, tmp_path):
        report_a = self._report(tmp_path, "AAPL_2026-07-31.md")
        report_b = self._report(tmp_path, f"AAPL_{self.DAY}.md")
        watch_cli._queue_write("AAPL", str(report_a), 3)
        briefs.rcs["AAPL"] = 2
        assert watch_cli._run_brief("AAPL", report_b) == 2
        bodies = list(self._queued().values())
        assert len(bodies) == 2
        assert any(b.startswith(f"{report_a}\n") and "attempts=3" in b for b in bodies)
        assert any(b.startswith(f"{report_b}\n") and "attempts=1" in b for b in bodies)
        # The same target failing again climbs its own count, not A's.
        assert watch_cli._run_brief("AAPL", report_b) == 2
        bodies = list(self._queued().values())
        assert any(b.startswith(f"{report_a}\n") and "attempts=3" in b for b in bodies)
        assert any(b.startswith(f"{report_b}\n") and "attempts=2" in b for b in bodies)

    def test_a_full_success_clears_its_events_print_night_entry_and_nothing_else(
            self, briefs, tmp_path):
        # The print-night brief failing is queued; the 10-Q rebuild of the
        # same event is its second chance, and its success clears it. The
        # old quarter's entry is a different event and stays.
        report_a = self._report(tmp_path, "AAPL_2026-07-31.md")
        watch_cli._queue_write("AAPL", str(report_a), watch_cli.BRIEF_MAX_ATTEMPTS)
        briefs.rcs["AAPL"] = 2
        assert watch_cli._run_brief("AAPL", None) == 2
        assert len(self._queued()) == 2
        # A print-night success clears only its own entry, never a full one.
        briefs.rcs["AAPL"] = 0
        report_b = self._report(tmp_path, f"AAPL_{self.DAY}.md")
        watch_cli._queue_write("AAPL", str(report_b), 1)
        assert watch_cli._run_brief("AAPL", None) == 0
        bodies = sorted(self._queued().values())
        assert len(bodies) == 2 and not any(b.startswith("-\n") for b in bodies)
        # The full brief of the event succeeding clears it and the print-night.
        watch_cli._queue_write("AAPL", "-", 2)
        assert len(self._queued()) == 3
        assert watch_cli._run_brief("AAPL", report_b) == 0
        assert [str(report_a) in b for b in self._queued().values()] == [True]

    def test_a_same_event_full_entry_still_owns_the_print_night_brief(self, briefs, tmp_path):
        # A full brief of THIS print queued (the 10-Q landed, its brief
        # failed): its retry writes this 8-K's brief with the engine findings,
        # so the trigger does not spend a second run on a release-only one.
        report_b = self._report(tmp_path, f"AAPL_{self.DAY}.md")
        watch_cli._queue_write("AAPL", str(report_b), 1)
        briefs.rcs["AAPL"] = 2
        assert watch_cli.cmd_sweep(_sweep_args()) == 5
        assert [r for t, r in briefs.runs if t == "AAPL"] == [report_b]  # the retry, only

    def test_what_parses_and_what_does_not(self, briefs, tmp_path):
        # A marker parses only whole: a target, then known lines. Anything
        # else is a torn write or a hand edit, never guessed at.
        marker = tmp_path / "m"
        cases = {
            "-\n": ("-", 1, ""),  # before `attempts` existed
            "-\n\nattempts=2\n": ("-", 2, ""),  # a blank line is not damage
            "/r/A_2026-07-31.md\nattempts=6\nalerted=2026-10-13\n":
                ("/r/A_2026-07-31.md", 6, "2026-10-13"),
            "-\nattempts=2\ngarbage\n": None,
            "-\nattempts=two\n": None,
            "/r/A_2026-07-3": None,
            "": None,
        }
        for body, parsed in cases.items():
            marker.write_text(body)
            assert watch_cli._parse_marker(marker) == parsed, body
        marker.write_bytes(b"\xff\xfe-\n")  # not text at all
        assert watch_cli._parse_marker(marker) is None
        assert watch_cli._parse_marker(tmp_path / "missing") is None
        # The trigger's check reads the same way, even in a pass whose retry
        # never ran to remove the torn marker (a 10-Q pass).
        watch_cli.BRIEF_PENDING.mkdir(parents=True)
        (watch_cli.BRIEF_PENDING / "AAPL__AAPL_2026-10-14").write_text("")
        (watch_cli.BRIEF_PENDING / "AAPL").write_text("\n")
        assert not watch_cli._event_queued("AAPL", watch_cli.date(2026, 10, 13))

    def test_one_run_when_the_events_full_brief_clears_its_print_night_entry(
            self, briefs, tmp_path):
        # Both of this print's entries queued: the full one is retried first,
        # and its success clears the print-night entry, which then costs no
        # second headless run.
        report_b = self._report(tmp_path, f"AAPL_{self.DAY}.md")
        watch_cli._queue_write("AAPL", "-", 1)
        watch_cli._queue_write("AAPL", str(report_b), 1)
        assert watch_cli._retry_pending_brief("AAPL") == 0
        assert briefs.runs == [("AAPL", report_b)]
        assert self._queued() == {}
        # The same when the full entry is a legacy marker, which is listed
        # after every per-event one: order is by kind, not by file name.
        watch_cli._queue_write("AAPL", "-", 1)
        (watch_cli.BRIEF_PENDING / "AAPL").write_text(f"{report_b}\nattempts=1\n")
        briefs.runs.clear()
        assert watch_cli._retry_pending_brief("AAPL") == 0
        assert briefs.runs == [("AAPL", report_b)]
        assert self._queued() == {}

    def test_a_legacy_entry_past_its_cap_is_kept_under_its_new_name(
            self, briefs, tmp_path, capsys):
        # Print-night past the cap: given up under its per-event name, the
        # legacy file gone (left, it would say "giving up" every pass).
        watch_cli.BRIEF_PENDING.mkdir(parents=True)
        legacy = watch_cli.BRIEF_PENDING / "AAPL"
        legacy.write_text(f"-\nattempts={watch_cli.PRINT_BRIEF_MAX_ATTEMPTS}\n")
        assert watch_cli._retry_pending_brief("AAPL") == 5
        assert "giving up" in capsys.readouterr().err
        assert list(self._queued()) == ["AAPL__print-night"] and briefs.runs == []
        assert "gave_up=" in self._queued()["AAPL__print-night"]
        assert watch_cli._retry_pending_brief("AAPL") == 0
        assert "giving up" not in capsys.readouterr().err
        watch_cli._queue_clear("AAPL", "-")
        # A full brief past the cap: kept, and the log names the file it is
        # now in, the one to delete to silence it.
        report_a = self._report(tmp_path, "AAPL_2026-07-31.md")
        legacy.write_text(f"{report_a}\nattempts={watch_cli.BRIEF_MAX_ATTEMPTS}\n")
        assert watch_cli._retry_pending_brief("AAPL") == 5
        err = capsys.readouterr().err
        assert f"Queued at {watch_cli.BRIEF_PENDING / 'AAPL__AAPL_2026-07-31'};" in err
        assert list(self._queued()) == ["AAPL__AAPL_2026-07-31"] and briefs.runs == []

    def test_a_legacy_ticker_only_marker_is_honoured_and_migrated(self, briefs, tmp_path):
        # A queue left on disk by the ticker-keyed version: `.pending/AAPL`.
        watch_cli.BRIEF_PENDING.mkdir(parents=True)
        legacy = watch_cli.BRIEF_PENDING / "AAPL"
        legacy.write_text("-\nattempts=2\n")
        # It still owns the print-night brief (the retry runs it, once) ...
        briefs.rcs["AAPL"] = 2
        assert watch_cli.cmd_sweep(_sweep_args()) == 5
        assert [r for t, r in briefs.runs if t == "AAPL"] == [None]
        # ... and its count carries over into the per-event marker.
        assert not legacy.exists()
        assert self._queued() == {"AAPL__print-night": "-\nattempts=3\n"}
        # A legacy full entry keeps its target and count where it is.
        report_a = self._report(tmp_path, "AAPL_2026-07-31.md")
        legacy.write_text(f"{report_a}\nattempts=2\n")
        briefs.runs.clear()
        # A different event's success leaves it; its own success clears it.
        briefs.rcs["AAPL"] = 0
        assert watch_cli._run_brief("AAPL", None) == 0
        assert legacy.exists()
        assert watch_cli._retry_pending_brief("AAPL") == 0
        assert briefs.runs == [("AAPL", None), ("AAPL", report_a)]
        assert self._queued() == {}

    def test_a_print_night_record_that_does_not_match_its_brief_is_no_record(self, briefs):
        # built.json is written after the brief; a kill between the two can
        # leave a NEW full brief beside an OLD print-night record. Read as
        # "print-night", a same-day second 8-K would rebuild over the full
        # brief. A record whose hash does not match reads as no record, and
        # no record is never rebuilt here.
        import hashlib
        import json

        brief = watch_cli.BRIEFS / f"AAPL_{self.DAY}.md"
        (watch_cli.BRIEFS / "AAPL" / self.DAY).mkdir(parents=True)
        brief.write_text("# print-night brief")
        record = {"kind": "print-night", "accession": "k-prelim",
                  "brief_sha256": hashlib.sha256(brief.read_bytes()).hexdigest()}
        (watch_cli.BRIEFS / "AAPL" / self.DAY / "built.json").write_text(json.dumps(record))
        assert watch_cli._built_meta("AAPL", self.DAY)["kind"] == "print-night"
        brief.write_text("# the FULL brief, its record never written")
        assert watch_cli._built_meta("AAPL", self.DAY) is None
        for t in ("MSFT", "NVDA"):  # out of the way: they have briefs already
            (watch_cli.BRIEFS / f"{t}_{self.DAY}.md").write_text("# brief")
        assert watch_cli.cmd_sweep(_sweep_args()) == 0
        assert briefs.runs == []
        assert "FULL brief" in brief.read_text()

    # --- the print-night cap must hold (PR #104 review) ---------------------

    @staticmethod
    def _only(monkeypatch, ticker: str = "NVDA") -> None:
        monkeypatch.setattr(watch_cli.wl, "load", lambda path=None: [_watch(ticker)])

    @pytest.mark.parametrize("queued_at", [None, "cap"])
    def test_a_print_night_brief_given_up_stays_given_up(
            self, briefs, monkeypatch, capsys, queued_at):
        # Giving up deleted the marker; with no brief on disk and nothing
        # queued, the trigger fired again (in the same pass, since e35c213)
        # and wrote a fresh attempts=1: a paid run every pass for the whole
        # 14-day window, and "giving up" was false. 21 passes cost 21 runs.
        self._only(monkeypatch)
        briefs.rcs["NVDA"] = 2
        if queued_at == "cap":
            watch_cli._queue_write("NVDA", "-", watch_cli.PRINT_BRIEF_MAX_ATTEMPTS)
        codes = [watch_cli.cmd_sweep(_sweep_args()) for _ in range(21)]
        err = capsys.readouterr().err
        assert len(briefs.runs) == (0 if queued_at else watch_cli.PRINT_BRIEF_MAX_ATTEMPTS)
        assert err.count("giving up") == 1  # said once ...
        assert codes.count(5) == len(briefs.runs) + 1  # ... and reported once
        assert codes[-1] == 0
        # The entry is kept, marked, and names the 8-K it gave up on.
        assert "gave_up=2026-10-13" in self._queued()["NVDA__print-night"]
        assert "accession=k-b" in self._queued()["NVDA__print-night"]

    def test_a_newer_8k_is_a_new_print_and_is_not_blocked(self, briefs, monkeypatch, capsys):
        from app.services.watch.poller import Filing

        self._only(monkeypatch)
        briefs.rcs["NVDA"] = 2
        watch_cli._queue_write("NVDA", "-", watch_cli.PRINT_BRIEF_MAX_ATTEMPTS)
        assert watch_cli.cmd_sweep(_sweep_args()) == 5  # gives up on k-b
        assert briefs.runs == []
        # A newer earnings 8-K (a second print in the window): its brief is
        # built, with a count of its own.
        monkeypatch.setattr(
            watch_cli, "latest_earnings_8k",
            lambda subs: Filing("8-K", "k-c", watch_cli.date(2026, 10, 20), items="2.02,9.01"))
        self._at(monkeypatch, "2026-10-20T22:05:00+00:00")
        assert watch_cli.cmd_sweep(_sweep_args()) == 5
        assert briefs.runs == [("NVDA", None)]
        assert self._queued() == {"NVDA__print-night": "-\nattempts=1\n"}
        assert "gave up on k-b" in capsys.readouterr().out

    def test_the_events_full_build_still_clears_a_given_up_print_night_entry(
            self, briefs, monkeypatch, tmp_path):
        self._only(monkeypatch)
        watch_cli._queue_write("NVDA", "-", watch_cli.PRINT_BRIEF_MAX_ATTEMPTS)
        assert watch_cli.cmd_sweep(_sweep_args()) == 5
        report = self._report(tmp_path, f"NVDA_{self.DAY}.md")
        assert watch_cli._run_brief("NVDA", report) == 0
        assert self._queued() == {}

    def test_a_given_up_marker_from_this_change_or_before_still_parses(self, briefs, tmp_path):
        marker = tmp_path / "m"
        marker.write_text("-\nattempts=6\ngave_up=2026-10-13\naccession=k-b\n")
        assert watch_cli._parse_marker(marker) == ("-", 6, "")
        marker.write_text("-\nattempts=6\ngave_up=2026-10-13\n")  # 8-K unknown
        assert watch_cli._parse_marker(marker) == ("-", 6, "")

    def test_a_kept_full_entry_for_this_print_does_not_leave_it_brief_less(
            self, briefs, monkeypatch, tmp_path):
        # This print's full brief failed BRIEF_MAX_ATTEMPTS times and is kept
        # (alerting daily). It was counted as "queued for this print", so the
        # print-night trigger stayed quiet for good and the print got no
        # brief at all. It no longer counts: the print-night brief is the
        # fallback, and its own cap still holds.
        self._only(monkeypatch)
        report = self._report(tmp_path, f"NVDA_{self.DAY}.md")
        watch_cli._queue_write("NVDA", str(report), watch_cli.BRIEF_MAX_ATTEMPTS,
                               alerted="2026-10-14")
        assert not watch_cli._event_queued("NVDA", watch_cli.date(2026, 10, 13))
        briefs.rcs["NVDA"] = 2
        for _ in range(21):
            watch_cli.cmd_sweep(_sweep_args())
        assert briefs.runs == [("NVDA", None)] * watch_cli.PRINT_BRIEF_MAX_ATTEMPTS
        briefs.runs.clear()
        # It succeeding instead: the print has its (release-only) brief.
        watch_cli._queue_clear("NVDA", "-")
        briefs.rcs["NVDA"] = 0
        assert watch_cli.cmd_sweep(_sweep_args()) == 0
        assert briefs.runs == [("NVDA", None)]
        assert (watch_cli.BRIEFS / f"NVDA_{self.DAY}.md").exists()


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

        from app.services.ingestion import vintages

        # Counted from the markers (the isolated store's), not the manifest.
        for n in range(watch_cli.VINTAGE_STALE_DAYS):
            vintages._record_problem_day(1045810, watch_cli.date(2026, 9, 20 + n))
        monkeypatch.setattr(vintages, "read_manifest",
                            lambda cik, root=None: {"problem_days": 0})
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

    # --- the counter itself can neither reset nor vanish (Hermes audit) ----

    def _capped(self, monkeypatch, tmp_path, rc: int = 4):
        report = tmp_path / "NVDA_2026-11-17.md"
        report.write_text("# report\n")
        runs: list[Path] = []
        monkeypatch.setattr(watch_cli, "_run_audit", lambda r: runs.append(r) or rc)
        return report, runs

    def test_a_garbled_counter_is_spent_not_zero(self, monkeypatch, tmp_path, capsys):
        """Reading garbage as 0 reopened the cap: every garbled count bought
        three more paid runs."""
        report, runs = self._capped(monkeypatch, tmp_path)
        watch_cli._audit_attempts_path(report).write_text("2\x00\x00")
        assert watch_cli._run_audit_capped(report) == (0, True)
        assert runs == []
        assert "unreadable" in capsys.readouterr().err

    def test_a_count_that_cannot_be_saved_abandons_rather_than_retries_forever(
            self, monkeypatch, tmp_path, capsys):
        """The failed save was swallowed ("costs a retry"): with the count
        never recorded, every hourly pass spent another paid run."""
        report, runs = self._capped(monkeypatch, tmp_path)

        def refuse(path, text, **kw):
            raise PermissionError(13, "read-only", str(path))
        monkeypatch.setattr(watch_cli, "write_atomic", refuse)
        assert watch_cli._run_audit_capped(report) == (4, True)
        assert len(runs) == 1
        assert "could not be recorded" in capsys.readouterr().err

    def test_the_count_is_written_whole(self, monkeypatch, tmp_path):
        report, _runs = self._capped(monkeypatch, tmp_path)
        written: list[tuple[Path, str]] = []
        monkeypatch.setattr(watch_cli, "write_atomic",
                            lambda path, text, **kw: written.append((path, text)))
        assert watch_cli._run_audit_capped(report) == (4, False)
        assert written == [(watch_cli._audit_attempts_path(report), "1\n")]

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


# --- follow-up: a publish in doubt has its own exit code --------------------------------
# `PublishInDoubt` means the new run MAY be live. On the auto track it was
# caught as any failure (exit 1), and the journal track's code was not in
# the sweep's severity order, so an alert keyed on the code could miss it.


class TestAPublishInDoubt:
    def test_the_journal_track_passes_it_on_and_marks_nothing(self, poll_env, monkeypatch):
        _force_decision(monkeypatch, "generate")
        monkeypatch.setattr(watch_cli, "_generate",
                            lambda t, day, nd: (watch_cli.PUBLISH_IN_DOUBT_RC, None))
        assert watch_cli.cmd_poll(_poll_args()) == 8
        assert poll_env.marked == [] and poll_env.audit == [] and poll_env.rearm == []

    def test_the_auto_track_says_it_and_exits_with_its_code(self, poll_env, monkeypatch, capsys):
        from app.services.journal import reporting
        from app.services.reporting.report_files import PublishInDoubt

        def in_doubt(*a, **k):
            raise PublishInDoubt("NVDA_x.md: publishing g failed, and switching back failed: "
                                 "the NEW generation g may be live.")

        _force_decision(monkeypatch, "refuse")
        monkeypatch.setattr(watch_cli, "_generate_auto", _REAL_GENERATE_AUTO)
        monkeypatch.setattr(reporting, "build_report", in_doubt)
        assert watch_cli.cmd_poll(_poll_args()) == 8
        assert "the NEW generation g may be live" in capsys.readouterr().err
        assert poll_env.audit == [] and poll_env.rearm == []

    def test_it_outranks_every_other_sweep_code_and_is_named(self, sweep_env, monkeypatch):
        assert all(watch_cli._worst([8, c]) == 8 for c in (0, 1, 2, 3, 4, 5, 6, 7))
        sweep_env.table.update({"AAPL": "generate", "NVDA": "generate"})
        monkeypatch.setattr(watch_cli, "_generate",
                            lambda t, day, nd: (8 if t == "AAPL" else 1, None))
        assert watch_cli.cmd_sweep(_sweep_args()) == 8
        (title, text), = sweep_env.notified
        assert "AAPL: report publish IN DOUBT" in text

    def test_codes_outside_the_severity_order_fall_back_to_the_highest(self):
        """Every code a pass can return is ranked (8 and 9 included); the
        fallback for any other is the highest of them."""
        assert watch_cli._worst([10]) == 10 and watch_cli._worst([11, 10]) == 11


class TestAChildKilledByASignal:
    """rev28c_signal: a child killed by a signal (OOM, SIGKILL) returns a
    negative code, which `_worst` ranked below 0: beside a name that waited
    or completed, the sweep exited 0 with that entry left pending."""

    @staticmethod
    def _killed() -> int:
        import subprocess
        import sys

        return subprocess.run([sys.executable, "-c",
                               "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"]
                              ).returncode

    def test_it_counts_as_an_error(self):
        rc = self._killed()
        assert rc < 0
        assert watch_cli._worst([rc, 3]) == 1 and watch_cli._worst([rc, 0]) == 1
        assert watch_cli._worst([rc]) == 1 and watch_cli._worst([rc, 4]) == 1
        assert watch_cli._worst([rc, 8]) == 8 and watch_cli._worst([-1, 5]) == 1

    def test_the_sweep_exits_1_and_names_the_signal(self, sweep_env, monkeypatch):
        rc = self._killed()
        sweep_env.table.update({"AAPL": "generate", "NVDA": "refuse"})
        monkeypatch.setattr(watch_cli, "_generate", lambda t, day, nd: (rc, None))
        assert watch_cli.cmd_sweep(_sweep_args()) == 1
        (title, text), = sweep_env.notified
        assert f"AAPL: killed by signal {-rc}" in text


class TestTheSweepOwnsTheReportItDefers:
    """The pending marker names the sweep (or poll) that will audit the
    report, not the `journal.py report --defer-mark` child, which exits
    once it has published: `_generate` hands the child its identity."""

    def test_generate_hands_its_child_the_sweeps_identity(self, monkeypatch, tmp_path):
        import json
        import os

        monkeypatch.setattr(watch_cli, "SWEEP_LOCK", tmp_path / "sweep.lock")
        seen = []
        cmds = []
        monkeypatch.setattr(watch_cli.subprocess, "run", lambda cmd, **kw: seen.append(kw)
                            or cmds.append(cmd) or SimpleNamespace(returncode=0))
        assert watch_cli._generate("NVDA", "2026-08-26", True)[0] == 0
        assert watch_cli._generate("NVDA", "2026-08-26", True)[0] == 0
        assert cmds[0][-3:] == ["--date", "2026-08-26", "--no-docs"]
        owners = [json.loads(kw["env"]["FQE_REPORT_OWNER"]) for kw in seen]
        assert owners[0] == owners[1]  # one owner for the whole run
        assert owners[0]["pid"] == os.getpid() and owners[0]["host"] == os.uname().nodename
        assert owners[0]["lock"] == str(watch_cli.SWEEP_LOCK) and len(owners[0]["token"]) >= 16
        assert all(kw["env"]["PATH"] == os.environ["PATH"] for kw in seen)  # the rest as is
        assert "FQE_REPORT_OWNER" not in os.environ  # handed to the child only
        assert all(kw["cwd"] == watch_cli.ROOT for kw in seen)


# --- review of the 3b fix: the entry's report is PENDING while the sweep audits it --------
# `journal.py report --defer-mark` leaves a marker that makes a plain
# `journal.py report` (or the web page) refuse until `mark-reported`. Here
# the sweep's own flow, with the journal commands run for real: every way a
# pass ends either stamps (and clears the marker) or leaves the case
# retryable with the marker kept.


class TestAReportPendingItsAudit:
    DAY = "2026-08-26"  # poll_env's pinned thesis entry

    @staticmethod
    def _entry(store, day: str):
        from datetime import date, datetime

        from app.services.journal.schema_v2 import (
            Assumption,
            BeforeBlock,
            EntryV2,
            lock_entry,
        )

        d = date.fromisoformat(day)
        return store.save_v2(lock_entry(EntryV2(
            ticker="NVDA", day=d, opened=datetime(d.year, d.month, d.day, 9, tzinfo=UTC),
            before=BeforeBlock(thesis="data-center demand holds", conviction=3,
                               intended_action="hold",
                               assumptions=[Assumption(metric="revenue", comparator=">",
                                                       threshold=1.0, window="FY2026Q2",
                                                       source="10-Q",
                                                       resolve_by=date(2026, 12, 15))]))))

    def _journal(self, poll_env, monkeypatch, tmp_path, audits):
        from app.services.journal import reporting, store
        from app.services.reporting.report_files import replacing

        monkeypatch.setattr(store, "ENTRIES", tmp_path / "entries")
        path = self._entry(store, self.DAY)
        reports = tmp_path / "reports"
        reports.mkdir()
        monkeypatch.setattr(reporting, "REPORTS", reports)
        journal = _journal_module()
        self.journal, self.built = journal, []

        def build(*a, **k):
            self.built.append(a)
            out = reports / f"NVDA_{k['report_day']}.md"
            with replacing(out) as staged:  # published, as the real build does
                staged.report.write_text("# report\n")
                staged.ledger.write_text("{}")
            return out, "no acute signals"

        journal.build_report = build
        ns = {"no_docs": True, "fresh": True}
        # The sweep's own `journal.py report --defer-mark` and `mark-reported`,
        # run in this process: the child is handed the sweep's identity and
        # says which run it published (its --result-file).
        _journal_in_process(monkeypatch, journal)
        monkeypatch.setattr(watch_cli, "_generate", _REAL_GENERATE)
        monkeypatch.setattr(watch_cli, "_mark_reported", _REAL_MARK_REPORTED)
        seq = list(audits)
        monkeypatch.setattr(watch_cli, "_run_audit", lambda p: seq.pop(0) if len(seq) > 1 else seq[0])
        plain = lambda: journal.cmd_report(Namespace(ticker="NVDA", date=self.DAY,  # noqa: E731
                                                     defer_mark=False, **ns))
        return store, path, plain

    def test_a_retry_by_hand_during_the_audit_is_refused(self, poll_env, monkeypatch, tmp_path):
        """rev28c_pid, end to end: while the sweep audits the report (its
        `journal.py report --defer-mark` child long gone), the operator's
        `--retry` is refused; the audit passes and the sweep stamps the
        report it audited."""
        import os
        from unittest import mock

        _force_decision(monkeypatch, "generate")
        store, path, _ = self._journal(poll_env, monkeypatch, tmp_path, [0])
        retried = []

        def audit(report):
            with mock.patch.dict(os.environ):
                os.environ.pop("FQE_REPORT_OWNER", None)  # the operator's shell
                retried.append(self.journal.cmd_report(Namespace(
                    ticker="NVDA", date=self.DAY, defer_mark=False, retry=True,
                    no_docs=True, fresh=True)))
            return 0

        monkeypatch.setattr(watch_cli, "_run_audit", audit)
        assert watch_cli.cmd_poll(_poll_args()) == 0
        assert retried == [1] and len(self.built) == 1
        assert store.load_v2(path).reported is not None and store.report_pending(path) is None

    def test_a_failed_audit_names_the_entry_to_stamp(
            self, poll_env, monkeypatch, tmp_path, capsys):
        """rev28c_nodate: the exit-4 message named `journal.py mark-reported
        NVDA` without `--date`, which stamps the ticker's NEWEST entry: here
        a later one, never reported, and not the one whose report was
        audited (left unstamped and pending)."""
        import re
        import shlex

        _force_decision(monkeypatch, "generate")
        store, path, _ = self._journal(poll_env, monkeypatch, tmp_path, [4])
        newer = self._entry(store, "2026-09-01")
        assert watch_cli.cmd_poll(_poll_args()) == 4
        hints = re.findall(r"`journal\.py (mark-reported [^`]+)`", capsys.readouterr().err)
        assert len(hints) == 1
        assert self.journal.cmd_mark_reported(
            self.journal.build_parser().parse_args(shlex.split(hints[0]))) == 0
        assert store.load_v2(path).reported is not None and store.report_pending(path) is None
        assert store.load_v2(newer).reported is None

    def test_with_no_entry_pinned_the_hint_names_the_one_generated_for(
            self, poll_env, monkeypatch, capsys):
        """No `--date` to give the child (an ad-hoc poll without
        --entry-day): the report was generated for the newest entry, which
        the child's result names, and the hint names that entry and run —
        never `--date None`, nor whichever entry is newest when it is run."""
        monkeypatch.setattr(watch_cli, "_generate",
                            lambda t, day, nd: (0, _fake_generated(t, "2026-08-20")))
        monkeypatch.setattr(watch_cli, "_run_audit_capped", lambda report: (4, False))
        watch = _watch("NVDA", thesis_entry=None)
        assert watch_cli._act("NVDA", watch, Decision("generate", "x"), _poll_args()) == 4
        assert (f"`journal.py mark-reported NVDA --date 2026-08-20 --generation {'0' * 32}`."
                in capsys.readouterr().err)

    def test_a_failed_audit_keeps_it_pending_and_a_plain_report_refused(
            self, poll_env, monkeypatch, tmp_path):
        _force_decision(monkeypatch, "generate")
        store, path, plain = self._journal(poll_env, monkeypatch, tmp_path, [4])
        assert watch_cli.cmd_poll(_poll_args()) == 4
        assert store.load_v2(path).reported is None and store.report_pending(path) is not None
        assert plain() == 1

    def test_a_passing_audit_stamps_it_and_clears_the_marker(
            self, poll_env, monkeypatch, tmp_path):
        _force_decision(monkeypatch, "generate")
        store, path, _ = self._journal(poll_env, monkeypatch, tmp_path, [4, 0])
        assert watch_cli.cmd_poll(_poll_args()) == 4  # a failed audit first: the retry
        assert watch_cli.cmd_poll(_poll_args()) == 0
        assert store.load_v2(path).reported is not None and store.report_pending(path) is None

    def test_an_abandoned_audit_stamps_it_and_clears_the_marker(
            self, poll_env, monkeypatch, tmp_path):
        """Abandoning the audit completes the case (the brief is built
        without it): it is stamped like a passed one, so nothing stays
        pending to block the operator."""
        _force_decision(monkeypatch, "generate")
        store, path, _ = self._journal(poll_env, monkeypatch, tmp_path, [4])
        for _ in range(watch_cli.AUDIT_MAX_ATTEMPTS - 1):
            assert watch_cli.cmd_poll(_poll_args()) == 4
            assert store.report_pending(path) is not None
        assert watch_cli.cmd_poll(_poll_args()) == watch_cli.AUDIT_ABANDONED_RC
        assert store.load_v2(path).reported is not None and store.report_pending(path) is None


@pytest.mark.parametrize("action", ["generate", "refuse"])
@pytest.mark.parametrize("capped, noted", [((0, False), False), ((0, True), False),
                                           ((4, True), True)])
def test_the_abandoned_note_is_only_for_an_audit_that_failed_and_was_abandoned(
        poll_env, monkeypatch, action, capped, noted):
    """`_act` was changed for the publish in doubt; its other branches are
    pinned here: an abandoned counter read without a run (exit 0) or a
    passing audit is not reported as a failed one."""
    notes = []
    _force_decision(monkeypatch, action)
    monkeypatch.setattr(watch_cli, "_run_audit_capped", lambda report: capped)
    monkeypatch.setattr(watch_cli, "_abandoned_note", lambda *a: notes.append(a))
    watch_cli.cmd_poll(_poll_args())
    assert bool(notes) == noted


# --- Hermes re-audit of 84e65b0, finding 3: the report audited is the one generated ------
# `_act` generated with `journal.py report --defer-mark`, then audited the
# ticker's NEWEST report by mtime (`_latest_report`) and stamped the pinned
# entry for it. A same-ticker report published between the two (a manual run,
# the web UI, another entry's day) was audited instead: Hermes audited
# NVDA_2026-09-02.md and marked the 2026-09-01 entry. The child now says which
# report it published (`--result-file`, one temporary per run), the watcher
# audits exactly that generation, and `mark-reported --generation` stamps only
# that run.

_REAL_GENERATE = watch_cli._generate  # before poll_env stubs it
_REAL_MARK_REPORTED = watch_cli._mark_reported  # likewise
# What chose the report to audit before the fix (the newest by mtime); gone with it.
_REAL_LATEST_REPORT = getattr(watch_cli, "_latest_report", None)


def _journal_module():
    spec = importlib.util.spec_from_file_location("journal_cli_identity",
                                                  ROOT / "scripts" / "journal.py")
    journal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(journal)
    return journal


def _journal_in_process(monkeypatch, journal, after_report=None):
    """watch.py's `journal.py` children, run in this process: parsed by
    journal.py's own parser, with the environment the child is handed."""
    import os
    from unittest import mock

    ran: list[list[str]] = []

    def run(cmd, cwd=None, env=None, **kw):
        assert str(cmd[1]).endswith("journal.py"), cmd
        ran.append(list(cmd[2:]))
        args = journal.build_parser().parse_args(cmd[2:])
        with mock.patch.dict(os.environ, env or {}):
            rc = args.func(args)
        if cmd[2] == "report" and after_report is not None:
            after_report()
        return SimpleNamespace(returncode=rc)

    monkeypatch.setattr(watch_cli.subprocess, "run", run)
    return ran


class TestTheWatcherAuditsTheReportItGenerated:
    DAY = "2026-08-26"  # poll_env's pinned thesis entry

    def _setup(self, poll_env, monkeypatch, tmp_path, *, meanwhile_day=None):
        import os
        import time

        from app.services.journal import reporting, store
        from app.services.reporting.report_files import read_live, replacing

        _force_decision(monkeypatch, "generate")
        monkeypatch.setattr(store, "ENTRIES", tmp_path / "entries")
        path = TestAReportPendingItsAudit._entry(store, self.DAY)
        reports = tmp_path / "reports"
        reports.mkdir()
        monkeypatch.setattr(reporting, "REPORTS", reports)
        journal = _journal_module()

        def publish(day):
            out = reports / f"NVDA_{day}.md"
            with replacing(out) as staged:
                staged.report.write_text(f"# NVDA {day}\n")
                staged.ledger.write_text("{}")
            return out

        journal.build_report = lambda ticker, with_docs=True, report_day=None, fresh=False, **k: (
            publish(report_day), "no acute signals")

        def meanwhile():
            # Another same-ticker report goes live between the generate and
            # the audit's choice of report, newer by mtime.
            other = publish(meanwhile_day)
            later = time.time() + 60
            os.utime(read_live(other).report, (later, later))

        ran = _journal_in_process(monkeypatch, journal,
                                  after_report=meanwhile if meanwhile_day else None)
        if _REAL_LATEST_REPORT is not None:
            monkeypatch.setattr(watch_cli, "_latest_report",
                                lambda t, d: _REAL_LATEST_REPORT(t, reports))
        monkeypatch.setattr(watch_cli, "_generate", _REAL_GENERATE)
        monkeypatch.setattr(watch_cli, "_mark_reported", _REAL_MARK_REPORTED)
        return store, path, reports, ran

    def test_a_report_published_meanwhile_is_not_the_one_audited(
            self, poll_env, monkeypatch, tmp_path):
        """Hermes's reproduction: NVDA_2026-09-02.md went live while the
        pinned entry's report was being generated, and was audited."""
        from app.services.reporting.report_files import read_live

        store, path, reports, ran = self._setup(poll_env, monkeypatch, tmp_path,
                                                meanwhile_day="2026-09-02")
        assert watch_cli.cmd_poll(_poll_args()) == 0
        mine = read_live(reports / f"NVDA_{self.DAY}.md")
        assert poll_env.audit == [mine.report]  # its own generation, not the newest report
        assert poll_env.brief == [("NVDA", mine.report)]
        (mark,) = [argv for argv in ran if argv[0] == "mark-reported"]
        assert mark[mark.index("--date") + 1] == self.DAY
        assert mark[mark.index("--generation") + 1] == mine.generation_id
        assert store.load_v2(path).reported is not None and store.report_pending(path) is None

    def test_a_rebuild_of_its_own_name_meanwhile_is_not_the_one_audited(
            self, poll_env, monkeypatch, tmp_path):
        """The same live name rebuilt meanwhile: the run the sweep generated
        is the one audited (the real audit then keeps its audit with that
        run and exits 1, as for a rebuild during the audit: run_audit's
        tests), and the stamp names that run, which the entry's pending
        marker records."""
        from app.services.reporting.report_files import generations

        store, path, reports, ran = self._setup(poll_env, monkeypatch, tmp_path,
                                                meanwhile_day=self.DAY)
        assert watch_cli.cmd_poll(_poll_args()) == 0
        first, second = generations(reports / f"NVDA_{self.DAY}.md")
        assert poll_env.audit == [first / f"NVDA_{self.DAY}.md"]
        (mark,) = [argv for argv in ran if argv[0] == "mark-reported"]
        assert mark[mark.index("--generation") + 1] in first.name

    def test_the_audit_attempts_counter_is_kept_at_the_live_name(self, tmp_path):
        """A retry rebuilds the report (a new generation); a counter kept in
        the generation would start again at 0 and never cap the audit."""
        from app.services.reporting.report_files import GENERATIONS_DIR

        gen = tmp_path / GENERATIONS_DIR / "NVDA_2026-08-26" / "20260826T000000Z_0001_ab"
        assert watch_cli._audit_attempts_path(gen / "NVDA_2026-08-26.md") == (
            tmp_path / "NVDA_2026-08-26_audit.attempts")


class TestTheGenerateResultFile:
    def _run(self, monkeypatch, write):
        """`_generate` with its child faked: ``write(path)`` is what the
        child writes to its --result-file."""
        seen = []

        def run(cmd, **kw):
            path = Path(cmd[cmd.index("--result-file") + 1])
            seen.append(path)
            assert path.parent.is_dir() and not path.exists()  # fresh, never a stale one
            write(path)
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(watch_cli.subprocess, "run", run)
        return seen

    def _published(self, tmp_path, day="2026-08-26", ticker="NVDA"):
        from app.services.reporting.report_files import read_live, replacing

        out = tmp_path / "reports" / f"{ticker}_{day}.md"
        out.parent.mkdir(exist_ok=True)
        with replacing(out) as staged:
            staged.report.write_text("# r\n")
            staged.ledger.write_text("{}")
        return read_live(out)

    def _doc(self, run, **over):
        base = {"report": str(run.report), "generation_id": run.generation_id,
                "ticker": "NVDA", "entry_day": "2026-08-26", "before_sha256": "ab" * 32}
        base.update(over)
        return base

    def test_each_run_has_its_own_result_file_removed_after(self, monkeypatch, tmp_path):
        import json

        run = self._published(tmp_path)
        docs = [self._doc(run), None]

        def write(path):
            doc = docs.pop(0)
            if doc is not None:
                path.write_text(json.dumps(doc))

        seen = self._run(monkeypatch, write)
        rc, made = watch_cli._generate("NVDA", "2026-08-26", False)
        assert rc == 0 and made == watch_cli.Generated(
            run.report, run.generation_id, "NVDA", "2026-08-26", "ab" * 32)
        # The second run writes nothing: the first's result is never read as its own.
        assert watch_cli._generate("NVDA", "2026-08-26", False) == (0, None)
        assert len(seen) == 2 and seen[0] != seen[1]
        assert not any(p.exists() or p.parent.exists() for p in seen)

    def test_the_entry_day_of_an_unpinned_run_comes_from_its_result(self, monkeypatch, tmp_path):
        import json

        run = self._published(tmp_path)
        self._run(monkeypatch, lambda p: p.write_text(json.dumps(self._doc(run))))
        rc, made = watch_cli._generate("NVDA", None, False)
        assert rc == 0 and made.entry_day == "2026-08-26"

    @pytest.mark.parametrize("case", [
        "missing", "not json", "not an object", "another ticker", "another day",
        "another report", "another generation", "no day"])
    def test_a_result_that_is_not_this_run_audits_and_marks_nothing(
            self, poll_env, monkeypatch, tmp_path, capsys, case):
        import json

        run = self._published(tmp_path)
        other = self._published(tmp_path, day="2026-09-02")
        doc = {"missing": None, "not json": "{", "not an object": "[]",
               "another ticker": self._doc(run, ticker="AAPL"),
               "another day": self._doc(run, entry_day="2026-09-02"),
               "another report": self._doc(other, entry_day="2026-08-26"),
               "another generation": self._doc(run, generation_id=other.generation_id),
               "no day": self._doc(run, entry_day=None)}[case]

        def write(path):
            if doc is not None:
                path.write_text(doc if isinstance(doc, str) else json.dumps(doc))

        monkeypatch.setattr(watch_cli, "_generate", _REAL_GENERATE)
        self._run(monkeypatch, write)
        # "no day": an unpinned run, whose day can only come from the result.
        watch = (_watch("NVDA", thesis_entry=None) if case == "no day"
                 else watch_cli._find_watch("NVDA"))
        assert watch_cli._act("NVDA", watch, Decision("generate", "x"), _poll_args()) == 4
        err = capsys.readouterr().err
        assert poll_env.audit == [] and poll_env.marked == []
        assert "journal.py report exited 0, but" in err
        assert "none is audited or marked" in err and "NOT marked reported" in err

    def test_with_no_audit_an_unidentified_run_is_not_marked(self, poll_env, monkeypatch):
        _force_decision(monkeypatch, "generate")
        monkeypatch.setattr(watch_cli, "_generate", lambda t, day, nd: (0, None))
        assert watch_cli.cmd_poll(_poll_args(no_audit=True)) == 4
        assert poll_env.marked == []

    def test_mark_reported_is_pinned_to_the_entry_and_the_generation(self, monkeypatch):
        cmds = []
        monkeypatch.setattr(watch_cli.subprocess, "run",
                            lambda cmd, **kw: cmds.append(cmd) or SimpleNamespace(returncode=0))
        assert watch_cli._mark_reported("NVDA", "2026-08-26", "c" * 32) == 0
        assert cmds[0][2:] == ["mark-reported", "NVDA", "--date", "2026-08-26",
                               "--generation", "c" * 32]


class TestPublishedNotStamped:
    """Hermes re-audit of 84e65b0, finding 4: `journal.py report` exits 9
    when its report went live and the entry could not be stamped."""

    def test_nine_is_ranked_below_a_publish_in_doubt_and_above_everything_else(self):
        assert watch_cli.PUBLISHED_NOT_STAMPED_RC == 9
        assert watch_cli._worst([9, 8]) == 8
        assert all(watch_cli._worst([9, c]) == 9 for c in (0, 1, 2, 3, 4, 5, 6, 7, -9))
        assert watch_cli.SEVERITY_ORDER[:3] == (8, 9, 1)

    def test_nine_is_named_in_the_notification(self, sweep_env, monkeypatch):
        sweep_env.table.update({"AAPL": "generate"})
        monkeypatch.setattr(watch_cli, "_generate", lambda t, day, nd: (9, None))
        assert watch_cli.cmd_sweep(_sweep_args()) == 9
        (title, text), = sweep_env.notified
        assert "AAPL: report published but NOT stamped" in text
        assert watch_cli._rc_words(9).startswith("report published but NOT stamped")


# --- review of 6563168 -------------------------------------------------------------------


def test_a_temporary_directory_that_cannot_be_made_is_an_ordinary_failure(
        poll_env, monkeypatch, capsys):
    """`mkdtemp` failing (a full or read-only /tmp) was a traceback out of the
    poll; it is exit 1 for that name, nothing generated."""
    import errno

    def cannot(**kw):
        raise OSError(errno.ENOSPC, "No space left on device")

    ran = []
    monkeypatch.setattr(watch_cli.tempfile, "mkdtemp", cannot)
    monkeypatch.setattr(watch_cli.subprocess, "run", lambda *a, **k: ran.append(a))
    assert _REAL_GENERATE("NVDA", "2026-08-26", False) == (1, None)
    assert ran == []
    assert "No space left on device" in capsys.readouterr().err
    _force_decision(monkeypatch, "generate")
    monkeypatch.setattr(watch_cli, "_generate", _REAL_GENERATE)
    assert watch_cli.cmd_poll(_poll_args()) == 1
    assert poll_env.audit == [] and poll_env.marked == []


def test_nine_in_a_notification_says_both_ways_it_ends():
    """rev31c_exit9_msg: a plain report's 9 names the stamp command; the
    sweep's is rebuilt and audited by its next pass, never stamped by hand."""
    words = watch_cli._rc_words(9)
    assert words.startswith("report published but NOT stamped")
    assert "the next pass rebuilds and audits it" in words
