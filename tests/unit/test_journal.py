"""Decision-impact journal CLI + store — regression tests for the thesis/report lock.

`build_report` is stubbed, so these run offline. The load-bearing case is
`test_report_generates_on_fresh_entry`: the `reported:` guard once used `\\s*\\S`,
whose `\\s` spanned the newline into the next heading and false-tripped on EVERY
fresh entry — silently breaking the whole happy path.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

from app.services.journal import store

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("journal_cli", ROOT / "scripts" / "journal.py")
journal = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(journal)


def _open(ticker: str, thesis: str | None, conviction: int | None = 3):
    ns = argparse.Namespace(ticker=ticker, thesis=thesis, conviction=conviction, action="hold")
    assert journal.cmd_open(ns) == 0


def _report(ticker: str) -> int:
    return journal.cmd_report(argparse.Namespace(ticker=ticker, date=None, no_docs=True))


def _stub_build(monkeypatch, calls):
    monkeypatch.setattr(journal, "build_report",
                        lambda ticker, with_docs=True, report_day=None:
                        calls.append((ticker, report_day)) or (Path("x.md"), 31.2))


def test_report_generates_on_fresh_entry(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    _stub_build(monkeypatch, calls)

    _open("AAPL", "clean compounder; watching services margin and buyback pace")
    rc = _report("AAPL")

    assert rc == 0                 # regression: guard must NOT false-trip on a fresh entry
    assert calls == [("AAPL", store.today())]  # report generation actually invoked
    text = (tmp_path / f"AAPL_{store.today()}.md").read_text()
    assert "reported: 2" in text   # thesis timestamp was stamped


def test_report_refuses_second_time(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    _stub_build(monkeypatch, [])

    _open("MSFT", "durable moat; cloud reacceleration")
    assert _report("MSFT") == 0
    assert _report("MSFT") == 1    # already reported -> refuse (no peek-then-regenerate)


def test_report_refuses_without_thesis(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    _stub_build(monkeypatch, [])

    _open("NVDA", None, conviction=None)  # placeholder thesis
    assert _report("NVDA") == 1           # empty BEFORE block -> refuse


def test_report_failure_does_not_burn_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ENTRIES", tmp_path)

    def fail_build(ticker, with_docs=True, report_day=None):
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(journal, "build_report", fail_build)
    _open("CRM", "margin reset looks credible")

    assert _report("CRM") == 1
    entry_path = tmp_path / f"CRM_{store.today()}.md"
    assert not store.is_reported(entry_path.read_text())


def test_report_uses_entry_date_for_dated_case(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    _stub_build(monkeypatch, calls)
    p = store.open_entry("KO", "steady staple")
    old = tmp_path / "KO_2026-01-15.md"
    p.rename(old)

    rc = journal.cmd_report(argparse.Namespace(ticker="KO", date="2026-01-15", no_docs=True))

    assert rc == 0
    assert calls == [("KO", "2026-01-15")]
    assert store.is_reported(old.read_text())


def test_store_tally_and_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    p = store.open_entry("KO", "steady staple", conviction=3)
    store.mark_reported(p)
    store.set_field(p, "impact", "changed_confidence")
    store.set_field(p, "conviction_after", "4")
    store.set_field(p, "verdict", "helped")

    parsed = store.parse_entry(p)
    assert parsed["impact"] == "changed_confidence"
    assert parsed["needs_after"] is False and parsed["needs_outcome"] is False

    t = store.tally()
    assert t["total"] == 1 and t["scored"] == 1
    assert t["impact_counts"]["changed_confidence"] == 1
    assert t["conv_moved"] == 1 and t["verdicts"]["helped"] == 1
