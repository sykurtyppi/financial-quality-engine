"""Minimal SEC EDGAR data client (dependency-free).

Fetches company facts from the public XBRL companyfacts API with local
caching. SEC fair-access rules require a descriptive User-Agent: set
EDGAR_IDENTITY (e.g. "Jane Doe jane@example.com") or pass identity explicitly.

Endpoints used:
- https://www.sec.gov/files/company_tickers.json      (ticker -> CIK)
- https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
_REQUEST_INTERVAL_S = 0.15  # stay far under SEC's 10 req/s limit

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
    payload_cik = payload.get("cik")
    try:
        matches = payload_cik is not None and int(payload_cik) == cik
    except (TypeError, ValueError):
        matches = False
    if not matches:
        raise ValueError(
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


def _is_readable_json(path: Path) -> bool:
    """Whether `path` currently holds parseable JSON. Re-checked before any
    corrective unlink so recovery never discards a replacement."""
    try:
        json.loads(path.read_text())
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
        self._last_request = 0.0

    def _get(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": self.identity})
        last: Exception | None = None
        tried = 0
        for attempt in range(_MAX_ATTEMPTS):
            tried += 1
            wait = _REQUEST_INTERVAL_S - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return resp.read()
            except urllib.error.HTTPError as e:
                # The server answered. Only "ask again" statuses are retried.
                last = e
                if e.code not in _RETRY_STATUSES:
                    break
            except Exception as e:  # noqa: BLE001 - every transport failure is retryable
                last = e
            finally:
                # Set even on failure: a refused request still cost the SEC a
                # connection, and the pacing is a fair-access obligation.
                self._last_request = time.monotonic()
            if attempt + 1 < _MAX_ATTEMPTS:
                time.sleep(_RETRY_BACKOFF_S[attempt])
        # Says what actually happened: a 404 stops after one try, and a
        # message claiming three would send the reader hunting a flaky network.
        tries = "" if tried == 1 else f" after {tried} attempts"
        raise SecClientError(f"SEC request failed for {url}{tries}: {last}") from last

    def _cached_json(self, cache_name: str, url: str, max_age_s: float = 86400.0,
                     *, honor_fresh: bool = True) -> dict:
        path = self.cache_dir / cache_name
        use_cache = not (self.fresh and honor_fresh)
        if use_cache and path.exists() and (time.time() - path.stat().st_mtime) < max_age_s:
            try:
                return json.loads(path.read_text())
            except ValueError:
                # A poisoned entry (truncated write, partial download) must
                # not fail every read for a day: drop it and refetch. Under
                # the publication lock, and only after confirming the entry is
                # STILL unreadable — between our failed parse and this unlink
                # a concurrent writer may have published a perfectly good one
                # at the same pathname, and deleting that would turn one bad
                # response into a discarded good one.
                with _publication_lock(path):
                    if not _is_readable_json(path):
                        logger.warning("discarding unreadable cache entry %s", path)
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
                if published is not None and published >= started_ns:
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

    def resolve_cik(self, ticker: str) -> int:
        # `fresh` exists so a filing-day answer is never served from a <24h
        # cache. A ticker-to-CIK map is not filing-day data: a company's CIK
        # does not change when it files. Re-downloading this ~1 MB table for
        # every name on every pass of an hourly job is pure waste and one
        # more chance for a transient failure to cost a name.
        table = self._cached_json("company_tickers.json", TICKERS_URL, honor_fresh=False)
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
        )

    def submissions_by_cik(self, cik: int) -> dict:
        return self._cached_json(
            f"submissions_CIK{cik:010d}.json",
            f"https://data.sec.gov/submissions/CIK{cik:010d}.json",
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
        return self._cached_json(name, f"https://data.sec.gov/submissions/{name}")
