"""SEC fair-access pacing holds for the whole machine, not for one client.

The pacing state used to live on the `SecClient` instance, read and written
with no lock. Twelve threads sharing one client all read the same "last
request" stamp and started within 0.8 ms of each other; twelve clients did not
pace each other at all. That is the web UI's real shape — `report_view` is a
sync handler in FastAPI's threadpool and builds a new client per request —
and the hourly sweep, the web UI and CLI scripts can run as separate
processes at the same moment. SEC's limit is 10 requests/s per machine.

Also here: a 429/503 carrying `Retry-After` was retried on the fixed 2 s / 5 s
schedule, i.e. sooner than the server asked.

No test here touches the network: `urllib.request.urlopen` is faked and
records the moment each request actually started.
"""

from __future__ import annotations

import email.message
import email.utils
import itertools
import logging
import multiprocessing
import os
import threading
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services.ingestion import sec_client as sc

INTERVAL = 0.05  # small, so twelve paced requests take ~0.6 s
# Slots are exactly INTERVAL apart, but a request the scheduler wakes late
# shortens the gap to the next one by its lateness — tens of ms on a loaded CI
# box — without any extra request being made. So one gap may fall short by
# TOL, while the span of all starts may fall short by TOL in total, never per
# request. The unfixed code's gaps were ~0.1 ms and its span ~1 ms.
TOL = 0.02


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _recording_urlopen(starts: list[float]):
    lock = threading.Lock()

    def fake(req, timeout=None):
        with lock:
            starts.append(time.monotonic())
        return _Resp(b'{"ok": true}')

    return fake


def _assert_paced(starts: list[float], expected: int) -> None:
    assert len(starts) == expected
    ordered = sorted(starts)
    gaps = [b - a for a, b in itertools.pairwise(ordered)]
    # Sorted-adjacent gaps are the smallest pairwise gaps, so this is the
    # every-pair condition.
    shown = [round(g * 1000, 2) for g in gaps]
    assert min(gaps) >= INTERVAL - TOL, (
        f"requests started {min(gaps) * 1000:.2f} ms apart; the fair-access "
        f"interval is {INTERVAL * 1000:.0f} ms (all gaps: {shown})"
    )
    # The rate over the whole run: n starts need n-1 whole intervals.
    assert ordered[-1] - ordered[0] >= (expected - 1) * INTERVAL - TOL, shown


@pytest.fixture
def fast_pacing(monkeypatch):
    """A small interval and a clean process-wide schedule. `raising=False`:
    on the unfixed module the schedule attribute does not exist, and the test
    must then FAIL on its assertion, not error in setup."""
    monkeypatch.setattr(sc, "_REQUEST_INTERVAL_S", INTERVAL)
    monkeypatch.setattr(sc, "_last_start", 0.0, raising=False)
    monkeypatch.setenv("EDGAR_IDENTITY", "Test Suite test@example.com")


