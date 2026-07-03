"""Restatement (8-K Item 4.02) forensic control — the acid test of the project's
original claim.

Distress the engine can see (docs/distressed_control.md). The forensic-accounting
claim is different and stronger: can the engine flag an *accounting problem*
before the company admits it — especially at companies that were NOT financially
distressed, where there is no stress signal to lean on?

This scores companies that filed an 8-K Item 4.02 (non-reliance on previously
issued financials — an unambiguous accounting event) in the quarters BEFORE the
4.02 announcement, when the misstated numbers were still being reported. The
sharp question is not only "did it elevate" but "was the elevation driven by the
ACCOUNTING blocks (Earnings Quality = accruals, Revenue Quality = receivables vs
revenue — the classic channel-stuffing tell) or merely by the DISTRESS blocks?"
Only accounting-block elevation is genuine forensic detection.

Event date is auto-detected (the first 4.02 filing) — 4.02 is unambiguous, unlike
delisting notices, so no hand-assignment is needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from app.core.pipeline import analyze
from app.services.backtesting.pit import build_pit_dataset, trim_to_mapped_tags
from app.services.backtesting.survivorship import (
    FILING_LAG_DAYS,
    MIN_PIT_PERIODS,
    P80,
    P90,
    STALENESS_LIMIT_DAYS,
    band,
)
from app.services.ingestion.sec_client import SecClient

ACCOUNTING_BLOCKS = ("Earnings Quality", "Revenue Quality")
DISTRESS_BLOCKS = ("Cash Conversion", "Balance Sheet Stress")
ACCOUNTING_CONCERN = 60.0  # a block score at/above this is a real concern signal
HORIZONS_MONTHS = (18, 12, 6)


@dataclass(frozen=True)
class RestatementCase:
    name: str
    cik: int
    healthy_at_time: bool  # True = not in obvious financial distress (the pure forensic test)
    note: str


# Confirmed 4.02 non-reliance cases (verified via submissions), non-financial,
# post-XBRL. `healthy_at_time` marks the pure forensic cases: companies whose
# accounting was wrong but which were NOT financially distressed, so distress
# blocks cannot rescue the flag.
CASES: list[RestatementCase] = [
    RestatementCase("MiMedx", 1376339, True, "channel-stuffing fraud; revenue looked strong — the ideal forensic test"),
    RestatementCase("Molson Coors", 24545, True, "healthy; deferred-tax restatement 2019"),
    RestatementCase("Comscore", 1158172, True, "revenue-recognition restatement 2016; not distressed"),
    RestatementCase("WageWorks", 1158863, True, "revenue restatement 2018; healthy, later acquired"),
    RestatementCase("Rockwell Medical", 1041024, True, "revenue-recognition restatement 2018"),
    RestatementCase("Kandi Technologies", 1316517, True, "related-party/revenue restatement 2017"),
    RestatementCase("Kraft Heinz", 1637459, False, "2017-19 non-reliance; large, leveraged"),
    RestatementCase("Plug Power", 1093691, False, "2021 restatement; cash-burning"),
    RestatementCase("SunPower", 867773, False, "2023 restatement; financially stressed"),
    RestatementCase("Nikola", 1731289, False, "2021 fraud restatement; SPAC, pre-revenue"),
]


@dataclass
class HorizonScore:
    months_before: int
    asof: date
    status: str
    overall: float | None = None
    blocks: dict[str, float] = field(default_factory=dict)
    top_block: str = ""
    coverage: float | None = None


@dataclass
class CaseResult:
    case: RestatementCase
    event_date: date | None
    sic: int | None
    excluded_financial: bool
    horizons: list[HorizonScore]

    @property
    def best_overall(self) -> float | None:
        v = [h.overall for h in self.horizons if h.overall is not None]
        return max(v) if v else None

    @property
    def best_accounting_block(self) -> float | None:
        vals = [
            h.blocks[b]
            for h in self.horizons
            for b in ACCOUNTING_BLOCKS
            if b in h.blocks
        ]
        return max(vals) if vals else None

    @property
    def accounting_driven(self) -> bool:
        """Did an accounting block reach the concern threshold pre-event?"""
        b = self.best_accounting_block
        return b is not None and b >= ACCOUNTING_CONCERN


def _months_before(d: date, months: int) -> date:
    y, m = d.year, d.month - months
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, min(d.day, 28))


def first_402_date(client: SecClient, cik: int) -> date | None:
    subs = client.submissions_by_cik(cik)
    r = subs.get("filings", {}).get("recent", {})
    forms, items, dates = r.get("form", []), r.get("items", []), r.get("filingDate", [])
    hits = [
        dates[i]
        for i in range(min(len(forms), len(items), len(dates)))
        if forms[i].startswith("8-K") and "4.02" in (items[i] or "")
    ]
    if not hits:
        return None
    return min(datetime.strptime(d, "%Y-%m-%d").date() for d in hits)


def evaluate_case(client: SecClient, rc: RestatementCase) -> CaseResult:
    subs = client.submissions_by_cik(rc.cik)
    sic = int(subs["sic"]) if subs.get("sic") else None
    if sic is not None and 6000 <= sic <= 6999:
        return CaseResult(rc, None, sic, True, [])
    event = first_402_date(client, rc.cik)
    if event is None:
        return CaseResult(rc, None, sic, False, [])

    facts = client.company_facts_by_cik(rc.cik)
    trimmed = trim_to_mapped_tags(facts)
    horizons: list[HorizonScore] = []
    for months in HORIZONS_MONTHS:
        asof = _months_before(event, months) + timedelta(days=FILING_LAG_DAYS)
        try:
            dset, diag = build_pit_dataset(trimmed, rc.name, asof, n_quarters=8)
        except ValueError:
            horizons.append(HorizonScore(months, asof, "no_pit_data"))
            continue
        latest = dset.periods[-1].period_end
        if (asof - latest).days > STALENESS_LIMIT_DAYS:
            horizons.append(HorizonScore(months, asof, f"stale(latest={latest})"))
            continue
        if len(dset.periods) < MIN_PIT_PERIODS:
            horizons.append(HorizonScore(months, asof, "short_history"))
            continue
        result = analyze(dset)
        if result.overall is None or result.overall.score is None:
            horizons.append(HorizonScore(months, asof, "no_score", coverage=round(diag.coverage(), 2)))
            continue
        blocks = {b.name: b.score for b in result.block_scores if b.score is not None}
        top = max(blocks.items(), key=lambda kv: kv[1])[0] if blocks else ""
        horizons.append(HorizonScore(
            months, asof, "ok",
            overall=round(result.overall.score, 1),
            blocks={k: round(v, 1) for k, v in blocks.items()},
            top_block=top,
            coverage=round(diag.coverage(), 2),
        ))
    return CaseResult(rc, event, sic, False, horizons)


def run_restatement_control(client: SecClient | None = None) -> list[CaseResult]:
    client = client or SecClient()
    return [evaluate_case(client, rc) for rc in CASES]
