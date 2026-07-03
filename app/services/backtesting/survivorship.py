"""Survivorship-corrected miss test on delisted companies.

The v0.3 miss test could only score companies still in the current SEC ticker
registry — i.e. survivors. This module scores companies that DIED (bankruptcy /
non-reliance / delisting) using CIK-direct companyfacts, which persist on EDGAR
after delisting. It answers the question the survivor-only test structurally
could not: on companies that actually failed, did the engine elevate BEFORE the
event?

Event dates are HAND-ASSIGNED from public record (see PILOT below). Auto-
detecting the *terminal* event from 8-K item codes is unreliable — item 3.01
(delisting/deficiency) fires for benign, later-cured notices, and item dates can
predate a SPAC listing or attach to a subsidiary. For a small curated pilot,
curation is the rigorous choice; the auto-detector (backtesting/events.py) is for
the later large base-rate study, where noise averages out.

SIC is still fetched live so the engine's financial-institution exclusion applies
automatically (delisted financials are dropped, not mis-scored).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from app.core.pipeline import analyze
from app.services.backtesting.pit import build_pit_dataset, trim_to_mapped_tags
from app.services.ingestion.sec_client import SecClient

# Calibration reference bands (v0.3 distribution, n=1141): p50 31.7 / p80 40.3 / p90 45.1.
P50, P80, P90 = 31.7, 40.3, 45.1
FILING_LAG_DAYS = 75
STALENESS_LIMIT_DAYS = 200  # a bit looser than live: dying firms file late (itself a signal)
MIN_PIT_PERIODS = 6


@dataclass(frozen=True)
class DeadCompany:
    name: str
    cik: int
    event_date: date
    event_type: str  # "bankruptcy" | "non_reliance" | "delisting"
    note: str = ""


# Curated post-XBRL delistings with hand-assigned canonical event dates from
# public record. Event date = the terminal accounting/insolvency event we would
# have wanted to see coming. Financials (WeWork SIC 6512) are intentionally
# omitted — the engine excludes them by design.
PILOT: list[DeadCompany] = [
    DeadCompany("Tupperware Brands", 1008654, date(2023, 3, 16), "non_reliance",
                "8-K 4.02 non-reliance; delisted 2023, Ch.11 2024"),
    DeadCompany("Bed Bath & Beyond", 886158, date(2023, 4, 23), "bankruptcy",
                "Ch.11 Apr-2023; going-concern warning Jan-2023"),
    DeadCompany("Sears Holdings", 1310067, date(2018, 10, 15), "bankruptcy", "Ch.11 Oct-2018"),
    DeadCompany("J.C. Penney", 1166126, date(2020, 5, 15), "bankruptcy", "Ch.11 May-2020"),
    DeadCompany("Revlon", 887921, date(2022, 6, 15), "bankruptcy", "Ch.11 Jun-2022"),
    DeadCompany("Mallinckrodt", 1567892, date(2020, 10, 12), "bankruptcy", "Ch.11 Oct-2020"),
    DeadCompany("Whiting Petroleum", 1255474, date(2020, 4, 1), "bankruptcy", "Ch.11 Apr-2020"),
    DeadCompany("Chesapeake Energy", 895126, date(2020, 6, 28), "bankruptcy", "Ch.11 Jun-2020"),
    DeadCompany("Rite Aid", 84129, date(2023, 10, 15), "bankruptcy", "Ch.11 Oct-2023"),
    DeadCompany("Party City", 1592058, date(2023, 1, 17), "bankruptcy", "Ch.11 Jan-2023"),
    DeadCompany("Diebold Nixdorf", 93556, date(2022, 1, 26), "non_reliance",
                "8-K 4.02 non-reliance Jan-2022; Ch.11 2023"),
    DeadCompany("Enviva", 1592057, date(2024, 3, 12), "bankruptcy", "Ch.11 Mar-2024"),
]

# As-of horizons before the event (months). Each is scored point-in-time.
HORIZONS_MONTHS = (18, 12, 6)


def _months_before(d: date, months: int) -> date:
    y, m = d.year, d.month - months
    while m <= 0:
        m += 12
        y -= 1
    day = min(d.day, 28)
    return date(y, m, day)


def band(score: float) -> str:
    if score >= P90:
        return ">=p90"
    if score >= P80:
        return ">=p80"
    if score >= P50:
        return ">=p50"
    return "<p50"


@dataclass
class HorizonResult:
    months_before: int
    asof: date
    status: str
    overall: float | None = None
    top_blocks: str = ""
    coverage: float | None = None
    n_red_flags: int | None = None


@dataclass
class CompanyResult:
    company: DeadCompany
    sic: int | None
    excluded_financial: bool
    horizons: list[HorizonResult]

    @property
    def best_pre_event_score(self) -> float | None:
        scores = [h.overall for h in self.horizons if h.overall is not None]
        return max(scores) if scores else None


def evaluate_company(client: SecClient, dc: DeadCompany) -> CompanyResult:
    subs = client.submissions_by_cik(dc.cik)
    sic = int(subs["sic"]) if subs.get("sic") else None
    is_financial = sic is not None and 6000 <= sic <= 6999
    if is_financial:
        return CompanyResult(dc, sic, True, [])

    facts = client.company_facts_by_cik(dc.cik)
    trimmed = trim_to_mapped_tags(facts)
    horizons: list[HorizonResult] = []
    for months in HORIZONS_MONTHS:
        asof = _months_before(dc.event_date, months) + timedelta(days=FILING_LAG_DAYS)
        try:
            ds, diag = build_pit_dataset(trimmed, dc.name, asof, n_quarters=8)
        except ValueError:
            horizons.append(HorizonResult(months, asof, "no_pit_data"))
            continue
        latest = ds.periods[-1].period_end
        if (asof - latest).days > STALENESS_LIMIT_DAYS:
            horizons.append(HorizonResult(months, asof, f"stale(latest={latest})"))
            continue
        if len(ds.periods) < MIN_PIT_PERIODS:
            horizons.append(HorizonResult(months, asof, "short_history"))
            continue
        result = analyze(ds)
        if result.overall is None or result.overall.score is None:
            horizons.append(HorizonResult(months, asof, "no_score", coverage=round(diag.coverage(), 2)))
            continue
        blocks = sorted((b for b in result.block_scores if b.score is not None),
                        key=lambda b: -b.score)[:2]  # type: ignore[arg-type]
        horizons.append(HorizonResult(
            months, asof, "ok",
            overall=round(result.overall.score, 1),
            top_blocks=", ".join(f"{b.name}={b.score:.0f}" for b in blocks),
            coverage=round(diag.coverage(), 2),
            n_red_flags=len(result.red_flags),
        ))
    return CompanyResult(dc, sic, False, horizons)


def run_pilot(client: SecClient | None = None) -> list[CompanyResult]:
    client = client or SecClient()
    return [evaluate_company(client, dc) for dc in PILOT]
