"""Evidence streams fail closed (Hermes audit round 3, findings 2 and 3).

Finding 2: SEC's filing index is one table stored column by column. A
payload whose columns differ in length was zipped to the shortest column,
the rows past it vanished, and a section then said "none found" about
filings it never read. Every reader now refuses a misaligned index.

Finding 3: a stream that raised halfway kept what it had already added — a
Tier-1 alert beside a line saying the same stream was unavailable. Streams
now stage their output and commit it only when the whole stream succeeded.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.services.backtesting.events import fetch_entity_events
from app.services.backtesting.restatement_control import first_402_date
from app.services.ingestion.edgar_documents import _merged_filings, fetch_documents
from app.services.ingestion.offerings import fetch_offerings
from app.services.ingestion.payloads import ExternalPayloadError
from app.services.reporting import report_builder
from app.services.reporting.report_builder import _collect_streams
from app.services.watch.poller import PollerError
from app.services.watch.poller import recent_filings as poller_filings

DAY = date(2026, 9, 21)
CIK = 320193


def _index(**overrides) -> dict:
    """A well-formed index: a 4.02 8-K, an S-3 and a 10-Q, then overrides."""
    recent = {
        "form": ["8-K", "S-3", "10-Q"],
        "items": ["4.02", "", ""],
        "filingDate": ["2026-06-01", "2026-05-01", "2026-04-30"],
        "accessionNumber": ["0000320193-26-000003", "0000320193-26-000002", "0000320193-26-000001"],
        "primaryDocument": ["a.htm", "b.htm", "c.htm"],
        "reportDate": ["2026-05-29", "", "2026-03-28"],
        "acceptanceDateTime": ["2026-06-01T16:01:00.000Z"] * 3,
    }
    recent.update(overrides)
    return {"cik": CIK, "sic": "3571", "filings": {"recent": recent}}


# Hermes's reproduction: two forms, two items (the second is 4.02), ONE date.
MISALIGNED = _index(
    form=["10-Q", "8-K"], items=["", "4.02"], filingDate=["2026-04-30"],
    accessionNumber=["0000320193-26-000001", "0000320193-26-000003"],
    primaryDocument=["c.htm", "a.htm"], reportDate=["2026-03-28", "2026-05-29"],
    acceptanceDateTime=["x", "y"],
)


class _Client:
    def __init__(self, subs: dict) -> None:
        self.subs = subs

    def company_facts(self, ticker):
        return {"facts": {}}

    def company_facts_by_cik(self, cik):
        return {"facts": {}}

    def resolve_cik(self, ticker):
        return CIK

    def submissions(self, ticker):
        return self.subs

    def submissions_by_cik(self, cik):
        return self.subs

    def submissions_page(self, name):
        raise AssertionError("not reached")


class TestEveryReaderRefusesAMisalignedIndex:
    def test_well_formed_index_still_reads(self):
        events = fetch_entity_events(_Client(_index()), "AAPL", submissions=_index())
        assert events.non_reliance_8k_dates == [date(2026, 6, 1)]

    def test_entity_events(self):
        with pytest.raises(ExternalPayloadError, match="unequal lengths"):
            fetch_entity_events(_Client(MISALIGNED), "AAPL", submissions=MISALIGNED)

    def test_offerings(self):
        with pytest.raises(ExternalPayloadError, match="unequal lengths"):
            fetch_offerings(_Client(MISALIGNED), "AAPL", as_of=DAY, submissions=MISALIGNED)

    def test_document_index(self):
        with pytest.raises(ExternalPayloadError, match="unequal lengths"):
            _merged_filings(_Client(MISALIGNED), MISALIGNED, None)

    def test_documents_read_nothing_and_say_so(self):
        result = fetch_documents(_Client(MISALIGNED), "AAPL", {"facts": {}}, submissions=MISALIGNED)
        assert result.documents == []
        (line,) = result.diagnostics
        assert "filing index malformed" in line and "UNAVAILABLE, not clean" in line

    def test_document_index_pads_only_absent_columns(self):
        subs = _index()
        del subs["filings"]["recent"]["items"]
        subs["filings"]["recent"]["primaryDocument"] = []
        arrays = _merged_filings(_Client(subs), subs, None)
        assert arrays["items"] == ["", "", ""] and arrays["primaryDocument"] == ["", "", ""]

    def test_watch_poller(self):
        with pytest.raises(PollerError, match="unequal lengths"):
            poller_filings(MISALIGNED)
        assert [f.form for f in poller_filings(_index())] == ["8-K", "S-3", "10-Q"]

    def test_watch_poller_optional_column_misaligned(self):
        with pytest.raises(PollerError, match="unequal lengths"):
            poller_filings(_index(items=["4.02"]))

    def test_backtest_control(self):
        assert first_402_date(_Client(_index()), CIK) == date(2026, 6, 1)
        with pytest.raises(ExternalPayloadError, match="unequal lengths"):
            first_402_date(_Client(MISALIGNED), CIK)


class TestTheReportNeverReadsAMisalignedIndexAsClean:
    def test_hermes_case(self):
        body, _lines, tier1, errors, _t, _scan, _v = _collect_streams(
            _Client(MISALIGNED), "AAPL", DAY, company_facts={"facts": {}}, submissions=MISALIGNED
        )
        assert errors["events"] is not None and errors["events"].kind == "data"
        assert errors["offerings"] is not None and errors["offerings"].kind == "data"
        assert "unequal lengths" in errors["events"].message
        text = "\n".join(body)
        assert "No offering-related filings found" not in text
        assert "Capital Markets Activity" not in text
        assert not any("4.02" in t for t in tier1)


def _break_after_first_write(monkeypatch, target, name):
    """Make `name` in module `target` raise a data failure — called after
    the stream has already staged some output."""
    def broken(*a, **k):
        raise ExternalPayloadError("injected after the stream's first write")

    monkeypatch.setattr(target, name, broken)


class TestStreamsAreTransactional:
    def test_restatements_leave_no_section_behind(self, monkeypatch):
        # The section is staged, then Tier-1 promotion fails.
        _break_after_first_write(monkeypatch, report_builder, "_restatement_tier1_lines")
        body, _l, tier1, errors, _t, scan, _v = _collect_streams(
            _Client(_index()), "AAPL", DAY, company_facts={"facts": {}}, submissions=_index()
        )
        assert errors["restatements"] is not None
        assert scan is None
        assert not any("Prior-Period Restatements" in b for b in body)
        # The other streams still committed.
        assert any("Capital Markets Activity" in b for b in body)
        assert any("4.02" in t for t in tier1)

    def test_offerings_leave_no_section_or_event_line_behind(self, monkeypatch):
        import app.services.ingestion.offerings as offerings_mod

        class Timeline:
            """Renders fine; fails when the stream reads its outcome, which
            happens after the section was added."""

            @property
            def acquisition_error(self):
                raise ExternalPayloadError("injected after rendering")

        monkeypatch.setattr(offerings_mod, "fetch_offerings", lambda *a, **k: Timeline())
        monkeypatch.setattr(offerings_mod, "render_offerings_section",
                            lambda t: "## Capital Markets Activity (staged)")
        body, lines, _tier1, errors, takedowns, _s, _v = _collect_streams(
            _Client(_index()), "AAPL", DAY, company_facts={"facts": {}}, submissions=_index()
        )
        assert errors["offerings"] is not None and "injected" in errors["offerings"].message
        assert takedowns == [] and lines == []
        assert not any("Capital Markets Activity" in b for b in body)

    def test_vintage_leaves_no_section_behind(self, monkeypatch, tmp_path):
        import app.services.ingestion.vintages as vintages_mod

        _break_after_first_write(monkeypatch, vintages_mod, "silent_revision_tier1_lines")
        real_section = report_builder._silent_revisions_section
        staged = []

        def section(rep):
            staged.append(real_section(rep))
            return staged[-1]

        monkeypatch.setattr(report_builder, "_silent_revisions_section", section)
        # Two snapshots, so there is a window to promote from.
        facts = lambda v: {"cik": CIK, "facts": {"us-gaap": {"Assets": {"units": {"USD": [  # noqa: E731
            {"end": "2026-03-31", "val": v, "filed": "2026-05-01", "form": "10-Q", "accn": "a"}]}}}}}
        vintages_mod.store_snapshot(CIK, facts(100.0), now=datetime(2026, 9, 1, 12, tzinfo=UTC), root=tmp_path)
        vintages_mod.store_snapshot(CIK, facts(200.0), now=datetime(2026, 9, 2, 12, tzinfo=UTC), root=tmp_path)
        body, _l, _t, errors, _tk, _s, diff = _collect_streams(
            _Client(_index()), "AAPL", DAY, company_facts={"facts": {}}, submissions=_index(),
            vintage_root=tmp_path,
        )
        assert staged, "the section was built before the failure"
        assert errors["vintage"] is not None and diff is None
        assert "injected" in errors["vintage"].message
        assert not any("Silent Revisions" in b for b in body)


READS = {
    "events": ("form", "items", "filingDate"),
    "offerings": ("form", "filingDate", "accessionNumber", "primaryDocument"),
    "documents": ("form", "accessionNumber", "primaryDocument", "reportDate", "items", "filingDate"),
}


@given(data=st.data())
def test_dropping_one_element_of_any_read_column_is_refused(data):
    """Whichever column loses whichever element, no reader returns a result
    built from the survivors."""
    reader = data.draw(st.sampled_from(sorted(READS)))
    column = data.draw(st.sampled_from(READS[reader]))
    subs = _index()
    col = subs["filings"]["recent"][column]
    del col[data.draw(st.integers(0, len(col) - 1))]
    client = _Client(subs)
    with pytest.raises(ExternalPayloadError, match="unequal lengths"):
        if reader == "events":
            fetch_entity_events(client, "AAPL", submissions=subs)
        elif reader == "offerings":
            fetch_offerings(client, "AAPL", as_of=DAY, submissions=subs)
        else:
            _merged_filings(client, subs, None)


def test_documents_degrade_when_the_filing_index_cannot_be_fetched():
    """Found by the CLI drills: an index outage used to abort the whole
    report from inside the document fetch."""
    from app.services.ingestion.sec_client import SecClientError

    class Down(_Client):
        def submissions(self, ticker):
            raise SecClientError("SEC request failed for submissions after 3 attempts")

        def submissions_by_cik(self, cik):
            raise SecClientError("SEC request failed for submissions after 3 attempts")

    for kw in ({}, {"cik": CIK}):
        result = fetch_documents(Down(_index()), "AAPL", {"facts": {}}, **kw)
        assert result.documents == []
        (line,) = result.diagnostics
        assert line.startswith("filing index unavailable (SEC request failed")
        assert "UNAVAILABLE, not clean" in line


def test_a_failed_stream_leaves_no_ledger_evidence(monkeypatch):
    """The ledger reads what `_collect_streams` hands back in `evidence`: a
    stream that failed after staging its scan must not reach it either."""
    _break_after_first_write(monkeypatch, report_builder, "_restatement_tier1_lines")
    evidence: dict = {}
    _collect_streams(
        _Client(_index()), "AAPL", DAY, company_facts={"facts": {}}, submissions=_index(),
        evidence=evidence,
    )
    assert "restatements" not in evidence
    assert "offerings" in evidence and "events" in evidence  # the others committed
