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
# What the limiter decides is each request's reserved start, and those are
# asserted exactly (to RESERVE_TOL: a slot shared across processes is stored
# as wall-clock time and read back on the monotonic clock, two clock reads
# a descheduled process can straddle). When a request actually starts also
# depends on when the OS wakes it: one woken late shortens the gap to the
# next by its lateness — 29 ms was seen on a loaded box, with every slot
# exactly 50 ms apart and no extra request made. So actual starts are only
# held to what jitter cannot fake: on average at least half an interval
# apart. The unfixed code's reservations did not exist and its twelve
# starts spanned ~1 ms.
RESERVE_TOL = 0.005


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


def _gaps(times: list[float]) -> list[float]:
    ordered = sorted(times)
    return [b - a for a, b in itertools.pairwise(ordered)]


def _assert_paced(starts: list[float], expected: int,
                  reserved: list[float] | None = None) -> None:
    """Every request reserved a slot at least INTERVAL after the previous
    one, and the requests really were spread out in time."""
    reserved = RESERVED if reserved is None else reserved
    assert len(starts) == expected
    assert len(reserved) == expected, f"{len(reserved)} slots reserved for {expected} requests"
    gaps = _gaps(reserved)
    # Sorted-adjacent gaps are the smallest pairwise gaps, so this is the
    # every-pair condition.
    shown = [round(g * 1000, 2) for g in gaps]
    assert min(gaps) >= INTERVAL - RESERVE_TOL, (
        f"slots reserved {min(gaps) * 1000:.2f} ms apart; the fair-access "
        f"interval is {INTERVAL * 1000:.0f} ms (all gaps: {shown})"
    )
    span = max(starts) - min(starts)
    assert span >= (expected - 1) * INTERVAL / 2, (
        f"{expected} requests started within {span * 1000:.1f} ms"
    )


# Slots reserved in this process (recorded by `fast_pacing`).
RESERVED: list[float] = []


def _recording_reserve(real, into: list[float]):
    def reserve(cache_dir):
        start = real(cache_dir)
        into.append(start)
        return start
    return reserve


