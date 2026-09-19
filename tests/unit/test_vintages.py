"""The companyfacts vintage store: what it keeps, what it refuses to keep
twice, and what it can see that a single fetch cannot."""

from __future__ import annotations

import gzip
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.services.ingestion import vintages as v


def _facts(rows, tag="Assets", taxonomy="us-gaap", unit="USD"):
    """rows: (end, filed, val, form, accn[, start])"""
    return {"cik": 1045810, "facts": {taxonomy: {tag: {"units": {unit: [
        {"end": r[0], "filed": r[1], "val": r[2], "form": r[3], "accn": r[4],
         **({"start": r[5]} if len(r) > 5 else {})}
        for r in rows]}}}}}


class _Client:
    def __init__(self, *payloads):
        self._payloads = list(payloads)
        self.fetches = 0

    def resolve_cik(self, ticker):
        return 1045810

    def company_facts_by_cik(self, cik):
        self.fetches += 1
        return self._payloads[min(self.fetches - 1, len(self._payloads) - 1)]


AT = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
NEXT_DAY = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


class TestCapture:
    def test_first_capture_writes_a_gzipped_snapshot_and_a_manifest(self, tmp_path):
        payload = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "acc-1")])
        res = v.capture(_Client(payload), "NVDA", now=AT, root=tmp_path)
        assert res.wrote and res.reason == "captured"
        assert res.path.name == "2026-09-19.json.gz"
        assert v.load_vintage(res.path) == payload
        man = v.read_manifest(1045810, tmp_path)
        assert man["last_checked"] == "2026-09-19"
        assert [s["file"] for s in man["snapshots"]] == ["2026-09-19.json.gz"]
        assert man["snapshots"][0]["sha256"] == res.sha256

    def test_same_day_second_call_does_not_even_fetch(self, tmp_path):
        client = _Client(_facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "a")]))
        v.capture(client, "NVDA", now=AT, root=tmp_path)
        again = v.capture(client, "NVDA", now=AT, root=tmp_path)
        assert client.fetches == 1  # the document changes on filing days, not hourly
        assert not again.wrote and again.reason == "already checked today"

    def test_identical_content_the_next_day_is_not_stored_twice(self, tmp_path):
        payload = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "a")])
        client = _Client(payload, payload)
        v.capture(client, "NVDA", now=AT, root=tmp_path)
        res = v.capture(client, "NVDA", now=NEXT_DAY, root=tmp_path)
        assert client.fetches == 2 and not res.wrote and res.reason == "unchanged"
        assert len(v.list_vintages(1045810, tmp_path)) == 1
        # ...but "we looked and it was identical" is still recorded.
        assert v.read_manifest(1045810, tmp_path)["last_checked"] == "2026-09-20"

    def test_changed_content_the_next_day_is_appended(self, tmp_path):
        first = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "a")])
        second = _facts([("2026-06-30", "2026-11-01", 1200.0, "10-Q", "b")])
        client = _Client(first, second)
        v.capture(client, "NVDA", now=AT, root=tmp_path)
        res = v.capture(client, "NVDA", now=NEXT_DAY, root=tmp_path)
        assert res.wrote and len(v.list_vintages(1045810, tmp_path)) == 2
        assert v.load_vintage(res.path) == second

    def test_force_refetches_the_same_day(self, tmp_path):
        payload = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "a")])
        client = _Client(payload, payload)
        v.capture(client, "NVDA", now=AT, root=tmp_path)
        assert v.capture(client, "NVDA", now=AT, root=tmp_path, force=True).reason == "unchanged"
        assert client.fetches == 2

    def test_snapshot_bytes_depend_only_on_content(self, tmp_path):
        # gzip stamps mtime by default, which would make an unchanged document
        # look different to anything comparing the archive itself.
        payload = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "a")])
        a = v.capture(_Client(payload), "NVDA", now=AT, root=tmp_path / "a").path
        b = v.capture(_Client(payload), "NVDA", now=NEXT_DAY, root=tmp_path / "b").path
        assert a.read_bytes() == b.read_bytes()

    def test_a_corrupt_manifest_does_not_lose_the_snapshots(self, tmp_path):
        payload = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "a")])
        v.capture(_Client(payload), "NVDA", now=AT, root=tmp_path)
        (v.cik_dir(1045810, tmp_path) / v.MANIFEST).write_text("{not json")
        assert v.read_manifest(1045810, tmp_path) == {"last_checked": None, "snapshots": []}
        assert len(v.list_vintages(1045810, tmp_path)) == 1  # disk is the record

    def test_no_temp_files_survive(self, tmp_path):
        payload = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "a")])
        v.capture(_Client(payload), "NVDA", now=AT, root=tmp_path)
        assert not list(v.cik_dir(1045810, tmp_path).glob(".*tmp"))


