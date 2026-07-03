"""EDGAR ingestion: thin network wrapper around the pure companyfacts mapper.

v0.2 replaced the earlier edgartools-based adapter with a dependency-free
client (sec_client.py) + offline-testable mapper (companyfacts_mapper.py).
See docs/real_data_validation.md for what the mapper handles and its
validated behavior against real filings.

Requires EDGAR_IDENTITY (SEC fair-access User-Agent), e.g.:
    export EDGAR_IDENTITY="Your Name you@example.com"
"""

from __future__ import annotations

from app.schemas.financials import CompanyDataset
from app.services.ingestion.companyfacts_mapper import IngestionDiagnostics, build_dataset
from app.services.ingestion.sec_client import SecClient


def fetch_dataset(
    ticker: str,
    n_quarters: int = 8,
    sector: str | None = None,
    cache_dir: str = "data/cache",
    identity: str | None = None,
) -> tuple[CompanyDataset, IngestionDiagnostics]:
    """Fetch quarterly fundamentals for `ticker` from SEC EDGAR and map them
    to the canonical dataset, returning per-field ingestion diagnostics.

    Documents (transcripts, releases) are not fetched; supply them via the
    canonical JSON format if narrative analysis is wanted.
    """
    client = SecClient(cache_dir=cache_dir, identity=identity)
    facts = client.company_facts(ticker)
    return build_dataset(facts, ticker=ticker, n_quarters=n_quarters, sector=sector)
