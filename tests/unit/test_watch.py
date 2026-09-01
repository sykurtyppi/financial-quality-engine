"""Watch-layer tests: calendar parsing, the pre-print nudge window, filing
detection, and — the one that matters — the thesis gate's refusal paths.

The gate is what keeps automation from destroying the blind-prior property the
journal measures (journal/JOURNAL.md rule 1). It runs unattended on an earnings
night, so every branch is pinned here rather than discovered live.
"""

from datetime import date, datetime, timezone

import pytest

from app.services.journal import store
from app.services.journal.schema_v2 import Assumption, BeforeBlock, EntryV2, lock_entry
from app.services.watch import watchlist as wl
from app.services.watch.poller import (
    Gate,
    PollerError,
    decide,
    find_filing,
    recent_filings,
    thesis_state,
)

PRINT_AT = datetime(2026, 8, 26, 20, 20, tzinfo=timezone.utc)


def _watch(**over) -> wl.Watch:
    base = dict(ticker="NVDA", print_at=PRINT_AT, forms=("10-Q",))
    base.update(over)
    return wl.Watch(**base)


def _submissions(*rows) -> dict:
    """rows: (form, accession, filingDate[, acceptanceDateTime])"""
    return {
        "filings": {
            "recent": {
                "form": [r[0] for r in rows],
                "accessionNumber": [r[1] for r in rows],
                "filingDate": [r[2] for r in rows],
                "reportDate": [None for _ in rows],
                "acceptanceDateTime": [r[3] if len(r) > 3 else None for r in rows],
                "primaryDocument": [None for _ in rows],
            }
        }
    }


def _v2_entry(ticker="NVDA", day=date(2026, 8, 26)) -> EntryV2:
    before = BeforeBlock(
        thesis="Data-center growth already priced; watching inventory and China.",
        conviction=3,
        intended_action="hold",
        assumptions=[
            Assumption(metric="revenue", comparator=">", threshold=5e10,
                       window="FY2027Q2", source="10-Q", resolve_by=date(2026, 9, 30))
        ],
    )
    return EntryV2(ticker=ticker, day=day,
                   opened=datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc),
                   before=before)


class TestWatchlistParsing:
    def test_missing_file_is_empty_not_an_error(self, tmp_path):
        # A cron job must not crash before the calendar has been set up.
        assert wl.load(tmp_path / "nope.json") == []

    def test_parses_and_sorts_by_print_time(self, tmp_path):
        p = tmp_path / "w.json"
        p.write_text('{"watchlist":['
                     '{"ticker":"NVDA","print_at":"2026-08-26T20:20:00Z"},'
                     '{"ticker":"AMD","print_at":"2026-08-04T20:05:00Z"}]}')
        assert [w.ticker for w in wl.load(p)] == ["AMD", "NVDA"]

    def test_naive_timestamp_rejected(self, tmp_path):
        # The whole schedule turns on this field; "server local time" is a
        # silent off-by-hours on a US print.
        p = tmp_path / "w.json"
        p.write_text('{"watchlist":[{"ticker":"NVDA","print_at":"2026-08-26T20:20:00"}]}')
        with pytest.raises(wl.WatchlistError, match="no timezone"):
            wl.load(p)

    def test_offset_normalized_to_utc(self, tmp_path):
        p = tmp_path / "w.json"
        p.write_text('{"watchlist":[{"ticker":"NVDA","print_at":"2026-08-26T16:20:00-04:00"}]}')
        assert wl.load(p)[0].print_at == PRINT_AT

    def test_duplicate_tickers_refused(self, tmp_path):
        p = tmp_path / "w.json"
        p.write_text('{"watchlist":['
                     '{"ticker":"NVDA","print_at":"2026-08-26T20:20:00Z"},'
                     '{"ticker":"NVDA","print_at":"2026-11-18T21:20:00Z"}]}')
        with pytest.raises(wl.WatchlistError, match="duplicate"):
            wl.load(p)

    def test_bad_ticker_refused(self, tmp_path):
        p = tmp_path / "w.json"
        p.write_text('{"watchlist":[{"ticker":"../etc","print_at":"2026-08-26T20:20:00Z"}]}')
        with pytest.raises(wl.WatchlistError):
            wl.load(p)

    def test_empty_forms_refused(self, tmp_path):
        p = tmp_path / "w.json"
        p.write_text('{"watchlist":[{"ticker":"NVDA","print_at":"2026-08-26T20:20:00Z",'
                     '"forms":[]}]}')
        with pytest.raises(wl.WatchlistError, match="nothing could ever trigger"):
            wl.load(p)

    def test_invalid_json_names_the_file(self, tmp_path):
        p = tmp_path / "w.json"
        p.write_text("{not json")
        with pytest.raises(wl.WatchlistError, match="invalid JSON"):
            wl.load(p)


