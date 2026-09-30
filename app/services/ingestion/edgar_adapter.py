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
from datetime import date
from pathlib import Path

from app.schemas.financials import CompanyDataset
from app.services.ingestion.companyfacts_mapper import (
    IngestionDiagnostics,
    build_dataset,
)
from app.services.ingestion.sec_client import SecClient, SecClientError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DatasetSnapshot:
    """Mapped fundamentals and the exact Company Facts payload behind them."""

    dataset: CompanyDataset
    diagnostics: IngestionDiagnostics
    company_facts: dict


class ScoredSnapshotUnmappable(RuntimeError):
    """The mapper tripped (TypeError / AttributeError / KeyError) on a stored
    snapshot a report scored, during a historical replay. The mapper built
    that payload when the report scored it, so this is a defect, not a data
    gap, and it is not passed over — but a bare traceback named neither the
    snapshot nor the way out (review of 626ca1b, finding 2). Not a
    ValueError, so no caller reads it as an unmappable payload."""

    def __init__(self, path: Path, captured: str, kind: str, cause: BaseException):
        self.path, self.captured, self.kind, self.cause = path, captured, kind, cause
        super().__init__(
            f"mapper defect on a snapshot a report scored: {path} (captured {captured}, kind "
            f"{kind}): {type(cause).__name__}: {cause}; move it aside to replay from an "
            "older state"
        )


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


def replay_snapshot(
    client: SecClient,
    ticker: str,
    as_of: date,
    *,
    n_quarters: int = 8,
    sector: str | None = None,
    root: Path | None = None,
) -> tuple[DatasetSnapshot, str]:
    """The fundamentals a reader on `as_of` could have had, and a line saying
    where they came from.

    Preferred: a companyfacts snapshot the vintage store captured on or
    before that day (which one: below) — what was actually knowable then, including values the
    filer has since revised in place. Otherwise today's payload, cut to facts
    filed on or before the day: that undoes later filings but not a value
    revised without a new filing date, and the line says so. Either way the
    mapper applies the same cut (`build_dataset(as_of=)`).

    Which stored snapshot (review of 224b896, finding 1): the newest one a
    report scored (or one of unrecorded kind — stored before kinds were
    recorded, or under a manifest rebuilt from disk — which the line says
    nothing marks as scored), falling back to
    the newest raw capture only when none of those maps. Taking the newest
    observation of any kind scored a watch-sweep capture — possibly partial,
    mapped with fields missing — and a bare one failed the whole replay as
    unmappable though an earlier scored state was stored. A snapshot that
    cannot be read or mapped is passed over; the line says which kind was
    used and what newer was not, and the log says why each was passed over.
    A snapshot a report scored was built by the mapper already, so only an
    unreadable or unmappable one is passed over: a TypeError / AttributeError
    / KeyError there is a mapper defect and raises (review of c131583,
    finding 3), as `ScoredSnapshotUnmappable`, naming the snapshot.
    """
    from app.services.ingestion.vintages import (
        RAW,
        SCORED,
        UNREADABLE,
        UNUSABLE,
        load_vintage,
        observed_vintages,
    )

    cik = client.resolve_cik(ticker)
    visible = [s for s in observed_vintages(cik, root) if date.fromisoformat(s.captured) <= as_of]
    # Indices, not states: one content observed twice on one day is two
    # equal observations, and "newer" is by position.
    newest_first = range(len(visible) - 1, -1, -1)
    failed: set[Path] = set()
    for i in [i for i in newest_first if visible[i].kind != RAW] + [
        i for i in newest_first if visible[i].kind == RAW
    ]:
        stored = visible[i]
        if stored.path in failed:
            continue
        skippable = UNREADABLE if stored.kind == SCORED else UNUSABLE
        try:
            facts = load_vintage(stored.path)
            dataset, diagnostics = build_dataset(
                facts, ticker=ticker, n_quarters=n_quarters, sector=sector, as_of=as_of
            )
        except skippable as e:
            logger.warning("replay %s as of %s: vintage snapshot %s (captured %s, kind %s) "
                           "passed over: %s: %s", ticker, as_of, stored.path.name,
                           stored.captured, stored.kind or "unrecorded", type(e).__name__, e)
            failed.add(stored.path)
            continue
        except UNUSABLE as e:
            # Only a SCORED state reaches here (every other kind skips
            # UNUSABLE): a mapper defect on a payload a report scored.
            raise ScoredSnapshotUnmappable(stored.path, stored.captured, SCORED, e) from e
        kind = (
            "scored by a report" if stored.kind == SCORED
            else "a raw watch-sweep capture: no snapshot a report scored by then maps"
            if stored.kind == RAW
            else "of unrecorded kind (manifest rebuilt or written before kinds): nothing "
            "says a report scored it"
        )
        source = (
            f"the vintage snapshot captured {stored.captured} "
            f"(sha {stored.sha256[:12]}; {kind}), cut to facts filed on or before {as_of}"
        )
        newer = visible[i + 1:]
        if newer:
            bad = sum(s.path in failed for s in newer)
            raw = sum(s.kind == RAW and s.path not in failed for s in newer)
            parts = [f"{raw} raw capture(s)" if raw else "", f"{bad} not mappable" if bad else ""]
            source += (f"; {len(newer)} newer stored snapshot(s) by then not used "
                       f"({', '.join(p for p in parts if p)})")
        return DatasetSnapshot(dataset, diagnostics, facts), source
    facts = client.company_facts(ticker)
    stored_note = (
        f"no snapshot stored by then can be mapped ({len(visible)} stored)" if visible
        else "no snapshot that old is stored"
    )
    source = (
        f"today's companyfacts cut to facts filed on or before {as_of} — {stored_note}, "
        "so a value the filer revised in place since then shows as revised"
    )
    dataset, diagnostics = build_dataset(
        facts, ticker=ticker, n_quarters=n_quarters, sector=sector, as_of=as_of
    )
    return DatasetSnapshot(dataset, diagnostics, facts), source


def store_vintage_snapshot(client, ticker: str, facts: dict, *, enabled: bool = True) -> str | None:
    """Archive the companyfacts payload a report just scored; return the
    data-quality line, or None when capture is switched off.

    Never raises. The vintage store is the baseline the silent-revision check
    diffs against, and its value compounds with time — but a report must not
    fail because the archive could not be written. Like
    `fetch_submissions_snapshot`, a failure here is something the report SAYS,
    not something that stops it.
    """
    if not enabled:
        return None
    from app.services.ingestion.vintages import store_snapshot

    try:
        cik = client.resolve_cik(ticker)
        return store_snapshot(cik, facts).describe()
    except Exception as e:  # noqa: BLE001 - the archive must never break the report
        logger.warning("vintage snapshot for %s not stored: %s: %s", ticker, type(e).__name__, e)
        return f"NOT captured (failed): {type(e).__name__}: {e}"
