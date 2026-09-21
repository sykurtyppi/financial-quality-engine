"""The companyfacts vintage store: what it keeps, what it refuses to keep
twice, and what it can see that a single fetch cannot."""

from __future__ import annotations

import gzip
import json
from datetime import date, datetime, timezone
from pathlib import Path
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
DAY1 = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
DAY2 = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
NEXT_DAY = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


class TestCapture:
    def test_first_capture_writes_a_gzipped_snapshot_and_a_manifest(self, tmp_path):
        payload = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "acc-1")])
        res = v.capture(_Client(payload), "NVDA", now=AT, root=tmp_path)
        assert res.wrote and res.reason == "captured"
        # Named for its CONTENT, not just the day — see TestSameDayRevision.
        assert res.path.name == v.snapshot_name(date(2026, 9, 19), res.sha256)
        assert res.path.name.startswith("2026-09-19-") and res.path.name.endswith(".json.gz")
        assert v.load_vintage(res.path) == payload
        man = v.read_manifest(1045810, tmp_path)
        assert man["last_checked"] == "2026-09-19"
        assert [s["file"] for s in man["snapshots"]] == [res.path.name]
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

    def test_a_corrupt_manifest_is_rebuilt_from_disk(self, tmp_path):
        payload = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "a")])
        first = v.capture(_Client(payload), "NVDA", now=AT, root=tmp_path)
        (v.cik_dir(1045810, tmp_path) / v.MANIFEST).write_text("{not json")
        man = v.read_manifest(1045810, tmp_path)
        assert [s["file"] for s in man["snapshots"]] == [first.path.name]
        assert man["snapshots"][0]["sha256"] == first.sha256  # recovered from the name

    def test_a_lost_manifest_does_not_make_capture_re_store_what_it_has(self, tmp_path):
        # The manifest is an index; the files are the archive. A corrupt index
        # must not make capture think there is no history and start again.
        payload = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "a")])
        v.capture(_Client(payload), "NVDA", now=AT, root=tmp_path)
        (v.cik_dir(1045810, tmp_path) / v.MANIFEST).unlink()
        res = v.capture(_Client(payload), "NVDA", now=NEXT_DAY, root=tmp_path)
        assert not res.wrote and res.reason == "unchanged"
        assert len(v.list_vintages(1045810, tmp_path)) == 1

    def test_a_legacy_date_named_snapshot_is_still_indexed(self, tmp_path):
        # Snapshots written before names carried a digest are real data.
        import gzip as _gzip

        payload = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "a")])
        d = v.cik_dir(1045810, tmp_path)
        d.mkdir(parents=True)
        with _gzip.open(d / "2026-09-18.json.gz", "wb") as fh:
            fh.write(v.canonical_bytes(payload))
        man = v.read_manifest(1045810, tmp_path)
        assert [s["file"] for s in man["snapshots"]] == ["2026-09-18.json.gz"]
        assert man["snapshots"][0]["sha256"] == v.digest_of(payload)  # hashed from content
        # ...and capture recognises it as already held.
        assert v.capture(_Client(payload), "NVDA", now=AT, root=tmp_path).reason == "unchanged"

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
        assert len(wide) == 1 and wide[0].field_name == "us-gaap:MadeUpTag"

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


