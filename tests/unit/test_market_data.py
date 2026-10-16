from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.market_data import MarketDataError, YahooMarketDataProvider


def test_yahoo_provider_normalizes_market_snapshot_metadata() -> None:
    observed_at = datetime(2026, 10, 13, 20, 0, tzinfo=UTC)
    retrieved_at = datetime(2026, 10, 14, 12, 30, tzinfo=UTC)
    requested_urls: list[str] = []

    def fetch_json(url: str) -> dict:
        requested_urls.append(url)
        return {
            "chart": {
                "result": [
                    {
                        "meta": {
                            "symbol": "AAPL",
                            "regularMarketPrice": 247.77,
                            "chartPreviousClose": 245.50,
                            "currency": "USD",
                            "exchangeName": "NMS",
                            "marketState": "CLOSED",
                            "regularMarketTime": int(observed_at.timestamp()),
                            "exchangeDataDelayedBy": 0,
                        }
                    }
                ],
                "error": None,
            }
        }

    snapshot = YahooMarketDataProvider(
        fetch_json=fetch_json,
        clock=lambda: retrieved_at,
    ).get_snapshot("aapl")

    assert snapshot.ticker == "AAPL"
    assert snapshot.price == 247.77
    assert snapshot.previous_close == 245.50
    assert snapshot.currency == "USD"
    assert snapshot.exchange == "NMS"
    assert snapshot.market_session == "CLOSED"
    assert snapshot.observed_at == observed_at
    assert snapshot.retrieved_at == retrieved_at
    assert snapshot.provider == "Yahoo Finance"
    assert snapshot.delay_seconds == 0
    assert snapshot.is_delayed is False
    assert requested_urls == [
        "https://query1.finance.yahoo.com/v8/finance/chart/AAPL?range=5d&interval=1d"
    ]


def test_yahoo_provider_rejects_unusable_payload_without_leaking_shape_errors() -> None:
    provider = YahooMarketDataProvider(fetch_json=lambda _url: {"chart": {"result": []}})

    with pytest.raises(MarketDataError, match="payload unusable"):
        provider.get_snapshot("AAPL")
