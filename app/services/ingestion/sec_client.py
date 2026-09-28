"""Minimal SEC EDGAR data client (dependency-free).

Fetches company facts from the public XBRL companyfacts API with local
caching. SEC fair-access rules require a descriptive User-Agent: set
EDGAR_IDENTITY (e.g. "Jane Doe jane@example.com") or pass identity explicitly.

Endpoints used:
- https://www.sec.gov/files/company_tickers.json      (ticker -> CIK)
- https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
"""

from __future__ import annotations

import email.utils
import errno
import fcntl
import json
import logging
import math
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.services.ingestion.payloads import (
    ExternalPayloadError,
    SubmissionsMismatchError,
)

logger = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{doc}"
_REQUEST_INTERVAL_S = 0.15  # stay far under SEC's 10 req/s limit
# The longest Retry-After honoured (see `_retry_after_s`). Here because the
# shared schedule's credibility cap below is derived from it.
_RETRY_AFTER_CAP_S = 60.0

# --- fair-access pacing ---------------------------------------------------------
#
# SEC's limit is 10 requests/s from one MACHINE, so the interval above is a
# budget every request on the machine shares. It used to live on the
# `SecClient` instance, read and written with no lock: twelve threads on one
# client all read the same stamp before any wrote it and started within
# 0.8 ms, and separate clients did not pace each other at all. That is the web
# UI's real shape — `report_view` is a sync handler run in FastAPI's
# threadpool and builds a new client per request — and the hourly sweep, the
# web UI and CLI scripts can be separate processes running at the same time.
#
# Two layers, both reservations of START times (SEC counts requests begun,
# not finished — the old end-to-start spacing was stricter than the limit for
# slow responses and no guarantee at all under concurrency):
#
# - process-wide: `_last_start` under `_pace_lock`, on the monotonic clock.
#   Every client in the process shares it whatever its cache directory.
# - cross-process: a tiny state file in the cache directory (`_RATE_STATE`),
#   guarded by `fcntl.flock` on a sidecar, holding the last reserved start
#   in WALL-CLOCK time. Monotonic stamps do not compare across reboots and
#   are not promised to share an epoch across processes on every platform,
#   and the file outlives the processes that wrote it; wall time is the only
#   clock every reader of the file agrees on. Its weakness — a clock stepped
#   backwards makes the stored start look far in the future — is bounded by
#   `_SHARED_AHEAD_CAP_S`. Clients share it when they share a cache dir.
#   Both files are opened O_NOFOLLOW: a symlink planted at either name must
#   not have its target overwritten (or created); pacing then falls back to
#   process-wide like any other failure to use the file.
#
# A caller RESERVES its slot under the locks (`start = max(now, last + i)`,
# then `last = start`) and sleeps until it OUTSIDE them, so waiters queue in
# reservation order and no lock is ever held across a sleep.
#
# A 429/503 with Retry-After pushes the SAME schedule (`_hold_off`): SEC
# throttles by IP, so the wait it asks for binds every thread, client and
# process on the machine, not only the call that was refused.
_pace_lock = threading.Lock()
_last_start = 0.0  # time.monotonic() of the last reserved request start
_RATE_STATE = ".sec_rate"
# How far ahead of now a stored reservation may legitimately be: a Retry-After
# hold-off (at most `_RETRY_AFTER_CAP_S`) plus the queue of waiters behind it
# (a 40-thread web pool plus a sweep is ~7 s at the interval). A value further
# out is a wall clock that stepped backwards or a corrupted file; honouring it
# would park every request on the machine behind it, so it is discarded and
# the schedule restarts from now. One field and one cap, rather than a
# separate hold-off field with its own: the price is that a clock stepped back
# by less than the cap can delay the next request by up to that step, once.
_SHARED_QUEUE_S = 15.0
_SHARED_AHEAD_CAP_S = _RETRY_AFTER_CAP_S + _SHARED_QUEUE_S
_shared_pacing_warned: set[str] = set()
# The sidecar descriptor while a reservation holds its flock (always under
# `_pace_lock`), so a forked child can drop its inherited copy; see below.
_held_rate_fd: int | None = None