class TestSameDayRevisionIsNotLost:
    """The case the store exists for, and the one it used to destroy: a
    filing lands mid-day, the document changes, and the morning's snapshot —
    the only record of what the number used to be — must survive."""

    def test_two_different_documents_on_one_day_are_two_snapshots(self, tmp_path):
        before = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "acc-1")])
        after = _facts([("2026-06-30", "2026-09-19", 1200.0, "10-K", "acc-2")])
        client = _Client(before, after)
        morning = v.capture(client, "NVDA", now=AT, root=tmp_path)
        afternoon = v.capture(client, "NVDA", now=AT.replace(hour=17), root=tmp_path, force=True)
        assert morning.wrote and afternoon.wrote
        assert morning.path != afternoon.path
        files = v.list_vintages(1045810, tmp_path)
        assert len(files) == 2
        assert v.load_vintage(morning.path) == before      # the pre-revision value survives
        assert v.load_vintage(afternoon.path) == after
        assert len(v.read_manifest(1045810, tmp_path)["snapshots"]) == 2
        # ...and the diff across them is exactly the revision.
        changes = v.diff_vintages(v.load_vintage(morning.path), v.load_vintage(afternoon.path))
        assert [c.kind for c in changes] == ["revised"]

    def test_identical_content_twice_in_one_day_is_still_one_file(self, tmp_path):
        payload = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "a")])
        client = _Client(payload, payload)
        first = v.capture(client, "NVDA", now=AT, root=tmp_path)
        again = v.capture(client, "NVDA", now=AT, root=tmp_path, force=True)
        assert first.wrote and not again.wrote and again.reason == "unchanged"
        assert len(v.list_vintages(1045810, tmp_path)) == 1

    def test_content_already_held_is_never_stored_again(self, tmp_path):
        # A filer reverting a value should not re-store a document we have.
        a = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "a")])
        b = _facts([("2026-06-30", "2026-09-19", 1200.0, "10-K", "b")])
        client = _Client(a, b, a)
        v.capture(client, "NVDA", now=AT, root=tmp_path)
        v.capture(client, "NVDA", now=NEXT_DAY, root=tmp_path)
        back = v.capture(client, "NVDA", now=NEXT_DAY.replace(day=21), root=tmp_path)
        assert not back.wrote and len(v.list_vintages(1045810, tmp_path)) == 2


class TestConcurrentCapture:
    def test_a_second_capture_yields_rather_than_racing_the_manifest(self, tmp_path, monkeypatch):
        # Two writers building a manifest from the same stale read is how the
        # index silently loses a snapshot. The loser does nothing instead.
        import fcntl

        payload = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "a")])
        d = v.cik_dir(1045810, tmp_path)
        d.mkdir(parents=True)
        monkeypatch.setattr(v, "LOCK_TIMEOUT_S", 0.2)
        with (d / v.LOCK).open("w") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            res = v.capture(_Client(payload), "NVDA", now=AT, root=tmp_path)
        assert not res.wrote and res.reason == "busy"
        assert v.list_vintages(1045810, tmp_path) == []
        # released: the next attempt proceeds normally
        assert v.capture(_Client(payload), "NVDA", now=AT, root=tmp_path).wrote


class TestTagSwitchIsNotAWithdrawal:
    def test_a_field_that_moved_tags_is_not_reported_gone(self):
        # Both tags are candidates for `inventory`; the period still has a
        # value, so this is a taxonomy migration, not a disappearance.
        before = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "a")], tag="InventoryNet")
        after = _facts([("2026-06-30", "2026-11-01", 1000.0, "10-K", "b")], tag="InventoryGross")
        assert v.diff_vintages(before, after) == []

    def test_a_move_that_also_changed_the_value_is_still_reported(self):
        # A migration can carry a revision with it; silence would be the one
        # outcome this store cannot afford.
        before = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "a")], tag="InventoryNet")
        after = _facts([("2026-06-30", "2026-11-01", 1400.0, "10-K", "b")], tag="InventoryGross")
        changes = v.diff_vintages(before, after)
        assert [(c.kind, c.field_name) for c in changes] == [("revised", "inventory")]
        assert (changes[0].old_value, changes[0].new_value) == (1000.0, 1400.0)

    def test_a_genuine_disappearance_is_still_reported(self):
        before = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "a")])
        after = {"facts": {"us-gaap": {"Assets": {"units": {"USD": []}}}}}
        assert [c.kind for c in v.diff_vintages(before, after)] == ["withdrawn"]


