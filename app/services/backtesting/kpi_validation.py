"""KPI-drift validation harness (design stage 11).

Measures the KPI-definition-change signal — recall on restatement cases, false-
positive rate on clean companies, and lead time — under a swappable adjudicator.
Phase 2 runs it with the DeterministicAdjudicator to reproduce the baseline
(clean FP ~18%, restater recall ~60%, MiMedx lead). Phase 4 re-runs it with the
LLM adjudicator and compares, holding everything else fixed.

"Fires" = at least one REDEFINITION pair judged material (matching the validated
`kpi_definition_change` signal; drops are tracked separately). Lead time uses the
emergence period's filing date vs the 4.02, as in the timing analysis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from app.services.backtesting.clean_narrative_control import ANCHOR, CLEAN, CleanCompany
from app.services.backtesting.restatement_control import CASES, RestatementCase, first_402_date
from app.services.narrative.kpi_adjudicator import Adjudicator, DeterministicAdjudicator
from app.services.narrative.kpi_extraction import pair_definition_changes
from app.services.ingestion.companyfacts_mapper import fiscal_year_end_month  # noqa: F401 (kept for parity)
from app.services.ingestion.edgar_documents import fetch_documents
from app.services.ingestion.sec_client import SecClient


@dataclass
class KpiFireResult:
    name: str
    group: str  # "restater" | "clean"
    event_or_anchor: date | None
    fired: bool = False
    material_kpis: list[str] = field(default_factory=list)
    emergence_period: str | None = None
    emergence_filed: str | None = None
    lead_days: int | None = None
    n_pairs: int = 0
    error: str | None = None


def _accession(source: str | None) -> str | None:
    parts = (source or "").split()
    return parts[1] if len(parts) >= 2 else None


def _evaluate(
    client: SecClient,
    name: str,
    cik: int,
    group: str,
    cutoff: date,
    event: date | None,
    adjudicator: Adjudicator,
    use_cik_facts: bool,
) -> KpiFireResult:
    res = KpiFireResult(name=name, group=group, event_or_anchor=event or cutoff)
    try:
        facts = client.company_facts_by_cik(cik) if use_cik_facts else client.company_facts(name)
    except Exception as e:  # noqa: BLE001
        res.error = f"facts: {str(e)[:50]}"
        return res
    docs = fetch_documents(client, name, facts, n_filings=12, cik=cik, before=cutoff)
    if len(docs.documents) < 2:
        res.error = "insufficient documents"
        return res

    pairs = [p for p in pair_definition_changes(docs.documents) if p.change_type == "redefinition"]
    res.n_pairs = len(pairs)
    material = []
    for p in pairs:
        adj = adjudicator.adjudicate(p)
        if adj is not None and adj.is_material:
            material.append(p)
    if not material:
        return res
    res.fired = True
    res.material_kpis = [p.kpi for p in material]

    # Lead time: emergence = the latest documented period (current_period of the pairs).
    if event is not None:
        acc_filed: dict[str, str] = {}
        subs = client.submissions_by_cik(cik)
        r = subs.get("filings", {}).get("recent", {})
        accs, fds = r.get("accessionNumber", []), r.get("filingDate", [])
        for i in range(min(len(accs), len(fds))):
            acc_filed[accs[i]] = fds[i]
        emergence = material[0].current_period
        filed = None
        for d in docs.documents:
            if d.fiscal_label == emergence:
                fd = acc_filed.get(_accession(d.source))
                if fd and (filed is None or fd < filed):
                    filed = fd
        res.emergence_period = emergence
        res.emergence_filed = filed
        if filed:
            res.lead_days = (event - datetime.strptime(filed, "%Y-%m-%d").date()).days
    return res


def run_validation(
    adjudicator: Adjudicator | None = None,
    client: SecClient | None = None,
    restaters: list[RestatementCase] | None = None,
    clean: list[CleanCompany] | None = None,
) -> list[KpiFireResult]:
    adjudicator = adjudicator or DeterministicAdjudicator()
    client = client or SecClient()
    results: list[KpiFireResult] = []

    for rc in restaters or CASES:
        event = first_402_date(client, rc.cik)
        if event is None:
            results.append(KpiFireResult(rc.name, "restater", None, error="no 4.02"))
            continue
        results.append(_evaluate(client, rc.name, rc.cik, "restater", event, event, adjudicator, use_cik_facts=True))

    for cc in clean or CLEAN:
        try:
            cik = client.resolve_cik(cc.ticker)
        except Exception as e:  # noqa: BLE001
            results.append(KpiFireResult(cc.name, "clean", ANCHOR, error=f"cik: {str(e)[:40]}"))
            continue
        results.append(_evaluate(client, cc.ticker, cik, "clean", ANCHOR, None, adjudicator, use_cik_facts=False))
    return results


def summarize(results: list[KpiFireResult]) -> dict:
    restaters = [r for r in results if r.group == "restater" and r.error is None]
    clean = [r for r in results if r.group == "clean" and r.error is None]
    rec = sum(1 for r in restaters if r.fired)
    fp = sum(1 for r in clean if r.fired)
    leads = sorted(r.lead_days for r in restaters if r.fired and r.lead_days is not None)
    return {
        "restater_n": len(restaters),
        "restater_recall": rec / len(restaters) if restaters else None,
        "restater_fired": rec,
        "clean_n": len(clean),
        "clean_fp_rate": fp / len(clean) if clean else None,
        "clean_fired": fp,
        "leads_days": leads,
        "early_warning_leads": [d for d in leads if d >= 135],
    }
