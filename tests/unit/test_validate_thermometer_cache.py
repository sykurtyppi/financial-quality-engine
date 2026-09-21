"""The thermometer validator reconstructs from whatever the cache holds.

A ticker it cannot find is dropped silently and the completeness gate then
withholds the regime-inclusive AUC — so a cache-layout change that stopped
matching would quietly weaken the kill gate rather than fail. Entries are
CIK-keyed; the ticker-keyed name is the older layout and is still read.
"""

from __future__ import annotations

import json

from scripts import validate_thermometer as vt

CIK, TICKER = 320193, "AAPL"


def _cache(tmp_path, monkeypatch, *, registry=True):
    monkeypatch.setattr(vt, "CACHE", tmp_path)
    if registry:
        (tmp_path / "company_tickers.json").write_text(
            json.dumps({"0": {"ticker": TICKER, "cik_str": CIK}})
        )
    return tmp_path


def _names(paths):
    from pathlib import Path

    return [Path(p).name for p in paths]


def test_cik_keyed_entry_is_found(tmp_path, monkeypatch):
    cache = _cache(tmp_path, monkeypatch)
    (cache / f"companyfacts_CIK{CIK:010d}.json").write_text("{}")
    assert _names(vt._cache_files(TICKER, vt._ticker_to_cik())) == [
        f"companyfacts_CIK{CIK:010d}.json"
    ]


def test_legacy_ticker_keyed_entry_still_reconstructs(tmp_path, monkeypatch):
    cache = _cache(tmp_path, monkeypatch)
    (cache / f"companyfacts_{TICKER}.json").write_text("{}")
    assert _names(vt._cache_files(TICKER, vt._ticker_to_cik())) == [
        f"companyfacts_{TICKER}.json"
    ]


def test_the_cik_entry_wins_when_both_exist(tmp_path, monkeypatch):
    cache = _cache(tmp_path, monkeypatch)
    (cache / f"companyfacts_CIK{CIK:010d}.json").write_text("{}")
    (cache / f"companyfacts_{TICKER}.json").write_text("{}")
    # The caller reads files[0], so the current layout must come first.
    assert _names(vt._cache_files(TICKER, vt._ticker_to_cik()))[0] == (
        f"companyfacts_CIK{CIK:010d}.json"
    )


def test_a_missing_registry_falls_back_instead_of_raising(tmp_path, monkeypatch):
    cache = _cache(tmp_path, monkeypatch, registry=False)
    (cache / f"companyfacts_{TICKER}.json").write_text("{}")
    assert vt._ticker_to_cik() == {}
    assert _names(vt._cache_files(TICKER, {})) == [f"companyfacts_{TICKER}.json"]


def test_an_unknown_ticker_yields_nothing(tmp_path, monkeypatch):
    _cache(tmp_path, monkeypatch)
    assert vt._cache_files("ZZZZ", vt._ticker_to_cik()) == []