def _reinit_after_fork() -> None:
    """A child forked while another thread held `_pace_lock` inherits it
    LOCKED with no thread left to release it, and would deadlock on its first
    request. The inherited `_last_start` is kept: CLOCK_MONOTONIC is
    system-wide, so the parent's reservation still means the same instant.

    The same fork also duplicates the sidecar descriptor if that thread held
    the flock at the moment. A flock is released only when EVERY descriptor
    for it is closed, so the child's copy — which no thread in the child will
    ever close — would hold the lock for the child's whole life and block
    every other process's next request. Closed here (never LOCK_UN, which
    would release the parent's lock under it). Descriptors from `os.open`
    are close-on-exec already, so fork+exec children never had the problem."""
    global _pace_lock, _held_rate_fd
    _pace_lock = threading.Lock()
    if _held_rate_fd is not None:
        try:
            os.close(_held_rate_fd)
        except OSError:
            pass
        _held_rate_fd = None


os.register_at_fork(after_in_child=_reinit_after_fork)


def _open_state(path: Path) -> int:
    """Open one of the pacing files read-write, creating it, WITHOUT following
    a symlink at `path` (ELOOP, an OSError, if one is there). Where the
    platform has no O_NOFOLLOW the file is not opened at all — the caller
    falls back to process-wide pacing rather than risk writing through a
    link."""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError(errno.ENOTSUP, "no O_NOFOLLOW: cannot open the pacing state safely",
                      str(path))
    return os.open(path, os.O_RDWR | os.O_CREAT | nofollow, 0o644)


