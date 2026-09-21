"""Regression tests for SEC snapshot reuse at report entry points.

Both SEC inputs a report reads more than once — Company Facts and the
submissions filing index — are fetched once per run and threaded to every
consumer. These tests drive the real entry-point bodies and fail if either
payload stops being passed through.
"""

from types import SimpleNamespace

from app.core.pipeline import analyze as real_analyze
from app.services.ingestion.edgar_adapter import SNAPSHOT_UNAVAILABLE
from app.services.ingestion.sec_client import SecClientError
from app.services.journal import reporting as journal_reporting
from scripts import generate_report
from tests.fixtures.companies import stretch_dataset


class _NoRefetchClient:
    """Fail loudly if an entry point bypasses a payload it already holds."""

    def __init__(self, submissions: dict):
        self._submissions = submissions
        self.submissions_calls = 0

    def company_facts(self, ticker: str) -> dict:
        raise AssertionError(f"unexpected Company Facts refetch for {ticker}")

    def submissions(self, ticker: str) -> dict:
        self.submissions_calls += 1
        return self._submissions

    def submissions_by_cik(self, cik: int) -> dict:
        raise AssertionError(f"unexpected submissions refetch for CIK {cik}")

    def _cached_json(self, name: str, url: str) -> dict:
        raise AssertionError(f"unexpected uncoordinated fetch of {name}")


def _snapshot(company_facts: dict) -> SimpleNamespace:
    diagnostics = SimpleNamespace(coverage=lambda: 1.0, warnings=[])
    return SimpleNamespace(
        dataset=stretch_dataset(),
        diagnostics=diagnostics,
        company_facts=company_facts,
    )


def _documents() -> SimpleNamespace:
    return SimpleNamespace(documents=[], diagnostics=[])


def _payloads() -> tuple[dict, dict]:
    return {"facts": {"sentinel": object()}}, {"filings": {"sentinel": object()}}


class _IndexOutageClient(_NoRefetchClient):
    """The shared index read fails; the streams are left to fend for
    themselves, which the report has to say out loud."""

    def submissions(self, ticker: str) -> dict:
        self.submissions_calls += 1
        raise SecClientError("SEC request failed: 403 fair-access throttle")


def test_cli_reuses_snapshots_for_documents_and_report(monkeypatch, tmp_path):
    company_facts, submissions = _payloads()
    snapshot = _snapshot(company_facts)
    client = _NoRefetchClient(submissions)
    observed: dict[str, object] = {}

    monkeypatch.setattr(generate_report, "ROOT", tmp_path)
    monkeypatch.setattr(generate_report, "SecClient", lambda fresh=False: client)
    monkeypatch.setattr(generate_report, "fetch_dataset_snapshot", lambda *a, **k: snapshot)
    monkeypatch.setattr(generate_report, "analyze", real_analyze)

    def fake_fetch_documents(actual_client, ticker, facts, *, n_filings, submissions=None):
        observed["document_client"] = actual_client
        observed["document_facts"] = facts
        observed["document_submissions"] = submissions
        return _documents()

    def fake_build_report(*args, **kwargs):
        observed["report_facts"] = kwargs["company_facts"]
        observed["report_submissions"] = kwargs["submissions"]
        return "report", SimpleNamespace(reading=None, regime_flags=[], hottest_cluster=None)

    monkeypatch.setattr(generate_report, "fetch_documents", fake_fetch_documents)
    monkeypatch.setattr(generate_report, "build_report", fake_build_report)
    monkeypatch.setattr(generate_report.sys, "argv", ["generate_report.py", "AAPL"])

    assert generate_report.main() == 0
    assert observed["document_client"] is client
    assert observed["document_facts"] is company_facts
    assert observed["report_facts"] is company_facts
    assert observed["document_submissions"] is submissions
    assert observed["report_submissions"] is submissions
    assert client.submissions_calls == 1


