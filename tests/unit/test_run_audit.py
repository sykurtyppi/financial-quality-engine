"""Headless audit runner: prompt/output-path derivation and failure handling.

The subprocess itself is mocked — Claude output is not fixture-able and the
runner's only jobs are building the invocation, saving stdout, and failing
loudly without touching the engine report.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location("run_audit", ROOT / "scripts" / "run_audit.py")
run_audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_audit)


def test_prompt_names_ticker_report_and_headless_rule():
    prompt = run_audit.build_prompt("NVDA", Path("reports/auto/NVDA_2026-08-26.md"))
    assert "NVDA" in prompt
    assert "reports/auto/NVDA_2026-08-26.md" in prompt
    assert "earnings-audit" in prompt
    assert "UNAVAILABLE" in prompt


def test_audit_output_path_sits_beside_the_report():
    out = run_audit.audit_output_path(Path("reports/auto/NVDA_2026-08-26.md"))
    assert out == Path("reports/auto/NVDA_2026-08-26_audit.md")


def test_success_writes_stdout_next_to_report(tmp_path, monkeypatch):
    report = tmp_path / "KTOS_2026-07-31.md"
    report.write_text("# report")
    monkeypatch.setattr(
        run_audit.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="AUDIT TEXT", stderr=""),
    )
    assert run_audit.run_audit(report) == 0
    assert (tmp_path / "KTOS_2026-07-31_audit.md").read_text() == "AUDIT TEXT"


def test_nonzero_exit_fails_without_writing(tmp_path, monkeypatch):
    report = tmp_path / "KTOS_2026-07-31.md"
    report.write_text("# report")
    monkeypatch.setattr(
        run_audit.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    assert run_audit.run_audit(report) == 1
    assert not (tmp_path / "KTOS_2026-07-31_audit.md").exists()


def test_timeout_fails_cleanly(tmp_path, monkeypatch):
    report = tmp_path / "KTOS_2026-07-31.md"
    report.write_text("# report")

    def raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    monkeypatch.setattr(run_audit.subprocess, "run", raise_timeout)
    assert run_audit.run_audit(report, timeout=1) == 1


def test_missing_claude_cli_fails_cleanly(tmp_path, monkeypatch):
    report = tmp_path / "KTOS_2026-07-31.md"
    report.write_text("# report")

    def raise_missing(*a, **k):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(run_audit.subprocess, "run", raise_missing)
    assert run_audit.run_audit(report) == 1


def test_missing_report_refuses(tmp_path):
    assert run_audit.run_audit(tmp_path / "nope.md") == 1
