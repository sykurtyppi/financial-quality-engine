"""scripts/preflight.py: what an unattended sweep needs, checked before the
first print. Every check says what to do; FAIL is reserved for what would
make the season's runs fail or unattributable."""

from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import subprocess
import sys
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services.ingestion.sec_client import SecClientError

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("preflight", ROOT / "scripts" / "preflight.py")
preflight = importlib.util.module_from_spec(_spec)
sys.modules["preflight"] = preflight  # dataclasses resolve their module by name
_spec.loader.exec_module(preflight)

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)


def _row(ticker, print_at, **extra):
    return {"ticker": ticker, "print_at": print_at, "baseline_accession": "0000000000-26-000001",
            "expected_report_date": "2026-09-30", **extra}


def _journal(root, rows, portfolio="AAPL\n"):
    (root / "journal").mkdir(parents=True, exist_ok=True)
    (root / "journal" / "watchlist.json").write_text(json.dumps({"watchlist": rows}))
    if portfolio is not None:
        (root / "journal" / "portfolio.txt").write_text(portfolio)


def _by_name(results):
    return {r.name: r for r in results}


class TestEngine:
    @pytest.mark.parametrize("stated, expect, status", [
        ("c62095dd997f (clean checkout)", None, "PASS"),
        ("c62095dd997f (clean checkout)", "c62095d", "PASS"),
        ("c62095dd997f (clean checkout)", "c62095dd997f6a9154455835c27a2800d5f8a16f", "PASS"),
        ("c62095dd997f (clean checkout)", "02c2aac", "FAIL"),
        ("c62095dd997f (clean checkout)", "c62", "FAIL"),  # below git's 7-char minimum
        ("c62095dd997f + uncommitted changes to the engine code (not reproducible from "
         "c62095dd997f)", None, "WARN"),
        ("c62095dd997f + uncommitted changes to the engine code (not reproducible from "
         "c62095dd997f)", "c62095d", "FAIL"),
        ("c62095dd997f (could not check the checkout for uncommitted changes)", "c62095d",
         "FAIL"),
        ("c62095d (stated by FQE_ENGINE_COMMIT; not a git checkout)", "c62095d", "WARN"),
        ("c62095d (stated by FQE_ENGINE_COMMIT; not a git checkout)", "02c2aac", "FAIL"),
        ("unknown (not run from a git checkout; set FQE_ENGINE_COMMIT)", None, "WARN"),
        ("unknown (not run from a git checkout; set FQE_ENGINE_COMMIT)", "c62095d", "FAIL"),
    ])
    def test_a_pinned_season_runs_one_clean_commit(self, monkeypatch, stated, expect, status):
        monkeypatch.setattr(preflight, "engine_commit", lambda: stated)
        assert preflight.check_engine(expect).status == status


class TestIdentity:
    @pytest.mark.parametrize("value, status", [
        (None, "FAIL"), ("   ", "FAIL"), ("Your Name you@example.com", "FAIL"),
        ("Jane Doe", "WARN"), ("jane@example.com", "WARN"),
        ("Jane Doe jane@example.com", "PASS"),
    ])
    def test_sec_asks_for_a_name_and_an_email(self, monkeypatch, value, status):
        if value is None:
            monkeypatch.delenv("EDGAR_IDENTITY", raising=False)
        else:
            monkeypatch.setenv("EDGAR_IDENTITY", value)
        result = preflight.check_identity()
        assert result.status == status
        if value and status == "PASS":
            assert value not in result.detail  # never echoed back


