"""EDGAR ingestion: thin network wrapper around the pure companyfacts mapper.

v0.2 replaced the earlier edgartools-based adapter with a dependency-free
client (sec_client.py) + offline-testable mapper (companyfacts_mapper.py).
See docs/real_data_validation.md for what the mapper handles and its
validated behavior against real filings.

Requires EDGAR_IDENTITY (SEC fair-access User-Agent), e.g.:
    export EDGAR_IDENTITY="Your Name you@example.com"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.schemas.financials import CompanyDataset
from app.services.ingestion.companyfacts_mapper import IngestionDiagnostics, build_dataset
from app.services.ingestion.sec_client import SecClient, SecClientError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DatasetSnapshot:
    """Mapped fundamentals and the exact Company Facts payload behind them."""

    dataset: CompanyDataset
    diagnostics: IngestionDiagnostics
    company_facts: dict


def fetch_submissions_snapshot(ticker: str, client: SecClient) -> dict | None:
    """Read the filing index once for a whole report, or return None.

    Documents, capital-markets activity and 4.02 events each derive from the
    submissions index. Reading it per stream lets one report describe filings
    from two different moments, which on a filing day is the pre-filing view
    P0-D exists to prevent. Fetching once removes that.

    Returns None rather than raising when the index cannot be acquired, so
    each stream falls back to its own fetch and reports its own outage. But a
    failure the per-stream retries then survive — a 403 fair-access throttle
    is not retried at all (`_RETRY_STATUSES`) — leaves a report that looks
    complete while silently having given up the single-vintage guarantee.
    Callers must therefore pass `index_degraded=True` to `build_report`, which
    states it on the card and in the data-quality appendix together.

    OSError is absorbed alongside SecClientError because this call sits ahead
    of the whole report, outside the per-stream handlers that used to contain
    an index failure. The cache's own file I/O — stat, read, the atomic
    replace — raises OSError bare, so a full or read-only cache volume would
    otherwise abort a run that previously completed (a `--no-docs` run reached
    the index only from inside those handlers). A programming error still
    raises rather than disabling the snapshot in silence.
    """
    try:
        return client.submissions(ticker)
    except (SecClientError, OSError) as e:
        logger.warning("filing index snapshot unavailable for %s: %s", ticker, e)
        return None


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