@pytest.fixture
def fast_pacing(monkeypatch):
    """A small interval and a clean process-wide schedule. `raising=False`:
    on the unfixed module the schedule attribute does not exist, and the test
    must then FAIL on its assertion, not error in setup."""
    monkeypatch.setattr(sc, "_REQUEST_INTERVAL_S", INTERVAL)
    monkeypatch.setattr(sc, "_last_start", 0.0, raising=False)
    # Likewise the process-wide hold-off: a virtual-clock Retry-After test
    # leaves it far ahead of the real clock, and a later test must not wait
    # it out.
    monkeypatch.setattr(sc, "_blocked_until", 0.0, raising=False)
    RESERVED.clear()
    if hasattr(sc, "_reserve_slot"):
        monkeypatch.setattr(sc, "_reserve_slot", _recording_reserve(sc._reserve_slot, RESERVED))
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

    # Daemon threads and a bounded join: a request that never goes out (a
    # pacing loop that never ends) fails the test instead of hanging the run.
    threads = [threading.Thread(target=body, args=(i,), daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + 30.0
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    stuck = sum(t.is_alive() for t in threads)
    assert stuck == 0, f"{stuck} of {n} requests never finished"
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
    reserved: list[float] = []
    sc.urllib.request.urlopen = _recording_urlopen(starts)  # type: ignore[assignment]
    if hasattr(sc, "_reserve_slot"):
        sc._reserve_slot = _recording_reserve(sc._reserve_slot, reserved)  # type: ignore[assignment]
    client = sc.SecClient(cache_dir=cache_dir, identity="Test Suite test@example.com")
    barrier.wait()
    for i in range(n):
        client._get(f"https://example/{os.getpid()}/{i}")
    out.put((starts, reserved))


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
        _assert_paced([s for r, _ in results for s in r], 12,
                      [s for _, r in results for s in r])
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


class _VirtualTime:
    """Stands in for the `time` module inside sec_client only: a sleep is
    recorded and ADVANCES the clocks instead of passing. A plain no-op sleep
    would leave the clocks where they were, so a Retry-After hold-off already
    waited out by the retrying thread would be waited out again by the
    limiter and recorded twice."""

    def __init__(self) -> None:
        self.offset = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return time.monotonic() + self.offset

    def time(self) -> float:
        return time.time() + self.offset

    def time_ns(self) -> int:
        return time.time_ns() + int(self.offset * 1e9)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.offset += max(seconds, 0.0)


class TestRetryAfter:
    @pytest.fixture
    def run(self, fast_pacing, monkeypatch, tmp_path):
        """Fetch once against the given outcomes; return (body, sleeps).
        Sleeps are recorded, not slept (virtual time)."""

        def go(*outcomes):
            clock = _VirtualTime()
            sleeps = clock.sleeps
            monkeypatch.setattr(sc, "time", clock)
            queue = list(outcomes)

            def fake(req, timeout=None):
                item = queue.pop(0)
                if isinstance(item, Exception):
                    raise item
                return _Resp(item)

            monkeypatch.setattr(sc.urllib.request, "urlopen", fake)
            body = sc.SecClient(cache_dir=tmp_path)._get("https://example/x")
            # The limiter's own waits (an interval at most) are recorded
            # too; only the between-attempt waits, all >= 1 s here, matter.
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


# --- a Retry-After holds off the whole machine ----------------------------------

HOLD = "1"  # seconds of Retry-After in the hold-off tests: short, but >> INTERVAL


def _signal_after_hold_off(event, monkeypatch=None) -> bool:
    """Set `event` once the throttled request has pushed the schedule. On a
    module without a hold-off the throttling fake sets it itself, so the test
    fails on its assertion rather than hanging."""
    if not hasattr(sc, "_hold_off"):
        return False
    real = sc._hold_off

    def hold_off(cache_dir, seconds):
        real(cache_dir, seconds)
        event.set()

    if monkeypatch is None:
        sc._hold_off = hold_off  # type: ignore[assignment]
    else:
        monkeypatch.setattr(sc, "_hold_off", hold_off)
    return True


def _throttling_urlopen(throttled_at: list[float], event, sets_event: bool, others: list[float]):
    """The first request is refused with `429, Retry-After: HOLD`; every
    other request succeeds and records when it started."""
    lock = threading.Lock()

    def fake(req, timeout=None):
        with lock:
            first = not throttled_at
            (throttled_at if first else others).append(time.monotonic())
        if first:
            if not sets_event:
                event.set()
            raise _http(429, HOLD)
        return _Resp(b"ok")

    return fake


def _stored_schedule(cache_dir: Path) -> float:
    """The shared schedule's last reserved start: the state file's first
    field (a second one, the hold-off's end, is present while one is
    active)."""
    return float((cache_dir / ".sec_rate").read_text().split()[0])


class TestRetryAfterHoldsOffEveryone:
    """Defect: only the call that got the 429 waited. SEC throttles by IP,
    so while it did, a second thread made 20 requests inside the window SEC
    had just asked the whole machine to stay out of."""

    def test_another_thread_on_another_client_waits_it_out(
            self, fast_pacing, monkeypatch, tmp_path):
        throttled_at: list[float] = []
        others: list[float] = []
        event = threading.Event()
        sets = _signal_after_hold_off(event, monkeypatch)
        monkeypatch.setattr(sc.urllib.request, "urlopen",
                            _throttling_urlopen(throttled_at, event, sets, others))
        monkeypatch.setattr(sc, "_RETRY_BACKOFF_S", (0.0, 0.0))
        retried: list[bytes] = []
        a = threading.Thread(target=lambda: retried.append(
            sc.SecClient(cache_dir=tmp_path / "a")._get("https://example/a")))
        a.start()
        assert event.wait(5)
        # A different client on a different cache dir: the process-wide
        # schedule, not only the shared file, must carry the hold-off.
        b = sc.SecClient(cache_dir=tmp_path / "b")
        for i in range(5):
            b._get(f"https://example/b/{i}")
        a.join(5)
        assert retried == [b"ok"]
        not_before = throttled_at[0] + float(HOLD)
        early = [round(t - throttled_at[0], 3) for t in others if t < not_before - RESERVE_TOL]
        assert early == [], f"requests {early} s after the 429 asked for {HOLD} s"

    def test_another_process_waits_it_out(self, tmp_path):
        ctx = multiprocessing.get_context("spawn")
        event = ctx.Event()
        out = ctx.Queue()
        procs = [
            ctx.Process(target=_throttled_worker, args=(str(tmp_path), INTERVAL, event, out)),
            ctx.Process(target=_waiting_worker, args=(str(tmp_path), INTERVAL, event, out)),
        ]
        for p in procs:
            p.start()
        results = dict(out.get(timeout=60) for _ in procs)
        for p in procs:
            p.join(timeout=60)
            assert p.exitcode == 0
        not_before = results["throttled"] + float(HOLD)
        early = [round(t - results["throttled"], 3) for t in results["waiting"]
                 if t < not_before - RESERVE_TOL]
        assert early == [], f"the other process started {early} s after a {HOLD} s Retry-After"

    def test_the_shared_cap_admits_any_honoured_retry_after(self):
        """A stored start more than the cap ahead is taken for a stepped
        clock and discarded — which must never include a real hold-off."""
        assert sc._SHARED_AHEAD_CAP_S > sc._RETRY_AFTER_CAP_S

    def test_a_hold_off_written_by_another_process_is_honoured(self, fast_pacing, tmp_path):
        (tmp_path / ".sec_rate").write_text(repr(time.time() + 50))
        start = sc._reserve_slot(tmp_path)
        assert start - time.monotonic() >= 49.0

    def test_a_hold_off_over_a_garbled_state_file_restarts_it(self, fast_pacing, tmp_path):
        (tmp_path / ".sec_rate").write_text("not a number")
        before = time.time()
        sc._hold_off(tmp_path, 1.0)
        stored = _stored_schedule(tmp_path)
        assert before + 1.0 - INTERVAL <= stored <= time.time() + 1.0
        # ...and the next reservation lands at or after the hold-off.
        assert sc._reserve_slot(tmp_path) - time.monotonic() >= 1.0 - 0.05

    def test_a_hold_off_never_pulls_the_schedule_earlier(self, fast_pacing, tmp_path):
        far = time.time() + 30
        (tmp_path / ".sec_rate").write_text(repr(far))
        sc._hold_off(tmp_path, 1.0)
        assert _stored_schedule(tmp_path) == far

    def test_an_unwritable_cache_still_holds_off_this_process(
            self, fast_pacing, monkeypatch, tmp_path):
        monkeypatch.setattr(sc, "_shared_pacing_warned", set())

        def no_locks(fd, op):
            raise OSError(37, "No locks available")

        monkeypatch.setattr(sc.fcntl, "flock", no_locks)
        sc._hold_off(tmp_path, 1.0)
        assert sc._reserve_slot(tmp_path) - time.monotonic() >= 1.0 - 0.05


def _throttled_worker(cache_dir: str, interval: float, event, out) -> None:
    """Gets the 429, pushes the shared schedule, then retries."""
    sc._REQUEST_INTERVAL_S = interval
    sc._RETRY_BACKOFF_S = (0.0, 0.0)  # type: ignore[assignment]
    throttled_at: list[float] = []
    sets = _signal_after_hold_off(event)
    sc.urllib.request.urlopen = _throttling_urlopen(  # type: ignore[assignment]
        throttled_at, event, sets, [])
    sc.SecClient(cache_dir=cache_dir, identity="Test Suite test@example.com")._get(
        "https://example/throttled")
    out.put(("throttled", throttled_at[0]))


def _waiting_worker(cache_dir: str, interval: float, event, out) -> None:
    """Starts its requests only once the other process has been throttled."""
    sc._REQUEST_INTERVAL_S = interval
    starts: list[float] = []
    sc.urllib.request.urlopen = _recording_urlopen(starts)  # type: ignore[assignment]
    client = sc.SecClient(cache_dir=cache_dir, identity="Test Suite test@example.com")
    event.wait(30)
    for i in range(3):
        client._get(f"https://example/waiting/{i}")
    out.put(("waiting", starts))


# --- the state files are never followed through a symlink -----------------------

class TestStateFilesDoNotFollowSymlinks:
    """Defect: `.sec_rate` was written with `Path.write_text` and the lock
    opened with plain O_CREAT, so a symlink planted at either name had its
    target overwritten or created. Both are opened O_NOFOLLOW now; a link
    there makes pacing fall back to process-wide, as any other OSError."""

    def test_a_symlinked_state_file_is_not_written_through(
            self, fast_pacing, monkeypatch, tmp_path):
        monkeypatch.setattr(sc, "_shared_pacing_warned", set())
        victim = tmp_path / "victim.txt"
        victim.write_text("precious")
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / ".sec_rate").symlink_to(victim)
        starts: list[float] = []
        monkeypatch.setattr(sc.urllib.request, "urlopen", _recording_urlopen(starts))
        client = sc.SecClient(cache_dir=cache)
        for i in range(3):
            assert client._get(f"https://example/{i}") == b'{"ok": true}'
        assert victim.read_text() == "precious"
        _assert_paced(starts, 3)

    def test_a_symlinked_lock_file_is_not_created_through(
            self, fast_pacing, monkeypatch, tmp_path):
        monkeypatch.setattr(sc, "_shared_pacing_warned", set())
        target = tmp_path / "created-through-the-link"
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / ".sec_rate.lock").symlink_to(target)
        monkeypatch.setattr(sc.urllib.request, "urlopen", _recording_urlopen([]))
        sc.SecClient(cache_dir=cache)._get("https://example/x")
        assert not target.exists()
        assert not (cache / ".sec_rate").exists()

    def test_without_o_nofollow_pacing_is_process_wide(self, fast_pacing, monkeypatch, tmp_path):
        monkeypatch.setattr(sc, "_shared_pacing_warned", set())
        monkeypatch.delattr(sc.os, "O_NOFOLLOW")
        starts: list[float] = []
        monkeypatch.setattr(sc.urllib.request, "urlopen", _recording_urlopen(starts))
        client = sc.SecClient(cache_dir=tmp_path)
        for i in range(2):
            client._get(f"https://example/{i}")
        assert not (tmp_path / ".sec_rate").exists()
        _assert_paced(starts, 2)


