"""PR 0.4 — filing-document fetches honor `--fresh`, and the data-quality line
reports what was actually served.

`_fetch_archive` kept a permanent, TTL-free cache on the side that
`SecClient(fresh=True)` never saw, while the report said "caches bypassed".
The cache now lives on the client: `fresh` refetches, both outcomes are
counted, and the section prints the counts.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.ingestion import edgar_documents as ed
from app.services.ingestion import sec_client as sc
from app.services.reporting.report_builder import data_quality_section

CIK, ACCN, DOC = 320193, "0000320193-26-000001", "aapl-10q.htm"
URL = sc.ARCHIVES_URL.format(cik=CIK, accession="000032019326000001", doc=DOC)


def _client(tmp_path, *, fresh, bodies):
    """A client whose transport returns the next canned body and records URLs."""
    c = sc.SecClient(fresh=fresh, cache_dir=tmp_path, identity="Test Suite test@example.com")
    c.requested: list[str] = []
    queue = list(bodies)

    def fake_get(url):
        c.requested.append(url)
        if not queue:
            raise AssertionError(f"unexpected request {url}")
        body = queue.pop(0)
        if isinstance(body, Exception):
            raise body
        return body

    c._get = fake_get
    return c


def test_first_read_fetches_then_the_immutable_cache_serves(tmp_path):
    c = _client(tmp_path, fresh=False, bodies=[b"<html>10-Q</html>"])
    assert c.archive_text(CIK, ACCN, DOC) == "<html>10-Q</html>"
    assert c.requested == [URL]
    assert c.archive_text(CIK, ACCN, DOC) == "<html>10-Q</html>"
    assert c.requested == [URL], "second read must not touch the network"
    assert (c.archives_fetched, c.archives_from_cache) == (1, 1)
    assert c.archive_summary() == "1 fetched from EDGAR, 1 served from the immutable archive cache"


def test_fresh_refetches_even_though_the_entry_exists(tmp_path):
    warm = _client(tmp_path, fresh=False, bodies=[b"stale body"])
    warm.archive_text(CIK, ACCN, DOC)
    c = _client(tmp_path, fresh=True, bodies=[b"refetched body"])
    assert c.archive_text(CIK, ACCN, DOC) == "refetched body"
    assert c.requested == [URL]
    assert (c.archives_fetched, c.archives_from_cache) == (1, 0)
    # The refetch replaced the entry for the next non-fresh reader.
    later = _client(tmp_path, fresh=False, bodies=[])
    assert later.archive_text(CIK, ACCN, DOC) == "refetched body"
    assert later.requested == []


def test_honor_fresh_false_serves_the_cache_under_fresh(tmp_path):
    _client(tmp_path, fresh=False, bodies=[b"body"]).archive_text(CIK, ACCN, DOC)
    c = _client(tmp_path, fresh=True, bodies=[])
    assert c.archive_text(CIK, ACCN, DOC, honor_fresh=False) == "body"
    assert c.requested == [] and c.archives_from_cache == 1


def test_a_failed_fetch_leaves_nothing_behind_and_counts_nothing(tmp_path):
    c = _client(tmp_path, fresh=False, bodies=[sc.SecClientError("SEC request failed: 503")])
    with pytest.raises(sc.SecClientError):
        c.archive_text(CIK, ACCN, DOC)
    assert list(tmp_path.iterdir()) == []
    assert (c.archives_fetched, c.archives_from_cache) == (0, 0)
    assert c.archive_summary() is None


def test_publication_is_atomic_and_leaves_no_temp_files(tmp_path):
    c = _client(tmp_path, fresh=False, bodies=[b"body"])
    c.archive_text(CIK, ACCN, DOC)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == [f"archive_000032019326000001_{DOC}"]
    assert not any(n.endswith(".tmp") for n in names)


def test_a_slash_in_the_document_name_stays_inside_the_cache_dir(tmp_path):
    c = _client(tmp_path, fresh=False, bodies=[b"idx"])
    c.archive_text(CIK, ACCN, "sub/index.json")
    assert (tmp_path / "archive_000032019326000001_sub_index.json").read_text() == "idx"


def test_the_document_fetcher_routes_through_the_client(monkeypatch):
    seen = {}

    class _Client:
        def archive_text(self, cik, accession, doc):
            seen.update(cik=cik, accession=accession, doc=doc)
            return "routed"

    assert ed._fetch_archive(_Client(), CIK, ACCN, DOC) == "routed"
    assert seen == {"cik": CIK, "accession": ACCN, "doc": DOC}
    # The old side cache is gone: nothing in the module formats archive URLs.
    assert not hasattr(ed, "ARCHIVES_URL")


def test_the_data_quality_line_is_the_counter_not_the_flag():
    kw = dict(fetched_at="x", coverage=1.0, warnings=[], doc_diagnostics=[])
    fresh = data_quality_section(
        fresh=True, archives="9 fetched from EDGAR, 0 served from the immutable archive cache", **kw
    )
    assert "(EDGAR JSON caches bypassed)" in fresh
    assert "(caches bypassed)" not in fresh, "the blanket claim must not survive"
    assert "- Filing documents: 9 fetched from EDGAR, 0 served from the immutable archive cache" in fresh
    cached = data_quality_section(
        fresh=False, archives="0 fetched from EDGAR, 9 served from the immutable archive cache", **kw
    )
    assert "EDGAR JSON caches up to 24h old" in cached
    assert "- Filing documents: 0 fetched from EDGAR, 9 served" in cached
    assert "Filing documents" not in data_quality_section(fresh=True, **kw)


def test_build_report_prints_what_the_client_counted(tmp_path):
    """End to end: the same client that fetched the documents reports its
    counts through build_report's data-quality section."""
    from app.core.pipeline import analyze
    from app.services.reporting.report_builder import build_report
    from tests.fixtures.companies import stretch_dataset

    class _Client:
        """Complete on purpose (a missing method would read as a stream
        failure). Counts like the real client does."""

        def __init__(self):
            self.archives_fetched, self.archives_from_cache = 3, 5

        def archive_summary(self):
            return sc.SecClient.archive_summary(self)

        def company_facts(self, ticker):
            return {"facts": {}}

        def company_facts_by_cik(self, cik):
            return {"facts": {}}

        def resolve_cik(self, ticker):
            return CIK

        def submissions(self, ticker):
            return {"filings": {"recent": {}}}

        def submissions_by_cik(self, cik):
            return {"filings": {"recent": {}}}

    ds = stretch_dataset()
    report, _ = build_report(
        analyze(ds), ds, generated_on=date(2026, 9, 22).isoformat(), coverage=1.0,
        client=_Client(), ticker="AAPL", fetched_at="2026-09-22 09:00 UTC", fresh=True,
        company_facts={"facts": {}}, field_tags={},
    )
    assert "- Filing documents: 3 fetched from EDGAR, 5 served from the immutable archive cache" in report
    assert "(EDGAR JSON caches bypassed)" in report


