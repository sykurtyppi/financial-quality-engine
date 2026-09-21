"""Concurrent cache refreshes must not publish an older SEC snapshot.

`os.replace` is atomic but not ORDERED. It guarantees no reader ever sees
half a file, and nothing about which of two concurrent writers wins. A slow
request that started first lands AFTER a fast one that started later, and
overwrites it — so every subsequent read is served the older snapshot for up
to the cache TTL.

On filing day that is a report built from a pre-filing index. The web UI and
the earnings watcher construct separate `SecClient` instances, so the
per-instance request pacing does not serialize these writes.

The existing atomic-write tests cannot see this: they check that the file is
never truncated, which remains true throughout.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from app.services.ingestion.sec_client import SecClient, _is_newer_than

URL = "https://data.sec.gov/x"
NAME = "x.json"


class _PacedClient(SecClient):
    """A client whose fetch takes a controlled amount of time."""

    def __init__(self, cache_dir, payload: dict, delay: float):
        super().__init__(identity="Test test@example.com")
        self.cache_dir = cache_dir
        self._payload = payload
        self._delay = delay

    def _get(self, url: str) -> bytes:
        time.sleep(self._delay)
        return json.dumps(self._payload).encode()


def _cached(tmp_path):
    return json.loads((tmp_path / NAME).read_text())


def test_a_slower_older_request_does_not_overwrite_a_newer_one(tmp_path):
    returned: dict[str, dict] = {}

    def run(key: str, payload: dict, delay: float) -> None:
        client = _PacedClient(tmp_path, payload, delay)
        returned[key] = client._cached_json(NAME, URL, max_age_s=0)

    # `old` starts first and finishes last; `new` starts later and finishes first.
    first = threading.Thread(target=run, args=("old", {"version": "old"}, 0.40))
    first.start()
    time.sleep(0.05)
    second = threading.Thread(target=run, args=("new", {"version": "new"}, 0.05))
    second.start()
    first.join()
    second.join()

    # Each caller still receives the response it actually fetched — the
    # ordering rule governs what is PUBLISHED, never what is returned.
    assert returned == {"old": {"version": "old"}, "new": {"version": "new"}}
    assert _cached(tmp_path) == {"version": "new"}


def test_a_lone_writer_still_publishes(tmp_path):
    client = _PacedClient(tmp_path, {"version": "only"}, 0.0)
    assert client._cached_json(NAME, URL, max_age_s=0) == {"version": "only"}
    assert _cached(tmp_path) == {"version": "only"}


def test_a_later_refresh_replaces_an_earlier_entry(tmp_path):
    """Sequential refreshes must still update — the guard distinguishes a
    concurrent newer writer from an entry this request supersedes."""
    _PacedClient(tmp_path, {"version": "first"}, 0.0)._cached_json(NAME, URL, max_age_s=0)
    assert _cached(tmp_path) == {"version": "first"}
    _PacedClient(tmp_path, {"version": "second"}, 0.0)._cached_json(NAME, URL, max_age_s=0)
    assert _cached(tmp_path) == {"version": "second"}


def test_no_temporary_files_are_left_behind(tmp_path):
    test_a_slower_older_request_does_not_overwrite_a_newer_one(tmp_path)
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != NAME]
    assert leftovers == [], f"temp files leaked: {leftovers}"


class TestGenerationGuard:
    def test_a_missing_entry_is_not_newer(self, tmp_path):
        assert _is_newer_than(tmp_path / "absent.json", time.time_ns()) is False

    def test_an_entry_written_after_the_stamp_is_newer(self, tmp_path):
        target = tmp_path / NAME
        stamp = time.time_ns()
        time.sleep(0.01)
        target.write_text("{}")
        assert _is_newer_than(target, stamp) is True

    def test_an_entry_written_before_the_stamp_is_not_newer(self, tmp_path):
        target = tmp_path / NAME
        target.write_text("{}")
        time.sleep(0.01)
        assert _is_newer_than(target, time.time_ns()) is False

    def test_a_stat_failure_never_blocks_the_cache_forever(self, tmp_path, monkeypatch):
        """If the guard could raise or return True on an unreadable entry, a
        single bad stat would stop the cache ever being written again."""
        target = tmp_path / NAME

        def boom(self):
            raise OSError("stat failed")

        monkeypatch.setattr("pathlib.Path.stat", boom)
        assert _is_newer_than(target, time.time_ns()) is False


@pytest.mark.parametrize("delay_first,delay_second", [(0.30, 0.02), (0.02, 0.30)])
def test_the_cache_always_ends_holding_the_later_started_request(
    tmp_path, delay_first, delay_second
):
    """Whichever finishes first, the entry that survives is the one from the
    request that started later — the only ordering SEC responses give us."""
    def run(payload, delay):
        _PacedClient(tmp_path, payload, delay)._cached_json(NAME, URL, max_age_s=0)

    a = threading.Thread(target=run, args=({"version": "earlier"}, delay_first))
    a.start()
    time.sleep(0.05)
    b = threading.Thread(target=run, args=({"version": "later"}, delay_second))
    b.start()
    a.join()
    b.join()
    assert _cached(tmp_path) == {"version": "later"}