def _update_shared(
    cache_dir: Path, step: Callable[[float | None, float, float], float],
) -> tuple[float, float, float]:
    """Read-modify-write the shared schedule under its flock: `step(last_w,
    now_m, now_w)` returns the new stored start from the stored one (None
    when absent, garbled or not credible) and the clocks. Returns
    `(new_w, now_m, now_w)`. Raises OSError when the state cannot be locked
    or written (read-only cache, no lock support, a symlink at either name).

    The clocks are read INSIDE the flock, after the file I/O: a slot dated
    before a slow open (a loaded disk, a first-use create) would already be
    in the past when the request went out, starting it late and leaving the
    next caller less than an interval behind it.

    The lock is a sidecar, following `watch/watchlist.py` `_write_lock` and
    `reporting/report_files.py` `publish_lock`, and carries the same
    assumption: `fcntl.flock` is advisory and NOT reliable over NFS, so a
    cache directory on a network mount paces only within each process. It is
    held for one read and one write of a few bytes, never across a sleep.
    The state is rewritten in place, through the descriptor of its own
    no-follow open (never a path-based write, which would follow a link
    swapped in after the open): every reader holds the lock, and a write
    torn by a crash reads as garbage, which restarts the schedule (one
    unpaced request) rather than failing a fetch."""
    global _held_rate_fd
    fd = _open_state(cache_dir / f"{_RATE_STATE}.lock")
    _held_rate_fd = fd
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        state = _open_state(cache_dir / _RATE_STATE)
        try:
            try:
                last_w: float | None = float(os.read(state, 64).decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                last_w = None  # first use, or a torn write: restart the schedule
            now_m, now_w = time.monotonic(), time.time()
            if last_w is not None and not (math.isfinite(last_w)
                                           and last_w - now_w <= _SHARED_AHEAD_CAP_S):
                last_w = None
            new_w = step(last_w, now_m, now_w)
            os.ftruncate(state, 0)
            os.pwrite(state, repr(new_w).encode("ascii"), 0)
            return new_w, now_m, now_w
        finally:
            os.close(state)
    finally:
        _held_rate_fd = None
        os.close(fd)  # closing releases the lock


def _reserve_shared(cache_dir: Path, interval: float, local_last_m: float) -> float:
    """Reserve a start against every process sharing `cache_dir`, no earlier
    than this process's own next slot (`local_last_m + interval`); return it
    on the monotonic clock. OSError as `_update_shared`."""

    def step(last_w: float | None, now_m: float, now_w: float) -> float:
        earliest_w = now_w + max(0.0, local_last_m + interval - now_m)
        return earliest_w if last_w is None else max(earliest_w, last_w + interval)

    start_w, now_m, now_w = _update_shared(cache_dir, step)
    return now_m + (start_w - now_w)


def _warn_unshared(cache_dir: Path, e: OSError) -> None:
    """Never fail a fetch over pacing: the caller falls back to this
    process's schedule. Said once per directory, not per request. Called
    under `_pace_lock`, which guards the set."""
    key = str(cache_dir)
    if key not in _shared_pacing_warned:
        _shared_pacing_warned.add(key)
        logger.warning(
            "SEC request pacing is process-wide only for cache %s: "
            "cannot share its schedule with other processes (%s)", cache_dir, e,
        )


def _reserve_slot(cache_dir: Path) -> float:
    """Reserve this request's start and return it on the monotonic clock.

    The flock is taken while `_pace_lock` is held, always in that order, so
    the two cannot deadlock; the flock is only ever held for a tiny file read
    and write, so the thread lock is never held across a sleep either."""
    global _last_start
    interval = _REQUEST_INTERVAL_S
    with _pace_lock:
        try:
            start = _reserve_shared(cache_dir, interval, _last_start)
        except OSError as e:
            _warn_unshared(cache_dir, e)
            # The clock is read after the failed attempt and the log line,
            # for the same reason `_update_shared` reads it last.
            start = max(time.monotonic(), _last_start + interval)
        _last_start = start
    return start


def _hold_off(cache_dir: Path, seconds: float) -> None:
    """Keep EVERY request on the machine out for `seconds` from now, as a
    429/503's Retry-After asks. The schedule is pushed so that its next slot
    (`last + interval`) is no earlier than now + `seconds` — in this process
    and, through the shared state, in every process using `cache_dir`. Never
    pulls a schedule already further out back in. Same locks, same order and
    same fallback as `_reserve_slot`; nothing sleeps here."""
    global _last_start
    interval = _REQUEST_INTERVAL_S
    with _pace_lock:
        _last_start = max(_last_start, time.monotonic() + seconds - interval)

        def step(last_w: float | None, now_m: float, now_w: float) -> float:
            held_w = now_w + seconds - interval
            return held_w if last_w is None else max(last_w, held_w)

        try:
            _update_shared(cache_dir, step)
        except OSError as e:
            _warn_unshared(cache_dir, e)


# A single transport failure used to end a whole unattended sweep pass. Three
# days of the hourly job logged 162 of them across 73 passes, and 154 were one
# error: `[Errno 8] nodename nor servname provided` — the machine waking from
# sleep and resolving DNS before the network was up, failing all eleven names
# at once. Those recover in seconds, so one attempt is the wrong number: it
# turns a transient into a lost hour and an alert the operator learns to
# ignore.
#
# Retried ONLY for transport errors and the server-side statuses that mean
# "ask again" (429, 5xx). A 403 or 404 is an answer, not a failure — document
# fetches legitimately 404 — and retrying those would add seconds of sleep to
# every missing exhibit.
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_S = (2.0, 5.0)
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# 429 and 503 are the statuses that carry SEC's own "come back in N seconds".
# Retrying sooner than asked is exactly what fair access forbids; waiting
# longer than a minute would stall an unattended pass on one bad header.
_RETRY_AFTER_STATUSES = frozenset({429, 503})


def _retry_after_s(err: urllib.error.HTTPError) -> float | None:
    """The wait a 429/503 asks for, in seconds, capped; None if it asks for
    none we can read. RFC 9110 allows delta-seconds (a non-negative integer)
    or an HTTP-date; anything else is ignored rather than guessed at."""
    headers = err.headers
    raw = headers.get("Retry-After") if headers is not None else None
    if raw is None:
        return None
    raw = str(raw).strip()
    if raw.isdecimal():  # not isdigit(): "²" is a digit that float() refuses
        seconds = float(raw)
    else:
        try:
            when = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError):
            return None
        if when.tzinfo is None:  # "-0000": UTC with no claim about the source zone
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(UTC)).total_seconds()
    return min(max(seconds, 0.0), _RETRY_AFTER_CAP_S)


