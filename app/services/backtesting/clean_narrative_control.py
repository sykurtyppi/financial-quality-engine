"""Clean-company narrative control — does the narrative layer DISCRIMINATE?

The restatement narrative test fired independent findings on 10/10 cases, but a
100% hit rate is meaningless without a base rate. This runs the identical
narrative layer on companies that did NOT restate, and compares per-detector
firing rates. The hypothesis:

- adjustment_recurrence fires ~equally (it is language every large company uses —
  confirmed noise if clean ~ restaters);
- high_severity_disclosure (material weakness / going concern / substantial doubt
  / restatement / delisting) fires MUCH LESS on clean companies (real signal);
- touted-KPI drops / definition changes and genuine disclosure collapse: TBD.

The clean set is deliberately loaded with SERIAL ACQUIRERS and non-GAAP-heavy
industrials (Danaher, Thermo Fisher, Roper, Honeywell, Emerson) — the exact
profile that drives adjustment-language recurrence — so the comparison is fair:
not fraudsters-who-use-adjustment-language vs simple clean companies, but
restaters vs equally-complex clean companies.

Anchor 2019-09-01: a settled, pre-COVID quarter with 8 clean trailing quarters
for all names, avoiding pandemic-era language noise in the baseline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.core.pipeline import analyze
from app.services.backtesting.pit import build_pit_dataset, trim_to_mapped_tags
from app.services.backtesting.restatement_control import first_402_date
from app.services.backtesting.restatement_narrative import INDEPENDENT_KINDS
from app.services.ingestion.edgar_documents import fetch_documents
from app.services.ingestion.sec_client import SecClient, SecClientError

ANCHOR = date(2019, 9, 1)

# Per-detector firing rates on the restatement set (n=10), from
# docs/restatement_narrative.md — the reference to compare against.
RESTATEMENT_RATES = {
    "adjustment_recurrence": (9, 10),
    "high_severity_disclosure": (3, 10),
    "kpi_removed": (3, 10),
    "kpi_added": (3, 10),
    "kpi_definition_change": (6, 10),
    "disclosure_reduction": (6, 10),
    "guidance_shift": (2, 10),
}


@dataclass(frozen=True)
class CleanCompany:
    name: str
    ticker: str
    sector: str
    note: str


# Sector-matched to the restatement set, and deliberately heavy on serial
# acquirers / non-GAAP-heavy names to match the adjustment-language confound.
CLEAN: list[CleanCompany] = [
    # Serial acquirers / non-GAAP-heavy industrials (the key confound match)
    CleanCompany("Danaher", "DHR", "serial_acquirer", "constant M&A + non-GAAP; clean"),
    CleanCompany("Thermo Fisher", "TMO", "serial_acquirer", "constant M&A + non-GAAP; clean"),
    CleanCompany("Roper", "ROP", "serial_acquirer", "roll-up + non-GAAP; clean"),
    CleanCompany("Honeywell", "HON", "industrial", "restructuring language heavy; clean"),
    CleanCompany("Emerson", "EMR", "industrial", "restructuring + M&A; clean"),
    # Consumer staples (match Molson Coors / Kraft Heinz)
    CleanCompany("PepsiCo", "PEP", "staples", "non-GAAP heavy; clean"),
    CleanCompany("Procter & Gamble", "PG", "staples", "restructuring language; clean"),
    CleanCompany("Colgate", "CL", "staples", "clean"),
    CleanCompany("General Mills", "GIS", "staples", "M&A + non-GAAP; clean"),
    # Tech / services (match Comscore)
    CleanCompany("Adobe", "ADBE", "tech", "non-GAAP; clean"),
    CleanCompany("Cisco", "CSCO", "tech", "constant M&A + non-GAAP; clean"),
    CleanCompany("Accenture", "ACN", "services", "clean"),
    # Retail (match retail restaters)
    CleanCompany("Home Depot", "HD", "retail", "clean"),
    CleanCompany("TJX", "TJX", "retail", "clean"),
    # Medical (match Rockwell / MiMedx sector)
    CleanCompany("Abbott", "ABT", "medical", "M&A + non-GAAP; clean"),
    CleanCompany("Medtronic", "MDT", "medical", "constant M&A + non-GAAP; clean"),
]


@dataclass
class CleanResult:
    company: CleanCompany
    has_402: bool
    n_documents: int
    doc_periods: list[str]
    independent_kinds: set[str] = field(default_factory=set)
    error: str | None = None


def evaluate_clean(client: SecClient, cc: CleanCompany, anchor: date = ANCHOR) -> CleanResult:
    try:
        cik = client.resolve_cik(cc.ticker)
    except SecClientError as e:
        return CleanResult(cc, False, 0, [], error=f"cik: {e}")
    # Exclude any name that actually had a 4.02 on/before the anchor — it is not
    # a clean control.
    ev = first_402_date(client, cik)
    has_402 = ev is not None and ev <= anchor
    try:
        facts = client.company_facts(cc.ticker)
        trimmed = trim_to_mapped_tags(facts)
        ds, _ = build_pit_dataset(trimmed, cc.name, anchor, n_quarters=8)
    except (SecClientError, ValueError) as e:
        return CleanResult(cc, has_402, 0, [], error=str(e)[:60])
    docs = fetch_documents(client, cc.ticker, facts, n_filings=8, cik=cik, before=anchor)
    ds.documents = docs.documents
    result = analyze(ds)
    kinds = {f.kind for f in result.narrative_findings if f.kind in INDEPENDENT_KINDS}
    return CleanResult(cc, has_402, len(docs.documents), sorted({d.fiscal_label for d in docs.documents}), kinds)


def run_clean_control(client: SecClient | None = None) -> list[CleanResult]:
    client = client or SecClient()
    return [evaluate_clean(client, cc) for cc in CLEAN]
