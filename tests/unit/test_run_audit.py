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


def test_invokes_the_resolved_cli_path_not_a_bare_name(tmp_path, monkeypatch):
    report = tmp_path / "KTOS_2026-07-31.md"
    report.write_text("# report")
    seen = {}
    monkeypatch.setenv("CLAUDE_BIN", "/opt/claude/bin/claude")

    def run(argv, **k):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout="AUDIT", stderr="")

    monkeypatch.setattr(run_audit.subprocess, "run", run)
    assert run_audit.run_audit(report) == 0
    assert seen["argv"][:2] == ["/opt/claude/bin/claude", "-p"]


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


# --- Hermes deep audit, finding 1: an audit belongs to one report generation ------


def _published(tmp_path, tag):
    from app.services.reporting.report_files import replacing

    report = tmp_path / "KTOS_2026-07-31.md"
    with replacing(report) as staged:
        staged.ledger.write_text("{}")
        staged.report.write_text(f"# {tag} report")
    return report, staged.generation_id


def test_the_audit_names_the_generation_it_read(tmp_path, monkeypatch):
    from app.services.reporting.report_files import read_live

    report, gid = _published(tmp_path, "first")
    monkeypatch.setattr(
        run_audit.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="AUDIT TEXT", stderr=""),
    )
    assert run_audit.run_audit(report) == 0
    audit = tmp_path / "KTOS_2026-07-31_audit.md"
    assert audit.read_text() == f"<!-- generation: {gid} -->\n\nAUDIT TEXT"
    assert read_live(report).audit == audit


def test_a_report_rebuilt_during_the_audit_does_not_get_it(tmp_path, monkeypatch, capsys):
    """The audit (up to 30 minutes) read one run; a rebuild published another
    meanwhile, and the audit was written beside it, as its audit."""
    report, _gid = _published(tmp_path, "first")

    def audit_while_rebuilt(*a, **k):
        _published(tmp_path, "second")
        return SimpleNamespace(returncode=0, stdout="AUDIT OF THE FIRST RUN", stderr="")

    monkeypatch.setattr(run_audit.subprocess, "run", audit_while_rebuilt)
    assert run_audit.run_audit(report) == 1
    assert not (tmp_path / "KTOS_2026-07-31_audit.md").exists()
    assert "rebuilt while it ran" in capsys.readouterr().err
    staging = tmp_path / ".staging"
    assert [p for p in staging.iterdir() if p.suffix != ".lock"] == []


def test_a_failed_audit_write_leaves_no_temporary_file(tmp_path, monkeypatch):
    """The audit was written to a staging file and replaced into place; a
    failed replace left the staging file behind as unexplained staged work."""
    import app.services.reporting.report_files as rf

    report, gid = _published(tmp_path, "first")

    def full(src, dst):
        raise OSError("ENOSPC")

    monkeypatch.setattr(rf.os, "replace", full)
    try:
        run_audit.publish_audit(report, gid, "AUDIT")
    except OSError:
        pass
    monkeypatch.undo()
    left = [p for p in tmp_path.rglob("*") if p.is_file() and p.suffix not in (".lock",)]
    assert sorted(p.name for p in left) == ["KTOS_2026-07-31.ledger.json", "KTOS_2026-07-31.md"]


def test_an_audit_of_a_report_set_aside_is_not_published(tmp_path, capsys):
    """A report from before generations names none; set aside while its
    audit ran, `None == None` published the audit beside no report."""
    from app.services.reporting.report_files import archive_existing

    report = tmp_path / "KTOS_2026-07-31.md"
    report.write_text("# report from before generations")
    archive_existing(report)
    assert run_audit.publish_audit(report, None, "AUDIT") == 1
    assert not (tmp_path / "KTOS_2026-07-31_audit.md").exists()
    assert "no longer live" in capsys.readouterr().err
