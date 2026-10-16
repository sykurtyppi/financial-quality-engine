from __future__ import annotations

from datetime import UTC, datetime
from math import inf

import pytest

from app.services import market_data
from app.services.market_data import MarketDataError, YahooMarketDataProvider


class _Response:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


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


def test_yahoo_provider_rejects_non_mapping_chart_without_leaking_attribute_error() -> None:
    provider = YahooMarketDataProvider(fetch_json=lambda _url: {"chart": []})

    with pytest.raises(MarketDataError, match="payload unusable"):
        provider.get_snapshot("AAPL")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("regularMarketTime", 10**100),
        ("regularMarketTime", 2**63 - 1),
        ("regularMarketTime", True),
        ("regularMarketPrice", inf),
        ("regularMarketPrice", True),
        ("chartPreviousClose", inf),
        ("exchangeDataDelayedBy", inf),
        ("exchangeDataDelayedBy", True),
    ],
)
def test_yahoo_provider_normalizes_invalid_numeric_metadata(
    field: str,
    value: float | int,
) -> None:
    meta = {
        "symbol": "AAPL",
        "regularMarketPrice": 247.77,
        "chartPreviousClose": 245.50,
        "currency": "USD",
        "regularMarketTime": 1_700_000_000,
    }
    meta[field] = value
    provider = YahooMarketDataProvider(
        fetch_json=lambda _url: {"chart": {"result": [{"meta": meta}], "error": None}}
    )

    with pytest.raises(MarketDataError, match="payload unusable"):
        provider.get_snapshot("AAPL")


@pytest.mark.parametrize(
    "payload",
    [
        b'"\xff"',
        (b"[" * 2_000) + b"0" + (b"]" * 2_000),
    ],
)
def test_default_fetcher_normalizes_malformed_json_bytes(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
) -> None:
    monkeypatch.setattr(
        market_data.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(payload),
    )

    with pytest.raises(MarketDataError):
        market_data._fetch_json("https://example.invalid")


def test_default_fetcher_normalizes_json_parser_recursion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        market_data.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(b"{}"),
    )
    monkeypatch.setattr(
        market_data.json,
        "loads",
        lambda _payload: (_ for _ in ()).throw(RecursionError("too deeply nested")),
    )

    with pytest.raises(MarketDataError, match="request failed"):
        market_data._fetch_json("https://example.invalid")
