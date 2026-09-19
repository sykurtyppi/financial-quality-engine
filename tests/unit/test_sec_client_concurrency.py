"""Concurrent cache writes to the same entry (the web UI's threads all resolve
CIKs through company_tickers.json) must never collide on a temp file."""

from __future__ import annotations

import threading

from app.services.ingestion import sec_client as sc


def test_concurrent_writes_to_one_entry_never_raise(tmp_path):
    c = sc.SecClient(fresh=True, cache_dir=tmp_path, identity="Test Suite test@example.com")
    c._get = lambda url: b'{"ok": true}'  # noqa: SLF001
    errors = []

    def hit():
        try:
            for _ in range(20):
                assert c._cached_json("company_tickers.json", "https://example/t") == {"ok": True}
        except BaseException as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=hit) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert not list(tmp_path.glob(".company_tickers.json.*.tmp"))