class TestCorruptAndCollidingFiles:
    """A damaged file in the archive must not be written over, must not take
    down a read, and must not make capture think it holds what it does not."""

    @staticmethod
    def _corrupt(path):
        path.write_bytes(b"\x1f\x8b\x08truncated")   # a gz header and nothing else

    def test_gzip_eof_is_treated_as_unreadable_everywhere(self, tmp_path):
        d = v.cik_dir(1045810, tmp_path)
        d.mkdir(parents=True)
        self._corrupt(d / v.snapshot_name(date(2026, 9, 19), "a" * 64))
        with pytest.raises(v.UNREADABLE):
            v.load_vintage(next(d.glob("*.json.gz")))
        # ...but neither the index nor a capture raises because of it.
        man = v.read_manifest(1045810, tmp_path)
        assert len(man["snapshots"]) == 1 and man["snapshots"][0]["sha256"] == ""
        payload = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "a")])
        assert v.capture(_Client(payload), "NVDA", now=AT, root=tmp_path).wrote

    def test_an_unreadable_file_is_never_written_over(self, tmp_path):
        d = v.cik_dir(1045810, tmp_path)
        d.mkdir(parents=True)
        taken = d / v.snapshot_name(date(2026, 9, 19), "f" * 64)
        self._corrupt(taken)
        before = taken.read_bytes()
        chosen = v._free_path(d, date(2026, 9, 19), "f" * 64)
        assert chosen != taken and chosen.name.endswith("-1.json.gz")
        assert taken.read_bytes() == before

    def test_the_same_content_reuses_its_name(self, tmp_path):
        payload = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "a")])
        first = v.capture(_Client(payload), "NVDA", now=AT, root=tmp_path)
        again = v._free_path(v.cik_dir(1045810, tmp_path), AT.date(), first.sha256)
        assert again == first.path   # no pointless -1 copy of identical bytes


class TestIndexCostDoesNotGrow:
    def test_a_valid_manifest_is_not_rebuilt_from_disk(self, tmp_path, monkeypatch):
        # Rebuilding an entry decompresses and re-hashes a multi-megabyte
        # document; capture reads the manifest every pass of an hourly job.
        payloads = [_facts([("2026-06-30", "2026-08-01", float(i), "10-Q", f"a{i}")])
                    for i in range(3)]
        client = _Client(*payloads)
        for i in range(3):
            v.capture(client, "NVDA", now=AT.replace(day=19 + i), root=tmp_path, force=True)
        assert len(v.list_vintages(1045810, tmp_path)) == 3
        hashed = []
        real = v.digest_of
        monkeypatch.setattr(v, "digest_of", lambda f: hashed.append(1) or real(f))
        v.read_manifest(1045810, tmp_path)
        assert hashed == []            # the index is trusted when it is intact
        # ...and an unknown file on disk is still picked up.
        v.load_vintage(v.list_vintages(1045810, tmp_path)[0])
        man = v.read_manifest(1045810, tmp_path)
        (v.cik_dir(1045810, tmp_path) / v.MANIFEST).write_text(
            json.dumps({"last_checked": None, "snapshots": man["snapshots"][:2]}))
        assert len(v.read_manifest(1045810, tmp_path)["snapshots"]) == 3


