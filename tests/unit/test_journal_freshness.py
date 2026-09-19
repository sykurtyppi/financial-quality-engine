"""A generated report LOCKS the thesis against what it fetched, so the manual
paths bypass the EDGAR cache by default: on a filing day a <24h cached answer
can predate the very filing the case is about."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("journal_cli", ROOT / "scripts" / "journal.py")
journal_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(journal_cli)


def _fresh_for(monkeypatch, argv: list[str]) -> bool:
    seen = {}
    monkeypatch.setattr(journal_cli, "cmd_report", lambda a: seen.setdefault("a", a) and 0)
    monkeypatch.setattr(journal_cli, "cmd_open", lambda a: seen.setdefault("a", a) and 0)
    monkeypatch.setattr(sys, "argv", ["journal.py", *argv])
    with contextlib.redirect_stdout(io.StringIO()):
        journal_cli.main()
    return seen["a"].fresh


@pytest.mark.parametrize("argv,expected", [
    (["report", "NVDA"], True),          # the documented command
    (["report", "NVDA", "--fresh"], True),   # kept for scripts and the runbook
    (["report", "NVDA", "--no-fresh"], False),
    (["open", "NVDA"], True),            # `open` generates a report too
    (["open", "NVDA", "--no-fresh"], False),
])
def test_freshness_defaults(monkeypatch, argv, expected):
    assert _fresh_for(monkeypatch, argv) is expected
