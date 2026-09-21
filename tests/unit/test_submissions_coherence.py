"""One report, one filing index.

Documents, capital-markets activity and 4.02 events all read the submissions
index. They used to acquire it separately, and two of them named the cache
entry after the ticker while the third named it after the CIK — two files for
one URL, which `_cached_json` cannot recognize as the same resource because it
keys on the filename and never looks at the URL. A report could therefore read
two filing indexes fetched at different times, and on a filing day one of them
could be the pre-filing view that `fresh=True` exists to prevent.
"""

from __future__ import annotations

import json
from datetime import date

from app.services.backtesting.events import fetch_entity_events
from app.services.ingestion.edgar_adapter import fetch_submissions_snapshot
from app.services.ingestion.offerings import fetch_offerings
from app.services.ingestion.sec_client import SecClient, SecClientError

CIK = 320193
TICKER = "AAPL"

_SUBMISSIONS = {
    "cik": CIK,
    "sic": "3571",
    "sicDescription": "Electronic Computers",
    "filings": {
        "recent": {
            "form": ["8-K", "424B5"],
            "items": ["4.02", ""],
            "filingDate": ["2026-05-01", "2026-04-01"],
            "accessionNumber": ["0000320193-26-000001", "0000320193-26-000002"],
            "primaryDocument": ["a.htm", "b.htm"],
            "reportDate": ["", ""],
        },
        "files": [],
    },
}

_TICKERS = {"0": {"ticker": TICKER, "cik_str": CIK, "title": "Apple Inc."}}


def _client(tmp_path, *, fresh: bool = False) -> tuple[SecClient, list[str]]:
    """A real SecClient with only the transport replaced, so cache naming,
    TTL and `fresh` behave exactly as in production."""
    client = SecClient(cache_dir=tmp_path, identity="test test@example.com", fresh=fresh)
    fetched: list[str] = []

    def fake_get(url: str) -> bytes:
        fetched.append(url)
        if "company_tickers" in url:
            return json.dumps(_TICKERS).encode()
        if "/submissions/" in url:
            return json.dumps(_SUBMISSIONS).encode()
        raise AssertionError(f"unexpected URL {url}")

    client._get = fake_get  # noqa: SLF001 - transport seam
    return client, fetched


def _submissions_gets(fetched: list[str]) -> list[str]:
    return [u for u in fetched if "/submissions/" in u]


def _run_streams(client, submissions):
    """The submissions-reading streams of one report run."""
    fetch_offerings(client, TICKER, as_of=date(2026, 5, 15), parse_takedowns=False,
                    submissions=submissions)
    fetch_entity_events(client, TICKER, submissions=submissions)


class TestOneIndexPerReport:
    def test_cached_run_fetches_the_index_once(self, tmp_path):
        client, fetched = _client(tmp_path)
        submissions = fetch_submissions_snapshot(TICKER, client)
        _run_streams(client, submissions)
        assert len(_submissions_gets(fetched)) == 1

    def test_fresh_run_fetches_the_index_once(self, tmp_path):
        # `fresh=True` is the default on every automated surface, and it
        # bypasses the cache on every call — so before the snapshot each
        # stream was its own live read of the same URL.
        client, fetched = _client(tmp_path, fresh=True)
        submissions = fetch_submissions_snapshot(TICKER, client)
        _run_streams(client, submissions)
        assert len(_submissions_gets(fetched)) == 1

    def test_one_url_is_one_cache_entry(self, tmp_path):
        client, _ = _client(tmp_path)
        fetch_submissions_snapshot(TICKER, client)
        fetch_entity_events(client, TICKER)
        client.submissions_by_cik(CIK)
        entries = sorted(p.name for p in tmp_path.glob("submissions*"))
        assert entries == [f"submissions_CIK{CIK:010d}.json"]

    def test_streams_use_the_supplied_index_and_never_refetch(self, tmp_path):
        client, _ = _client(tmp_path)
        submissions = fetch_submissions_snapshot(TICKER, client)

        class _RefusesToFetch:
            def resolve_cik(self, ticker):
                return CIK

            def submissions_by_cik(self, cik):
                raise AssertionError("stream refetched an index it was given")

        timeline = fetch_offerings(_RefusesToFetch(), TICKER, as_of=date(2026, 5, 15),
                                   parse_takedowns=False, submissions=submissions)
        events = fetch_entity_events(_RefusesToFetch(), TICKER, submissions=submissions)

        assert timeline.acquisition_error is None
        assert events.non_reliance_8k_dates == [date(2026, 5, 1)]


class TestCacheKeyFollowsTheEntity:
    def test_ticker_accessor_stores_under_the_resolved_cik(self, tmp_path):
        client, _ = _client(tmp_path)
        client.submissions(TICKER)
        assert (tmp_path / f"submissions_CIK{CIK:010d}.json").exists()
        assert not (tmp_path / f"submissions_{TICKER}.json").exists()

    def test_ticker_and_cik_accessors_share_one_entry(self, tmp_path):
        client, fetched = _client(tmp_path)
        by_ticker = client.submissions(TICKER)
        by_cik = client.submissions_by_cik(CIK)
        assert by_ticker == by_cik
        assert len(_submissions_gets(fetched)) == 1


def _break_submissions(client) -> None:
    """Take the submissions endpoint down, leaving the ticker registry up."""
    def selective(url: str) -> bytes:
        if "/submissions/" in url:
            raise SecClientError("SEC request failed: submissions outage")
        return json.dumps(_TICKERS).encode()

    client._get = selective  # noqa: SLF001


class TestAcquisitionFailureStaysVisible:
    def test_snapshot_returns_none_instead_of_raising(self, tmp_path):
        client, _ = _client(tmp_path)
        _break_submissions(client)
        assert fetch_submissions_snapshot(TICKER, client) is None

    def test_streams_still_report_their_own_outage(self, tmp_path):
        # A failed shared fetch must not read as "checked and clean": with no
        # payload to pass, each stream falls back to its own fetch and records
        # its own acquisition error, exactly as before the snapshot existed.
        client, _ = _client(tmp_path)
        _break_submissions(client)

        submissions = fetch_submissions_snapshot(TICKER, client)
        assert submissions is None

        timeline = fetch_offerings(client, TICKER, as_of=date(2026, 5, 15),
                                   parse_takedowns=False, submissions=submissions)
        assert timeline.acquisition_error is not None
