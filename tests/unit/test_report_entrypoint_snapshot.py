"""Regression tests for Company Facts snapshot reuse at report entry points."""

from types import SimpleNamespace

from app.core.pipeline import analyze as real_analyze
from app.services.journal import reporting as journal_reporting
from scripts import generate_report
from tests.fixtures.companies import stretch_dataset


class _NoRefetchClient:
    """Fail loudly if an entry point bypasses its retained Company Facts payload."""

    def company_facts(self, ticker: str) -> dict:
        raise AssertionError(f"unexpected Company Facts refetch for {ticker}")


def _snapshot(company_facts: dict) -> SimpleNamespace:
    diagnostics = SimpleNamespace(coverage=lambda: 1.0, warnings=[])
    return SimpleNamespace(
        dataset=stretch_dataset(),
        diagnostics=diagnostics,
        company_facts=company_facts,
    )


def _documents() -> SimpleNamespace:
    return SimpleNamespace(documents=[], diagnostics=[])


def test_cli_reuses_snapshot_company_facts_for_documents_and_report(monkeypatch, tmp_path):
    company_facts = {"facts": {"sentinel": object()}}
    snapshot = _snapshot(company_facts)
    client = _NoRefetchClient()
    observed: dict[str, object] = {}

    monkeypatch.setattr(generate_report, "ROOT", tmp_path)
    monkeypatch.setattr(generate_report, "SecClient", lambda fresh=False: client)
    monkeypatch.setattr(generate_report, "fetch_dataset_snapshot", lambda *a, **k: snapshot)
    monkeypatch.setattr(generate_report, "analyze", real_analyze)

    def fake_fetch_documents(actual_client, ticker, facts, *, n_filings):
        observed["document_client"] = actual_client
        observed["document_facts"] = facts
        return _documents()

    def fake_build_report(*args, **kwargs):
        observed["report_facts"] = kwargs["company_facts"]
        return "report", SimpleNamespace(reading=None, regime_flags=[], hottest_cluster=None)

    monkeypatch.setattr(generate_report, "fetch_documents", fake_fetch_documents)
    monkeypatch.setattr(generate_report, "build_report", fake_build_report)
    monkeypatch.setattr(generate_report.sys, "argv", ["generate_report.py", "AAPL"])

    assert generate_report.main() == 0
    assert observed["document_client"] is client
    assert observed["document_facts"] is company_facts
    assert observed["report_facts"] is company_facts


def test_journal_reuses_snapshot_company_facts_for_documents_and_report(monkeypatch, tmp_path):
    company_facts = {"facts": {"sentinel": object()}}
    snapshot = _snapshot(company_facts)
    client = _NoRefetchClient()
    observed: dict[str, object] = {}

    monkeypatch.setattr(journal_reporting, "REPORTS", tmp_path)
    monkeypatch.setattr(journal_reporting, "SecClient", lambda *a, **k: client)
    monkeypatch.setattr(journal_reporting, "fetch_dataset_snapshot", lambda *a, **k: snapshot)
    monkeypatch.setattr(journal_reporting, "analyze", real_analyze)

    def fake_fetch_documents(actual_client, ticker, facts, *, n_filings):
        observed["document_client"] = actual_client
        observed["document_facts"] = facts
        return _documents()

    def fake_build_report(*args, **kwargs):
        observed["report_facts"] = kwargs["company_facts"]
        return "report", SimpleNamespace(reading=None, regime_flags=[], hottest_cluster=None)

    monkeypatch.setattr(journal_reporting, "fetch_documents", fake_fetch_documents)
    monkeypatch.setattr(journal_reporting, "build_full_report", fake_build_report)

    output, _ = journal_reporting.build_report("aapl")

    assert output.read_text() == "report"
    assert observed["document_client"] is client
    assert observed["document_facts"] is company_facts
    assert observed["report_facts"] is company_facts
