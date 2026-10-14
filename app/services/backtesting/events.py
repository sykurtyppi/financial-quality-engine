"""Accounting-warning events and entity metadata from the SEC submissions API.

Detects 8-K filings whose item codes include 4.02 ("Non-Reliance on Previously
Issued Financial Statements") — the closest free, dateable proxy for
restatement warnings. Also returns the SIC code, used to auto-flag financial
institutions (SIC 6000-6999).

Coverage note: the submissions "recent" block covers the filer's last ~1000
filings; for heavy filers that reaches back ~5-10 years, which spans the
backtest window. Older history would require paging archived indexes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.services.ingestion.payloads import (
    ExternalPayloadError,
    recent_filings,
    sec_date,
)
from app.services.ingestion.sec_client import SecClient, assert_submissions_match

FINANCIAL_SIC_RANGE = (6000, 6999)


@dataclass(frozen=True)
class EntityEvents:
    ticker: str
    sic: int | None
    sic_description: str | None
    non_reliance_8k_dates: list[date]
    # (filed, accession, form) of each of those 8-Ks, in filing order — the
    # evidence ledger names the filing, not just its date.
    non_reliance_8k_filings: tuple[tuple[date, str, str], ...] = ()

    @property
    def is_financial_institution(self) -> bool:
        return self.sic is not None and FINANCIAL_SIC_RANGE[0] <= self.sic <= FINANCIAL_SIC_RANGE[1]

    def non_reliance_within(self, start: date, days: int) -> bool:
        from datetime import timedelta

        end = start + timedelta(days=days)
        return any(start < d <= end for d in self.non_reliance_8k_dates)


def _sic(raw: object) -> int | None:
    """SEC sends the SIC code as a digit string ("3674"), or empty for none."""
    if not raw:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise ExternalPayloadError(f"submissions sic {raw!r} is not a code")
    try:
        return int(raw)
    except ValueError as e:
        raise ExternalPayloadError(f"submissions sic {raw!r} is not a code") from e


def fetch_entity_events(
    client: SecClient,
    ticker: str,
    cik: int | None = None,
    submissions: dict | None = None,
) -> EntityEvents:
    """`cik` pins the entity when the registry's ticker mapping has moved to a
    successor filer (see UniverseMember.cik); the cache key follows the CIK so
    the pinned entity's submissions never alias the ticker's current ones — and
    now so does the unpinned path, which a report always takes. `submissions`
    supplies a payload the caller already holds, so the events stream reads the
    same filing index as the rest of the report."""
    if submissions is not None:
        assert_submissions_match(submissions, cik)
        data = submissions
    else:
        if cik is None:
            cik = client.resolve_cik(ticker)
        data = client.submissions_by_cik(cik)
    rows = recent_filings(
        data, form=str, items=(str, type(None)), filingDate=str, accessionNumber=str
    )
    dates: list[date] = []
    filings: list[tuple[date, str, str]] = []
    for form, items, filed, accession in rows:
        if form.startswith("8-K") and "4.02" in (items or ""):
            day = sec_date(filed, f"{form} filingDate")
            dates.append(day)
            filings.append((day, accession, form))
    sic_raw = data.get("sic")
    return EntityEvents(
        ticker=ticker.upper(),
        sic=_sic(sic_raw),
        sic_description=data.get("sicDescription"),
        non_reliance_8k_dates=sorted(dates),
        non_reliance_8k_filings=tuple(sorted(filings)),
    )
