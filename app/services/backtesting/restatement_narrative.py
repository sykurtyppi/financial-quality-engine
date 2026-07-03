"""Narrative-contradiction test on the restatement (4.02) cases.

The metric controls showed the deterministic numbers mostly missed these
accounting problems (docs/restatement_control.md). This exercises the one
untested capability — the narrative-vs-economics layer — on the same cases,
using point-in-time pre-4.02 documents (MD&A, risk factors, earnings releases).

Two distinct things are measured:

1. METRIC-GATED mismatches (demand-vs-working-capital, profitability-vs-cash,
   etc.). These fire only when a positive narrative coincides with an already-
   flagged metric — so where the metrics missed, these structurally cannot fire.
   Reported to confirm/quantify that structural dependency.

2. INDEPENDENT narrative detectors (adjustment-language recurrence, KPI drift,
   disclosure reduction, defensive tone, guidance shift, high-severity term
   emergence). These read the documents directly and are the ONLY part that
   could catch something the metrics missed. This is the real question.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.pipeline import analyze
from app.services.backtesting.pit import build_pit_dataset, trim_to_mapped_tags
from app.services.backtesting.restatement_control import CASES, RestatementCase, first_402_date
from app.services.ingestion.edgar_documents import fetch_documents
from app.services.ingestion.sec_client import SecClient

# Findings that read documents directly (not gated on a flagged metric).
INDEPENDENT_KINDS = {
    "adjustment_recurrence",
    "kpi_removed",
    "kpi_added",
    "kpi_definition_change",
    "disclosure_reduction",
    "defensive_tone_increase",
    "guidance_shift",
    "high_severity_disclosure",
}


@dataclass
class NarrativeResult:
    case: RestatementCase
    event_date: object | None
    n_documents: int
    doc_periods: list[str]
    independent_findings: list[tuple[str, str]] = field(default_factory=list)  # (kind, detail)
    mismatches: list[tuple[str, str]] = field(default_factory=list)  # (kind, confidence)
    error: str | None = None

    @property
    def has_independent_signal(self) -> bool:
        return bool(self.independent_findings)


def evaluate_case(client: SecClient, rc: RestatementCase) -> NarrativeResult:
    event = first_402_date(client, rc.cik)
    if event is None:
        return NarrativeResult(rc, None, 0, [], error="no 4.02 date")
    facts = client.company_facts_by_cik(rc.cik)
    trimmed = trim_to_mapped_tags(facts)
    try:
        ds, _ = build_pit_dataset(trimmed, rc.name, event, n_quarters=8)
    except ValueError as e:
        return NarrativeResult(rc, event, 0, [], error=f"pit: {e}")

    docs = fetch_documents(client, rc.name, facts, n_filings=8, cik=rc.cik, before=event)
    ds.documents = docs.documents
    result = analyze(ds)

    independent = [
        (f.kind, f.detail[:120])
        for f in result.narrative_findings
        if f.kind in INDEPENDENT_KINDS
    ]
    mismatches = [(m.kind, m.confidence) for m in result.mismatches]
    return NarrativeResult(
        rc, event, len(docs.documents),
        sorted({d.fiscal_label for d in docs.documents}),
        independent, mismatches,
    )


def run(client: SecClient | None = None) -> list[NarrativeResult]:
    client = client or SecClient()
    return [evaluate_case(client, rc) for rc in CASES]
