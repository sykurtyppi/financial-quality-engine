"""Daily adjusted-close prices via Yahoo Finance chart API, with caching, and
benchmark-relative forward returns.

Adjusted closes include dividend adjustment, so relative returns approximate
relative total returns. No API key; identify with a browser-like User-Agent.
"""

from __future__ import annotations

import bisect
import json
import logging
import time
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

CHART_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    "?period1={p1}&period2={p2}&interval=1d"
)
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
_INTERVAL_S = 0.35

TRADING_DAYS = {"3m": 63, "6m": 126, "12m": 252}


class PriceSeries:
    def __init__(self, dates: list[date], closes: list[float]):
        self.dates = dates
        self.closes = closes

    def price_on_or_after(self, d: date) -> tuple[date, float] | None:
        i = bisect.bisect_left(self.dates, d)
        if i >= len(self.dates):
            return None
        return self.dates[i], self.closes[i]

    def forward_return(self, start: date, horizon_days: int) -> float | None:
        """Return over `horizon_days` trading days from the first trading day
        on/after `start`. None if the horizon extends beyond available data."""
        i = bisect.bisect_left(self.dates, start)
        j = i + horizon_days
        if i >= len(self.dates) or j >= len(self.dates):
            return None
        p0, p1 = self.closes[i], self.closes[j]
        if p0 <= 0:
            return None
        return p1 / p0 - 1.0


class PriceClient:
    def __init__(self, cache_dir: str | Path = "data/cache/prices"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._last = 0.0

    def fetch(self, ticker: str, start: date, end: date) -> PriceSeries | None:
        cache = self.cache_dir / f"{ticker}_{start}_{end}.json"
        if cache.exists():
            raw = json.loads(cache.read_text())
        else:
            wait = _INTERVAL_S - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            url = CHART_URL.format(
                ticker=ticker,
                p1=int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp()),
                p2=int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp()),
            )
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = json.loads(resp.read())
                self._last = time.monotonic()
            except Exception as e:  # noqa: BLE001 - one bad ticker must not kill the run
                logger.warning("price fetch failed for %s: %s", ticker, e)
                return None
            cache.write_text(json.dumps(raw))
        try:
            result = raw["chart"]["result"][0]
            ts = result["timestamp"]
            ind = result["indicators"]
            closes = (
                ind["adjclose"][0]["adjclose"]
                if "adjclose" in ind
                else ind["quote"][0]["close"]
            )
        except (KeyError, IndexError, TypeError):
            logger.warning("price payload unusable for %s", ticker)
            return None
        dates, vals = [], []
        for t, c in zip(ts, closes):
            if c is None:
                continue
            dates.append(datetime.fromtimestamp(t, tz=timezone.utc).date())
            vals.append(float(c))
        return PriceSeries(dates, vals) if dates else None


def relative_forward_returns(
    stock: PriceSeries, benchmark: PriceSeries, as_of: date
) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for label, days in TRADING_DAYS.items():
        s = stock.forward_return(as_of, days)
        b = benchmark.forward_return(as_of, days)
        out[f"rel_{label}"] = (s - b) if (s is not None and b is not None) else None
    return out
