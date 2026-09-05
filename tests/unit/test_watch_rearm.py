"""Re-arming: after a watch's one-shot event identity is consumed, the next
identity is derived from the issuer's filing history so a name stays on the
calendar across the season without an operator step per print.

Every field has a fallback because an un-re-armed watch on the auto track
would regenerate the same report on every sweep.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services.watch import watchlist as wl
from app.services.watch.poller import Filing
from app.services.watch.rearm import (
    DEFAULT_PERIOD_STEP_DAYS,
    FALLBACK_PRINT_LAG_DAYS,
    event_identity,
    next_arming,
)

NOW = datetime(2026, 11, 18, 21, 0, tzinfo=timezone.utc)


def _submissions(rows) -> dict:
    """rows: (form, accession, filingDate, reportDate, items, acceptance)"""
    return {
        "filings": {
            "recent": {
                "form": [r[0] for r in rows],
                "accessionNumber": [r[1] for r in rows],
                "filingDate": [r[2] for r in rows],
                "reportDate": [r[3] for r in rows],
                "items": [r[4] if len(r) > 4 else None for r in rows],
                "acceptanceDateTime": [r[5] if len(r) > 5 else None for r in rows],
                "primaryDocument": [None for _ in rows],
            }
        }
    }


def _history(with_prints: bool = True):
    """NVDA-shaped history newest first: the FQ3-27 10-Q just landed."""
    periods = [date(2026, 10, 25), date(2026, 7, 26), date(2026, 4, 26),
               date(2026, 1, 25), date(2025, 10, 26)]
    rows = []
    for i, p in enumerate(periods):
        filed = p + timedelta(days=24)
        form = "10-K" if p.month == 1 else "10-Q"
        rows.append((form, f"q-{i}", filed.isoformat(), p.isoformat()))
        if with_prints:
            rows.append(("8-K", f"k-{i}", filed.isoformat(), filed.isoformat(),
                         "2.02,9.01", f"{filed.isoformat()}T21:20:00.000Z"))
    return rows


LANDED = Filing(form="10-Q", accession="q-0", filing_date=date(2026, 11, 18),
                report_date=date(2026, 10, 25))


class TestEventIdentity:
    def test_baseline_is_newest_qualifying_and_period_steps_forward(self):
        baseline, expected = event_identity(_submissions(_history()), ("10-Q", "10-K"))
        assert baseline == "q-0"
        assert expected == date(2026, 10, 25) + timedelta(days=91)

    def test_no_periodic_history_yields_no_period(self):
        subs = _submissions([("8-K", "k", "2026-11-18", "2026-11-18", "2.02")])
        assert event_identity(subs, ("10-Q",)) == ("", None)


class TestNextArming:
    def test_derives_next_event_and_clears_the_pin(self):
        arming = next_arming(_submissions(_history()), ("10-Q", "10-K"),
                             filed=LANDED, previous_expected=date(2026, 10, 25), now=NOW)
        assert arming.baseline_accession == "q-0"  # the filing just consumed
        assert arming.expected_report_date == date(2027, 1, 24)
        assert arming.print_at > NOW
        assert "re-armed 2026-11-18 after 10-Q q-0" in arming.note
        upd = arming.as_updates()
        assert upd["thesis_entry"] is None and upd["thesis_sha256"] is None
        assert upd["label"] is None

    def test_print_hint_falls_back_when_cadence_not_inferable(self):
        arming = next_arming(_submissions(_history(with_prints=False)), ("10-Q", "10-K"),
                             filed=LANDED, previous_expected=date(2026, 10, 25), now=NOW)
        assert arming.print_at.date() == LANDED.filing_date + timedelta(days=FALLBACK_PRINT_LAG_DAYS)
        assert "not inferable" in arming.note

    def test_stale_history_still_steps_past_the_consumed_period(self):
        # Submissions that do not yet show the landed period (or show it as
        # the newest) must not re-arm on the SAME period — that would make the
        # consumed filing... not consumed, since only the baseline blocks it.
        rows = [r for r in _history() if r[1] not in ("q-0", "k-0")]
        arming = next_arming(_submissions(rows), ("10-Q", "10-K"),
                             filed=LANDED, previous_expected=date(2026, 10, 25), now=NOW)
        assert arming.expected_report_date > date(2026, 10, 25)
        assert arming.baseline_accession == "q-0"  # the consumed filing, not q-1

    def test_stale_payload_baselines_on_the_consumed_filing_and_skips_its_print(self):
        # Neither the landed 10-Q nor this print's 8-K is in the payload yet.
        rows = [r for r in _history() if r[1] not in ("q-0", "k-0")]
        landed = Filing(form="10-Q", accession="q-new", filing_date=date(2026, 11, 18),
                        report_date=date(2026, 10, 25))
        arming = next_arming(_submissions(rows), ("10-Q", "10-K"),
                             filed=landed, previous_expected=date(2026, 10, 25), now=NOW)
        assert arming.baseline_accession == "q-new"
        # Cadence off the stale history projects ~Nov 18 — the print just
        # consumed — so the hint must step to the next quarter.
        assert arming.print_at.date() > date(2026, 11, 18) + timedelta(days=45)

    def test_no_history_at_all_never_fails(self):
        subs = _submissions([])
        arming = next_arming(subs, ("10-Q",), filed=LANDED,
                             previous_expected=date(2026, 10, 25), now=NOW)
        assert arming.baseline_accession == "q-0"  # the consumed filing itself
        assert arming.expected_report_date == date(2026, 10, 25) + timedelta(
            days=DEFAULT_PERIOD_STEP_DAYS)

    def test_no_filing_and_no_previous_uses_now(self):
        arming = next_arming(_submissions([]), ("10-Q",), filed=None,
                             previous_expected=None, now=NOW)
        assert arming.expected_report_date == NOW.date() + timedelta(days=DEFAULT_PERIOD_STEP_DAYS)
        assert "previous event" in arming.note


class TestWatchlistRearmPersistence:
    def _seed(self, tmp_path: Path) -> Path:
        p = tmp_path / "watchlist.json"
        p.write_text(json.dumps({"watchlist": [{
            "ticker": "NVDA", "label": "FQ3-27",
            "print_at": "2026-11-18T20:20:00+00:00",
            "baseline_accession": "q-1", "expected_report_date": "2026-10-25",
            "thesis_entry": "2026-11-17", "thesis_sha256": "ab" * 32,
        }, {
            "ticker": "AAPL", "print_at": "2026-10-29T20:30:00+00:00",
            "baseline_accession": "a-1", "expected_report_date": "2026-09-26",
        }]}) + "\n")
        return p

    def test_update_with_none_drops_the_field(self, tmp_path):
        p = self._seed(tmp_path)
        arming = next_arming(_submissions(_history()), ("10-Q", "10-K"),
                             filed=LANDED, previous_expected=date(2026, 10, 25), now=NOW)
        w = wl.update_entry("NVDA", arming.as_updates(), path=p)
        assert w.thesis_entry is None and w.thesis_sha256 is None and w.label is None
        assert w.baseline_accession == "q-0"
        raw = json.loads(p.read_text())["watchlist"][0]
        assert "thesis_entry" not in raw and "label" not in raw
        assert "null" not in p.read_text()
        assert wl.load(p)[0].ticker == "AAPL"  # re-sorted by the new print_at

    def test_remove_entry(self, tmp_path):
        p = self._seed(tmp_path)
        wl.remove_entry("AAPL", path=p)
        assert [w.ticker for w in wl.load(p)] == ["NVDA"]
        with pytest.raises(wl.WatchlistError, match="not on the watchlist"):
            wl.remove_entry("AAPL", path=p)
