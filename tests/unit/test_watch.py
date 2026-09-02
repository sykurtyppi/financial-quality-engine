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
    find_filing_since,
    pinned_thesis_state,
    recent_filings,
    thesis_state,
)

PRINT_AT = datetime(2026, 8, 26, 20, 20, tzinfo=timezone.utc)


BASELINE = "0001045810-26-000052"  # the FQ1-27 10-Q on file when the watch armed
EXPECTED = date(2026, 7, 26)       # FQ2-27 period end


def _watch(**over) -> wl.Watch:
    base = dict(
        ticker="NVDA", print_at=PRINT_AT, forms=("10-Q",),
        baseline_accession=BASELINE, expected_report_date=EXPECTED,
    )
    base.update(over)
    return wl.Watch(**base)


def _submissions(*rows) -> dict:
    """rows: (form, accession, filingDate[, acceptance[, reportDate[, items]]])"""
    return {
        "filings": {
            "recent": {
                "form": [r[0] for r in rows],
                "accessionNumber": [r[1] for r in rows],
                "filingDate": [r[2] for r in rows],
                "reportDate": [r[4] if len(r) > 4 else "2026-07-26" for r in rows],
                "acceptanceDateTime": [r[3] if len(r) > 3 else None for r in rows],
                "primaryDocument": [None for _ in rows],
                "items": [r[5] if len(r) > 5 else None for r in rows],
            }
        }
    }