class TestClaudeCli:
    def _exe(self, tmp_path):
        exe = tmp_path / "claude"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        return exe

    def test_the_scheduler_path_not_this_shells(self, tmp_path, monkeypatch):
        """On the shell's PATH is not enough: cron and launchd give /usr/bin:/bin."""
        monkeypatch.delenv("CLAUDE_BIN", raising=False)
        monkeypatch.setenv("PATH", f"{self._exe(tmp_path).parent}{os.pathsep}/usr/bin:/bin")
        monkeypatch.setattr(preflight.headless, "FALLBACK", tmp_path / "nowhere" / "claude")
        monkeypatch.setattr(preflight, "SCHEDULER_PATH", str(tmp_path / "empty"))
        result = preflight.check_claude_cli()
        assert result.status == "FAIL" and "CLAUDE_BIN" in result.detail

    def test_claude_bin_is_what_the_scheduler_runs(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_BIN", str(self._exe(tmp_path)))
        assert preflight.check_claude_cli().status == "PASS"

    def test_a_claude_bin_that_is_not_executable_fails(self, tmp_path, monkeypatch):
        (tmp_path / "claude").write_text("")
        monkeypatch.setenv("CLAUDE_BIN", str(tmp_path / "claude"))
        assert preflight.check_claude_cli().status == "FAIL"

    def test_the_fallback_counts(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_BIN", raising=False)
        monkeypatch.setattr(preflight, "SCHEDULER_PATH", str(tmp_path / "empty"))
        monkeypatch.setattr(preflight.headless, "FALLBACK", self._exe(tmp_path))
        assert preflight.check_claude_cli().status == "PASS"


class TestClaudeLogin:
    def _run(self, stdout, returncode=0, stderr=""):
        seen = {}

        def run(argv, **kw):
            seen.update(argv=argv, env=kw["env"])
            return subprocess.CompletedProcess(argv, returncode, stdout, stderr)
        return run, seen

    def test_ok_from_a_scheduler_shaped_env(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_BIN", "/opt/claude")
        monkeypatch.setenv("SOME_SHELL_VAR", "x")
        run, seen = self._run("OK\n")
        assert preflight.check_claude_login(run).status == "PASS"
        assert seen["argv"][:2] == ["/opt/claude", "-p"]
        assert set(seen["env"]) == {"HOME", "USER", "PATH"}
        assert seen["env"]["PATH"] == preflight.SCHEDULER_PATH

    @pytest.mark.parametrize("stdout, code, stderr", [
        ("", 1, "Not logged in · Please run /login"),
        ("Sure! OK", 0, ""),
    ])
    def test_anything_but_ok_fails(self, stdout, code, stderr):
        run, _ = self._run(stdout, code, stderr)
        result = preflight.check_claude_login(run)
        assert result.status == "FAIL" and "/login" in result.detail

    def test_a_cli_that_cannot_start(self):
        def run(argv, **kw):
            raise FileNotFoundError(argv[0])
        assert preflight.check_claude_login(run).status == "FAIL"


class TestJournal:
    def test_a_ready_calendar(self, tmp_path):
        _journal(tmp_path, [_row("AAPL", "2026-10-29T20:25:00Z", thesis_entry="2026-10-20",
                                 thesis_sha256="0" * 64)])
        r = _by_name(preflight.check_journal(tmp_path, NOW))
        assert r["portfolio"].status == "PASS" and r["watchlist"].status == "PASS"
        assert "theses" not in r and "print hints" not in r

    def test_no_portfolio_warns(self, tmp_path):
        _journal(tmp_path, [_row("AAPL", "2026-10-29T20:25:00Z")], portfolio=None)
        assert _by_name(preflight.check_journal(tmp_path, NOW))["portfolio"].status == "WARN"

    def test_past_hints_and_missing_theses_warn(self, tmp_path):
        _journal(tmp_path, [_row("AMKR", "2026-09-30T20:00:00Z"),
                            _row("NVDA", "2026-11-17T21:00:00Z")])
        r = _by_name(preflight.check_journal(tmp_path, NOW))
        assert r["print hints"].status == "WARN" and "AMKR" in r["print hints"].detail
        assert r["theses"].status == "WARN" and r["theses"].detail.startswith("2 of 2")

    def test_a_row_that_can_never_fire_fails(self, tmp_path):
        _journal(tmp_path, [{"ticker": "OLD", "print_at": "2026-10-29T20:25:00Z"}])
        r = _by_name(preflight.check_journal(tmp_path, NOW))
        assert r["watchlist"].status == "FAIL" and "OLD" in r["watchlist"].detail

    def test_a_broken_watchlist_fails(self, tmp_path):
        (tmp_path / "journal").mkdir()
        (tmp_path / "journal" / "watchlist.json").write_text("{not json")
        assert _by_name(preflight.check_journal(tmp_path, NOW))["watchlist"].status == "FAIL"

    def test_an_empty_watchlist_warns(self, tmp_path):
        _journal(tmp_path, [])
        assert _by_name(preflight.check_journal(tmp_path, NOW))["watchlist"].status == "WARN"


class TestWatchlistPrivacy:
    @pytest.mark.parametrize("code, status", [(0, "PASS"), (1, "WARN")])
    def test_a_synced_watchlist_is_flagged_before_it_is_committed(self, tmp_path, code, status):
        def run(argv, **kw):
            assert argv[-1] == "journal/watchlist.json"
            return subprocess.CompletedProcess(argv, code)
        assert preflight.check_watchlist_private(tmp_path, run).status == status


class TestScheduler:
    @pytest.mark.parametrize("system, argv0, listing, status", [
        ("Darwin", "launchctl", f"-\t0\t{preflight.LABEL}\n", "PASS"),
        ("Darwin", "launchctl", "-\t0\tcom.apple.other\n", "WARN"),
        ("Linux", "crontab", "0 * * * * cd /x && python scripts/watch.py sweep\n", "PASS"),
        ("Linux", "crontab", "", "WARN"),
    ])
    def test_finds_the_hourly_sweep(self, system, argv0, listing, status):
        def run(argv, **kw):
            assert argv[0] == argv0
            return subprocess.CompletedProcess(argv, 0, listing, "")
        assert preflight.check_scheduler(run, system).status == status

    def test_no_crontab_is_a_warning_not_a_crash(self):
        def run(argv, **kw):
            raise FileNotFoundError(argv[0])
        assert preflight.check_scheduler(run, "Linux").status == "WARN"


class TestEdgarLive:
    def test_one_fresh_request(self):
        made = []

        class Client:
            def submissions_by_cik(self, cik):
                made.append(cik)
                return {"name": "Apple Inc."}
        result = preflight.check_edgar_live(lambda cache: Client())
        assert result.status == "PASS" and made == [preflight.PROBE_CIK]

    def test_a_403_says_what_it_may_be(self):
        class Client:
            def submissions_by_cik(self, cik):
                raise SecClientError("HTTP 403 for https://data.sec.gov/submissions/x")
        result = preflight.check_edgar_live(lambda cache: Client())
        assert result.status == "FAIL" and "rate block" in result.detail


class TestLaunchd:
    def test_the_plist_runs_the_sweep_from_this_checkout(self, tmp_path):
        env = {"EDGAR_IDENTITY": "Jane Doe jane@example.com", "CLAUDE_BIN": "/opt/claude"}
        plist = plistlib.loads(preflight.launchd_plist(tmp_path, 3600, env))
        assert plist["Label"] == preflight.LABEL
        assert plist["ProgramArguments"][1:] == [
            str(tmp_path / "scripts" / "watch.py"), "sweep", "--portfolio",
            "journal/portfolio.txt"]
        assert plist["WorkingDirectory"] == str(tmp_path)
        assert plist["EnvironmentVariables"] == {
            "EDGAR_IDENTITY": "Jane Doe jane@example.com", "CLAUDE_BIN": "/opt/claude",
            "PATH": preflight.SCHEDULER_PATH}
        assert plist["StartInterval"] == 3600 and plist["RunAtLoad"] is True

    def test_without_an_identity_the_placeholder_is_left_to_edit(self, tmp_path):
        plist = plistlib.loads(preflight.launchd_plist(tmp_path, 3600, {}))
        assert plist["EnvironmentVariables"]["EDGAR_IDENTITY"] == preflight.IDENTITY_PLACEHOLDER

    def test_an_interval_that_would_hammer_edgar_is_refused(self):
        with pytest.raises(SystemExit):
            preflight.main(["launchd", "--interval", "60"])


class TestMain:
    @pytest.mark.parametrize("statuses, code", [
        (["PASS", "PASS"], 0), (["PASS", "WARN"], 0), (["WARN", "FAIL"], 1)])
    def test_exit_code_is_one_only_on_a_failure(self, monkeypatch, capsys, statuses, code):
        monkeypatch.setattr(preflight, "run_checks", lambda args: [
            preflight.Result(f"c{i}", s, "d") for i, s in enumerate(statuses)])
        assert preflight.main([]) == code
        assert ("NOT READY" in capsys.readouterr().out) == (code == 1)

    def test_paid_and_networked_checks_only_when_asked(self, monkeypatch, tmp_path):
        calls = []
        for name in ("check_python", "check_identity", "check_claude_cli", "check_claude_login",
                     "check_watchlist_private", "check_scheduler", "check_edgar_live"):
            monkeypatch.setattr(preflight, name, lambda *a, _n=name, **k: (
                calls.append(_n) or preflight.Result(_n, "PASS", "")))
        monkeypatch.setattr(preflight, "check_engine", lambda expect: preflight.Result(
            "engine", "PASS", ""))
        monkeypatch.setattr(preflight, "check_journal", lambda root: [])
        preflight.run_checks(Namespace(expect=None, live=False, claude_login=False), tmp_path)
        assert "check_edgar_live" not in calls and "check_claude_login" not in calls
        calls.clear()
        preflight.run_checks(Namespace(expect=None, live=True, claude_login=True), tmp_path)
        assert "check_edgar_live" in calls and "check_claude_login" in calls