class TestDiff:
    """The case this store exists for: a figure revised WITHOUT the original
    being re-presented, which no single companyfacts fetch can show."""

    def test_a_silent_revision_is_caught(self):
        before = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "acc-1")])
        after = _facts([("2026-06-30", "2026-11-01", 1200.0, "10-K", "acc-2")])
        changes = v.diff_vintages(before, after)
        assert len(changes) == 1
        c = changes[0]
        assert c.kind == "revised" and c.field_name == "total_assets"
        assert (c.old_value, c.new_value) == (1000.0, 1200.0)
        assert c.old_accession == "acc-1" and c.new_accession == "acc-2"
        assert c.pct_change == pytest.approx(0.2)

    def test_a_withdrawn_fact_is_caught(self):
        before = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "acc-1")])
        after = {"facts": {"us-gaap": {"Assets": {"units": {"USD": []}}}}}
        changes = v.diff_vintages(before, after)
        assert [c.kind for c in changes] == ["withdrawn"]
        assert changes[0].new_value is None

    def test_an_immaterial_move_is_not_a_finding(self):
        before = _facts([("2026-06-30", "2026-08-01", 1_000_000.0, "10-Q", "a")])
        after = _facts([("2026-06-30", "2026-11-01", 1_000_100.0, "10-K", "b")])
        assert v.diff_vintages(before, after) == []
        assert len(v.diff_vintages(before, after, materiality_pct=0.0)) == 1

    def test_a_new_period_is_not_a_revision(self):
        before = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "a")])
        after = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "a"),
                        ("2026-09-30", "2026-11-01", 1100.0, "10-Q", "b")])
        assert v.diff_vintages(before, after) == []

    def test_a_later_filing_that_agrees_is_not_a_revision(self):
        before = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "a")])
        after = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "a"),
                        ("2026-06-30", "2026-11-01", 1000.0, "10-K", "b")])
        assert v.diff_vintages(before, after) == []

    def test_since_bounds_the_window(self):
        before = _facts([("2019-06-30", "2019-08-01", 1000.0, "10-Q", "a")])
        after = _facts([("2019-06-30", "2026-11-01", 5000.0, "10-K", "b")])
        assert len(v.diff_vintages(before, after)) == 1
        assert v.diff_vintages(before, after, since=date(2024, 1, 1)) == []

    def test_unscored_tags_are_ignored_unless_asked_for(self):
        before = _facts([("2026-06-30", "2026-08-01", 10.0, "10-Q", "a")], tag="MadeUpTag")
        after = _facts([("2026-06-30", "2026-11-01", 99.0, "10-K", "b")], tag="MadeUpTag")
        assert v.diff_vintages(before, after) == []
        wide = v.diff_vintages(before, after, scored_only=False)
        assert len(wide) == 1 and wide[0].field_name == "MadeUpTag"

    def test_malformed_rows_are_skipped_not_fatal(self):
        bad = {"facts": {"us-gaap": {"Assets": {"units": {"USD": [
            {"end": "not-a-date", "filed": "2026-08-01", "val": 1.0},
            {"end": "2026-06-30", "filed": "2026-08-01", "val": "NaN-ish"},
            "not even a dict",
        ]}}}}}
        assert v.diff_vintages(bad, bad) == []

    def test_render_says_plainly_when_nothing_moved(self):
        assert "No prior-period figure changed" in v.render_changes([], "a", "b")
        before = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "acc-1")])
        after = _facts([("2026-06-30", "2026-11-01", 1200.0, "10-K", "acc-2")])
        md = v.render_changes(v.diff_vintages(before, after), "2026-09-19", "2026-12-01")
        assert "total_assets" in md and "1,000" in md and "1,200" in md and "20.0%" in md


class TestSplitsAreNotRestatements:
    """Measured on the first real capture: NVDA's June 2024 ten-for-one split
    produced four "revisions" of exactly 900% and they were the only findings
    in the file. A split rewrites every prior share count; it is a corporate
    action, not an accounting revision."""

    @staticmethod
    def _shares(val, filed, tag="WeightedAverageNumberOfDilutedSharesOutstanding"):
        return {"facts": {"us-gaap": {tag: {"units": {"shares": [
            {"start": "2024-01-29", "end": "2024-04-28", "filed": filed, "val": val,
             "form": "10-Q", "accn": f"acc-{filed}"}]}}}}}

    def test_a_split_is_silent_by_default(self):
        before, after = self._shares(2_489_000_000, "2024-05-29"), self._shares(24_890_000_000, "2025-05-29")
        assert v.diff_vintages(before, after) == []

    def test_but_can_be_asked_for(self):
        before, after = self._shares(2_489_000_000, "2024-05-29"), self._shares(24_890_000_000, "2025-05-29")
        changes = v.diff_vintages(before, after, include_split_adjusted=True)
        assert len(changes) == 1 and changes[0].field_name == "shares_diluted"

    def test_a_real_revision_in_another_field_is_still_reported(self):
        before = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "a")])
        before["facts"]["us-gaap"].update(self._shares(2_489_000_000, "2024-05-29")["facts"]["us-gaap"])
        after = _facts([("2026-06-30", "2026-11-01", 1200.0, "10-K", "b")])
        after["facts"]["us-gaap"].update(self._shares(24_890_000_000, "2025-05-29")["facts"]["us-gaap"])
        changes = v.diff_vintages(before, after)
        assert [c.field_name for c in changes] == ["total_assets"]