class SecClientError(RuntimeError):
    pass


def assert_submissions_match(payload: dict, cik: int | None) -> None:
    """Refuse a submissions payload that is not the pinned entity's.

    A pin exists precisely because the registry has re-pointed a ticker at a
    successor filer, so letting a supplied payload override it would quietly
    undo the pin: the filing list would describe one entity while accession
    URLs are built for another. Unpinned callers are unaffected.
    """
    if cik is None:
        return
    if not isinstance(payload, dict):
        raise ExternalPayloadError(
            f"submissions payload is {type(payload).__name__}, expected an object"
        )
    payload_cik = payload.get("cik")
    try:
        matches = payload_cik is not None and int(payload_cik) == cik
    except (TypeError, ValueError):
        matches = False
    if not matches:
        raise SubmissionsMismatchError(
            f"submissions payload is for CIK {payload_cik!r}, not the pinned CIK {cik}"
        )


def _identity(explicit: str | None) -> str:
    identity = explicit or os.environ.get("EDGAR_IDENTITY")
    if not identity:
        raise SecClientError(
            "SEC fair-access rules require identifying yourself. Set EDGAR_IDENTITY "
            'to e.g. "Your Name you@example.com" or pass identity=.'
        )
    return identity


def _supersedes(published_ns: int | None, started_ns: int) -> bool:
    """Whether what is already published should be kept instead of our write.

    Both numbers are request-START times (see `_published_generation_ns`), so
    this compares like with like. Equality means the two requests asked SEC at
    the same instant and are equally fresh; keeping the incumbent avoids a
    pointless rewrite and is why this is `>=` rather than `>`.

    A generation in the FUTURE cannot be a real request-start time — no
    request has started yet — so it is not allowed to win. Without that, a
    clock stepping backwards or one corrupted timestamp made an entry
    permanently unpublishable: it lost its freshness (see `_is_fresh`) so
    every read refetched, and then every publish stood down against a
    generation it could never beat. The entry could never be updated again by
    any means, including `--fresh`.
    """
    if published_ns is None:
        return False
    if published_ns > time.time_ns():
        return False
    return published_ns >= started_ns


def _is_fresh(mtime_s: float, max_age_s: float) -> bool:
    """Whether an entry stamped `mtime_s` may still be served.

    The age must be NON-NEGATIVE as well as under the limit. An entry's mtime
    is its request-generation stamp, so a clock stepping backwards — or any
    corrupted future timestamp — otherwise makes the entry immortal: the age
    reads negative, every TTL passes, and `--fresh` fetches new data and then
    declines to publish it because the future generation looks newer. The
    result is a cache that can never be updated again by any means.
    """
    age = time.time() - mtime_s
    return 0 <= age < max_age_s


# A cache entry's shape check. It raises ExternalPayloadError — a ValueError —
# so a parseable entry of the wrong shape takes exactly the path an
# unparseable one does, in every `except ValueError` below.
Validator = Callable[[object], None]


def _object(payload: object, what: str) -> dict:
    if not isinstance(payload, dict):
        raise ExternalPayloadError(f"{what} is {type(payload).__name__}, expected an object")
    return payload


