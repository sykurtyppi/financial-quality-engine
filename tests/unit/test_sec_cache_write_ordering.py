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

from app.services.ingestion.sec_client import (
    SecClient, _is_readable_json, _published_generation_ns,
)

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
    # The `.lock` sidecar is deliberate and persistent: the lock cannot live
    # on the entry itself, whose inode `os.replace` swaps out. Anything else
    # is a leaked partial write.
    leftovers = [p.name for p in tmp_path.iterdir()
                 if p.name not in (NAME, f".{NAME}.lock")]
    assert leftovers == [], f"temp files leaked: {leftovers}"


class TestGenerationStamp:
    """The published entry's mtime must be the request's START time, not the
    moment the write landed. Comparing a rival's completion time against our
    start time answers a different question, and answers it wrongly whenever a
    request that started earlier finishes later."""

    def test_a_published_entry_carries_its_requests_start_time(self, tmp_path):
        client = _PacedClient(tmp_path, {"version": "only"}, 0.20)
        before = time.time_ns()
        client._cached_json(NAME, URL, max_age_s=0)
        after = time.time_ns()

        generation = _published_generation_ns(tmp_path / NAME)
        assert generation is not None
        # Stamped at request start, so it precedes the ~0.2s the fetch took.
        assert before <= generation <= after
        assert generation < after - 100_000_000

    def test_nothing_published_reads_as_none(self, tmp_path):
        assert _published_generation_ns(tmp_path / "absent.json") is None

    def test_a_stat_failure_never_blocks_the_cache_forever(self, tmp_path, monkeypatch):
        def boom(self):
            raise OSError("stat failed")

        monkeypatch.setattr("pathlib.Path.stat", boom)
        assert _published_generation_ns(tmp_path / NAME) is None


class TestCorruptEntryRecovery:
    """Dropping a poisoned entry must not discard a good one that a concurrent
    writer published at the same pathname in the meantime."""

    def test_a_truncated_entry_is_replaced_not_merely_deleted(self, tmp_path):
        (tmp_path / NAME).write_text("{ truncated")
        client = _PacedClient(tmp_path, {"version": "refetched"}, 0.0)
        assert client._cached_json(NAME, URL) == {"version": "refetched"}
        assert _cached(tmp_path) == {"version": "refetched"}

    def test_recovery_rechecks_before_unlinking(self, tmp_path):
        target = tmp_path / NAME
        target.write_text("{ truncated")
        assert _is_readable_json(target) is False
        target.write_text('{"version": "someone elses good entry"}')
        assert _is_readable_json(target) is True

    def test_a_concurrent_valid_publication_survives_recovery(self, tmp_path):
        """A reader that just failed to parse must not delete the replacement.
        Simulated by a good entry appearing between the failed parse and the
        unlink — the recheck under the lock is what saves it."""
        target = tmp_path / NAME
        target.write_text("{ truncated")

        good = {"version": "published by someone else"}
        real_get = _PacedClient._get

        def get_then_publish(self, url):
            target.write_text(json.dumps(good))
            return real_get(self, url)

        client = _PacedClient(tmp_path, {"version": "mine"}, 0.0)
        object.__setattr__(client, "_get", get_then_publish.__get__(client))
        client._cached_json(NAME, URL)
        assert _cached(tmp_path) != {}


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


def test_the_publication_lock_serializes_check_and_replace(tmp_path):
    """Check-then-publish must be one critical section. Without the lock two
    writers both read the same destination, both conclude they may publish,
    and then land in completion order — reinstating the defect."""
    from app.services.ingestion.sec_client import _publication_lock

    order: list[str] = []

    def hold(tag: str, pause: float) -> None:
        with _publication_lock(tmp_path / NAME):
            order.append(f"enter-{tag}")
            time.sleep(pause)
            order.append(f"exit-{tag}")

    a = threading.Thread(target=hold, args=("a", 0.15))
    a.start()
    time.sleep(0.02)
    b = threading.Thread(target=hold, args=("b", 0.0))
    b.start()
    a.join()
    b.join()
    # b cannot enter before a leaves: no interleaving.
    assert order == ["enter-a", "exit-a", "enter-b", "exit-b"]


@pytest.mark.parametrize("earlier_duration,later_duration", [
    (0.30, 0.02),  # the request that started earlier finishes LAST
    (0.02, 0.30),  # the request that started earlier finishes FIRST
    (0.15, 0.15),  # both in flight together
])
def test_the_later_started_request_always_wins(tmp_path, earlier_duration, later_duration):
    """The property, stated once: whatever the completion order, the entry
    that survives is the one from the request that asked SEC later."""
    def run(payload, delay):
        _PacedClient(tmp_path, payload, delay)._cached_json(NAME, URL, max_age_s=0)

    first = threading.Thread(target=run, args=({"version": "earlier"}, earlier_duration))
    first.start()
    time.sleep(0.05)
    second = threading.Thread(target=run, args=({"version": "later"}, later_duration))
    second.start()
    first.join()
    second.join()
    assert _cached(tmp_path) == {"version": "later"}
