"""Transport failures are retried; answers are not.

An unattended sweep pass used to be ended by one DNS blip — 154 of 162 logged
failures over three days were the machine resolving a name before the network
was up after sleep, failing all eleven holdings at once. Those recover in
seconds. A 404, by contrast, is an answer: document fetches legitimately miss,
and sleeping between retries would tax every one of them.
"""

from __future__ import annotations

import urllib.error

import pytest

from app.services.ingestion import sec_client as sc


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch):
    monkeypatch.setattr(sc.time, "sleep", lambda _s: None)


def _client(monkeypatch):
    monkeypatch.setenv("EDGAR_IDENTITY", "Tester tester@example.com")
    return sc.SecClient()


def _urlopen(monkeypatch, outcomes):
    """Each call pops one outcome: an Exception to raise, or bytes to return."""
    calls = []

    class _Resp:
        def __init__(self, body): self._body = body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self._body

    def fake(req, timeout=None):
        calls.append(getattr(req, "full_url", req))
        item = outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return _Resp(item)

    monkeypatch.setattr(sc.urllib.request, "urlopen", fake)
    return calls


def _http(code):
    return urllib.error.HTTPError("http://x", code, "boom", {}, None)


class TestRetries:
    def test_a_dns_blip_recovers_instead_of_ending_the_pass(self, monkeypatch):
        client = _client(monkeypatch)
        calls = _urlopen(monkeypatch, [
            urllib.error.URLError("[Errno 8] nodename nor servname provided"),
            b"payload",
        ])
        assert client._get("http://x") == b"payload"
        assert len(calls) == 2

    def test_gives_up_after_the_cap_and_says_how_many_it_tried(self, monkeypatch):
        client = _client(monkeypatch)
        calls = _urlopen(monkeypatch, [urllib.error.URLError("down")] * sc._MAX_ATTEMPTS)
        with pytest.raises(sc.SecClientError, match=f"after {sc._MAX_ATTEMPTS} attempts"):
            client._get("http://x")
        assert len(calls) == sc._MAX_ATTEMPTS

    @pytest.mark.parametrize("code", sorted(sc._RETRY_STATUSES))
    def test_ask_again_statuses_are_retried(self, monkeypatch, code):
        client = _client(monkeypatch)
        calls = _urlopen(monkeypatch, [_http(code), b"payload"])
        assert client._get("http://x") == b"payload"
        assert len(calls) == 2

    @pytest.mark.parametrize("code", [403, 404])
    def test_an_answer_is_not_retried(self, monkeypatch, code):
        # A missing exhibit is a normal result. Retrying it would add seconds
        # of sleep to every document fetch that legitimately misses.
        client = _client(monkeypatch)
        calls = _urlopen(monkeypatch, [_http(code)])
        with pytest.raises(sc.SecClientError) as e:
            client._get("http://x")
        assert len(calls) == 1
        assert "attempts" not in str(e.value), "claimed retries it never made"

    def test_pacing_is_kept_even_when_a_request_fails(self, monkeypatch):
        # A refused request still cost the SEC a connection; fair access is an
        # obligation, not an optimisation.
        client = _client(monkeypatch)
        _urlopen(monkeypatch, [urllib.error.URLError("down")] * sc._MAX_ATTEMPTS)
        client._last_request = 0.0
        with pytest.raises(sc.SecClientError):
            client._get("http://x")
        assert client._last_request > 0.0