def test_cli_discloses_an_index_it_could_not_read_once(monkeypatch, tmp_path):
    company_facts, _ = _payloads()
    snapshot = _snapshot(company_facts)
    client = _IndexOutageClient({})
    observed: dict[str, object] = {}

    monkeypatch.setattr(generate_report, "ROOT", tmp_path)
    monkeypatch.setattr(generate_report, "SecClient", lambda fresh=False: client)
    monkeypatch.setattr(generate_report, "fetch_dataset_snapshot", lambda *a, **k: snapshot)
    monkeypatch.setattr(generate_report, "analyze", real_analyze)
    monkeypatch.setattr(generate_report, "fetch_documents",
                        lambda *a, **k: _documents())

    def fake_build_report(*args, **kwargs):
        observed["warnings"] = kwargs["warnings"]
        observed["report_submissions"] = kwargs["submissions"]
        return "report", SimpleNamespace(reading=None, regime_flags=[], hottest_cluster=None)

    monkeypatch.setattr(generate_report, "build_report", fake_build_report)
    monkeypatch.setattr(generate_report.sys, "argv", ["generate_report.py", "AAPL"])

    assert generate_report.main() == 0
    # The run still completes — each stream falls back — but the report must
    # not imply the single-vintage guarantee was in force.
    assert observed["report_submissions"] is None
    assert SNAPSHOT_UNAVAILABLE in observed["warnings"]


def test_journal_discloses_an_index_it_could_not_read_once(monkeypatch, tmp_path):
    company_facts, _ = _payloads()
    snapshot = _snapshot(company_facts)
    client = _IndexOutageClient({})
    observed: dict[str, object] = {}

    monkeypatch.setattr(journal_reporting, "REPORTS", tmp_path)
    monkeypatch.setattr(journal_reporting, "SecClient", lambda *a, **k: client)
    monkeypatch.setattr(journal_reporting, "fetch_dataset_snapshot", lambda *a, **k: snapshot)
    monkeypatch.setattr(journal_reporting, "analyze", real_analyze)
    monkeypatch.setattr(journal_reporting, "fetch_documents", lambda *a, **k: _documents())

    def fake_build_report(*args, **kwargs):
        observed["warnings"] = kwargs["warnings"]
        observed["report_submissions"] = kwargs["submissions"]
        return "report", SimpleNamespace(reading=None, regime_flags=[], hottest_cluster=None)

    monkeypatch.setattr(journal_reporting, "build_full_report", fake_build_report)

    journal_reporting.build_report("aapl")

    assert observed["report_submissions"] is None
    assert SNAPSHOT_UNAVAILABLE in observed["warnings"]


def test_journal_reuses_snapshots_for_documents_and_report(monkeypatch, tmp_path):
    company_facts, submissions = _payloads()
    snapshot = _snapshot(company_facts)
    client = _NoRefetchClient(submissions)
    observed: dict[str, object] = {}

    monkeypatch.setattr(journal_reporting, "REPORTS", tmp_path)
    monkeypatch.setattr(journal_reporting, "SecClient", lambda *a, **k: client)
    monkeypatch.setattr(journal_reporting, "fetch_dataset_snapshot", lambda *a, **k: snapshot)
    monkeypatch.setattr(journal_reporting, "analyze", real_analyze)

    def fake_fetch_documents(actual_client, ticker, facts, *, n_filings, submissions=None):
        observed["document_client"] = actual_client
        observed["document_facts"] = facts
        observed["document_submissions"] = submissions
        return _documents()

    def fake_build_report(*args, **kwargs):
        observed["report_facts"] = kwargs["company_facts"]
        observed["report_submissions"] = kwargs["submissions"]
        return "report", SimpleNamespace(reading=None, regime_flags=[], hottest_cluster=None)

    monkeypatch.setattr(journal_reporting, "fetch_documents", fake_fetch_documents)
    monkeypatch.setattr(journal_reporting, "build_full_report", fake_build_report)

    output, _ = journal_reporting.build_report("aapl")

    assert output.read_text() == "report"
    assert observed["document_client"] is client
    assert observed["document_facts"] is company_facts
    assert observed["report_facts"] is company_facts
    assert observed["document_submissions"] is submissions
    assert observed["report_submissions"] is submissions
    assert client.submissions_calls == 1