class TestOnlyTheTagTheEngineScores:
    """The sibling detector picks one best-coverage series per field on
    purpose, so a revision it reports is a revision to the number a report
    shows. An archive diff that scored every candidate tag would report
    movements on abandoned tags no report ever displays."""

    @staticmethod
    def _rev(entries):
        out = {"facts": {"us-gaap": {}}}
        for tag, rows in entries.items():
            out["facts"]["us-gaap"][tag] = {"units": {"USD": [
                {"start": r[0], "end": r[1], "filed": r[2], "val": r[3],
                 "form": "10-Q", "accn": r[4]} for r in rows]}}
        return out

    @staticmethod
    def _tags():
        from app.services.ingestion.companyfacts_mapper import FLOW_FIELDS
        return FLOW_FIELDS["revenue"][0][1], FLOW_FIELDS["revenue"][-1][1]

    def _wide_history(self):
        return [(f"2024-{m:02d}-01", f"2024-{m + 2:02d}-28", "2025-05-01", 100.0, "a")
                for m in (1, 4, 7, 10)]

    def test_a_losing_candidate_tag_moving_is_not_a_finding(self):
        active, legacy = self._tags()
        hist = self._wide_history()
        before = self._rev({active: hist, legacy: [("2024-01-01", "2024-03-28", "2023-05-01", 50.0, "x")]})
        after = self._rev({active: hist, legacy: [("2024-01-01", "2024-03-28", "2026-05-01", 75.0, "y")]})
        assert v.diff_vintages(before, after) == []

    def test_a_withdrawal_is_not_compared_against_a_stale_alternate_tag(self):
        # Reported as the withdrawal it is, at its own value — not as a
        # fabricated -98% "revision" to an abandoned tag's stale number.
        active, legacy = self._tags()
        hist = self._wide_history()
        stale = [("2024-01-01", "2024-03-28", "2019-05-01", 10.0, "stale")]
        before = self._rev({active: hist, legacy: stale})
        after = self._rev({active: hist[1:], legacy: stale})
        changes = v.diff_vintages(before, after)
        assert [(c.kind, c.old_value, c.new_value) for c in changes] == [("withdrawn", 100.0, None)]

    def test_a_real_migration_is_compared_and_the_new_tag_is_named(self):
        active, legacy = self._tags()
        old_hist = self._wide_history()
        new_hist = [(s, e, "2026-05-01", 140.0 if i == 0 else 100.0, "b")
                    for i, (s, e, _f, _v, _a) in enumerate(old_hist)]
        before = self._rev({active: old_hist})
        after = self._rev({legacy: new_hist})
        changes = [c for c in v.diff_vintages(before, after) if c.kind == "revised"]
        assert len(changes) == 1
        assert changes[0].moved_tag and changes[0].new_tag == legacy
        assert "now tagged" in v.render_changes(changes, "a", "b")

    def test_a_misfiled_unit_duplicate_is_not_a_share_count_revision(self):
        # A filer double-tagging a share count under USD would otherwise read
        # as a 9,900% revision of a count that never moved.
        rows = {"shares": [{"end": "2026-06-30", "filed": "2026-08-01", "val": 1_000.0,
                            "form": "10-Q", "accn": "a"}]}
        def doc(usd_val):
            return {"facts": {"dei": {"EntityCommonStockSharesOutstanding": {"units": {
                **rows, "USD": [{"end": "2026-06-30", "filed": "2026-08-01", "val": usd_val,
                                 "form": "10-Q", "accn": "a"}]}}}}}
        assert v.diff_vintages(doc(42.0), doc(4200.0), include_split_adjusted=True) == []


class TestTheDiffSaysWhatItIs:
    def test_every_finding_carries_the_context_caveat(self):
        before = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "acc-1")])
        after = _facts([("2026-06-30", "2026-11-01", 1200.0, "10-K", "acc-2")])
        md = v.render_changes(v.diff_vintages(before, after), "a", "b")
        # None of it has an amendment behind it — that is what makes it
        # invisible to Tier 1, and what makes it weak evidence.
        assert "Context, not an alarm" in md and "restatement" in md


class TestObservationOrder:
    def test_a_revert_is_recorded_even_though_the_content_is_not_restored(self, tmp_path):
        a = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "a")])
        b = _facts([("2026-06-30", "2026-09-19", 1200.0, "10-K", "b")])
        client = _Client(a, b, a)
        v.capture(client, "NVDA", now=AT, root=tmp_path)
        v.capture(client, "NVDA", now=NEXT_DAY, root=tmp_path)
        v.capture(client, "NVDA", now=NEXT_DAY.replace(day=21), root=tmp_path)
        obs = v.read_manifest(1045810, tmp_path)["observations"]
        assert [o["date"] for o in obs] == ["2026-09-19", "2026-09-20", "2026-09-21"]
        assert obs[0]["sha256"] == obs[2]["sha256"] != obs[1]["sha256"]
        assert len(v.list_vintages(1045810, tmp_path)) == 2  # content still stored once