def _pin(entry) -> dict:
    """thesis_entry/thesis_sha256 kwargs pinning `entry` (a locked EntryV2)."""
    return dict(thesis_entry=entry.day.isoformat(), thesis_sha256=entry.before_sha256)


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

    def test_event_identity_fields_parse(self, tmp_path):
        p = tmp_path / "w.json"
        p.write_text('{"watchlist":[{"ticker":"NVDA","print_at":"2026-11-18T20:21:00Z",'
                     '"baseline_accession":"0001045810-26-000075",'
                     '"expected_report_date":"2026-10-25"}]}')
        w = wl.load(p)[0]
        assert w.event_armed
        assert w.baseline_accession == "0001045810-26-000075"
        assert w.expected_report_date == date(2026, 10, 25)

    def test_legacy_row_parses_but_is_not_armed(self, tmp_path):
        # Loading must not crash on a legacy row (status/due still render it);
        # the POLLER is what fails closed on the missing identity.
        p = tmp_path / "w.json"
        p.write_text('{"watchlist":[{"ticker":"NVDA","print_at":"2026-08-26T20:20:00Z"}]}')
        assert not wl.load(p)[0].event_armed

    def test_thesis_pin_requires_both_fields(self, tmp_path):
        # A pin without its hash cannot be verified — refuse at parse time so
        # an inconsistent row never reaches the gate.
        p = tmp_path / "w.json"
        p.write_text('{"watchlist":[{"ticker":"NVDA","print_at":"2026-08-26T20:20:00Z",'
                     '"thesis_entry":"2026-08-26"}]}')
        with pytest.raises(wl.WatchlistError, match="set together"):
            wl.load(p)

    def test_malformed_pin_values_refused(self, tmp_path):
        p = tmp_path / "w.json"
        p.write_text('{"watchlist":[{"ticker":"NVDA","print_at":"2026-08-26T20:20:00Z",'
                     '"thesis_entry":"aug-26","thesis_sha256":"' + "a" * 64 + '"}]}')
        with pytest.raises(wl.WatchlistError, match="YYYY-MM-DD"):
            wl.load(p)

    def test_update_entry_persists_pin_atomically(self, tmp_path):
        p = tmp_path / "w.json"
        p.write_text('{"_comment":["keep me"],"watchlist":['
                     '{"ticker":"NVDA","print_at":"2026-11-18T20:21:00Z"}]}')
        w = wl.update_entry(
            "NVDA",
            {"thesis_entry": "2026-11-18", "thesis_sha256": "ab" * 32},
            p,
        )
        assert w.thesis_entry == "2026-11-18"
        reloaded = wl.load(p)[0]
        assert reloaded.thesis_sha256 == "ab" * 32
        assert "keep me" in p.read_text()  # comment block survived the rewrite
        assert not list(tmp_path.glob("*.tmp"))  # no temp litter

    def test_update_entry_rejects_invalid_merge(self, tmp_path):
        p = tmp_path / "w.json"
        p.write_text('{"watchlist":[{"ticker":"NVDA","print_at":"2026-11-18T20:21:00Z"}]}')
        before = p.read_text()
        with pytest.raises(wl.WatchlistError, match="set together"):
            wl.update_entry("NVDA", {"thesis_entry": "2026-11-18"}, p)
        assert p.read_text() == before  # failed validation never touches the file

    def test_update_entry_unknown_ticker(self, tmp_path):
        p = tmp_path / "w.json"
        p.write_text('{"watchlist":[{"ticker":"NVDA","print_at":"2026-11-18T20:21:00Z"}]}')
        with pytest.raises(wl.WatchlistError, match="not on the watchlist"):
            wl.update_entry("AMD", {"thesis_entry": "2026-11-18",
                                    "thesis_sha256": "ab" * 32}, p)


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
    def test_finds_qualifying_new_accession(self):
        subs = _submissions(("10-Q", "0001045810-26-000100", "2026-08-26"))
        f = find_filing(subs, ("10-Q",), baseline_accession=BASELINE,
                        expected_report_date=EXPECTED)
        assert f is not None and f.accession.endswith("000100")

    def test_early_filing_is_not_missed(self):
        # THE defect: the print was estimated for 8/26 but the company filed
        # 8/10. A forecast-date cutoff excluded this filing forever; the
        # accession baseline must catch it regardless of when it arrives.
        subs = _submissions(("10-Q", "0001045810-26-000100", "2026-08-10"))
        f = find_filing(subs, ("10-Q",), baseline_accession=BASELINE,
                        expected_report_date=EXPECTED)
        assert f is not None and f.accession.endswith("000100")

    def test_baseline_accession_never_retriggers(self):
        subs = _submissions(("10-Q", BASELINE, "2026-05-27", None, "2026-04-26"))
        assert find_filing(subs, ("10-Q",), baseline_accession=BASELINE,
                           expected_report_date=EXPECTED) is None

    def test_intervening_earlier_quarter_does_not_hijack(self):
        # Watch armed early for the FQ2 event; the FQ1 10-Q (a NEW accession,
        # wrong period) lands first. It must not trigger.
        subs = _submissions(("10-Q", "0001045810-26-000060", "2026-05-27", None, "2026-04-26"))
        assert find_filing(subs, ("10-Q",), baseline_accession=BASELINE,
                           expected_report_date=EXPECTED) is None

    def test_period_tolerance_covers_shifted_quarter_ends(self):
        subs = _submissions(("10-Q", "n", "2026-08-26", None, "2026-08-01"))
        f = find_filing(subs, ("10-Q",), baseline_accession=BASELINE,
                        expected_report_date=EXPECTED)
        assert f is not None  # 6 days off the expected end — same quarter

    def test_missing_report_date_cannot_qualify_periodic_form(self):
        subs = _submissions(("10-Q", "n", "2026-08-26", None, None))
        assert find_filing(subs, ("10-Q",), baseline_accession=BASELINE,
                           expected_report_date=EXPECTED) is None

    def test_ignores_unwatched_forms_and_amendments(self):
        # 10-Q/A must not re-trigger a case that already ran.
        subs = _submissions(("8-K", "a", "2026-08-26"), ("10-Q/A", "b", "2026-08-27"))
        assert find_filing(subs, ("10-Q",), baseline_accession=BASELINE,
                           expected_report_date=EXPECTED) is None

    def test_8k_requires_item_202(self):
        # A debt-agreement 8-K is not the print, even on the print date.
        subs = _submissions(
            ("8-K", "debt", "2026-08-17", None, "2026-08-17", "1.01,2.03"),
            ("8-K", "earn", "2026-08-26", None, "2026-08-26", "2.02,9.01"),
        )
        f = find_filing(subs, ("8-K",), baseline_accession=BASELINE,
                        expected_report_date=EXPECTED)
        assert f is not None and f.accession == "earn"

    def test_8k_202_before_expected_period_end_is_not_this_event(self):
        subs = _submissions(("8-K", "prior", "2026-05-20", None, "2026-05-20", "2.02"))
        assert find_filing(subs, ("8-K",), baseline_accession=BASELINE,
                           expected_report_date=EXPECTED) is None

    def test_8k_202_from_a_later_quarter_does_not_hijack(self):
        # A forgotten watch polled a quarter late: the NEXT quarter's earnings
        # 8-K is a new accession filed after the expected period end, but it
        # is not THIS event — the upper bound must reject it.
        subs = _submissions(("8-K", "next-q", "2026-11-25", None, "2026-11-25", "2.02,9.01"))
        assert find_filing(subs, ("8-K",), baseline_accession=BASELINE,
                           expected_report_date=EXPECTED) is None

    def test_picks_newest_deterministically(self):
        subs = _submissions(
            ("10-Q", "older", "2026-08-26", "2026-08-26T16:31:00.000Z"),
            ("10-Q", "newer", "2026-08-26", "2026-08-26T21:05:00.000Z"),
        )
        f = find_filing(subs, ("10-Q",), baseline_accession=BASELINE,
                        expected_report_date=EXPECTED)
        assert f.accession == "newer"
        # Identical dates and acceptance: accession is the final tiebreak, so
        # selection never depends on payload order.
        subs2 = _submissions(("10-Q", "aaa", "2026-08-26"), ("10-Q", "zzz", "2026-08-26"))
        assert find_filing(subs2, ("10-Q",), baseline_accession=BASELINE,
                           expected_report_date=EXPECTED).accession == "zzz"

    def test_since_variant_honors_operator_bound(self):
        subs = _submissions(("10-Q", "x", "2026-05-20"))
        assert find_filing_since(subs, ("10-Q",), date(2026, 8, 26)) is None
        assert find_filing_since(subs, ("10-Q",), date(2026, 5, 1)).accession == "x"

    def test_malformed_payload_raises_clearly(self):
        with pytest.raises(PollerError, match="submissions payload"):
            recent_filings({"filings": {}})

    def test_unparseable_filing_date_skipped(self):
        subs = _submissions(("10-Q", "bad", "not-a-date"))
        assert recent_filings(subs) == []


