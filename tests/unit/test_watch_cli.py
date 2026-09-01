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
        ticker="NVDA", since=None, interval=0.01, max_wait=1.0, once=True,
        dry_run=False, no_docs=False, no_auto=False, no_audit=False,
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
def poll_env(monkeypatch):
    """Neutralize network + subprocess; record what the poll routed to."""
    calls = SimpleNamespace(generate=[], generate_auto=[], audit=[])
    monkeypatch.setattr(watch_cli, "SecClient", _FakeClient)
    monkeypatch.setattr(
        watch_cli, "_find_watch",
        lambda t: watch_cli.wl.Watch(
            ticker=t, print_at=watch_cli._now("2026-08-26T20:20:00+00:00")
        ),
    )
    monkeypatch.setattr(
        watch_cli, "_generate",
        lambda t, nd: calls.generate.append(t) or 0,
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
        watch_cli, "_latest_report", lambda t, d: Path("/tmp/fake_journal.md")
    )
    return calls


def _force_decision(monkeypatch, action: str):
    monkeypatch.setattr(
        watch_cli, "decide",
        lambda watch, submissions, since=None: Decision(action, f"forced {action}"),
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
        assert poll_env.generate == ["NVDA"]
        assert poll_env.generate_auto == []
        assert poll_env.audit == [Path("/tmp/fake_journal.md")]

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


class TestFreshPropagation:
    def test_journal_generate_always_passes_fresh(self, monkeypatch):
        recorded = {}

        def fake_run(cmd, cwd=None):
            recorded["cmd"] = cmd
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(watch_cli.subprocess, "run", fake_run)
        assert watch_cli._generate("NVDA", no_docs=False) == 0
        assert "--fresh" in recorded["cmd"]

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

        out, overall = reporting.build_report(
            "nvda", with_docs=False, fresh=True,
            out_dir=tmp_path / "auto", banner="> BANNER LINE",
        )
        assert client_kwargs.get("fresh") is True
        assert out.parent == tmp_path / "auto"
        assert out.name.startswith("NVDA_")
        assert out.read_text().startswith("> BANNER LINE\n\nENGINE REPORT BODY")
        assert overall is None

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