def _companyfacts_shape(payload: object) -> None:
    """What `companyfacts_mapper` walks: `payload["facts"][taxonomy]`. `[]`
    or `{"facts": null}` parse, and used to fail later as an AttributeError
    that looked like a defect in the mapper.

    Agrees with the reader contract in `payloads.concept_rows`: a MISSING
    `facts` is "no rows" — the payload was acquired and is then reported as
    unmappable — so only a present `facts` of the wrong type is refused.
    Refusing the missing case too made a bare payload read as "could not be
    acquired; check the network", which sends an operator the wrong way."""
    obj = _object(payload, "companyfacts payload")
    if "facts" in obj:
        _object(obj["facts"], "companyfacts.facts")


def _submissions_shape(payload: object) -> None:
    """What `edgar_documents._merged_filings` walks: `filings.recent` and
    `filings.files` under an OBJECT `filings`; its columns are checked where
    they are read (`payloads.recent_filings`, `edgar_documents._aligned_arrays`).

    A MISSING `filings` is a new filer with none (`payloads.recent_filings`),
    not an unavailable index, so only a present one of the wrong type (`[]`,
    `null`) is refused."""
    obj = _object(payload, "submissions payload")
    if "filings" in obj:
        _object(obj["filings"], "submissions.filings")


def _submissions_page_shape(payload: object) -> None:
    """An older submissions page (`filings.files[].name`) is ONE flat block
    of filing columns — no `filings` wrapper, and older pages omit columns
    (`items`, `primaryDocument`) — so the object is all that can be required
    here; `_aligned_arrays` checks the columns it reads."""
    _object(payload, "submissions page")


def _tickers_shape(payload: object) -> None:
    """`company_tickers.json` is an object of row objects. An EMPTY table
    parses and is the worst case of all: `resolve_cik` would answer "not in
    the SEC registry" for every ticker for a day, `--fresh` included (the
    table deliberately ignores it)."""
    table = _object(payload, "ticker table")
    if not table:
        raise ExternalPayloadError("ticker table is empty")
    for key, row in table.items():
        _object(row, f"ticker table row {key!r}")


def _is_readable_json(path: Path, validate: Validator | None = None) -> bool:
    """Whether `path` currently holds parseable JSON of the required shape.
    Re-checked before any corrective unlink so recovery never discards a
    replacement."""
    try:
        parsed = json.loads(path.read_text())
        if validate is not None:
            validate(parsed)
    except (OSError, ValueError):
        return False
    return True


