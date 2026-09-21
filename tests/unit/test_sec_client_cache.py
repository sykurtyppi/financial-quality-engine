"""The SEC JSON cache must never hold what it cannot parse, and must heal
itself when it does (a truncated download used to be served for a day)."""

from __future__ import annotations

import json

import pytest

from app.services.ingestion import sec_client as sc


def _client(tmp_path, payloads, fresh=False):
    c = sc.SecClient(fresh=fresh, cache_dir=tmp_path, identity="Test Suite test@example.com")
    it = iter(payloads)
    c._get = lambda url: next(it)  # noqa: SLF001 - network seam
    return c


def test_invalid_response_is_an_error_and_never_cached(tmp_path):
    c = _client(tmp_path, [b'{"truncated": '])
    with pytest.raises(sc.SecClientError, match="not valid JSON"):
        c._cached_json("x.json", "https://example/x")
    assert not (tmp_path / "x.json").exists()
    assert not list(tmp_path.glob(".x.json.*.tmp"))  # no temp file left behind either


def test_poisoned_cache_entry_is_discarded_and_refetched(tmp_path):
    (tmp_path / "x.json").write_text('{"half": ')
    c = _client(tmp_path, [b'{"ok": 1}'])
    assert c._cached_json("x.json", "https://example/x") == {"ok": 1}
    assert json.loads((tmp_path / "x.json").read_text()) == {"ok": 1}


def test_good_response_is_cached_atomically_and_served(tmp_path):
    c = _client(tmp_path, [b'{"v": 1}', b'{"v": 2}'])
    assert c._cached_json("x.json", "https://example/x") == {"v": 1}
    assert c._cached_json("x.json", "https://example/x") == {"v": 1}  # cache hit, no refetch
    assert not list(tmp_path.glob(".x.json.*.tmp"))
    fresh = _client(tmp_path, [b'{"v": 2}'], fresh=True)
    assert fresh._cached_json("x.json", "https://example/x") == {"v": 2}


class TestOneUrlIsOneCacheEntry:
    """The cache keys on filename and never consults the URL, so a second name
    for one resource is a second copy of it that expires on its own clock.
    Company Facts had two: one per ticker, one per CIK. The vintage store
    fetches by CIK and hashes the bytes to detect restatements while the
    report fetched by ticker, so the snapshot hashed and the facts scored
    could be two reads taken at different moments.
    """

    CIK = 320193
    TICKER = "AAPL"

    def _client(self, tmp_path):
        c = sc.SecClient(cache_dir=tmp_path, identity="Test Suite test@example.com")
        fetched: list[str] = []

        def fake_get(url: str) -> bytes:
            fetched.append(url)
            if "company_tickers" in url:
                return json.dumps(
                    {"0": {"ticker": self.TICKER, "cik_str": self.CIK}}
                ).encode()
            return json.dumps({"cik": self.CIK, "facts": {}}).encode()

        c._get = fake_get  # noqa: SLF001 - network seam
        return c, fetched

    def test_ticker_accessor_stores_under_the_resolved_cik(self, tmp_path):
        c, _ = self._client(tmp_path)
        c.company_facts(self.TICKER)
        assert (tmp_path / f"companyfacts_CIK{self.CIK:010d}.json").exists()
        assert not (tmp_path / f"companyfacts_{self.TICKER}.json").exists()

    def test_both_accessors_share_one_entry_and_one_fetch(self, tmp_path):
        # The earnings-night pair: the vintage store reads by CIK, the report
        # by ticker. Same URL, so it must be one read of one entry.
        c, fetched = self._client(tmp_path)
        by_cik = c.company_facts_by_cik(self.CIK)
        by_ticker = c.company_facts(self.TICKER)

        assert by_cik == by_ticker
        facts_urls = [u for u in fetched if "companyfacts" in u]
        assert len(facts_urls) == 1, facts_urls
        assert sorted(p.name for p in tmp_path.glob("companyfacts*")) == [
            f"companyfacts_CIK{self.CIK:010d}.json"
        ]

    def test_a_delisted_cik_is_still_reachable_without_the_registry(self, tmp_path):
        # company_facts_by_cik exists for filers absent from the ticker
        # registry; delegating the ticker path to it must not require them to
        # be in the registry.
        c, fetched = self._client(tmp_path)
        c.company_facts_by_cik(99999)
        assert not any("company_tickers" in u for u in fetched)