# --- a hold-off binds requests that had already reserved their slot -------------

def _reserve_then_wait(real, reserved, held, only_thread: str | None = None):
    """A `_reserve_slot` that, once it has reserved, signals `reserved` and
    returns only after `held` (the 429's hold-off) is set: the slot was
    taken BEFORE SEC asked the machine to back off, and is slept to after.
    `only_thread` limits that to one thread (the other client's calls pass
    straight through)."""

    def reserve(cache_dir):
        start = real(cache_dir)
        if only_thread is None or threading.current_thread().name == only_thread:
            reserved.set()
            held.wait(30)
        return start

    return reserve


def _refuse_once_in_flight(reserved, url_part: str, throttled_at: list[float],
                           sends: list[tuple[str, float]]):
    """The first request whose URL contains `url_part` waits until the other
    request has reserved its slot, then is refused with `429, Retry-After:
    HOLD`; every other request succeeds. Records (url, start) of each."""
    lock = threading.Lock()

    def fake(req, timeout=None):
        url = req.full_url
        with lock:
            first = url_part in url and not throttled_at
        if first:
            reserved.wait(30)
            throttled_at.append(time.monotonic())
            raise _http(429, HOLD)
        with lock:
            sends.append((url, time.monotonic()))
        return _Resp(b"ok")

    return fake


