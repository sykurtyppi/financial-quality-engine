"""Local journal web UI — offline route tests (report generation stubbed).

Verifies the four screens operate on the shared store: dashboard renders, opening
a case writes an entry, the report route locks the thesis and renders, and the
impact form writes the AFTER/OUTCOME fields back to the same markdown file.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.services.journal import reporting, store
from app.web import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ENTRIES", tmp_path / "entries")
    monkeypatch.setattr(reporting, "REPORTS", tmp_path / "reports")

    def fake_build(ticker, with_docs=True, report_day=None):
        p = reporting.report_path(ticker, report_day)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {ticker} report\n\n| Block | Score |\n|---|---|\n| Earnings Quality | 29 |\n")
        return p, 31.2

    monkeypatch.setattr(reporting, "build_report", fake_build)
    return TestClient(app, follow_redirects=False)


def test_dashboard_empty(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Dashboard" in r.text and "No cases yet" in r.text


def test_open_creates_entry(client):
    r = client.post("/open", data={"ticker": "nvda", "thesis": "beat priced in", "conviction": 3, "action": "hold"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert store.find_entry("NVDA") is not None
    # duplicate open is rejected with an error redirect
    r2 = client.post("/open", data={"ticker": "nvda", "thesis": "again", "conviction": 3, "action": "hold"})
    assert r2.status_code == 303 and "already+exists" in r2.headers["location"]


def test_report_locks_and_renders(client):
    client.post("/open", data={"ticker": "AAPL", "thesis": "clean compounder", "conviction": 4, "action": "hold"})
    r = client.get("/report/AAPL")
    assert r.status_code == 200
    assert "AAPL report" in r.text and "<table>" in r.text          # markdown rendered to HTML
    entry = store.parse_entry(store.find_entry("AAPL"))
    assert entry["is_reported"]                                     # thesis locked on first view


def test_report_requires_thesis(client):
    # open with a placeholder thesis via the store directly, then hit report
    store.open_entry("XYZ")  # placeholder thesis
    r = client.get("/report/XYZ")
    assert r.status_code == 303 and "/open" in r.headers["location"]


def test_report_generation_failure_leaves_entry_unreported(client, monkeypatch):
    def fail_build(ticker, with_docs=True, report_day=None):
        raise RuntimeError("missing EDGAR_IDENTITY")

    monkeypatch.setattr(reporting, "build_report", fail_build)
    client.post("/open", data={"ticker": "CRM", "thesis": "margin reset credible", "conviction": 3, "action": "hold"})

    r = client.get("/report/CRM")

    assert r.status_code == 200
    assert "Report generation failed" in r.text
    assert not store.parse_entry(store.find_entry("CRM"))["is_reported"]


def test_report_for_dated_entry_reads_same_file_it_generates(client):
    client.post("/open", data={"ticker": "KO", "thesis": "steady staple", "conviction": 3, "action": "hold"})
    p = store.find_entry("KO")
    old = p.with_name("KO_2026-01-15.md")
    p.rename(old)

    r = client.get("/report/KO?date=2026-01-15")

    assert r.status_code == 200
    assert "KO report" in r.text
    assert reporting.report_path("KO", "2026-01-15").exists()
    assert store.parse_entry(old)["is_reported"]


def test_impact_saves_fields(client):
    client.post("/open", data={"ticker": "KO", "thesis": "steady staple", "conviction": 3, "action": "hold"})
    r = client.post("/impact/KO", data={"impact": "changed_confidence", "conviction_after": "4",
                                        "verdict": "helped", "what_happened": "guided down"})
    assert r.status_code == 303
    e = store.parse_entry(store.find_entry("KO"))
    assert e["impact"] == "changed_confidence" and e["conviction_after"] == "4" and e["verdict"] == "helped"


def test_impact_form_renders(client):
    # GET /impact renders impact.html — the template not otherwise exercised by tests.
    client.post("/open", data={"ticker": "KO", "thesis": "steady staple", "conviction": 3, "action": "hold"})
    r = client.get("/impact/KO")
    assert r.status_code == 200
    assert "Impact" in r.text and "Verdict" in r.text and "changed_thesis" in r.text


def test_dashboard_with_entries_renders(client):
    # Exercises the queue/table markup that only appears once cases exist.
    client.post("/open", data={"ticker": "KO", "thesis": "steady staple", "conviction": 3, "action": "hold"})
    r = client.get("/")
    assert r.status_code == 200
    assert "KO" in r.text and "report pending" in r.text  # unreported case shows in the queue


def test_missing_entry_redirects(client):
    assert client.get("/impact/NOPE").status_code == 303
    assert client.get("/report/NOPE").status_code == 303


def test_open_rejects_traversal_ticker(client, tmp_path):
    r = client.post("/open", data={"ticker": "../../../pwned", "thesis": "x", "conviction": 3, "action": "hold"})
    assert r.status_code == 303 and "Invalid+ticker" in r.headers["location"]
    # no file created anywhere outside the entries dir
    assert not list(tmp_path.rglob("*pwned*")) and not list(tmp_path.rglob("*PWNED*"))


def test_conviction_after_is_a_validated_select(client):
    client.post("/open", data={"ticker": "KO", "thesis": "steady staple", "conviction": 3, "action": "hold"})
    r = client.get("/impact/KO")
    assert '<select id="conviction_after"' in r.text
    # options 1-5 present, free-text input is gone
    for n in range(1, 6):
        assert f'value="{n}"' in r.text
    assert 'input id="conviction_after"' not in r.text


def test_unreported_report_links_get_loading_class(client):
    client.post("/open", data={"ticker": "KO", "thesis": "steady staple", "conviction": 3, "action": "hold"})
    r = client.get("/")
    assert "js-gen" in r.text and "data-loading-text" in r.text


def test_dashboard_shows_stale_outcome_banner(client):
    client.post("/open", data={"ticker": "KO", "thesis": "steady staple", "conviction": 3, "action": "hold"})
    p = store.find_entry("KO")
    store.mark_reported(p)
    store.set_field(p, "impact", "no_value")
    store.set_field(p, "reported", "2020-01-01T00:00:00Z")

    r = client.get("/")
    assert "awaiting outcome" in r.text.lower()
    assert "outcome overdue" in r.text.lower()
