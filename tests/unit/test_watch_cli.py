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
        no_brief=False,
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
    # Re-arming persists to the watchlist; never let a unit test touch the
    # real journal/watchlist.json.
    monkeypatch.setattr(
        watch_cli, "_rearm",
        lambda watch, decision, submissions: calls.rearm.append((watch.ticker, decision.action)),
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
                no_auto=False, no_audit=False, no_brief=False, verbose=False)
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

    def test_brief_failure_does_not_change_the_exit_code(self, poll_env, monkeypatch):
        _force_decision(monkeypatch, "generate")
        monkeypatch.setattr(watch_cli, "_run_brief", lambda t, p: 2)
        assert watch_cli.cmd_poll(_poll_args()) == 0
        assert poll_env.marked  # journal case still completed


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
        assert watch_cli.cmd_poll(_poll_args()) == 0
        assert poll_env.marked  # the case completed; only the re-arm failed
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
        monkeypatch.setattr(watch_cli, "_rearm", rearm)
        assert watch_cli.cmd_sweep(_sweep_args()) == 0
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