class _FrozenTime:
    """Stands in for `time` inside sec_client: clocks that never move, so a
    stored value can sit exactly on a boundary. Whole seconds on the wall
    clock, so `wall + cap - wall == cap` exactly. Nothing under it should
    wait: a sleep fails the test (and ends a loop that never would)."""

    def __init__(self) -> None:
        self.mono = float(int(time.monotonic()))
        self.wall = 1_800_000_000.0

    def monotonic(self) -> float:
        return self.mono

    def time(self) -> float:
        return self.wall

    def time_ns(self) -> int:
        return int(self.wall) * 1_000_000_000

    def sleep(self, seconds: float) -> None:
        raise AssertionError(f"slept {seconds!r} s on a frozen clock")


class TestAHoldOffBindsSlotsAlreadyReserved:
    """Defect (Hermes audit of 424b0b4, finding 6): a 429's Retry-After
    pushed the schedule for FUTURE reservations only. A request that had
    already reserved a slot slept to it and sent inside the cooldown — the
    second client's request went out ~0.3 s after a `Retry-After: 2`."""

    @pytest.mark.parametrize("layout", ["same-cache-dir", "other-cache-dir", "no-shared-file"])
    def test_another_client_in_this_process_waits_it_out(
            self, fast_pacing, monkeypatch, tmp_path, layout):
        if layout == "no-shared-file":
            # The shared file unusable (NFS without lockd): the process-wide
            # hold-off alone must stop the reserved request.
            monkeypatch.setattr(sc, "_shared_pacing_warned", set())

            def no_locks(fd, op):
                raise OSError(37, "No locks available")

            monkeypatch.setattr(sc.fcntl, "flock", no_locks)
        reserved, held = threading.Event(), threading.Event()
        throttled_at: list[float] = []
        sends: list[tuple[str, float]] = []
        monkeypatch.setattr(sc, "_RETRY_BACKOFF_S", (0.0, 0.0))
        monkeypatch.setattr(sc.urllib.request, "urlopen",
                            _refuse_once_in_flight(reserved, "/a", throttled_at, sends))
        real_hold = sc._hold_off

        def hold_off(cache_dir, seconds):
            real_hold(cache_dir, seconds)
            held.set()

        monkeypatch.setattr(sc, "_hold_off", hold_off)
        monkeypatch.setattr(sc, "_reserve_slot",
                            _reserve_then_wait(sc._reserve_slot, reserved, held, "B"))
        a_dir = tmp_path / "a"
        b_dir = tmp_path / ("a" if layout == "same-cache-dir" else "b")
        results: list[bytes] = []
        a = threading.Thread(target=lambda: results.append(
            sc.SecClient(cache_dir=a_dir)._get("https://example/a")), name="A", daemon=True)
        b = threading.Thread(target=lambda: results.append(
            sc.SecClient(cache_dir=b_dir)._get("https://example/b")), name="B", daemon=True)
        a.start()
        b.start()
        a.join(30)
        b.join(30)
        assert results == [b"ok", b"ok"]
        (b_sent,) = [t for url, t in sends if url.endswith("/b")]
        after = b_sent - throttled_at[0]
        assert after >= float(HOLD) - RESERVE_TOL, (
            f"a request reserved before the 429 went out {after:.3f} s after it; "
            f"Retry-After asked for {HOLD} s"
        )

    def test_another_process_waits_it_out(self, tmp_path):
        ctx = multiprocessing.get_context("spawn")
        reserved, held = ctx.Event(), ctx.Event()
        out = ctx.Queue()
        procs = [
            ctx.Process(target=_in_flight_throttled_worker,
                        args=(str(tmp_path), INTERVAL, reserved, held, out)),
            ctx.Process(target=_reserved_before_worker,
                        args=(str(tmp_path), INTERVAL, reserved, held, out)),
        ]
        for p in procs:
            p.start()
        results = dict(out.get(timeout=60) for _ in procs)
        for p in procs:
            p.join(timeout=60)
            assert p.exitcode == 0
        (sent,) = results["waiting"]
        after = sent - results["throttled"]
        assert after >= float(HOLD) - RESERVE_TOL, (
            f"the other process's reserved request went out {after:.3f} s after "
            f"a {HOLD} s Retry-After"
        )

    def test_a_hold_off_in_the_shared_file_holds_a_send(self, fast_pacing, monkeypatch, tmp_path):
        """Another process's hold-off, read from the file, binds a send here
        even when the stored schedule itself is long past."""
        now = time.time()
        (tmp_path / ".sec_rate").write_text(f"{now - 10.0!r} {now + 1.0!r}")
        starts: list[float] = []
        monkeypatch.setattr(sc.urllib.request, "urlopen", _recording_urlopen(starts))
        t0 = time.monotonic()
        sc.SecClient(cache_dir=tmp_path)._get("https://example/x")
        assert starts[0] - t0 >= 1.0 - RESERVE_TOL

    def test_the_hold_off_field_is_written_only_while_it_binds(self, fast_pacing, tmp_path):
        sc._hold_off(tmp_path, 0.0)  # ends as it starts: nothing to bind
        assert len((tmp_path / ".sec_rate").read_text().split()) == 1
        sc._hold_off(tmp_path, 1.0)
        schedule, until = (float(f) for f in (tmp_path / ".sec_rate").read_text().split())
        assert until == pytest.approx(time.time() + 1.0, abs=0.1)
        assert schedule == pytest.approx(until - INTERVAL, abs=0.001)
        sc._hold_off(tmp_path, 0.0)  # `Retry-After: 0` binds nothing new
        assert float((tmp_path / ".sec_rate").read_text().split()[1]) == until
        # Once it has passed it is dropped, and the file is back to the one
        # field a process running the previous module can read.
        now = time.time()
        (tmp_path / ".sec_rate").write_text(f"{now - 10.0!r} {now - 1.0!r}")
        assert sc._reserve_slot(tmp_path) - time.monotonic() < 1.0
        assert len((tmp_path / ".sec_rate").read_text().split()) == 1

    def test_a_hold_off_just_inside_the_cap_is_honoured(self, fast_pacing, tmp_path):
        """The cap discards a stepped clock, never a real (capped) Retry-After."""
        now = time.time()
        until = now + sc._RETRY_AFTER_CAP_S - 1.0
        (tmp_path / ".sec_rate").write_text(f"{now!r} {until!r}")
        assert sc._reserve_slot(tmp_path) - time.monotonic() >= sc._RETRY_AFTER_CAP_S - 2.0

    def test_a_hold_off_never_pulls_this_processs_schedule_earlier(
            self, fast_pacing, monkeypatch, tmp_path):
        """A queue already reserved past the hold-off stays queued (the
        shared file unusable, so the process-wide schedule alone decides)."""
        monkeypatch.setattr(sc, "_shared_pacing_warned", set())

        def no_locks(fd, op):
            raise OSError(37, "No locks available")

        monkeypatch.setattr(sc.fcntl, "flock", no_locks)
        monkeypatch.setattr(sc, "_last_start", time.monotonic() + 5.0)
        sc._hold_off(tmp_path, 1.0)
        assert sc._reserve_slot(tmp_path) - time.monotonic() >= 5.0 + INTERVAL - RESERVE_TOL

    # -- exact boundaries, on clocks that do not move --------------------------------

    @pytest.fixture
    def frozen(self, fast_pacing, monkeypatch):
        clock = _FrozenTime()
        monkeypatch.setattr(sc, "time", clock)
        return clock

    def test_a_slot_that_is_now_is_sent_at_once(self, frozen, monkeypatch, tmp_path):
        """No sleep and no second look: a request that is not held off goes
        straight out (a sleep here raises)."""
        starts: list[float] = []
        monkeypatch.setattr(sc.urllib.request, "urlopen", _recording_urlopen(starts))
        assert sc.SecClient(cache_dir=tmp_path)._get("https://example/x") == b'{"ok": true}'
        assert len(starts) == 1

    def test_a_hold_off_is_over_at_its_own_instant(self, frozen, monkeypatch, tmp_path):
        monkeypatch.setattr(sc, "_blocked_until", frozen.mono)
        (tmp_path / ".sec_rate").write_text(f"{frozen.wall - 10.0!r} {frozen.wall!r}")
        assert sc._held_off(tmp_path) is False
        monkeypatch.setattr(sc, "_blocked_until", frozen.mono + 0.5)
        assert sc._held_off(tmp_path) is True

    def test_the_caps_admit_a_value_exactly_at_them(self, frozen, tmp_path):
        state = tmp_path / ".sec_rate"
        state.write_text(f"{frozen.wall!r} {frozen.wall + sc._RETRY_AFTER_CAP_S!r}")
        assert sc._held_off(tmp_path) is True
        state.write_text(repr(frozen.wall + sc._SHARED_AHEAD_CAP_S))
        assert sc._reserve_slot(tmp_path) - frozen.mono == pytest.approx(
            sc._SHARED_AHEAD_CAP_S + INTERVAL, abs=1e-3)

    # -- compatibility guards: these pass on the unfixed module too ----------------

    def test_guard_an_old_one_field_state_file_is_still_the_schedule(
            self, fast_pacing, monkeypatch, tmp_path):
        """A file written before the hold-off field existed holds only the
        last start; it must still be read as the schedule, not as garbage."""
        ahead = time.time() + 0.3
        (tmp_path / ".sec_rate").write_text(repr(ahead))
        starts: list[float] = []
        monkeypatch.setattr(sc.urllib.request, "urlopen", _recording_urlopen(starts))
        t0_m, t0_w = time.monotonic(), time.time()
        sc.SecClient(cache_dir=tmp_path)._get("https://example/x")
        assert starts[0] - t0_m >= (ahead - t0_w) + INTERVAL - RESERVE_TOL
        # With no hold-off active the file keeps the one-field form, which a
        # process still running the old module reads as before.
        assert float((tmp_path / ".sec_rate").read_text()) >= ahead + INTERVAL - RESERVE_TOL

    @pytest.mark.parametrize("until", ["far", "nan", "inf"])
    def test_guard_a_hold_off_far_in_the_future_is_not_trusted(
            self, fast_pacing, monkeypatch, tmp_path, until):
        """Wall-clock like the schedule, so a clock stepped backwards (or a
        corrupted file) must not park every request on the machine behind
        it: a stored hold-off further out than any Retry-After honoured is
        discarded, as the schedule is past its own cap."""
        now = time.time()
        stored = {"far": repr(now + 3600.0), "nan": "nan", "inf": "inf"}[until]
        (tmp_path / ".sec_rate").write_text(f"{now!r} {stored}")
        starts: list[float] = []
        monkeypatch.setattr(sc.urllib.request, "urlopen", _recording_urlopen(starts))
        t0 = time.monotonic()
        sc.SecClient(cache_dir=tmp_path)._get("https://example/x")
        assert starts[0] - t0 < 1.0
        assert len((tmp_path / ".sec_rate").read_text().split()) == 1


