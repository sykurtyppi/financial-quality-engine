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

from app.services.ingestion.sec_client import SecClient

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
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


def fetch_entity_events(client: SecClient, ticker: str) -> EntityEvents:
    cik = client.resolve_cik(ticker)
    data = client._cached_json(  # noqa: SLF001 - same package family, reuses cache/rate limit
        f"submissions_{ticker.upper()}.json", SUBMISSIONS_URL.format(cik=cik)
    )
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
