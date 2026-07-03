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
    assert body["overall"]["score"] is not None
    assert body["red_flags"]


def test_report_endpoint_returns_markdown():
    payload = stretch_dataset().model_dump(mode="json")
    resp = client.post("/report", json=payload)
    assert resp.status_code == 200
    assert "# Earnings Quality & Narrative Drift Report" in resp.text
    assert "## 11. Disclaimer" in resp.text


def test_analyze_rejects_single_period():
    ds = stretch_dataset()
    ds.periods = ds.periods[:1]
    resp = client.post("/analyze", json=ds.model_dump(mode="json"))
    assert resp.status_code == 422
