"""Print-date inference: cadence-derived print_at with reject-over-guess guards.

An early estimate costs nothing (the poller just waits longer); a late one
costs the filing-night speed edge. So every ambiguous case must return None
and force a manual --print-at, never a plausible guess.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.watch import watchlist as wl
from app.services.watch.infer import infer_print_at

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


def _submissions(entries) -> dict:
    """entries: (form, items, acceptance_iso)"""
    n = len(entries)
    return {
        "filings": {
            "recent": {
                "form": [e[0] for e in entries],
                "accessionNumber": [f"acc-{i}" for i in range(n)],
                "filingDate": [e[2][:10] for e in entries],
                "items": [e[1] for e in entries],
                "acceptanceDateTime": [e[2] for e in entries],
            }
        }
    }


def _quarterly_prints(n: int, start: datetime, gap_days: int = 91):
    """n AMC earnings 8-Ks at a steady cadence, newest first like EDGAR."""
    rows = []
    for i in range(n):
        dt = start + timedelta(days=gap_days * i)
        rows.append(("8-K", "2.02,9.01", dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")))
    return list(reversed(rows))


class TestInference:
    def test_regular_amc_cadence_projects_next_print(self):
        start = datetime(2025, 8, 27, 20, 21, 19, tzinfo=timezone.utc)
        est = infer_print_at(_submissions(_quarterly_prints(5, start)), now=NOW)
        assert est is not None
        last = start + timedelta(days=91 * 4)
        assert est.print_at.date() == (last + timedelta(days=91)).date()
        assert est.print_at.hour == 20  # AMC clock time carried over
        assert "8-K 2.02" in est.basis

    def test_bmo_filer_keeps_morning_clock_time(self):
        start = datetime(2025, 9, 1, 11, 0, tzinfo=timezone.utc)
        est = infer_print_at(_submissions(_quarterly_prints(4, start)), now=NOW)
        assert est is not None
        assert est.print_at.hour == 11

    def test_fewer_than_three_prints_rejects(self):
        start = datetime(2026, 2, 1, 21, 0, tzinfo=timezone.utc)
        assert infer_print_at(_submissions(_quarterly_prints(2, start)), now=NOW) is None

    def test_irregular_cadence_rejects(self):
        rows = []
        for iso in (
            "2025-08-01T20:00:00.000Z",
            "2025-10-15T20:00:00.000Z",  # 75d in band
            "2026-02-20T20:00:00.000Z",  # 128d out of band
            "2026-05-20T20:00:00.000Z",  # 89d in band -> only 2 in-band gaps
        ):
            rows.append(("8-K", "2.02", iso))
        assert infer_print_at(_submissions(list(reversed(rows))), now=NOW) is None

    def test_fiscal_wobble_projects_from_the_shortest_band_gap(self):
        # NVDA-shaped cadence (83-98d wobble): a late estimate would make the
        # poller's `since` filter exclude the real filing, so the projection
        # must use the SHORTEST in-band gap — early, never late.
        rows, t = [], datetime(2025, 2, 26, 21, 20, tzinfo=timezone.utc)
        for gap in (0, 90, 84, 98, 83, 98):
            t = t + timedelta(days=gap)
            rows.append(("8-K", "2.02,9.01", t.strftime("%Y-%m-%dT%H:%M:%S.000Z")))
        est = infer_print_at(_submissions(list(reversed(rows))), now=t + timedelta(days=1))
        assert est is not None
        assert est.print_at.date() == (t + timedelta(days=83)).date()

    def test_non_202_8ks_are_ignored(self):
        start = datetime(2025, 8, 27, 20, 21, tzinfo=timezone.utc)
        rows = _quarterly_prints(4, start)
        rows.insert(1, ("8-K", "5.02", "2026-01-10T13:00:00.000Z"))
        rows.insert(0, ("8-K", "1.01,9.01", "2026-06-01T12:00:00.000Z"))
        est = infer_print_at(_submissions(rows), now=NOW)
        assert est is not None
        assert est.print_at.hour == 20

    def test_estimate_in_the_past_rolls_one_cadence_forward(self):
        # Last print long ago relative to `now`: naive projection lands in the
        # past; the estimate must step forward one more gap, not go stale.
        start = datetime(2025, 1, 15, 21, 0, tzinfo=timezone.utc)
        est = infer_print_at(_submissions(_quarterly_prints(4, start)), now=NOW)
        assert est is not None
        assert est.print_at > start + timedelta(days=91 * 4)


class TestAddEntry:
    def test_add_validates_then_appends_preserving_comment(self, tmp_path):
        p = tmp_path / "watchlist.json"
        p.write_text('{"_comment": ["keep me"], "watchlist": []}')
        wl.add_entry({"ticker": "NVDA", "print_at": "2026-08-26T20:20:00Z"}, path=p)
        watches = wl.load(p)
        assert [w.ticker for w in watches] == ["NVDA"]
        assert "keep me" in p.read_text()

    def test_add_refuses_duplicates(self, tmp_path):
        p = tmp_path / "watchlist.json"
        wl.add_entry({"ticker": "NVDA", "print_at": "2026-08-26T20:20:00Z"}, path=p)
        with pytest.raises(wl.WatchlistError, match="already"):
            wl.add_entry({"ticker": "NVDA", "print_at": "2026-11-18T21:20:00Z"}, path=p)

    def test_add_rejects_invalid_before_writing(self, tmp_path):
        p = tmp_path / "watchlist.json"
        with pytest.raises(wl.WatchlistError):
            wl.add_entry({"ticker": "NVDA", "print_at": "2026-08-26T20:20:00"}, path=p)  # naive tz
        assert not p.exists()

    def test_add_creates_missing_file(self, tmp_path):
        p = tmp_path / "watchlist.json"
        w = wl.add_entry(
            {"ticker": "AAPL", "print_at": "2026-10-29T20:30:00Z", "label": "FQ4-26"},
            path=p,
        )
        assert w.label == "FQ4-26"
        assert wl.load(p)[0].ticker == "AAPL"
