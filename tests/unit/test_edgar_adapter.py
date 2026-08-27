"""Regression tests for coherent Company Facts snapshots."""

import json
from pathlib import Path

from app.services.ingestion.edgar_adapter import fetch_dataset, fetch_dataset_snapshot


class _CountingClient:
    def __init__(self, facts: dict):
        self.facts = facts
        self.calls = 0

    def company_facts(self, ticker: str) -> dict:
        self.calls += 1
        return self.facts


def test_snapshot_fetches_company_facts_once_and_preserves_raw_payload():
    fixture = Path(__file__).parents[1] / "fixtures" / "real" / "companyfacts_AAPL_trimmed.json"
    facts = json.loads(fixture.read_text())
    client = _CountingClient(facts)

    snapshot = fetch_dataset_snapshot("AAPL", client=client)

    assert client.calls == 1
    assert snapshot.company_facts is facts
    assert snapshot.dataset.profile.ticker == "AAPL"
    assert snapshot.diagnostics.coverage() > 0


def test_fetch_dataset_preserves_two_tuple_contract_and_single_fetch():
    fixture = Path(__file__).parents[1] / "fixtures" / "real" / "companyfacts_AAPL_trimmed.json"
    facts = json.loads(fixture.read_text())
    client = _CountingClient(facts)

    result = fetch_dataset("AAPL", client=client)

    assert isinstance(result, tuple)
    assert len(result) == 2
    dataset, diagnostics = result
    assert client.calls == 1
    assert dataset.profile.ticker == "AAPL"
    assert diagnostics.coverage() > 0