def _run_threads(target, n: int = 12) -> list[str]:
    barrier = threading.Barrier(n)
    errors: list[str] = []

    def body(i: int) -> None:
        try:
            barrier.wait()
            target(i)
        except BaseException as e:  # noqa: BLE001 - reported below
            errors.append(repr(e))

    threads = [threading.Thread(target=body, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


class TestProcessWidePacing:
    def test_twelve_threads_on_one_client_are_paced(self, fast_pacing, monkeypatch, tmp_path):
        """Defect: all twelve read the same per-instance stamp before any of
        them wrote it, saw no wait, and fired together."""
        starts: list[float] = []
        monkeypatch.setattr(sc.urllib.request, "urlopen", _recording_urlopen(starts))
        client = sc.SecClient(cache_dir=tmp_path)
        t0 = time.monotonic()
        assert _run_threads(lambda i: client._get(f"https://example/{i}")) == []
        _assert_paced(starts, 12)
        assert time.monotonic() - t0 < 2.0

    def test_twelve_threads_each_with_its_own_client_are_paced(
            self, fast_pacing, monkeypatch, tmp_path):
        """Defect: pacing lived on the instance, so separate clients — one
        per web request — did not pace each other at all."""
        starts: list[float] = []
        monkeypatch.setattr(sc.urllib.request, "urlopen", _recording_urlopen(starts))
        clients = [sc.SecClient(cache_dir=tmp_path) for _ in range(12)]
        assert _run_threads(lambda i: clients[i]._get(f"https://example/{i}")) == []
        _assert_paced(starts, 12)

    def test_clients_on_different_cache_dirs_are_still_paced(
            self, fast_pacing, monkeypatch, tmp_path):
        """The shared state file is per cache directory, but the machine's
        budget is not: within one process every client shares one schedule."""
        starts: list[float] = []
        monkeypatch.setattr(sc.urllib.request, "urlopen", _recording_urlopen(starts))
        clients = [sc.SecClient(cache_dir=tmp_path / f"c{i}") for i in range(6)]
        assert _run_threads(lambda i: clients[i]._get(f"https://example/{i}"), n=6) == []
        _assert_paced(starts, 6)

    def test_every_attempt_takes_a_slot_even_when_it_fails(
            self, fast_pacing, monkeypatch, tmp_path):
        """A refused request still cost SEC a connection: retries are paced
        like first attempts, not fired straight after the back-off."""
        starts: list[float] = []

        def failing(req, timeout=None):
            starts.append(time.monotonic())
            raise urllib.error.URLError("down")

        monkeypatch.setattr(sc, "_RETRY_BACKOFF_S", (0.0, 0.0))
        monkeypatch.setattr(sc.urllib.request, "urlopen", failing)
        client = sc.SecClient(cache_dir=tmp_path)
        with pytest.raises(sc.SecClientError, match=f"after {sc._MAX_ATTEMPTS} attempts"):
            client._get("https://example/x")
        _assert_paced(starts, sc._MAX_ATTEMPTS)

    def test_the_slot_is_reserved_before_sleeping_not_while_holding_the_lock(
            self, fast_pacing, monkeypatch, tmp_path):
        """Waiters queue by reservation: nobody sleeps while holding the
        schedule lock, so a second thread can reserve the slot after the one a
        sleeping thread holds."""
        monkeypatch.setattr(sc, "_last_start", time.monotonic() + 10.0)
        seen_locked: list[bool] = []

        def sleep(_s):
            seen_locked.append(sc._pace_lock.locked())

        monkeypatch.setattr(sc.time, "sleep", sleep)
        monkeypatch.setattr(sc.urllib.request, "urlopen", _recording_urlopen([]))
        sc.SecClient(cache_dir=tmp_path)._get("https://example/x")
        assert seen_locked == [False]


def _process_worker(cache_dir: str, interval: float, n: int, barrier, out) -> None:
    """One process's share of the cross-process test: n sequential requests
    after both processes are up. Module-level so `spawn` can import it."""
    sc._REQUEST_INTERVAL_S = interval
    starts: list[float] = []
    sc.urllib.request.urlopen = _recording_urlopen(starts)  # type: ignore[assignment]
    client = sc.SecClient(cache_dir=cache_dir, identity="Test Suite test@example.com")
    barrier.wait()
    for i in range(n):
        client._get(f"https://example/{os.getpid()}/{i}")
    out.put(starts)


class TestCrossProcessPacing:
    def test_two_processes_sharing_a_cache_dir_are_paced(self, tmp_path):
        """Defect: the hourly sweep and a web-UI report are separate
        processes; nothing paced one against the other. CLOCK_MONOTONIC is
        system-wide on Linux and macOS, so the children's stamps compare."""
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(2)
        out = ctx.Queue()
        procs = [ctx.Process(target=_process_worker,
                             args=(str(tmp_path), INTERVAL, 6, barrier, out))
                 for _ in range(2)]
        for p in procs:
            p.start()
        results = [out.get(timeout=60) for _ in procs]
        for p in procs:
            p.join(timeout=60)
            assert p.exitcode == 0
        _assert_paced([s for r in results for s in r], 12)
        assert (tmp_path / ".sec_rate").is_file()

    def test_a_reservation_far_in_the_future_is_not_trusted(self, fast_pacing, tmp_path):
        """The shared schedule is wall-clock time (monotonic clocks do not
        compare across reboots, and the file outlives processes). A clock
        stepped backwards, or a corrupted file, must not park every request
        behind a start time hours away."""
        (tmp_path / ".sec_rate").write_text(repr(time.time() + 3600))
        t0 = time.monotonic()
        start = sc._reserve_slot(tmp_path)
        assert start - t0 < 1.0
        assert float((tmp_path / ".sec_rate").read_text()) < time.time() + 1.0

    def test_a_garbled_state_file_is_restarted_not_fatal(self, fast_pacing, tmp_path):
        (tmp_path / ".sec_rate").write_text("not a number")
        start = sc._reserve_slot(tmp_path)
        assert start - time.monotonic() < 1.0
        float((tmp_path / ".sec_rate").read_text())  # rewritten parseable

    def test_an_unwritable_cache_dir_falls_back_to_process_pacing(
            self, fast_pacing, monkeypatch, tmp_path, caplog):
        """A read-only cache (a shared mount, a container) must never fail the
        fetch: pacing drops to process-wide and says so once."""
        monkeypatch.setattr(sc, "_shared_pacing_warned", set(), raising=False)
        real_open = os.open

        def refusing_open(path, *a, **kw):
            if str(path).endswith(".sec_rate.lock"):
                raise PermissionError(13, "read-only", str(path))
            return real_open(path, *a, **kw)

        monkeypatch.setattr(sc.os, "open", refusing_open)
        starts: list[float] = []
        monkeypatch.setattr(sc.urllib.request, "urlopen", _recording_urlopen(starts))
        client = sc.SecClient(cache_dir=tmp_path)
        with caplog.at_level(logging.WARNING, logger=sc.__name__):
            for i in range(3):
                assert client._get(f"https://example/{i}") == b'{"ok": true}'
        _assert_paced(starts, 3)
        warnings = [r for r in caplog.records if "process-wide" in r.getMessage()]
        assert len(warnings) == 1
        assert not (tmp_path / ".sec_rate").exists()

    def test_a_flock_failure_falls_back_too(self, fast_pacing, monkeypatch, tmp_path):
        monkeypatch.setattr(sc, "_shared_pacing_warned", set(), raising=False)

        def no_locks(fd, op):
            raise OSError(37, "No locks available")  # ENOLCK: NFS without lockd

        monkeypatch.setattr(sc.fcntl, "flock", no_locks)
        starts: list[float] = []
        monkeypatch.setattr(sc.urllib.request, "urlopen", _recording_urlopen(starts))
        client = sc.SecClient(cache_dir=tmp_path)
        for i in range(2):
            client._get(f"https://example/{i}")
        _assert_paced(starts, 2)

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork only")
    def test_a_child_forked_mid_reservation_neither_deadlocks_nor_holds_the_flock(
            self, fast_pacing, tmp_path):
        """Forked while a reservation was in progress (module lock held,
        sidecar flocked), the child inherited the lock LOCKED — its first
        request would deadlock — and a copy of the flocked descriptor that no
        thread in it would ever close, so every OTHER process's next request
        blocked for the child's whole life."""
        lock_path = tmp_path / ".sec_rate.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        sc.fcntl.flock(fd, sc.fcntl.LOCK_EX)
        sc._pace_lock.acquire()
        sc._held_rate_fd = fd
        ready_r, ready_w = os.pipe()
        try:
            pid = os.fork()
            if pid == 0:  # child: report through the exit code, never return
                # The at-fork hook has run by the time fork() returns here;
                # tell the parent so its probe cannot race ahead of it.
                os.write(ready_w, b"x")
                ok = sc._pace_lock.acquire(timeout=1.0) and sc._held_rate_fd is None
                time.sleep(1.0)  # stay alive while the parent probes the flock
                os._exit(0 if ok else 1)
        finally:
            sc._held_rate_fd = None
            sc._pace_lock.release()
            os.close(fd)  # the parent's copy: the flock is free unless the child kept one
        os.read(ready_r, 1)
        os.close(ready_r)
        os.close(ready_w)
        probe = os.open(lock_path, os.O_RDWR)
        try:
            sc.fcntl.flock(probe, sc.fcntl.LOCK_EX | sc.fcntl.LOCK_NB)  # raises if still held
        finally:
            os.close(probe)
            _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0, "child deadlocked or kept the descriptor"


# --- Retry-After -------------------------------------------------------------------

def _http(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://example/x", code, "slow down", headers, None)


class TestRetryAfter:
    @pytest.fixture
    def run(self, fast_pacing, monkeypatch, tmp_path):
        """Fetch once against the given outcomes; return (body, sleeps).
        Sleeps are recorded, not slept."""

        def go(*outcomes):
            sleeps: list[float] = []
            monkeypatch.setattr(sc.time, "sleep", sleeps.append)
            queue = list(outcomes)

            def fake(req, timeout=None):
                item = queue.pop(0)
                if isinstance(item, Exception):
                    raise item
                return _Resp(item)

            monkeypatch.setattr(sc.urllib.request, "urlopen", fake)
            body = sc.SecClient(cache_dir=tmp_path)._get("https://example/x")
            # The limiter's own waits (a few intervals at most, since these
            # sleeps return at once) are recorded too; only the
            # between-attempt waits, all >= 1 s here, matter.
            return body, [s for s in sleeps if s >= 1.0]

        return go

    @pytest.mark.parametrize("code", [429, 503])
    def test_the_servers_retry_after_is_waited_out(self, run, code):
        """Defect: a throttled request came back after the fixed 2 s even
        when SEC asked for 30."""
        body, waits = run(_http(code, "30"), b"ok")
        assert body == b"ok"
        assert waits == [30.0]

    def test_retry_after_one_second_waits_at_least_that(self, run):
        _, waits = run(_http(429, "1"), b"ok")
        assert len(waits) == 1 and waits[0] >= 1.0

    def test_a_shorter_header_never_speeds_up_our_own_back_off(self, run):
        _, waits = run(_http(429, "0"), b"ok")
        assert waits == [sc._RETRY_BACKOFF_S[0]]

    def test_retry_after_is_capped(self, run):
        """A misconfigured or hostile header must not park an unattended
        sweep for an hour."""
        _, waits = run(_http(429, "3600"), b"ok")
        assert waits == [60.0]

    @pytest.mark.parametrize("form", [
        {"usegmt": True},  # "... GMT": the form RFC 9110 requires
        {"localtime": False},  # "... -0000": parses as a naive datetime
    ])
    def test_the_http_date_form_is_honoured(self, run, form):
        when = email.utils.formatdate(time.time() + 20, **form)
        _, waits = run(_http(503, when), b"ok")
        assert len(waits) == 1 and 17.0 <= waits[0] <= 20.0

    def test_an_http_date_with_an_offset_is_read_in_its_own_zone(self, run):
        # 20 s from now, written in UTC+1: read as UTC it would be an hour out.
        when = email.utils.format_datetime(
            datetime.now(timezone(timedelta(hours=1))) + timedelta(seconds=20))
        _, waits = run(_http(429, when), b"ok")
        assert len(waits) == 1 and 17.0 <= waits[0] <= 20.0

    @pytest.mark.parametrize("value", ["soon", "-5", "1.5", "", "\u00b2"])
    def test_an_unreadable_header_falls_back_to_the_back_off(self, run, value):
        _, waits = run(_http(429, value), b"ok")
        assert waits == [sc._RETRY_BACKOFF_S[0]]

    def test_other_retry_statuses_keep_the_fixed_back_off(self, run):
        _, waits = run(_http(500, "30"), b"ok")
        assert waits == [sc._RETRY_BACKOFF_S[0]]

    def test_the_second_retry_uses_the_second_header(self, run):
        _, waits = run(_http(429, "7"), _http(429, "11"), b"ok")
        assert waits == [7.0, 11.0]

    def test_the_attempt_count_is_unchanged(self, run):
        with pytest.raises(sc.SecClientError, match=f"after {sc._MAX_ATTEMPTS} attempts"):
            run(*[_http(429, "1")] * sc._MAX_ATTEMPTS)

    def test_no_wait_after_the_last_attempt(self, monkeypatch, tmp_path):
        """Waiting after the final failure only delays the error."""
        monkeypatch.setattr(sc, "_REQUEST_INTERVAL_S", 0.0)  # no limiter waits
        monkeypatch.setenv("EDGAR_IDENTITY", "Test Suite test@example.com")
        sleeps: list[float] = []
        monkeypatch.setattr(sc.time, "sleep", sleeps.append)

        def fake(req, timeout=None):
            raise _http(503)

        monkeypatch.setattr(sc.urllib.request, "urlopen", fake)
        with pytest.raises(sc.SecClientError):
            sc.SecClient(cache_dir=tmp_path)._get("https://example/x")
        assert sleeps == list(sc._RETRY_BACKOFF_S)

    def test_a_403_is_still_an_answer(self, run):
        with pytest.raises(sc.SecClientError, match="HTTP Error 403") as e:
            run(_http(403, "30"))
        assert "attempts" not in str(e.value)


def test_no_state_file_outside_the_cache_dir(fast_pacing, monkeypatch, tmp_path):
    """The shared schedule lives in the client's cache directory and nowhere
    else — a test or tool using its own cache never touches data/cache."""
    monkeypatch.setattr(sc.urllib.request, "urlopen", _recording_urlopen([]))
    sc.SecClient(cache_dir=tmp_path / "c")._get("https://example/x")
    assert sorted(p.name for p in (tmp_path / "c").iterdir()) == [".sec_rate", ".sec_rate.lock"]
    assert not Path(tmp_path / ".sec_rate").exists()