class TestDueWindow:
    def test_inside_window(self):
        now = PRINT_AT.replace(hour=0)  # 20.3h before
        assert [w.ticker for w in wl.due([_watch()], 36, now)] == ["NVDA"]

    def test_outside_window(self):
        now = datetime(2026, 8, 20, tzinfo=timezone.utc)
        assert wl.due([_watch()], 36, now) == []

    def test_past_print_drops_out(self):
        # Nudging after the print would invite a "prior" written with the tape
        # already visible — worse than no nudge at all.
        now = PRINT_AT.replace(hour=23)
        assert wl.due([_watch()], 36, now) == []


class TestThesisGate:
    def test_no_entry(self, monkeypatch, tmp_path):
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        assert thesis_state("NVDA").state is Gate.NO_ENTRY

    def test_placeholder_thesis_blocks(self, monkeypatch, tmp_path):
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        store.open_entry("NVDA")  # template placeholders, no real thesis
        g = thesis_state("NVDA")
        assert g.state is Gate.PLACEHOLDER_THESIS and not g.may_generate

    def test_v1_real_thesis_unlocks(self, monkeypatch, tmp_path):
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        store.open_entry("NVDA", thesis="Priced for perfection.", conviction=3)
        assert thesis_state("NVDA").may_generate

    def test_v1_already_reported(self, monkeypatch, tmp_path):
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        p = store.open_entry("NVDA", thesis="Priced for perfection.", conviction=3)
        store.mark_reported(p)
        g = thesis_state("NVDA")
        assert g.state is Gate.ALREADY_REPORTED and not g.may_generate

    def test_v2_locked_unlocks(self, monkeypatch, tmp_path):
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        store.save_v2(lock_entry(_v2_entry()))
        assert thesis_state("NVDA").may_generate

    def test_v2_tampered_before_block_refused(self, monkeypatch, tmp_path):
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        entry = lock_entry(_v2_entry())
        path = store.save_v2(entry)
        path.write_text(path.read_text().replace("already priced", "always was cheap"))
        g = thesis_state("NVDA")
        assert g.state is Gate.LOCK_BROKEN and not g.may_generate


class TestFilingDetection:
    def test_finds_qualifying_filing(self):
        subs = _submissions(("10-Q", "0001045810-26-000100", "2026-08-26"))
        f = find_filing(subs, ("10-Q",), date(2026, 8, 26))
        assert f is not None and f.accession.endswith("000100")

    def test_ignores_filings_before_the_print(self):
        subs = _submissions(("10-Q", "0001045810-26-000050", "2026-05-20"))
        assert find_filing(subs, ("10-Q",), date(2026, 8, 26)) is None

    def test_ignores_unwatched_forms_and_amendments(self):
        # 10-Q/A must not re-trigger a case that already ran.
        subs = _submissions(("8-K", "a", "2026-08-26"), ("10-Q/A", "b", "2026-08-27"))
        assert find_filing(subs, ("10-Q",), date(2026, 8, 26)) is None

    def test_picks_newest_by_acceptance(self):
        subs = _submissions(
            ("10-Q", "older", "2026-08-26", "2026-08-26T16:31:00.000Z"),
            ("10-Q", "newer", "2026-08-26", "2026-08-26T21:05:00.000Z"),
        )
        assert find_filing(subs, ("10-Q",), date(2026, 8, 26)).accession == "newer"

    def test_malformed_payload_raises_clearly(self):
        with pytest.raises(PollerError, match="submissions payload"):
            recent_filings({"filings": {}})

    def test_unparseable_filing_date_skipped(self):
        subs = _submissions(("10-Q", "bad", "not-a-date"))
        assert recent_filings(subs) == []


class TestDecide:
    def test_waits_when_nothing_filed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        d = decide(_watch(), _submissions(("8-K", "x", "2026-08-26")))
        assert d.action == "wait"

    def test_generates_when_filed_and_thesis_locked(self, monkeypatch, tmp_path):
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        store.save_v2(lock_entry(_v2_entry()))
        d = decide(_watch(), _submissions(("10-Q", "x", "2026-08-26")))
        assert d.action == "generate"

    def test_refuses_when_filed_without_thesis(self, monkeypatch, tmp_path):
        # THE case this module exists for: the filing is up, but generating now
        # would burn the blind case.
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        d = decide(_watch(), _submissions(("10-Q", "x", "2026-08-26")))
        assert d.action == "refuse"
        assert d.gate.state is Gate.NO_ENTRY
        assert "blind case" in d.message

    def test_skips_when_already_reported(self, monkeypatch, tmp_path):
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        p = store.open_entry("NVDA", thesis="Priced for perfection.", conviction=3)
        store.mark_reported(p)
        d = decide(_watch(), _submissions(("10-Q", "x", "2026-08-26")))
        assert d.action == "skip"

    def test_no_thesis_before_filing_is_wait_not_refuse(self, monkeypatch, tmp_path):
        # Ordering guard: pre-filing there is nothing to refuse, and crying
        # "refuse" every poll would train the alert to be ignored.
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        d = decide(_watch(), _submissions(("10-Q", "old", "2026-05-20")))
        assert d.action == "wait"
