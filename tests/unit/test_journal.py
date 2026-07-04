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


# --- outcome-staleness tracking ---------------------------------------------

def test_days_since_reported_none_when_unreported(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    p = store.open_entry("KO", "steady staple", conviction=3)
    assert store.parse_entry(p)["days_since_reported"] is None


def test_stale_outcome_appears_in_awaiting_outcome(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    p = store.open_entry("KO", "steady staple", conviction=3)
    store.mark_reported(p)
    store.set_field(p, "impact", "no_value")          # AFTER filled, no verdict yet
    store.set_field(p, "reported", "2020-01-01T00:00:00Z")  # force a very old timestamp

    entry = store.parse_entry(p)
    assert entry["days_since_reported"] is not None
    assert entry["days_since_reported"] > store.STALE_OUTCOME_DAYS
    assert entry["needs_outcome"] is True

    t = store.tally()
    assert len(t["awaiting_outcome"]) == 1
    assert t["awaiting_outcome"][0]["ticker"] == "KO"
    assert t["oldest_awaiting_days"] == entry["days_since_reported"]


def test_awaiting_outcome_excludes_verdicted_and_unreported(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    closed = store.open_entry("AAA", "closed case", conviction=3)
    store.mark_reported(closed)
    store.set_field(closed, "impact", "no_value")
    store.set_field(closed, "verdict", "neutral")       # already closed out

    pending = store.open_entry("BBB", "no report yet", conviction=3)  # is_reported False

    t = store.tally()
    tickers = {e["ticker"] for e in t["awaiting_outcome"]}
    assert tickers == set()
    assert store.parse_entry(pending)["days_since_reported"] is None


def test_awaiting_outcome_sorted_oldest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    older = store.open_entry("AAA", "thesis one", conviction=3)
    store.mark_reported(older)
    store.set_field(older, "impact", "no_value")
    store.set_field(older, "reported", "2020-01-01T00:00:00Z")

    newer = store.open_entry("BBB", "thesis two", conviction=3)
    store.mark_reported(newer)
    store.set_field(newer, "impact", "no_value")
    store.set_field(newer, "reported", "2024-01-01T00:00:00Z")

    t = store.tally()
    assert [e["ticker"] for e in t["awaiting_outcome"]] == ["AAA", "BBB"]


def test_awaiting_outcome_excludes_reported_but_after_not_filled(tmp_path, monkeypatch):
    """Codex review catch: `needs_outcome` alone is true for BOTH 'reported, AFTER
    still blank' and 'reported, AFTER done, no verdict' — conflating two different
    next-steps. A reported case whose AFTER block is empty needs THAT step, not an
    outcome, and must not show up in the outcome queue even if old."""
    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    p = store.open_entry("KO", "steady staple", conviction=3)
    store.mark_reported(p)
    store.set_field(p, "reported", "2020-01-01T00:00:00Z")  # old, but AFTER left blank

    entry = store.parse_entry(p)
    assert entry["needs_after"] is True
    assert entry["needs_outcome"] is True  # true, but must NOT drive the outcome queue

    t = store.tally()
    assert t["awaiting_outcome"] == []


# --- audit-finding regressions ---------------------------------------------

import pytest  # noqa: E402


def test_ticker_path_traversal_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    for bad in ("../../../tmp/pwned", "a/b", "..", "", "   ", "TICKERTOOLONGX"):
        with pytest.raises(ValueError):
            store.entry_path(bad)
        with pytest.raises(ValueError):
            store.open_entry(bad, "x")
    # nothing escaped the entries dir
    assert not list(tmp_path.parent.glob("*PWNED*")) and list(tmp_path.glob("*.md")) == []
    assert store.safe_ticker("brk.b") == "BRK.B"  # legit dotted/hyphen tickers pass


def test_blank_field_does_not_bleed_into_next_line(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    p = store.open_entry("KO", "steady staple", conviction=3)
    e = store.parse_entry(p)
    # blank AFTER/OUTCOME fields must read as None, not the following field's label
    assert e["what_it_surfaced"] is None
    assert e["outcome_date"] is None
    assert e["what_happened"] is None


def test_whitespace_thesis_does_not_unlock(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    p = store.open_entry("KO", "   ")            # whitespace-only -> placeholder
    assert store.has_thesis(p.read_text()) is False   # lock must still refuse


def test_hash_in_freetext_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    p = store.open_entry("KO", "steady staple", conviction=3)
    store.set_field(p, "what_it_surfaced", "found a #1 red flag in cost of revenue")
    assert store.parse_entry(p)["what_it_surfaced"] == "found a #1 red flag in cost of revenue"


def test_multiline_value_does_not_corrupt_structure(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    p = store.open_entry("KO", "steady staple", conviction=3)
    store.set_field(p, "what_it_surfaced", "line one\nline two\nline three")
    e = store.parse_entry(p)
    assert e["what_it_surfaced"] == "line one line two line three"  # collapsed to one line
    assert e["what_i_disagreed_with"] is None                       # next field intact
