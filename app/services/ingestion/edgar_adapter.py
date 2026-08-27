"""EDGAR ingestion: thin network wrapper around the pure companyfacts mapper.

v0.2 replaced the earlier edgartools-based adapter with a dependency-free
client (sec_client.py) + offline-testable mapper (companyfacts_mapper.py).
See docs/real_data_validation.md for what the mapper handles and its
validated behavior against real filings.

Requires EDGAR_IDENTITY (SEC fair-access User-Agent), e.g.:
    export EDGAR_IDENTITY="Your Name you@example.com"
"""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas.financials import CompanyDataset
from app.services.ingestion.companyfacts_mapper import IngestionDiagnostics, build_dataset
from app.services.ingestion.sec_client import SecClient


@dataclass(frozen=True)
class DatasetSnapshot:
    """Mapped fundamentals and the exact Company Facts payload behind them."""

    dataset: CompanyDataset
    diagnostics: IngestionDiagnostics
    company_facts: dict


def fetch_dataset_snapshot(
    ticker: str,
    n_quarters: int = 8,
    sector: str | None = None,
    cache_dir: str = "data/cache",
    identity: str | None = None,
    client: SecClient | None = None,
) -> DatasetSnapshot:
    """Fetch Company Facts once and retain that payload for adjacent analyses.

    Report generation also uses Company Facts for document and restatement
    evidence. Keeping the raw payload avoids multiple live reads that can fail
    independently or observe different SEC snapshots when caches are bypassed.
    """
    client = client or SecClient(cache_dir=cache_dir, identity=identity)
    facts = client.company_facts(ticker)
    dataset, diagnostics = build_dataset(
        facts, ticker=ticker, n_quarters=n_quarters, sector=sector
    )
    return DatasetSnapshot(dataset, diagnostics, facts)


def fetch_dataset(
    ticker: str,
    n_quarters: int = 8,
    sector: str | None = None,
    cache_dir: str = "data/cache",
    identity: str | None = None,
    client: SecClient | None = None,
) -> tuple[CompanyDataset, IngestionDiagnostics]:
    """Fetch quarterly fundamentals for `ticker` from SEC EDGAR and map them
    to the canonical dataset, returning per-field ingestion diagnostics.

    Documents (transcripts, releases) are not fetched; supply them via the
    canonical JSON format if narrative analysis is wanted.
    """
    snapshot = fetch_dataset_snapshot(
        ticker,
        n_quarters=n_quarters,
        sector=sector,
        cache_dir=cache_dir,
        identity=identity,
        client=client,
    )
    return snapshot.dataset, snapshot.diagnostics
