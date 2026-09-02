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


def _report(ticker: str, defer_mark: bool = False) -> int:
    return journal.cmd_report(argparse.Namespace(
        ticker=ticker, date=None, no_docs=True, defer_mark=defer_mark))


def _stub_build(monkeypatch, calls):
    monkeypatch.setattr(journal, "build_report",
                        lambda ticker, with_docs=True, report_day=None, fresh=False:
                        calls.append((ticker, report_day)) or (Path("x.md"), "no acute signals"))


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


def test_report_prints_distress_string_verbatim(tmp_path, monkeypatch, capsys):
    # Audit finding (final pre-merge review): build_report returns describe()'s
    # already-formatted STRING, but a leftover formatter treated it as the raw
    # thermometer object — getattr fell through and every run printed a fixed
    # "no reading, 0 cluster(s)" regardless of the actual signal. The CLI must
    # surface the returned summary verbatim.
    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    monkeypatch.setattr(journal, "build_report",
                        lambda ticker, with_docs=True, report_day=None, fresh=False:
                        (Path("x.md"), "regime signals present (going_concern)"))

    _open("AAPL", "clean compounder; watching services margin")
    assert _report("AAPL") == 0

    out = capsys.readouterr().out
    assert "distress: regime signals present (going_concern) -> x.md" in out
    assert "no reading" not in out


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


def test_day_path_traversal_rejected(tmp_path, monkeypatch):
    """Round-14 finding 3: the `day` half of TICKER_DAY.md used to accept any
    string. Direct traversal was blocked in practice (no matching file) but
    the invariant was violated. Now enforced by `safe_day`."""
    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    # Empty string is treated as "use default" (mirrors None) — that's the
    # explicit CLI/web behavior, not a validation failure.
    for bad in ("../../etc/passwd", "2026-08", "2026/08/13", "not-a-date",
                "2026-08-13T00:00:00", "2026-08-13 ", " 2026-08-13"):
        with pytest.raises(ValueError, match="invalid day"):
            store.entry_path("AAPL", bad)
        with pytest.raises(ValueError, match="invalid day"):
            store.find_entry("AAPL", bad)
    # Valid ISO day accepted.
    assert store.entry_path("AAPL", "2026-08-13").name == "AAPL_2026-08-13.md"
    # `safe_day` returns the same string on valid input.
    assert store.safe_day("2026-01-01") == "2026-01-01"


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


# ---------------------------------------------------------------------------
# openv2 CLI — round-11 fixes
# ---------------------------------------------------------------------------


