"""Decision-impact journal CLI — regression tests for the thesis/report lock.

The report-generation subprocess is mocked, so these run offline. The load-
bearing case is `test_report_generates_on_fresh_entry`: the `reported:` guard
once used `\\s*\\S`, whose `\\s` spanned the newline into the next heading and
false-tripped on EVERY fresh entry — silently breaking the whole happy path.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("journal_cli", ROOT / "scripts" / "journal.py")
journal = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(journal)


def _open(ticker: str, thesis: str | None, conviction: int | None = 3):
    ns = argparse.Namespace(ticker=ticker, thesis=thesis, conviction=conviction, action="hold")
    assert journal.cmd_open(ns) == 0


def _report(ticker: str) -> int:
    return journal.cmd_report(argparse.Namespace(ticker=ticker, date=None, no_docs=True))


def test_report_generates_on_fresh_entry(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(journal, "ENTRIES", tmp_path)
    monkeypatch.setattr(journal.subprocess, "call", lambda *a, **k: calls.append(a) or 0)

    _open("AAPL", "clean compounder; watching services margin and buyback pace")
    rc = _report("AAPL")

    assert rc == 0                 # regression: guard must NOT false-trip on a fresh entry
    assert calls                   # report generation actually invoked
    text = (tmp_path / f"AAPL_{journal._today()}.md").read_text()
    assert "reported: 2" in text   # thesis timestamp was stamped


def test_report_refuses_second_time(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "ENTRIES", tmp_path)
    monkeypatch.setattr(journal.subprocess, "call", lambda *a, **k: 0)

    _open("MSFT", "durable moat; cloud reacceleration")
    assert _report("MSFT") == 0
    assert _report("MSFT") == 1    # already reported -> refuse (no peek-then-regenerate)


def test_report_refuses_without_thesis(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "ENTRIES", tmp_path)
    monkeypatch.setattr(journal.subprocess, "call", lambda *a, **k: 0)

    _open("NVDA", None, conviction=None)  # placeholder thesis
    assert _report("NVDA") == 1           # empty BEFORE block -> refuse