class TestPinnedThesisGate:
    def _saved(self, monkeypatch, tmp_path, **entry_over):
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        entry = lock_entry(_v2_entry(**entry_over))
        store.save_v2(entry)
        return entry

    def test_unpinned_watch_never_generates(self, monkeypatch, tmp_path):
        # Even with a perfectly good entry on disk: no pin, no authorization.
        self._saved(monkeypatch, tmp_path)
        g = pinned_thesis_state(_watch())
        assert g.state is Gate.NO_PINNED and not g.may_generate

    def test_pinned_and_locked_generates(self, monkeypatch, tmp_path):
        entry = self._saved(monkeypatch, tmp_path)
        assert pinned_thesis_state(_watch(**_pin(entry))).may_generate

    def test_stale_prior_quarter_entry_cannot_authorize(self, monkeypatch, tmp_path):
        # THE defect: a May entry existed; the unpinned gate picked it as
        # "latest for the ticker" and authorized the August report.
        self._saved(monkeypatch, tmp_path, day=date(2026, 5, 20))
        g = pinned_thesis_state(_watch())  # watch is for the August event
        assert g.state is Gate.NO_PINNED and not g.may_generate

    def test_pinned_entry_missing_fails_closed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        g = pinned_thesis_state(_watch(thesis_entry="2026-08-26",
                                       thesis_sha256="0" * 64))
        assert g.state is Gate.NO_ENTRY and not g.may_generate

    def test_hash_mismatch_refused_as_tampering(self, monkeypatch, tmp_path):
        entry = self._saved(monkeypatch, tmp_path)
        g = pinned_thesis_state(_watch(thesis_entry=entry.day.isoformat(),
                                       thesis_sha256="0" * 64))
        assert g.state is Gate.LOCK_BROKEN and not g.may_generate

    def test_tampered_entry_content_refused(self, monkeypatch, tmp_path):
        entry = self._saved(monkeypatch, tmp_path)
        path = store.find_entry("NVDA", entry.day.isoformat())
        path.write_text(path.read_text().replace("already priced", "always was cheap"))
        g = pinned_thesis_state(_watch(**_pin(entry)))
        assert g.state is Gate.LOCK_BROKEN and not g.may_generate

    def test_legacy_v1_entry_cannot_be_pinned_gate(self, monkeypatch, tmp_path):
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        store.open_entry("NVDA", thesis="Priced for perfection.", conviction=3)
        day = store.today()
        g = pinned_thesis_state(_watch(thesis_entry=day, thesis_sha256="0" * 64))
        assert g.state is Gate.LOCK_BROKEN and not g.may_generate

    def test_reported_pinned_entry_skips(self, monkeypatch, tmp_path):
        entry = self._saved(monkeypatch, tmp_path)
        path = store.find_entry("NVDA", entry.day.isoformat())
        stamped = entry.model_copy(update={"reported": datetime.now(timezone.utc)})
        store.save_v2(stamped, path, allow_update=True)
        g = pinned_thesis_state(_watch(**_pin(entry)))
        assert g.state is Gate.ALREADY_REPORTED and not g.may_generate