@contextmanager
def _publication_lock(path: Path):
    """Serialize check-then-publish for one cache key.

    Comparing generations and then replacing must be ONE critical section.
    Without it two writers both read the same destination, both conclude they
    may publish, and land in completion order — which is the very ordering
    this code exists to stop.

    The lock lives on a sidecar file, never on the entry itself: `os.replace`
    swaps in a new inode, so a lock held on the old one guards nothing. Same
    POSIX assumption `watch/watchlist.py` already documents — `fcntl.flock`
    is not reliable over NFS, and there is no Windows implementation.
    """
    lock_path = path.with_name(f".{path.name}.lock")
    with open(lock_path, "a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _archive_has_text(path: Path) -> bool:
    """Whether an archive entry currently holds a non-blank document; the
    re-check before an empty entry is unlinked."""
    try:
        return bool(path.read_text(errors="replace").strip())
    except OSError:
        return False


def _published_generation_ns(path: Path) -> int | None:
    """The request-start time of whatever currently occupies `path`, or None
    if nothing does.

    Readable because the publisher stamps its TEMP INODE with its own start
    time before renaming, so an entry's mtime is the generation that produced
    it rather than the moment it happened to land. Comparing a rival's
    COMPLETION time against our START time — which is what an unstamped mtime
    gives you — answers a different question and gets the answer wrong
    whenever a request that started earlier finishes later.

    An unreadable entry reports None (treated as nothing published) so a stat
    failure can never permanently stop the cache being written.
    """
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


class SecClient:
    def __init__(
        self,
        cache_dir: str | Path = "data/cache",
        identity: str | None = None,
        fresh: bool = False,
    ):
        """`fresh=True` bypasses JSON caches for this client (P0-D): on a
        filing day a <24h cache can silently serve pre-filing data while the
        report is dated today."""
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.identity = _identity(identity)
        self.fresh = fresh
        # Filing-document traffic, so the report's data-quality line can say
        # exactly what this run served from disk instead of "caches bypassed".
        self.archives_fetched = 0
        self.archives_from_cache = 0

    def _get(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": self.identity})
        last: Exception | None = None
        tried = 0
        for attempt in range(_MAX_ATTEMPTS):
            tried += 1
            # Every attempt, retries included, takes a slot: a refused request
            # still cost SEC a connection, and pacing is a fair-access
            # obligation. Reserved under the lock, slept outside it.
            wait = _reserve_slot(self.cache_dir) - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            backoff = _RETRY_BACKOFF_S[attempt] if attempt + 1 < _MAX_ATTEMPTS else 0.0
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return resp.read()
            except urllib.error.HTTPError as e:
                # The server answered. Only "ask again" statuses are retried.
                last = e
                if e.code not in _RETRY_STATUSES:
                    break
                if e.code in _RETRY_AFTER_STATUSES:
                    # Never sooner than SEC asked, and never sooner than our
                    # own back-off either: a `Retry-After: 0` is not a reason
                    # to hammer a server that just said it is overloaded.
                    # The ask binds the whole machine, not this call: push the
                    # shared schedule so every other thread and process waits
                    # it out too (this call does, by the sleep below).
                    asked = _retry_after_s(e)
                    if asked is not None:
                        _hold_off(self.cache_dir, asked)
                        backoff = max(backoff, asked)
            except Exception as e:  # noqa: BLE001 - every transport failure is retryable
                last = e
            if attempt + 1 < _MAX_ATTEMPTS:
                time.sleep(backoff)
        # Says what actually happened: a 404 stops after one try, and a
        # message claiming three would send the reader hunting a flaky network.
        tries = "" if tried == 1 else f" after {tried} attempts"
        raise SecClientError(f"SEC request failed for {url}{tries}: {last}") from last

    def _cached_json(self, cache_name: str, url: str, max_age_s: float = 86400.0,
                     *, honor_fresh: bool = True, validate: Validator | None = None) -> dict:
        """`validate` is the shape the caller will walk. Parseable-but-wrong
        (`[]` for companyfacts) used to be served from disk for the whole TTL
        and then fail as an AttributeError deep in a parser; now a cached
        entry of the wrong shape is quarantined exactly like an unparseable
        one, and a fetched one is refused and never written."""
        path = self.cache_dir / cache_name
        use_cache = not (self.fresh and honor_fresh)
        if use_cache:
            try:
                # No `exists()` pre-check: the entry can be unlinked by another
                # process's corrupt-entry recovery between the check and the
                # read, and a reader that trusted `exists()` then died with
                # FileNotFoundError while the other process was busy HEALING
                # the cache. A disappearance is a miss, not a failure — the
                # whole read is one attempt, and OSError means "not usable".
                if _is_fresh(path.stat().st_mtime, max_age_s):
                    cached = json.loads(path.read_text())
                    if validate is not None:
                        validate(cached)
                    return cached
            except OSError:
                pass
            except ValueError as e:
                # A poisoned entry (truncated write, partial download, or
                # valid JSON of the wrong shape) must not fail every read for
                # a day: drop it and refetch. Under the publication lock, and
                # only after confirming the entry is STILL unusable — between
                # our failed check and this unlink a concurrent writer may
                # have published a perfectly good one at the same pathname,
                # and deleting that would turn one bad response into a
                # discarded good one.
                with _publication_lock(path):
                    if not _is_readable_json(path, validate):
                        logger.warning("discarding unusable cache entry %s: %s", path, e)
                        path.unlink(missing_ok=True)
        # Generation stamp, taken BEFORE the request goes out. A request that
        # started later asked SEC later, so its answer is at least as recent;
        # that is the only ordering available to us, since SEC responses carry
        # no vintage we can compare.
        started_ns = time.time_ns()
        data = self._get(url)
        try:
            parsed = json.loads(data)
        except ValueError as e:
            # Never cache what could not be parsed — the old order (write,
            # then parse) left a truncated response on disk to be served
            # as-is until it aged out.
            raise SecClientError(f"SEC response for {url} is not valid JSON: {e}") from e
        if validate is not None:
            # Same rule for shape: what the reader cannot walk is refused here,
            # in SEC's name, and never lands on disk to be served tomorrow.
            try:
                validate(parsed)
            except ValueError as e:
                raise SecClientError(f"SEC response for {url} is unusable: {e}") from e
        # Unique per call, not per process: the web UI serves concurrent
        # report views from threads of one process, and they all resolve
        # CIKs through the same company_tickers.json entry.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            # Stamp the TEMP INODE with this request's generation before it
            # becomes the entry, so the published file's mtime records WHEN
            # THE DATA WAS ASKED FOR rather than when the write happened to
            # land. Without this the comparison below reads a rival's
            # completion time against our start time and gets it backwards
            # whenever a request that started earlier finishes later.
            os.utime(tmp, ns=(started_ns, started_ns))

            # `os.replace` is atomic but not ORDERED: it guarantees no reader
            # sees half a file, and nothing about which of two concurrent
            # writers wins. A slow request that started first would land after
            # a fast one that started later and overwrite it, so every
            # subsequent read served the older SEC snapshot for up to the TTL
            # — on filing day, that is a report built from a pre-filing index.
            # The web UI and the watcher construct separate clients, so
            # per-instance request pacing does not serialize this.
            with _publication_lock(path):
                published = _published_generation_ns(path)
                if _supersedes(published, started_ns):
                    logger.debug(
                        "keeping cache entry %s: published by a request that "
                        "started at or after this one", path.name,
                    )
                    Path(tmp).unlink(missing_ok=True)
                    return parsed
                os.replace(tmp, path)  # atomic: old entry or new one, never half
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return parsed

    def archive_text(self, cik: int, accession: str, doc: str, *, honor_fresh: bool = True) -> str:
        """One filed document from the EDGAR archive, cached by accession.

        An accession's documents never change once filed, so the entry has
        no TTL. `fresh` still refetches it (`honor_fresh`): the report's
        data-quality line used to say "caches bypassed" while MD&A, risk
        factors and EX-99 text came from a cache nothing could invalidate,
        and the one way a cached body can be wrong — a write that did not
        finish, a transport that answered with something other than the
        document — is exactly what an operator asking for `--fresh` on a
        filing night wants ruled out. Both outcomes are counted, so
        `archive_summary` reports what actually happened.
        """
        path = self.cache_dir / f"archive_{accession.replace('-', '')}_{doc.replace('/', '_')}"
        if not (self.fresh and honor_fresh):
            try:
                text = path.read_text(errors="replace")
            except OSError:
                pass  # a miss, whether absent or unreadable
            else:
                if text.strip():
                    self.archives_from_cache += 1
                    return text
                # No filed document is empty, and with no TTL an empty entry
                # was served — and counted as "served from cache" — forever.
                # A miss: removed under the entry's lock after re-checking,
                # the same discipline as `_cached_json`'s corrupt-entry
                # recovery, so a document a concurrent fetch published since
                # our read is kept. (Archive publishers do not take that lock
                # — it would add a sidecar file per document — so a publish
                # landing in the instant between re-check and unlink can
                # still be removed; the cost is one refetch later, never a
                # wrong document served.) Removed even though the refetch
                # below would replace it: if SEC is down, the next read must
                # not find the empty entry either.
                with _publication_lock(path):
                    if not _archive_has_text(path):
                        logger.warning("discarding empty archive cache entry %s", path)
                        path.unlink(missing_ok=True)
        url = ARCHIVES_URL.format(cik=cik, accession=accession.replace("-", ""), doc=doc)
        text = self._get(url).decode("utf-8", errors="replace")
        if not text.strip():
            # An empty answer is a transport or server failure, not a filed
            # document; caching it would make the document permanently empty.
            raise SecClientError(f"SEC returned an empty document for {url}")
        # Atomic publication: a reader never sees half a document. No
        # generation ordering is needed here — every writer holds the same
        # immutable bytes.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        self.archives_fetched += 1
        return text

    def archive_summary(self) -> str | None:
        """What this client did for filing documents, for the data-quality
        section; None when it fetched none (no documents requested)."""
        if not (self.archives_fetched or self.archives_from_cache):
            return None
        return (
            f"{self.archives_fetched} fetched from EDGAR, "
            f"{self.archives_from_cache} served from the immutable archive cache"
        )

    def resolve_cik(self, ticker: str) -> int:
        # `fresh` exists so a filing-day answer is never served from a <24h
        # cache. A ticker-to-CIK map is not filing-day data: a company's CIK
        # does not change when it files. Re-downloading this ~1 MB table for
        # every name on every pass of an hourly job is pure waste and one
        # more chance for a transient failure to cost a name.
        table = self._cached_json("company_tickers.json", TICKERS_URL, honor_fresh=False,
                                  validate=_tickers_shape)
        want = ticker.upper()
        for entry in table.values():
            if entry.get("ticker", "").upper() == want:
                return int(entry["cik_str"])
        raise SecClientError(f"Ticker not found in SEC registry: {ticker}")

    def company_facts(self, ticker: str) -> dict:
        """Company Facts for a ticker, keyed in cache by the CIK it resolves to.

        Same reasoning as `submissions`, and it matters more here: these are
        the largest payloads this client caches and the ones the engine
        actually scores. Keying by ticker stored the same URL a second time,
        so the vintage store (which fetches by CIK and hashes the bytes to
        detect restatements) and the report (which fetched by ticker) could
        hold two copies taken at different moments — the snapshot hashed and
        the facts scored need not have been the same data.
        """
        return self.company_facts_by_cik(self.resolve_cik(ticker))

    def company_facts_by_cik(self, cik: int) -> dict:
        """Fetch companyfacts by CIK directly — required for delisted companies,
        which are absent from the current ticker->CIK registry but retain their
        filings on EDGAR."""
        return self._cached_json(
            f"companyfacts_CIK{cik:010d}.json",
            COMPANYFACTS_URL.format(cik=cik),
            validate=_companyfacts_shape,
        )

    def submissions_by_cik(self, cik: int) -> dict:
        return self._cached_json(
            f"submissions_CIK{cik:010d}.json",
            f"https://data.sec.gov/submissions/CIK{cik:010d}.json",
            validate=_submissions_shape,
        )

    def submissions(self, ticker: str) -> dict:
        """Submissions for a ticker, keyed in cache by the CIK it resolves to.

        The cache is keyed by filename and never inspects the URL, so a second
        name for one resource is a second copy of it. Ticker-keyed entries also
        outlive the mapping that produced them: a ticker reassigned to another
        filer keeps serving the old entity's submissions until the entry ages
        out. Resolving first and storing under the CIK gives every consumer of
        a report one entry, one vintage, one fetch.
        """
        return self.submissions_by_cik(self.resolve_cik(ticker))

    def submissions_page(self, name: str) -> dict:
        """Fetch an older submissions page (referenced in filings.files) for
        high-volume filers whose 'recent' block does not reach far enough back."""
        return self._cached_json(name, f"https://data.sec.gov/submissions/{name}",
                                 validate=_submissions_page_shape)