def test_a_failure_after_the_temp_file_exists_removes_it(tmp_path, monkeypatch):
    """The post-merge mutation sweep found the failure path between mkstemp
    and os.replace unpinned: dropping the cleanup left a `.tmp` beside the
    cache entry on every failed publication. A disk-full or permission error
    at publication must propagate AND leave the cache directory as it was."""
    import os

    c = _client(tmp_path, fresh=False, bodies=[b"body"])

    def refuse(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError):
        c.archive_text(CIK, ACCN, DOC)
    assert list(tmp_path.iterdir()) == [], "temp file left behind after a failed publication"
    assert (c.archives_fetched, c.archives_from_cache) == (0, 0)


def test_prospectus_fetches_are_counted_in_the_filing_documents_line(tmp_path):
    """Offerings fetched prospectuses with a direct client._get, so the
    report's "Filing documents: N fetched" line left them out."""
    from datetime import date

    from app.services.ingestion.offerings import fetch_offerings
    from app.services.ingestion.sec_client import SecClient

    class _Client(SecClient):
        def __init__(self):
            super().__init__(cache_dir=tmp_path, identity="Test Suite test@example.com")

        def resolve_cik(self, ticker):
            return 320193

        def _get(self, url):
            return b"<html><body>PROSPECTUS SUPPLEMENT 1,000,000 Shares of Common Stock</body></html>"

    subs = {"cik": "320193", "filings": {"recent": {
        "form": ["424B5", "424B5"], "filingDate": ["2026-08-01", "2026-07-01"],
        "accessionNumber": ["0000320193-26-000001", "0000320193-26-000002"],
        "primaryDocument": ["a.htm", "b.htm"],
    }}}
    client = _Client()
    fetch_offerings(client, "AAPL", as_of=date(2026, 9, 21), submissions=subs)
    assert client.archive_summary() == "2 fetched from EDGAR, 0 served from the immutable archive cache"
