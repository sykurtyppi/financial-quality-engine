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

    def fake_build(ticker, with_docs=True):
        p = reporting.report_path(ticker)
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


def test_impact_saves_fields(client):
    client.post("/open", data={"ticker": "KO", "thesis": "steady staple", "conviction": 3, "action": "hold"})
    r = client.post("/impact/KO", data={"impact": "changed_confidence", "conviction_after": "4",
                                        "verdict": "helped", "what_happened": "guided down"})
    assert r.status_code == 303
    e = store.parse_entry(store.find_entry("KO"))
    assert e["impact"] == "changed_confidence" and e["conviction_after"] == "4" and e["verdict"] == "helped"
