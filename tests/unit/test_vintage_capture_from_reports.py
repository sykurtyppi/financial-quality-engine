"""The scored companyfacts payload is archived by every report path.

`data/vintages/` had never been created. `vintages.capture` was wired only
into the watch sweep and `scripts/vintage.py`; the two report entry points —
the ones that actually hold the exact document the engine scored — threw it
away. The vintage store is the baseline the silent-revision check diffs
against, and that value compounds with time: a baseline that does not exist
before a filing cannot be reconstructed after it.

Two properties are pinned here. Storing a payload already in hand must
produce the SAME file and digest that `capture()` would have produced by
fetching it — otherwise the two paths would build two archives. And archiving
must never cost a report: a failed write is a line in the data-quality
appendix, not an exception.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.core.pipeline import analyze as real_analyze
from app.services.ingestion import edgar_adapter
from app.services.ingestion.vintages import (
    Capture,
    capture,
    digest_of,
    read_manifest,
    snapshot_name,
    store_snapshot,
)
from app.services.journal import reporting as journal_reporting
from app.services.reporting.report_builder import data_quality_section
from scripts import generate_report
from tests.fixtures.companies import stretch_dataset

CIK = 320193
NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
PAYLOAD = {"entityName": "Apple Inc.", "facts": {"us-gaap": {"Assets": {"units": {"USD": [
    {"end": "2026-03-28", "val": 1.0, "filed": "2026-05-01", "accn": "0000320193-26-000001", "form": "10-Q"},
]}}}}}


class _FakeClient:
    def __init__(self, payload: dict):
        self._payload = payload
        self.fetches = 0

    def resolve_cik(self, ticker: str) -> int:
        return CIK

    def company_facts_by_cik(self, cik: int) -> dict:
        self.fetches += 1
        return self._payload


# --- store_snapshot == capture ------------------------------------------------

def test_storing_a_payload_in_hand_matches_fetching_it(tmp_path):
    """Same content ⇒ same filename, same digest, same manifest entry, whichever
    path archived it. Two archives for one document would defeat the diff."""
    via_fetch = capture(_FakeClient(PAYLOAD), "AAPL", now=NOW, root=tmp_path / "a")
    via_store = store_snapshot(CIK, PAYLOAD, now=NOW, root=tmp_path / "b")

    assert via_fetch.reason == via_store.reason == "captured"
    assert via_fetch.sha256 == via_store.sha256 == digest_of(PAYLOAD)
    assert via_fetch.path.name == via_store.path.name == snapshot_name(NOW.date(), via_store.sha256)
    assert via_fetch.path.read_bytes() == via_store.path.read_bytes()

    man_a = read_manifest(CIK, tmp_path / "a")
    man_b = read_manifest(CIK, tmp_path / "b")
    assert man_a["snapshots"][0]["sha256"] == man_b["snapshots"][0]["sha256"]
    assert man_a["observations"] == man_b["observations"]


def test_store_snapshot_honors_the_daily_gate_and_dedupe(tmp_path):
    first = store_snapshot(CIK, PAYLOAD, now=NOW, root=tmp_path)
    assert first.reason == "captured"
    again = store_snapshot(CIK, PAYLOAD, now=NOW, root=tmp_path)
    assert again.reason == "already checked today" and again.path is None
    forced = store_snapshot(CIK, PAYLOAD, now=NOW, root=tmp_path, force=True)
    assert forced.reason == "unchanged" and forced.path is None


def test_capture_still_does_not_fetch_when_already_checked_today(tmp_path):
    """The split must keep the fetch BEHIND the gate: a name already checked
    today costs no request. Moving the fetch ahead of the gate would turn one
    multi-megabyte download a day into one per report."""
    client = _FakeClient(PAYLOAD)
    capture(client, "AAPL", now=NOW, root=tmp_path)
    assert client.fetches == 1
    capture(client, "AAPL", now=NOW, root=tmp_path)
    assert client.fetches == 1


# --- Capture.describe --------------------------------------------------------

@pytest.mark.parametrize("reason,detail,expect", [
    ("unchanged", "", "unchanged since the last snapshot (sha abcdef012345)"),
    ("already checked today", "", "already checked today; no new snapshot"),
    ("busy", "another capture holds the lock", "NOT captured (busy): another capture holds the lock"),
    ("failed", "OSError: disk full", "NOT captured (failed): OSError: disk full"),
])
def test_describe_says_what_happened(reason, detail, expect):
    cap = Capture(CIK, NOW.date(), None, "abcdef0123456789", reason, detail)
    assert cap.describe() == expect


def test_describe_a_capture_names_the_file(tmp_path):
    cap = store_snapshot(CIK, PAYLOAD, now=NOW, root=tmp_path)
    assert cap.describe().startswith(f"captured {cap.path.name}")


# --- the never-raise wrapper --------------------------------------------------

def test_a_failed_archive_is_a_line_not_an_exception(monkeypatch):
    def boom(cik, facts, **kw):
        raise OSError("disk full")

    monkeypatch.setattr("app.services.ingestion.vintages.store_snapshot", boom)
    note = edgar_adapter.store_vintage_snapshot(_FakeClient(PAYLOAD), "AAPL", PAYLOAD)
    assert note == "NOT captured (failed): OSError: disk full"


def test_disabled_capture_returns_none_and_touches_nothing(monkeypatch):
    called = []
    monkeypatch.setattr("app.services.ingestion.vintages.store_snapshot",
                        lambda *a, **k: called.append(1))
    assert edgar_adapter.store_vintage_snapshot(_FakeClient(PAYLOAD), "AAPL", PAYLOAD, enabled=False) is None
    assert called == []


def test_the_note_reaches_the_data_quality_appendix():
    section = data_quality_section(
        fetched_at="2026-09-22 12:00 UTC", fresh=True, coverage=0.9,
        warnings=[], doc_diagnostics=[], vintage="NOT captured (failed): OSError: disk full",
    )
    assert "- Vintage snapshot: NOT captured (failed): OSError: disk full" in section
    without = data_quality_section(
        fetched_at="2026-09-22 12:00 UTC", fresh=True, coverage=0.9,
        warnings=[], doc_diagnostics=[], vintage=None,
    )
    assert "Vintage snapshot" not in without


# --- both entry points archive the scored payload ----------------------------

def _snapshot(company_facts: dict) -> SimpleNamespace:
    diagnostics = SimpleNamespace(coverage=lambda: 1.0, warnings=[], selected_tags=lambda: {}, field_notes=lambda: [])
    return SimpleNamespace(dataset=stretch_dataset(), diagnostics=diagnostics, company_facts=company_facts)


class _NoRefetchClient:
    def submissions(self, ticker):
        return {"filings": {"sentinel": object()}}

    def resolve_cik(self, ticker):
        return CIK


def test_cli_archives_exactly_the_scored_payload(monkeypatch, tmp_path):
    """The payload handed to the archive must be the one the engine scored —
    `snapshot.company_facts` — not a second fetch that may straddle a filing."""
    facts = {"facts": {"sentinel": object()}}
    observed: dict = {}

    monkeypatch.setattr(generate_report, "ROOT", tmp_path)
    monkeypatch.setattr(generate_report, "SecClient", lambda fresh=False: _NoRefetchClient())
    monkeypatch.setattr(generate_report, "fetch_dataset_snapshot", lambda *a, **k: _snapshot(facts))
    monkeypatch.setattr(generate_report, "analyze", real_analyze)
    monkeypatch.setattr(generate_report, "fetch_documents",
                        lambda *a, **k: SimpleNamespace(documents=[], diagnostics=[]))

    def fake_store(client, ticker, payload, *, enabled=True):
        observed["payload"] = payload
        observed["enabled"] = enabled
        return "captured 2026-09-22-abcdef012345.json.gz (100 KB)"

    def fake_build(*args, **kwargs):
        observed["vintage_note"] = kwargs["vintage_note"]
        return "report", SimpleNamespace(reading=None, regime_flags=[], hottest_cluster=None)

    monkeypatch.setattr(generate_report, "store_vintage_snapshot", fake_store)
    monkeypatch.setattr(generate_report, "build_report", fake_build)
    monkeypatch.setattr(generate_report.sys, "argv", ["generate_report.py", "AAPL", "--no-docs"])

    assert generate_report.main() == 0
    assert observed["payload"] is facts
    assert observed["enabled"] is True
    assert observed["vintage_note"].startswith("captured ")


def test_cli_no_vintage_flag_disables_capture(monkeypatch, tmp_path):
    observed: dict = {}
    monkeypatch.setattr(generate_report, "ROOT", tmp_path)
    monkeypatch.setattr(generate_report, "SecClient", lambda fresh=False: _NoRefetchClient())
    monkeypatch.setattr(generate_report, "fetch_dataset_snapshot", lambda *a, **k: _snapshot({"facts": {}}))
    monkeypatch.setattr(generate_report, "analyze", real_analyze)
    monkeypatch.setattr(generate_report, "fetch_documents",
                        lambda *a, **k: SimpleNamespace(documents=[], diagnostics=[]))
    monkeypatch.setattr(generate_report, "store_vintage_snapshot",
                        lambda client, ticker, payload, *, enabled=True: observed.setdefault("enabled", enabled) and None)
    monkeypatch.setattr(generate_report, "build_report",
                        lambda *a, **k: ("report", SimpleNamespace(reading=None, regime_flags=[], hottest_cluster=None)))
    monkeypatch.setattr(generate_report.sys, "argv", ["generate_report.py", "AAPL", "--no-docs", "--no-vintage"])
    assert generate_report.main() == 0
    assert observed["enabled"] is False


def test_journal_archives_exactly_the_scored_payload(monkeypatch, tmp_path):
    facts = {"facts": {"sentinel": object()}}
    observed: dict = {}

    monkeypatch.setattr(journal_reporting, "REPORTS", tmp_path)
    monkeypatch.setattr(journal_reporting, "SecClient", lambda *a, **k: _NoRefetchClient())
    monkeypatch.setattr(journal_reporting, "fetch_dataset_snapshot", lambda *a, **k: _snapshot(facts))
    monkeypatch.setattr(journal_reporting, "analyze", real_analyze)
    monkeypatch.setattr(journal_reporting, "fetch_documents",
                        lambda *a, **k: SimpleNamespace(documents=[], diagnostics=[]))

    def fake_store(client, ticker, payload, *, enabled=True):
        observed["payload"] = payload
        observed["enabled"] = enabled
        return "unchanged since the last snapshot (sha abcdef012345)"

    def fake_build(*args, **kwargs):
        observed["vintage_note"] = kwargs["vintage_note"]
        return "report", SimpleNamespace(reading=None, regime_flags=[], hottest_cluster=None)

    monkeypatch.setattr(journal_reporting, "store_vintage_snapshot", fake_store)
    monkeypatch.setattr(journal_reporting, "build_full_report", fake_build)

    journal_reporting.build_report("aapl", with_docs=False)
    assert observed["payload"] is facts
    assert observed["enabled"] is True
    assert observed["vintage_note"].startswith("unchanged ")

    journal_reporting.build_report("aapl", with_docs=False, vintage=False)
    assert observed["enabled"] is False


def test_the_real_builder_renders_the_note(monkeypatch):
    """Through `build_report` itself, not a spy: the entry-point tests replace
    the builder, so without this the `vintage_note` → `data_quality_section`
    wiring inside `report_builder` is pinned by nothing — the same gap that
    once let the `field_tags` hand-off be reverted with the suite green."""
    from app.services.reporting.report_builder import build_report

    class _Client:
        def company_facts(self, t): return {"facts": {}}
        def company_facts_by_cik(self, c): return {"facts": {}}
        def resolve_cik(self, t): return CIK
        def submissions(self, t): return {"filings": {"recent": {}}}
        def submissions_by_cik(self, c): return {"filings": {"recent": {}}}

    ds = stretch_dataset()
    md, _ = build_report(
        real_analyze(ds), ds, generated_on="2026-09-22", coverage=1.0,
        client=_Client(), ticker="AAPL", fetched_at="2026-09-22 12:00 UTC",
        company_facts={"facts": {}}, field_tags={},
        vintage_note="captured 2026-09-22-abcdef012345.json.gz (100 KB)",
    )
    assert "- Vintage snapshot: captured 2026-09-22-abcdef012345.json.gz (100 KB)" in md
