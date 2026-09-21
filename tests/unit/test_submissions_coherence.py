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

import pytest

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


class TestReportBuilderThreadsTheIndex:
    """The builder is where the two streams that produced the two-file split
    live, so the threading has to be pinned here and not only at the seams
    either side of it."""

    def _report(self, client, submissions):
        from app.core.pipeline import analyze
        from app.services.reporting.report_builder import build_report
        from tests.fixtures.companies import stretch_dataset

        ds = stretch_dataset()
        report, _ = build_report(
            analyze(ds), ds,
            generated_on="2026-05-15",
            coverage=1.0,
            client=client,
            ticker=TICKER,
            fetched_at="2026-05-15 00:00 UTC",
            company_facts={"facts": {}},
            submissions=submissions,
        )
        return report

    def test_offerings_and_events_read_the_supplied_index(self, tmp_path):
        client, _ = _client(tmp_path)
        submissions = fetch_submissions_snapshot(TICKER, client)

        class _OutageExceptForTheIndex:
            """Every fetch fails, so a stream that reads the supplied payload
            renders and a stream that refetches reports unavailable."""

            cache_dir = None

            def resolve_cik(self, ticker):
                return CIK

            def submissions_by_cik(self, cik):
                raise SecClientError("submissions 503")

            def company_facts(self, ticker):
                raise SecClientError("companyfacts 503")

            def _cached_json(self, *a, **k):
                raise SecClientError("submissions 503")

            def _get(self, *a, **k):
                raise SecClientError("archive 503")

        report = self._report(_OutageExceptForTheIndex(), submissions)
        assert "Capital-markets appendix UNAVAILABLE" not in report
        assert "Event (8-K 4.02) appendix UNAVAILABLE" not in report


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

    def test_a_transient_failure_is_disclosed_not_absorbed(self, tmp_path):
        # The streams' own retries can succeed where the shared read failed,
        # leaving a report that looks complete while silently back on
        # per-stream vintages. That has to be stated, not swallowed.
        from app.services.ingestion.edgar_adapter import SNAPSHOT_UNAVAILABLE
        from app.services.reporting.report_builder import data_quality_section

        client, _ = _client(tmp_path)
        calls = {"n": 0}
        ok = json.dumps(_SUBMISSIONS).encode()

        def once_then_recover(url: str) -> bytes:
            if "company_tickers" in url:
                return json.dumps(_TICKERS).encode()
            calls["n"] += 1
            if calls["n"] == 1:
                raise SecClientError("SEC request failed: 403 fair-access throttle")
            return ok

        client._get = once_then_recover  # noqa: SLF001
        assert fetch_submissions_snapshot(TICKER, client) is None

        section = data_quality_section(
            fetched_at="2026-05-15 00:00 UTC", fresh=True, coverage=1.0,
            warnings=[SNAPSHOT_UNAVAILABLE], doc_diagnostics=[],
        )
        assert "read once for this run" in section

    def test_a_programming_error_is_not_absorbed(self, tmp_path):
        # Absorbing every exception would let a rename of the accessor
        # silently disable the snapshot while the suite stayed green.
        class _MissingAccessor:
            pass

        with pytest.raises(AttributeError):
            fetch_submissions_snapshot(TICKER, _MissingAccessor())


class TestASuppliedIndexCannotDefeatAPin:
    """`cik` pins the entity AND builds archive URLs, so a payload for another
    filer must be refused rather than quietly overriding the pin."""

    def test_events_refuse_a_payload_for_another_entity(self):
        class _Unused:
            pass

        with pytest.raises(ValueError, match="not the pinned CIK"):
            fetch_entity_events(_Unused(), "XOM", cik=34088, submissions=_SUBMISSIONS)

    def test_events_accept_a_payload_for_the_pinned_entity(self):
        class _Unused:
            pass

        events = fetch_entity_events(_Unused(), TICKER, cik=CIK, submissions=_SUBMISSIONS)
        assert events.non_reliance_8k_dates == [date(2026, 5, 1)]

    def test_documents_refuse_a_payload_for_another_entity(self):
        from app.services.ingestion.edgar_documents import fetch_documents

        class _Unused:
            pass

        with pytest.raises(ValueError, match="not the pinned CIK"):
            fetch_documents(_Unused(), "XOM", {}, cik=34088, submissions=_SUBMISSIONS)