def _openv2_ns(**overrides) -> argparse.Namespace:
    """Minimum valid openv2 args; overrides tweak individual fields."""
    base = dict(
        ticker="TST", thesis="a real thesis", conviction=3, action="hold",
        assumption=["revenue,>,100000000,FY2026Q2,,2026-08-15"],
        falsifier=None, catalyst=None, contamination=None,
        outcome_definition=None, p_outcome=None, reference_class=None,
        conviction_fine=None, date="2026-07-27",
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_parse_assumption_accepts_empty_source():
    """Round-11 finding 2: empty `source` field yields source=None."""
    a = journal._parse_assumption("revenue,>,165000000,FY2026Q2,,2026-08-15")
    assert a.source is None
    a2 = journal._parse_assumption("revenue,>,165000000,FY2026Q2,10-Q,2026-08-15")
    assert a2.source == "10-Q"


def test_openv2_refuses_p_outcome_without_outcome_definition(tmp_path, monkeypatch, capsys):
    """Round-11 finding 1: BEFORE is hash-locked at openv2 — the definition
    cannot be added after the fact, so it must accompany p_outcome now."""
    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    ns = _openv2_ns(p_outcome=0.7, outcome_definition=None)
    assert journal.cmd_openv2(ns) == 1
    err = capsys.readouterr().err
    assert "outcome-definition" in err or "outcome_definition" in err
    # Confirm nothing was written.
    assert list(tmp_path.glob("*.md")) == []


def test_openv2_refuses_whitespace_only_outcome_definition(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    ns = _openv2_ns(p_outcome=0.7, outcome_definition="   ")
    assert journal.cmd_openv2(ns) == 1
    assert list(tmp_path.glob("*.md")) == []


def test_openv2_accepts_p_outcome_with_outcome_definition(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    ns = _openv2_ns(p_outcome=0.7, outcome_definition="Q2 revenue > 100M by 2026-08-15")
    assert journal.cmd_openv2(ns) == 0
    entries = list(tmp_path.glob("*.md"))
    assert len(entries) == 1
    e = store.load_v2(entries[0])
    assert e.before.p_outcome == 0.7
    assert e.before.outcome_definition == "Q2 revenue > 100M by 2026-08-15"


# ---------------------------------------------------------------------------
# after / outcome-v2 CLI — the dogfood ergonomics layer
# ---------------------------------------------------------------------------


def _after_ns(**overrides) -> argparse.Namespace:
    base = dict(ticker="TST", date="2026-07-27", impact=None, conviction_after=None,
                surfaced=None, disagreed=None)
    base.update(overrides)
    return argparse.Namespace(**base)


def _outcome_ns(**overrides) -> argparse.Namespace:
    base = dict(ticker="TST", date="2026-07-27", outcome_date=None,
                what_happened=None, verdict=None, y=None)
    base.update(overrides)
    return argparse.Namespace(**base)


def _seed_v2(tmp_path, monkeypatch, *, reported: bool = True, **before_kwargs) -> object:
    """Seed a locked v2 entry and return the on-disk EntryV2.

    By default ALSO stamps `reported` (simulating a completed `journal.py report`
    step) so tests exercise the state after-report, which is the only state
    where `after`/`outcome` may legitimately run (round-13 findings 1 & 2).
    Pass `reported=False` when a test wants the pre-report state.
    """
    from datetime import datetime, timezone

    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    ns = _openv2_ns(**before_kwargs)
    assert journal.cmd_openv2(ns) == 0
    p = list(tmp_path.glob("*.md"))[0]
    entry = store.load_v2(p)
    if reported:
        stamped = entry.model_copy(update={"reported": datetime.now(timezone.utc)})
        store.save_v2(stamped, p, allow_update=True)
        entry = store.load_v2(p)
    return entry


class TestCmdAfter:
    def test_updates_after_and_preserves_lock(self, tmp_path, monkeypatch):
        from app.services.journal.schema_v2 import verify_lock

        before = _seed_v2(tmp_path, monkeypatch)
        rc = journal.cmd_after(_after_ns(impact="changed_confidence",
                                        conviction_after=4,
                                        surfaced="cash conversion trend"))
        assert rc == 0
        after = store.load_v2(list(tmp_path.glob("*.md"))[0])
        assert after.after.impact == "changed_confidence"
        assert after.after.conviction_after == 4
        assert after.after.what_it_surfaced == "cash conversion trend"
        # BEFORE untouched -> hash still matches.
        assert verify_lock(after) is True
        assert after.before_sha256 == before.before_sha256

    def test_refuses_before_report(self, tmp_path, monkeypatch, capsys):
        """Round-13 finding 1 (P1): AFTER cannot be filled before `report` has
        stamped `reported`. Without this guard, users could pre-fill AFTER
        before ever running the engine — degrading the post-report semantic."""
        _seed_v2(tmp_path, monkeypatch, reported=False)  # locked but not reported
        rc = journal.cmd_after(_after_ns(impact="changed_confidence"))
        assert rc == 1
        err = capsys.readouterr().err
        assert "report not yet generated" in err

    def test_refuses_v1_entry(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        # v1 entry at the same TICKER_DATE slot the after command targets.
        store.open_entry("TST", "v1 thesis")
        ns = _after_ns(date=None, impact="changed_confidence")  # find latest
        assert journal.cmd_after(ns) == 1
        assert "v1 entry" in capsys.readouterr().err

    def test_refuses_missing_fields(self, tmp_path, monkeypatch, capsys):
        _seed_v2(tmp_path, monkeypatch)
        rc = journal.cmd_after(_after_ns())  # all None
        assert rc == 1
        assert "Nothing to update" in capsys.readouterr().err

    def test_refuses_tampered_lock(self, tmp_path, monkeypatch, capsys):
        from app.services.journal.schema_v2 import render_entry

        seeded = _seed_v2(tmp_path, monkeypatch)
        path = list(tmp_path.glob("*.md"))[0]
        # Tamper thesis while preserving the old hash on disk.
        tampered = seeded.model_copy(update={
            "before": seeded.before.model_copy(update={"thesis": "hindsight edit"}),
        })
        tampered = tampered.model_copy(update={"before_sha256": seeded.before_sha256})
        path.write_text(render_entry(tampered))
        rc = journal.cmd_after(_after_ns(impact="changed_confidence"))
        assert rc == 1
        assert "LOCK BROKEN" in capsys.readouterr().err

    def test_no_entry_returns_error(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        rc = journal.cmd_after(_after_ns(ticker="NOPE", date=None, impact="no_value"))
        assert rc == 1
        assert "No entry" in capsys.readouterr().err


class TestCmdOutcomeV2:
    def test_updates_outcome_fields(self, tmp_path, monkeypatch):
        from app.services.journal.schema_v2 import verify_lock

        _seed_v2(tmp_path, monkeypatch,
                 outcome_definition="TST Q2 revenue > 100M by 2026-08-15",
                 p_outcome=0.65)
        rc = journal.cmd_outcome(_outcome_ns(outcome_date="2026-08-14",
                                             what_happened="revenue landed at 108M",
                                             verdict="helped", y=True))
        assert rc == 0
        e = store.load_v2(list(tmp_path.glob("*.md"))[0])
        assert e.outcome.outcome_date.isoformat() == "2026-08-14"
        assert e.outcome.what_happened == "revenue landed at 108M"
        assert e.outcome.verdict == "helped"
        assert e.outcome.y is True
        assert verify_lock(e) is True  # BEFORE preserved

    def test_y_requires_outcome_definition(self, tmp_path, monkeypatch, capsys):
        # No outcome_definition set at openv2 → --y meaningless (orphaned).
        _seed_v2(tmp_path, monkeypatch)  # default: no p_outcome, no outcome_definition
        rc = journal.cmd_outcome(_outcome_ns(y=True))
        assert rc == 1
        assert "outcome_definition" in capsys.readouterr().err

    def test_verdict_only_ok_without_outcome_definition(self, tmp_path, monkeypatch):
        # `--verdict` scores whether the engine helped — no outcome_definition
        # required. Independent of Brier.
        _seed_v2(tmp_path, monkeypatch)
        rc = journal.cmd_outcome(_outcome_ns(verdict="helped"))
        assert rc == 0
        e = store.load_v2(list(tmp_path.glob("*.md"))[0])
        assert e.outcome.verdict == "helped"
        assert e.outcome.y is None

    def test_bad_outcome_date_refused(self, tmp_path, monkeypatch, capsys):
        _seed_v2(tmp_path, monkeypatch)
        rc = journal.cmd_outcome(_outcome_ns(outcome_date="not-a-date"))
        assert rc == 1
        assert "outcome-date" in capsys.readouterr().err

    def test_v1_entry_keeps_edit_message(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        store.open_entry("TST", "v1 thesis")
        rc = journal.cmd_outcome(_outcome_ns(date=None, verdict="helped"))
        assert rc == 0
        out = capsys.readouterr().out
        assert "Edit the OUTCOME block" in out

    def test_refuses_tampered_lock(self, tmp_path, monkeypatch, capsys):
        from app.services.journal.schema_v2 import render_entry

        seeded = _seed_v2(tmp_path, monkeypatch)
        path = list(tmp_path.glob("*.md"))[0]
        tampered = seeded.model_copy(update={
            "before": seeded.before.model_copy(update={"thesis": "hindsight edit"}),
        })
        tampered = tampered.model_copy(update={"before_sha256": seeded.before_sha256})
        path.write_text(render_entry(tampered))
        rc = journal.cmd_outcome(_outcome_ns(verdict="helped"))
        assert rc == 1
        assert "LOCK BROKEN" in capsys.readouterr().err

    def test_refuses_before_report(self, tmp_path, monkeypatch, capsys):
        """Round-13 finding 2 (P1): OUTCOME cannot be filled before report."""
        _seed_v2(tmp_path, monkeypatch, reported=False)
        rc = journal.cmd_outcome(_outcome_ns(verdict="helped"))
        assert rc == 1
        assert "report not yet generated" in capsys.readouterr().err

    def test_outcome_date_before_entry_day_refused(self, tmp_path, monkeypatch, capsys):
        """Round-13 finding 3: an outcome that predates the preregistration
        is temporally impossible — the thesis wasn't written yet."""
        _seed_v2(tmp_path, monkeypatch)  # openv2 date default 2026-07-27
        rc = journal.cmd_outcome(_outcome_ns(outcome_date="2025-01-01"))
        assert rc == 1
        err = capsys.readouterr().err
        assert "precedes" in err
        # File unchanged: outcome_date still None.
        assert store.load_v2(list(tmp_path.glob("*.md"))[0]).outcome.outcome_date is None

    def test_outcome_date_equal_to_entry_day_ok(self, tmp_path, monkeypatch):
        _seed_v2(tmp_path, monkeypatch)
        rc = journal.cmd_outcome(_outcome_ns(outcome_date="2026-07-27"))
        assert rc == 0

    def test_y_cannot_be_flipped_once_set(self, tmp_path, monkeypatch, capsys):
        """Round-13 finding 4 (the retroactive-Brier defect): once `y` is
        recorded it cannot be flipped through the CLI. Same for `verdict` and
        `outcome_date`. `what_happened` remains editable (typos, added prose)."""
        _seed_v2(tmp_path, monkeypatch,
                 outcome_definition="Q2 revenue > 100M", p_outcome=0.65)
        assert journal.cmd_outcome(_outcome_ns(y=True)) == 0
        # Same value again: idempotent, allowed.
        assert journal.cmd_outcome(_outcome_ns(y=True)) == 0
        # Different value: refused.
        rc = journal.cmd_outcome(_outcome_ns(y=False))
        assert rc == 1
        err = capsys.readouterr().err
        assert "immutable" in err and "OUTCOME.y" in err
        # File still says True (no partial write).
        assert store.load_v2(list(tmp_path.glob("*.md"))[0]).outcome.y is True

    def test_verdict_cannot_be_flipped_once_set(self, tmp_path, monkeypatch, capsys):
        _seed_v2(tmp_path, monkeypatch)
        assert journal.cmd_outcome(_outcome_ns(verdict="helped")) == 0
        rc = journal.cmd_outcome(_outcome_ns(verdict="hurt"))
        assert rc == 1
        assert "OUTCOME.verdict" in capsys.readouterr().err

    def test_outcome_date_cannot_be_flipped_once_set(self, tmp_path, monkeypatch, capsys):
        _seed_v2(tmp_path, monkeypatch)
        assert journal.cmd_outcome(_outcome_ns(outcome_date="2026-08-14")) == 0
        rc = journal.cmd_outcome(_outcome_ns(outcome_date="2026-08-15"))
        assert rc == 1
        assert "OUTCOME.outcome_date" in capsys.readouterr().err

    def test_what_happened_stays_editable(self, tmp_path, monkeypatch):
        """Only factual fields (y, verdict, outcome_date) are locked. Free-text
        stays editable — typos and expanded prose are legitimate iteration."""
        _seed_v2(tmp_path, monkeypatch)
        assert journal.cmd_outcome(_outcome_ns(what_happened="revenue was flat")) == 0
        assert journal.cmd_outcome(_outcome_ns(what_happened="revenue was flat; margins compressed")) == 0
        e = store.load_v2(list(tmp_path.glob("*.md"))[0])
        assert "margins compressed" in e.outcome.what_happened

    def test_empty_free_text_normalizes_to_none(self, tmp_path, monkeypatch):
        """Round-13 finding 5: --what-happened '' becomes None (unset), not ''.
        Empty string was harmless but muddied the schema — 'is this set?'
        should have one answer, not two."""
        _seed_v2(tmp_path, monkeypatch)
        assert journal.cmd_outcome(_outcome_ns(what_happened="   ")) == 1  # nothing to update
        # Same for after --surfaced ""
        assert journal.cmd_after(_after_ns(surfaced="   ")) == 1


class TestParseBool:
    def test_truthy_variants(self):
        for s in ("true", "True", "TRUE", "t", "yes", "y", "1"):
            assert journal._parse_bool(s) is True

    def test_falsy_variants(self):
        # Round-13 finding 6: `"f"` was in the implementation's falsy set but
        # not tested — coverage gap now closed.
        for s in ("false", "False", "FALSE", "f", "F", "no", "n", "0"):
            assert journal._parse_bool(s) is False

    def test_garbage_rejected(self):
        import argparse as _argparse
        for s in ("", "maybe", "2", "on"):
            with pytest.raises(_argparse.ArgumentTypeError):
                journal._parse_bool(s)


class TestAfterOutcomeE2E:
    """End-to-end: openv2 → after → outcome → verify tally counts the pair."""

    def test_full_dogfood_flow(self, tmp_path, monkeypatch):
        _seed_v2(tmp_path, monkeypatch,
                 outcome_definition="TST Q2 revenue > 100M by 2026-08-15",
                 p_outcome=0.65)
        assert journal.cmd_after(_after_ns(impact="changed_confidence",
                                           conviction_after=4)) == 0
        assert journal.cmd_outcome(_outcome_ns(outcome_date="2026-08-14",
                                               verdict="helped", y=True)) == 0
        t = store.v2_tally()
        assert t["total"] == 1
        assert t["locked"] == 1
        assert t["lock_broken"] == 0
        # p_outcome + outcome_definition + y all set -> enters raw Brier count
        assert t["resolved_with_p_outcome"] == 1


# --- deferred `reported` marking (audit-before-mark ordering) ------------------
# The watch flow generates with --defer-mark and stamps `reported` only after a
# successful audit; a failed audit must leave the entry retryable.


def test_defer_mark_leaves_entry_unreported(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    _stub_build(monkeypatch, calls)
    _open("AAPL", "clean compounder; watching services margin")
    assert _report("AAPL", defer_mark=True) == 0
    assert calls  # the report WAS generated
    text = (tmp_path / f"AAPL_{store.today()}.md").read_text()
    assert not store.is_reported(text)  # ...but the entry stays retryable


def test_mark_reported_stamps_deferred_v1_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    _stub_build(monkeypatch, [])
    _open("AAPL", "clean compounder; watching services margin")
    assert _report("AAPL", defer_mark=True) == 0
    rc = journal.cmd_mark_reported(argparse.Namespace(ticker="AAPL", date=None))
    assert rc == 0
    assert store.is_reported((tmp_path / f"AAPL_{store.today()}.md").read_text())
    # Second stamp refused — the mark is single-shot.
    assert journal.cmd_mark_reported(argparse.Namespace(ticker="AAPL", date=None)) == 1


def test_defer_mark_v2_entry_stays_retryable_then_marks(tmp_path, monkeypatch):
    from datetime import date as _date, datetime as _dt, timezone as _tz

    from app.services.journal.schema_v2 import (
        Assumption, BeforeBlock, EntryV2, lock_entry,
    )

    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    _stub_build(monkeypatch, [])
    before = BeforeBlock(
        thesis="priced for perfection", conviction=3, intended_action="hold",
        assumptions=[Assumption(metric="revenue", comparator=">", threshold=1.0,
                                window="FY2027Q2", source="10-Q",
                                resolve_by=_date(2026, 12, 31))],
    )
    entry = lock_entry(EntryV2(ticker="NVDA", day=_date.today(),
                               opened=_dt.now(_tz.utc), before=before))
    path = store.save_v2(entry)

    ns = argparse.Namespace(ticker="NVDA", date=None, no_docs=True, defer_mark=True)
    assert journal.cmd_report(ns) == 0
    assert store.load_v2(path).reported is None  # retryable after deferred report

    assert journal.cmd_mark_reported(argparse.Namespace(ticker="NVDA", date=None)) == 0
    assert store.load_v2(path).reported is not None
    # And single-shot, same as v1.
    assert journal.cmd_mark_reported(argparse.Namespace(ticker="NVDA", date=None)) == 1


def test_mark_reported_dispatches_through_real_argparse(tmp_path):
    """Regression for the review-caught CRITICAL: the subparser was wired with
    `fn=` while main() dispatches `args.func` — every real invocation (exactly
    how watch.py calls it, via subprocess) crashed with AttributeError while
    the direct-call unit tests stayed green. Exercise the actual CLI boundary."""
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "journal.py"),
         "mark-reported", "ZZZZ", "--date", "2026-01-01"],
        capture_output=True, text=True, cwd=ROOT,
    )
    # Graceful refusal (no entry), NOT an AttributeError crash.
    assert proc.returncode == 1
    assert "AttributeError" not in proc.stderr
    assert "No entry" in proc.stderr


def test_defer_mark_hint_names_the_exact_entry_day(tmp_path, monkeypatch, capsys):
    """The printed recovery command must pin --date — a bare `mark-reported
    TICKER` falls back to latest-entry-wins, the Defect-2 pattern."""
    monkeypatch.setattr(store, "ENTRIES", tmp_path)
    _stub_build(monkeypatch, [])
    _open("AAPL", "clean compounder; watching services margin")
    assert _report("AAPL", defer_mark=True) == 0
    out = capsys.readouterr().out
    assert f"--date {store.today()}" in out