class TestDecide:
    FILED = ("10-Q", "0001045810-26-000075", "2026-08-26")

    def test_legacy_watch_without_identity_fails_closed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        legacy = _watch(baseline_accession=None, expected_report_date=None)
        with pytest.raises(PollerError, match="event identity"):
            decide(legacy, _submissions(self.FILED))

    def test_waits_when_nothing_new_filed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        d = decide(_watch(), _submissions(("8-K", "x", "2026-08-26")))
        assert d.action == "wait"

    def test_generates_when_filed_and_pinned_thesis_locked(self, monkeypatch, tmp_path):
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        entry = lock_entry(_v2_entry())
        store.save_v2(entry)
        d = decide(_watch(**_pin(entry)), _submissions(self.FILED))
        assert d.action == "generate"

    def test_early_filing_still_generates(self, monkeypatch, tmp_path):
        # decide-level regression for the early-filing defect.
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        entry = lock_entry(_v2_entry())
        store.save_v2(entry)
        early = ("10-Q", "0001045810-26-000075", "2026-08-10")
        d = decide(_watch(**_pin(entry)), _submissions(early))
        assert d.action == "generate"

    def test_refuses_when_filed_without_pin(self, monkeypatch, tmp_path):
        # THE case this module exists for — and the pin closes the loophole
        # where a stale entry could stand in for the missing thesis.
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        store.save_v2(lock_entry(_v2_entry(day=date(2026, 5, 20))))  # stale entry
        d = decide(_watch(), _submissions(self.FILED))
        assert d.action == "refuse"
        assert d.gate.state is Gate.NO_PINNED
        assert "blind case" in d.message

    def test_skips_when_pinned_entry_already_reported(self, monkeypatch, tmp_path):
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        entry = lock_entry(_v2_entry())
        path = store.save_v2(entry)
        store.save_v2(entry.model_copy(update={"reported": datetime.now(timezone.utc)}),
                      path, allow_update=True)
        d = decide(_watch(**_pin(entry)), _submissions(self.FILED))
        assert d.action == "skip"

    def test_adhoc_since_mode_needs_no_identity(self, monkeypatch, tmp_path):
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        legacy = _watch(baseline_accession=None, expected_report_date=None)
        d = decide(legacy, _submissions(self.FILED), since=date(2026, 8, 26))
        assert d.action == "refuse"  # filing found; nothing pinned

    def test_no_thesis_before_filing_is_wait_not_refuse(self, monkeypatch, tmp_path):
        # Ordering guard: pre-filing there is nothing to refuse, and crying
        # "refuse" every poll would train the alert to be ignored.
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        old = ("10-Q", "0001045810-26-000060", "2026-05-27", None, "2026-04-26")
        d = decide(_watch(), _submissions(old))
        assert d.action == "wait"

    def test_since_on_armed_watch_refuses_event_mismatch(self, monkeypatch, tmp_path):
        # Post-merge hardening: a stale --since on an ARMED watch could trigger
        # on an old filing, generate a report for the wrong quarter, and
        # permanently consume the pinned entry (ALREADY_REPORTED is terminal).
        # The since-match must be the same filing the event identity selects.
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        old = ("10-Q", "0001045810-26-000060", "2026-05-27", None, "2026-04-26")
        with pytest.raises(PollerError, match="Drop --since"):
            decide(_watch(), _submissions(old), since=date(2026, 5, 1))

    def test_since_force_overrides_the_cross_check(self, monkeypatch, tmp_path):
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        old = ("10-Q", "0001045810-26-000060", "2026-05-27", None, "2026-04-26")
        d = decide(_watch(), _submissions(old), since=date(2026, 5, 1), force=True)
        assert d.action == "refuse"  # filing accepted; the (empty) gate decides
        assert d.filing.accession == "0001045810-26-000060"

    def test_since_matching_the_event_filing_passes(self, monkeypatch, tmp_path):
        # A --since that finds the SAME filing the event identity selects is
        # consistent, not an override — no error, no --force needed.
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        d = decide(_watch(), _submissions(self.FILED), since=date(2026, 8, 1))
        assert d.action == "refuse"
        assert d.filing.accession == self.FILED[1]

    def test_since_on_unarmed_watch_skips_cross_check(self, monkeypatch, tmp_path):
        # The pure ad-hoc path (synthetic watch, no event identity) is the
        # operator's own bound; nothing exists to cross-check against.
        monkeypatch.setattr(store, "ENTRIES", tmp_path)
        legacy = _watch(baseline_accession=None, expected_report_date=None)
        old = ("10-Q", "0001045810-26-000060", "2026-05-27", None, "2026-04-26")
        d = decide(legacy, _submissions(old), since=date(2026, 5, 1))
        assert d.action == "refuse"