class TestFailuresAreNotSilent:
    def test_a_failed_write_counts_a_problem_day_and_keeps_the_daily_gate(self, tmp_path, monkeypatch):
        # Without the gate surviving, a broken disk turns one fetch a day into
        # one an hour while archiving nothing either way.
        payload = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "a")])
        client = _Client(payload, payload)
        real_open = Path.open

        def no_write(self, mode="r", *a, **k):
            if "w" in mode and self.name.endswith(".tmp") and self.name.count("json.gz"):
                raise OSError("disk full")
            return real_open(self, mode, *a, **k)

        monkeypatch.setattr(Path, "open", no_write)
        res = v.capture(client, "NVDA", now=AT, root=tmp_path)
        assert not res.wrote and res.reason == "failed" and res.problem
        man = v.read_manifest(1045810, tmp_path)
        assert man["problem_days"] == 1 and man["last_checked"] == "2026-09-19"
        monkeypatch.undo()
        again = v.capture(client, "NVDA", now=AT, root=tmp_path)
        assert again.reason == "already checked today"   # no hourly refetch loop

    def test_a_busy_lock_is_reported_not_swallowed(self, tmp_path, monkeypatch):
        import fcntl

        payload = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "a")])
        v.capture(_Client(payload), "NVDA", now=AT, root=tmp_path)
        d = v.cik_dir(1045810, tmp_path)
        before = (d / v.MANIFEST).read_bytes()
        monkeypatch.setattr(v, "LOCK_TIMEOUT_S", 0.2)
        with (d / v.LOCK).open("w") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            res = v.capture(_Client(payload), "NVDA", now=NEXT_DAY, root=tmp_path)
        assert res.reason == "busy" and res.problem and res.detail
        assert (d / v.MANIFEST).read_bytes() == before
        assert v.read_manifest(1045810, tmp_path)["problem_days"] == 1

    def test_a_successful_capture_clears_busy_day_markers(self, tmp_path):
        payload = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "a")])
        v._record_problem_day(1045810, AT.date(), tmp_path)
        assert v.read_manifest(1045810, tmp_path)["problem_days"] == 1
        assert v.capture(_Client(payload), "NVDA", now=NEXT_DAY, root=tmp_path).wrote
        assert v.read_manifest(1045810, tmp_path)["problem_days"] == 0


class TestObservedVintageOrder:
    def test_same_day_order_comes_from_observations_not_hashes(self, tmp_path):
        # The later payload's hash sorts before the earlier payload's hash.
        # Filename order would reverse this transition.
        earlier = _facts([("2026-06-30", "2026-08-01", 1200.0, "10-Q", "a")])
        later = _facts([("2026-06-30", "2026-09-19", 1000.0, "10-Q", "b")])
        client = _Client(earlier, later)
        first = v.capture(client, "NVDA", now=AT, root=tmp_path)
        second = v.capture(client, "NVDA", now=AT.replace(hour=17), root=tmp_path, force=True)
        assert first.path.name > second.path.name
        states = v.observed_vintages(1045810, tmp_path)
        assert [state.path for state in states] == [first.path, second.path]

    def test_a_revert_remains_a_later_state(self, tmp_path):
        a = _facts([("2026-06-30", "2026-08-01", 1000.0, "10-Q", "a")])
        b = _facts([("2026-06-30", "2026-09-19", 1200.0, "10-K", "b")])
        client = _Client(a, b, a)
        first = v.capture(client, "NVDA", now=AT, root=tmp_path)
        second = v.capture(client, "NVDA", now=NEXT_DAY, root=tmp_path)
        v.capture(client, "NVDA", now=NEXT_DAY.replace(day=21), root=tmp_path)
        states = v.observed_vintages(1045810, tmp_path)
        assert [state.path for state in states] == [first.path, second.path, first.path]
        assert [state.captured for state in states] == [
            "2026-09-19", "2026-09-20", "2026-09-21"
        ]


