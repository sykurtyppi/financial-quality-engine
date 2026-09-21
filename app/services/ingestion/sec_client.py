"""Minimal SEC EDGAR data client (dependency-free).

Fetches company facts from the public XBRL companyfacts API with local
caching. SEC fair-access rules require a descriptive User-Agent: set
EDGAR_IDENTITY (e.g. "Jane Doe jane@example.com") or pass identity explicitly.

Endpoints used:
- https://www.sec.gov/files/company_tickers.json      (ticker -> CIK)
- https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import urllib.error
import urllib.request
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


def _identity(explicit: str | None) -> str:
    identity = explicit or os.environ.get("EDGAR_IDENTITY")
    if not identity:
        raise SecClientError(
            "SEC fair-access rules require identifying yourself. Set EDGAR_IDENTITY "
            'to e.g. "Your Name you@example.com" or pass identity=.'
        )
    return identity


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
                # not fail every read for a day: drop it and refetch.
                logger.warning("discarding unreadable cache entry %s", path)
                path.unlink(missing_ok=True)
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
            os.replace(tmp, path)  # atomic: a reader sees the old entry or the new one, never half
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
        cik = self.resolve_cik(ticker)
        return self._cached_json(
            f"companyfacts_{ticker.upper()}.json",
            COMPANYFACTS_URL.format(cik=cik),
        )

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

    def submissions_page(self, name: str) -> dict:
        """Fetch an older submissions page (referenced in filings.files) for
        high-volume filers whose 'recent' block does not reach far enough back."""
        return self._cached_json(name, f"https://data.sec.gov/submissions/{name}")