class TestWatchlistWriteLock:
    """_atomic_write prevents torn files; the flock prevents lost updates —
    two concurrent add/link invocations must serialize their read-modify-write
    cycles instead of the second silently clobbering the first."""

    def _seed(self, tmp_path):
        import json

        p = tmp_path / "watchlist.json"
        p.write_text(json.dumps({"watchlist": [{
            "ticker": "NVDA", "print_at": "2026-08-26T20:20:00+00:00",
            "baseline_accession": "acc-1", "expected_report_date": "2026-07-26",
        }]}) + "\n")
        return p

    def _probe(self, monkeypatch, observed):
        """Swap _atomic_write for a probe that checks — from a second file
        descriptor, exactly as a concurrent process would — whether the
        exclusive lock is held at write time."""
        import fcntl

        orig = wl._atomic_write

        def probing_write(path, data):
            with open(path.with_name(path.name + ".lock"), "w") as fh:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    observed["held"] = False
                    fcntl.flock(fh, fcntl.LOCK_UN)
                except BlockingIOError:
                    observed["held"] = True
            orig(path, data)

        monkeypatch.setattr(wl, "_atomic_write", probing_write)

    def test_update_entry_holds_the_lock_across_the_write(self, tmp_path, monkeypatch):
        p = self._seed(tmp_path)
        observed: dict = {}
        self._probe(monkeypatch, observed)
        wl.update_entry("NVDA", {"label": "FQ2-27"}, path=p)
        assert observed["held"] is True

    def test_add_entry_holds_the_lock_across_the_write(self, tmp_path, monkeypatch):
        p = self._seed(tmp_path)
        observed: dict = {}
        self._probe(monkeypatch, observed)
        wl.add_entry({
            "ticker": "AAPL", "print_at": "2026-10-29T20:30:00+00:00",
            "baseline_accession": "acc-2", "expected_report_date": "2026-09-26",
        }, path=p)
        assert observed["held"] is True

    def test_lock_is_released_after_the_write(self, tmp_path, monkeypatch):
        import fcntl

        p = self._seed(tmp_path)
        wl.update_entry("NVDA", {"label": "FQ2-27"}, path=p)
        with open(p.with_name(p.name + ".lock"), "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)  # must not raise
            fcntl.flock(fh, fcntl.LOCK_UN)
