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
from datetime import date, datetime

from app.services.ingestion.sec_client import SecClient, assert_submissions_match

FINANCIAL_SIC_RANGE = (6000, 6999)


@dataclass(frozen=True)
class EntityEvents:
    ticker: str
    sic: int | None
    sic_description: str | None
    non_reliance_8k_dates: list[date]

    @property
    def is_financial_institution(self) -> bool:
        return self.sic is not None and FINANCIAL_SIC_RANGE[0] <= self.sic <= FINANCIAL_SIC_RANGE[1]

    def non_reliance_within(self, start: date, days: int) -> bool:
        from datetime import timedelta

        end = start + timedelta(days=days)
        return any(start < d <= end for d in self.non_reliance_8k_dates)


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
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    items = recent.get("items", [])
    filed = recent.get("filingDate", [])
    dates: list[date] = []
    for i in range(min(len(forms), len(items), len(filed))):
        if forms[i].startswith("8-K") and "4.02" in (items[i] or ""):
            dates.append(datetime.strptime(filed[i], "%Y-%m-%d").date())
    sic_raw = data.get("sic")
    return EntityEvents(
        ticker=ticker.upper(),
        sic=int(sic_raw) if sic_raw else None,
        sic_description=data.get("sicDescription"),
        non_reliance_8k_dates=sorted(dates),
    )