class TestOrphanCleanup:
    def test_a_stale_temp_file_is_removed_and_a_fresh_one_is_left_alone(self, tmp_path):
        import os, time as _t

        d = v.cik_dir(1045810, tmp_path)
        d.mkdir(parents=True)
        stale, fresh = d / ".x.json.gz.1.tmp", d / ".y.json.gz.2.tmp"
        stale.write_bytes(b"x"); fresh.write_bytes(b"y")
        old = _t.time() - 7200
        os.utime(stale, (old, old))
        v._sweep_orphans(d)
        assert not stale.exists() and fresh.exists()
        assert v.list_vintages(1045810, tmp_path) == []  # never mistaken for snapshots


def test_same_day_filed_ties_resolve_as_the_mapper_does():
    # The mapper keeps the FIRST fact at the latest filed date (`>`, not
    # `>=`). The sibling detector had a bug here once; this pins the diff to
    # the same rule so a revision it reports matches what the engine scores.
    rows = [("2026-06-30", "2026-08-01", 100.0, "10-Q", "first"),
            ("2026-06-30", "2026-08-01", 999.0, "10-Q", "second-same-day")]
    before = _facts([("2026-06-30", "2026-05-01", 50.0, "10-Q", "older")])
    after = _facts(rows)
    changes = v.diff_vintages(before, after)
    assert len(changes) == 1
    assert changes[0].new_value == 100.0 and changes[0].new_accession == "first"


class TestProblemDaysAreDistinctDays:
    """`problem_days` drives the operator alert at VINTAGE_STALE_DAYS. It has
    to count DAYS the archive got nothing — not attempts, and not one tally
    per kind of failure."""

    def _client(self, payload):
        class C:
            def resolve_cik(self, ticker): return 1045810
            def company_facts_by_cik(self, cik): return payload
        return C()

    def _fail_write(self, monkeypatch):
        real = v.os.replace

        def boom(a, b):
            if str(b).endswith(".json.gz"):
                raise OSError("disk full")
            return real(a, b)
        monkeypatch.setattr(v.os, "replace", boom)

    def _wedge_lock(self, monkeypatch):
        import contextlib

        @contextlib.contextmanager
        def wedged(cik, root=None, timeout=None):
            yield False
        monkeypatch.setattr(v, "_cik_lock", wedged)

    def test_a_failed_write_then_a_lock_timeout_counts_as_two_days(
            self, tmp_path, monkeypatch):
        # The realistic cascade: one incident causes a failed write and leaves
        # a wedged lock behind. Counting the two kinds separately and taking
        # the larger reported 1, so the alert came a day late.
        client = self._client({"cik": 1045810, "facts": {}})
        self._fail_write(monkeypatch)
        v.capture(client, "NVDA", now=DAY1, root=tmp_path)
        monkeypatch.undo()
        self._wedge_lock(monkeypatch)
        v.capture(client, "NVDA", now=DAY2, root=tmp_path)
        assert v.read_manifest(1045810, tmp_path)["problem_days"] == 2

    def test_many_failures_in_one_day_are_still_one_day(self, tmp_path, monkeypatch):
        # The hourly job meets the same wedged lock all morning.
        client = self._client({"cik": 1045810, "facts": {}})
        self._wedge_lock(monkeypatch)
        for _ in range(5):
            v.capture(client, "NVDA", now=DAY1, root=tmp_path)
        assert v.read_manifest(1045810, tmp_path)["problem_days"] == 1

    def test_a_success_clears_every_kind_of_problem_day(self, tmp_path, monkeypatch):
        client = self._client({"cik": 1045810, "facts": {}})
        self._fail_write(monkeypatch)
        v.capture(client, "NVDA", now=DAY1, root=tmp_path)
        monkeypatch.undo()
        v.capture(client, "NVDA", now=DAY2, root=tmp_path)
        assert v.read_manifest(1045810, tmp_path)["problem_days"] == 0
        assert list(v.cik_dir(1045810, tmp_path).glob(".busy-*")) == []
