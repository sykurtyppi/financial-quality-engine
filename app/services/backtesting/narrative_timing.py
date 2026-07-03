"""Timing analysis for the validated narrative signals.

The clean control validated two discriminating signals (high-severity disclosure
emergence, KPI-definition drift). The remaining question decides their value: does
the signal emerge QUARTERS AHEAD of the 4.02 (early warning) or only in the filing
that accompanies the restatement (contemporaneous — a faithful never-misser, but
not prediction)?

Two measures per case:
1. DETECTOR lead — when the validated detector fires (emergence period), how many
   days before the 4.02 was that period's filing? This is what the product would
   actually surface.
2. DIRECT-SCAN lead — the earliest pre-4.02 filing (by filing date) whose MD&A or
   earnings release mentions a high-severity term at all. MD&A/releases are scanned
   rather than risk factors, because risk factors are full of hypothetical
   ("a material weakness could...") boilerplate. This checks whether the
   information was available earlier than the detector caught it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from app.core.pipeline import analyze
from app.schemas.financials import DocumentType
from app.services.backtesting.pit import build_pit_dataset, trim_to_mapped_tags
from app.services.backtesting.restatement_control import CASES, RestatementCase, first_402_date
from app.services.ingestion.edgar_documents import fetch_documents
from app.services.ingestion.sec_client import SecClient
from app.services.narrative.detectors import HIGH_SEVERITY_RISK_TERMS

TRACKED_KINDS = ("high_severity_disclosure", "kpi_definition_change")
EARLY_WARNING_DAYS = 135  # ~1.5 quarters or more before the 4.02
CONTEMPORANEOUS_DAYS = 45  # within ~one filing cycle of the 4.02


def _accession(source: str | None) -> str | None:
    parts = (source or "").split()
    return parts[1] if len(parts) >= 2 else None


def _lead_days(event: date, filed: str | None) -> int | None:
    if not filed:
        return None
    return (event - datetime.strptime(filed, "%Y-%m-%d").date()).days


def classify(lead: int | None) -> str:
    if lead is None:
        return "unknown"
    if lead >= EARLY_WARNING_DAYS:
        return "EARLY WARNING"
    if lead < CONTEMPORANEOUS_DAYS:
        return "contemporaneous"
    return "one-quarter lead"


@dataclass
class DetectorHit:
    kind: str
    period: str
    filed: str | None
    lead_days: int | None


@dataclass
class TimingResult:
    case: RestatementCase
    event_date: date | None
    detector_hits: list[DetectorHit] = field(default_factory=list)
    direct_term: str | None = None
    direct_filed: str | None = None
    direct_lead_days: int | None = None
    error: str | None = None


def evaluate(client: SecClient, case: RestatementCase) -> TimingResult:
    event = first_402_date(client, case.cik)
    if event is None:
        return TimingResult(case, None, error="no 4.02")
    subs = client.submissions_by_cik(case.cik)
    r = subs.get("filings", {}).get("recent", {})
    accs, filings = r.get("accessionNumber", []), r.get("filingDate", [])
    acc_filed = {accs[i]: filings[i] for i in range(min(len(accs), len(filings)))}

    facts = client.company_facts_by_cik(case.cik)
    trimmed = trim_to_mapped_tags(facts)
    try:
        ds, _ = build_pit_dataset(trimmed, case.name, event, n_quarters=8)
    except ValueError as e:
        return TimingResult(case, event, error=f"pit: {e}")
    docs = fetch_documents(client, case.name, facts, n_filings=12, cik=case.cik, before=event)

    label_filed: dict[str, str] = {}
    for d in docs.documents:
        fd = acc_filed.get(_accession(d.source))
        if fd:
            label_filed[d.fiscal_label] = min(label_filed.get(d.fiscal_label, fd), fd)

    ds.documents = docs.documents
    result = analyze(ds)
    hits = []
    for f in result.narrative_findings:
        if f.kind in TRACKED_KINDS:
            fd = label_filed.get(f.fiscal_label)
            hits.append(DetectorHit(f.kind, f.fiscal_label, fd, _lead_days(event, fd)))

    # Direct scan: earliest MD&A/earnings-release mention of any high-severity term.
    direct: tuple[str, str] | None = None
    for d in docs.documents:
        if d.doc_type not in (DocumentType.MDNA, DocumentType.EARNINGS_RELEASE):
            continue
        fd = acc_filed.get(_accession(d.source))
        if not fd:
            continue
        for term in HIGH_SEVERITY_RISK_TERMS:
            if re.search(re.escape(term), d.text, re.IGNORECASE):
                if direct is None or fd < direct[1]:
                    direct = (term, fd)
    res = TimingResult(case, event, detector_hits=hits)
    if direct:
        res.direct_term, res.direct_filed = direct
        res.direct_lead_days = _lead_days(event, direct[1])
    return res


def run(client: SecClient | None = None) -> list[TimingResult]:
    client = client or SecClient()
    return [evaluate(client, c) for c in CASES]