def _in_flight_throttled_worker(cache_dir: str, interval: float, reserved, held, out) -> None:
    """Its first request is in flight when the other process reserves a
    slot, and is then refused with a Retry-After."""
    sc._REQUEST_INTERVAL_S = interval
    sc._RETRY_BACKOFF_S = (0.0, 0.0)  # type: ignore[assignment]
    real_hold = sc._hold_off

    def hold_off(cache_dir, seconds):
        real_hold(cache_dir, seconds)
        held.set()

    sc._hold_off = hold_off  # type: ignore[assignment]
    throttled_at: list[float] = []
    sc.urllib.request.urlopen = _refuse_once_in_flight(  # type: ignore[assignment]
        reserved, "/throttled", throttled_at, [])
    sc.SecClient(cache_dir=cache_dir, identity="Test Suite test@example.com")._get(
        "https://example/throttled")
    out.put(("throttled", throttled_at[0]))


def _reserved_before_worker(cache_dir: str, interval: float, reserved, held, out) -> None:
    """Reserves its slot before the other process's 429 and sleeps to it
    after the hold-off."""
    sc._REQUEST_INTERVAL_S = interval
    sc._reserve_slot = _reserve_then_wait(  # type: ignore[assignment]
        sc._reserve_slot, reserved, held)
    starts: list[float] = []
    sc.urllib.request.urlopen = _recording_urlopen(starts)  # type: ignore[assignment]
    sc.SecClient(cache_dir=cache_dir, identity="Test Suite test@example.com")._get(
        "https://example/waiting")
    out.put(("waiting", starts))
