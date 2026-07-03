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
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
_REQUEST_INTERVAL_S = 0.15  # stay far under SEC's 10 req/s limit


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
    def __init__(self, cache_dir: str | Path = "data/cache", identity: str | None = None):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.identity = _identity(identity)
        self._last_request = 0.0

    def _get(self, url: str) -> bytes:
        wait = _REQUEST_INTERVAL_S - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={"User-Agent": self.identity})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self._last_request = time.monotonic()
                return resp.read()
        except Exception as e:  # noqa: BLE001 - surface every network failure loudly
            raise SecClientError(f"SEC request failed for {url}: {e}") from e

    def _cached_json(self, cache_name: str, url: str, max_age_s: float = 86400.0) -> dict:
        path = self.cache_dir / cache_name
        if path.exists() and (time.time() - path.stat().st_mtime) < max_age_s:
            return json.loads(path.read_text())
        data = self._get(url)
        path.write_bytes(data)
        return json.loads(data)

    def resolve_cik(self, ticker: str) -> int:
        table = self._cached_json("company_tickers.json", TICKERS_URL)
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
