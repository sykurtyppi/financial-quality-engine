"""Provider-neutral, timestamped market snapshots.

Yahoo Finance is the initial internal-MVP adapter. Downstream valuation code
consumes :class:`MarketSnapshot` and is intentionally isolated from Yahoo's
undocumented response shape.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field, ValidationError

_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=5d&interval=1d"
_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"


class MarketDataError(RuntimeError):
    """Raised when a provider cannot return a usable market snapshot."""


class MarketSnapshot(BaseModel):
    ticker: str
    price: float = Field(gt=0)
    previous_close: float | None = Field(default=None, gt=0)
    currency: str
    exchange: str | None = None
    observed_at: datetime
    retrieved_at: datetime
    market_session: str | None = None
    provider: str
    delay_seconds: int | None = Field(default=None, ge=0)
    is_delayed: bool | None = None


class MarketDataProvider(Protocol):
    def get_snapshot(self, ticker: str) -> MarketSnapshot: ...


JsonFetcher = Callable[[str], dict[str, Any]]
Clock = Callable[[], datetime]


def _fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
    except (OSError, json.JSONDecodeError) as exc:
        raise MarketDataError("Market-data request failed") from exc
    if not isinstance(payload, dict):
        raise MarketDataError("Market-data response was not an object")
    return payload


class YahooMarketDataProvider:
    """Normalize Yahoo's chart metadata into the provider-neutral contract."""

    def __init__(
        self,
        *,
        fetch_json: JsonFetcher = _fetch_json,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        self._fetch_json = fetch_json
        self._clock = clock

    def get_snapshot(self, ticker: str) -> MarketSnapshot:
        symbol = ticker.strip().upper()
        if not symbol:
            raise ValueError("ticker must not be blank")
        encoded_symbol = urllib.parse.quote(symbol, safe=".-^")
        payload = self._fetch_json(_CHART_URL.format(ticker=encoded_symbol))
        try:
            chart = payload["chart"]
            if chart.get("error") is not None:
                raise MarketDataError(f"Yahoo Finance returned an error for {symbol}")
            meta = chart["result"][0]["meta"]
            price = float(meta["regularMarketPrice"])
            currency = str(meta["currency"])
            observed_at = datetime.fromtimestamp(
                int(meta["regularMarketTime"]), tz=UTC
            )
        except MarketDataError:
            raise
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise MarketDataError(f"Yahoo Finance payload unusable for {symbol}") from exc

        try:
            delay = meta.get("exchangeDataDelayedBy")
            delay_seconds = int(delay) if delay is not None else None
            previous_close = meta.get("chartPreviousClose", meta.get("previousClose"))
            return MarketSnapshot(
                ticker=str(meta.get("symbol") or symbol).upper(),
                price=price,
                previous_close=float(previous_close) if previous_close is not None else None,
                currency=currency,
                exchange=meta.get("exchangeName") or meta.get("fullExchangeName"),
                observed_at=observed_at,
                retrieved_at=self._clock(),
                market_session=meta.get("marketState"),
                provider="Yahoo Finance",
                delay_seconds=delay_seconds,
                is_delayed=(delay_seconds > 0) if delay_seconds is not None else None,
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise MarketDataError(f"Yahoo Finance payload unusable for {symbol}") from exc
