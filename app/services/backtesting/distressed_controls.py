"""Distressed-survivor control group for the survivorship miss test.

The pilot showed the engine elevated on companies that DIED. The obvious
confound: those companies were financially distressed, and the engine's Cash
Conversion / Balance Sheet blocks measure distress almost by definition. So the
real question is not "does it flag the dead" but "does it flag the dead MORE than
companies that were just as distressed and survived."

This module scores famous near-death SURVIVORS — companies a competent observer
genuinely feared might not make it, which pulled through — at their documented
moment of maximum peril (the "anchor"). If they elevate at the same rate as the
dead set (83% >=p80 / 75% >=p90), the engine detects distress, not death, and the
pilot's headline shrinks accordingly. If the dead scored materially higher, the
engine has discrimination beyond "this company is sick."

Survivors are current filers, so ticker resolution works; SIC still gates the
financial-institution exclusion. Anchor dates are hand-assigned from public
record. Sector mix is weighted to match the dead set (retail- and energy-heavy).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from app.core.pipeline import analyze
from app.services.backtesting.pit import build_pit_dataset, trim_to_mapped_tags
from app.services.backtesting.survivorship import (
    FILING_LAG_DAYS,
    MIN_PIT_PERIODS,
    P80,
    P90,
    STALENESS_LIMIT_DAYS,
    HorizonResult,
    _months_before,
    band,
)
from app.services.ingestion.sec_client import SecClient, SecClientError

# Score at and just before the peak-distress anchor (months before; 0 = at anchor).
HORIZONS_MONTHS = (12, 6, 0)


@dataclass(frozen=True)
class DistressedSurvivor:
    name: str
    ticker: str
    anchor_date: date  # documented moment of maximum peril
    sector: str
    note: str


# Curated near-death survivors, sector-matched to the dead set. Every one was, at
# its anchor, widely feared to be a bankruptcy/near-death candidate — and survived
# (no Chapter 11 or non-reliance in the ~3 years following the anchor).
CONTROLS: list[DistressedSurvivor] = [
    # Retail (matches Sears/JCP/BBBY/Party City/Rite Aid)
    DistressedSurvivor("Macy's", "M", date(2020, 5, 1), "retail", "COVID retail collapse; junk-rated; survived"),
    DistressedSurvivor("Kohl's", "KSS", date(2020, 6, 1), "retail", "COVID + secular decline; survived"),
    DistressedSurvivor("GameStop", "GME", date(2019, 12, 1), "retail", "pre-meme genuine bankruptcy fear; survived"),
    DistressedSurvivor("Gap", "GPS", date(2020, 6, 1), "retail", "COVID; survived"),
    DistressedSurvivor("Nordstrom", "JWN", date(2020, 6, 1), "retail", "COVID; survived"),
    DistressedSurvivor("Dillard's", "DDS", date(2020, 6, 1), "retail", "COVID; survived, later thrived"),
    DistressedSurvivor("Foot Locker", "FL", date(2020, 6, 1), "retail", "COVID; survived"),
    # Energy (matches Whiting/Chesapeake — the 2020 oil crash)
    DistressedSurvivor("Occidental", "OXY", date(2020, 6, 1), "energy", "Anadarko debt + oil crash; Berkshire lifeline; survived"),
    DistressedSurvivor("Devon Energy", "DVN", date(2020, 6, 1), "energy", "oil crash; survived"),
    DistressedSurvivor("Apache/APA", "APA", date(2020, 6, 1), "energy", "oil crash; survived"),
    DistressedSurvivor("Marathon Oil", "MRO", date(2020, 6, 1), "energy", "oil crash; survived (acq. 2024)"),
    DistressedSurvivor("Ovintiv", "OVV", date(2020, 6, 1), "energy", "oil crash; survived"),
    # Pharma / specialty (matches Mallinckrodt/Revlon)
    DistressedSurvivor("Bausch Health", "BHC", date(2017, 1, 1), "pharma", "Valeant peak leverage + accounting crisis; survived"),
    # Travel — the hardest test (as close to death as anyone in 2020, survived)
    DistressedSurvivor("Carnival", "CCL", date(2020, 9, 1), "travel", "COVID zero-revenue; massive raise; survived"),
    DistressedSurvivor("American Airlines", "AAL", date(2020, 9, 1), "travel", "COVID; most-leveraged US airline; survived"),
    # Industrial (matches Diebold/Tupperware)
    DistressedSurvivor("Ford", "F", date(2020, 6, 1), "auto", "drew revolver, junk downgrade 2020; survived"),
]


@dataclass
class ControlResult:
    survivor: DistressedSurvivor
    sic: int | None
    excluded_financial: bool
    error: str | None
    horizons: list[HorizonResult]

    @property
    def peak_score(self) -> float | None:
        scores = [h.overall for h in self.horizons if h.overall is not None]
        return max(scores) if scores else None


def evaluate_survivor(client: SecClient, ds: DistressedSurvivor) -> ControlResult:
    try:
        cik = client.resolve_cik(ds.ticker)
    except SecClientError as e:
        return ControlResult(ds, None, False, f"cik: {e}", [])
    subs = client.submissions_by_cik(cik)
    sic = int(subs["sic"]) if subs.get("sic") else None
    if sic is not None and 6000 <= sic <= 6999:
        return ControlResult(ds, sic, True, None, [])

    try:
        facts = client.company_facts(ds.ticker)
    except SecClientError as e:
        return ControlResult(ds, sic, False, f"facts: {e}", [])
    trimmed = trim_to_mapped_tags(facts)
    horizons: list[HorizonResult] = []
    for months in HORIZONS_MONTHS:
        asof = _months_before(ds.anchor_date, months) + timedelta(days=FILING_LAG_DAYS)
        try:
            dset, diag = build_pit_dataset(trimmed, ds.name, asof, n_quarters=8)
        except ValueError:
            horizons.append(HorizonResult(months, asof, "no_pit_data"))
            continue
        latest = dset.periods[-1].period_end
        if (asof - latest).days > STALENESS_LIMIT_DAYS:
            horizons.append(HorizonResult(months, asof, f"stale(latest={latest})"))
            continue
        if len(dset.periods) < MIN_PIT_PERIODS:
            horizons.append(HorizonResult(months, asof, "short_history"))
            continue
        result = analyze(dset)
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
    return ControlResult(ds, sic, False, None, horizons)


def run_controls(client: SecClient | None = None) -> list[ControlResult]:
    client = client or SecClient()
    return [evaluate_survivor(client, ds) for ds in CONTROLS]
