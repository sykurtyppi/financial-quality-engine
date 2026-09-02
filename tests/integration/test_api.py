from fastapi.testclient import TestClient

from app.main import app
from tests.fixtures.companies import stretch_dataset

client = TestClient(app)


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_analyze_endpoint():
    payload = stretch_dataset().model_dump(mode="json")
    resp = client.post("/analyze", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["red_flags"]


def test_analyze_does_not_expose_the_retired_composite():
    # The composite was measured non-discriminating (2026Q2) and retired from
    # the human report — the machine-readable API must not keep a second
    # product truth alive. `overall` stays internal (sort key only).
    payload = stretch_dataset().model_dump(mode="json")
    body = client.post("/analyze", json=payload).json()
    assert "overall" not in body


def test_report_endpoint_returns_markdown():
    payload = stretch_dataset().model_dump(mode="json")
    resp = client.post("/report", json=payload)
    assert resp.status_code == 200
    assert "# Earnings Quality & Narrative Drift Report" in resp.text
    assert "## 11. Disclaimer" in resp.text


def test_report_endpoint_leads_with_decision_card():
    # Review finding 1: the API must return the shared decision card, not the
    # bare appendix — and no 0-100 grade on the card (finding 4).
    payload = stretch_dataset().model_dump(mode="json")
    resp = client.post("/report", json=payload)
    assert resp.status_code == 200
    card = resp.text.split("Full report (appendix)")[0]
    assert "# Decision Card" in card
    assert "Distress signals (experimental" in card
    assert "/100" not in card
    assert "Full report (appendix)" in resp.text
    # Round-2 finding: the API omits EDGAR streams, so Tier-1 must say so rather
    # than read checked-and-clean.
    assert "not checked this run" in card


def test_analyze_rejects_single_period():
    ds = stretch_dataset()
    ds.periods = ds.periods[:1]
    resp = client.post("/analyze", json=ds.model_dump(mode="json"))
    assert resp.status_code == 422
