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
