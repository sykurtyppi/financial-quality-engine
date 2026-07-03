"""Deterministic KPI drift detection.

v1 approach: a curated dictionary of common disclosed KPIs is matched per
period; additions and removals are computed against the union of the prior
two documented periods. Definition-change detection requires semantic
comparison and is deferred to the LLM-assisted layer (see docs/roadmap.md) —
it is NOT approximated here to avoid false precision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.schemas.financials import DocumentRecord
from app.schemas.report import NarrativeFinding

KPI_DICTIONARY: dict[str, tuple[str, ...]] = {
    "ARR": (r"annual(?:ized)? recurring revenue", r"\bARR\b"),
    "MRR": (r"monthly recurring revenue", r"\bMRR\b"),
    "RPO": (r"remaining performance obligations?", r"\bRPO\b"),
    "Net revenue retention": (r"net (?:revenue|dollar) retention", r"dollar-based net (?:expansion|retention)"),
    "Billings": (r"\bbillings\b",),
    "Bookings": (r"\bbookings\b",),
    "Backlog": (r"\bbacklog\b",),
    "DAU": (r"daily active users", r"\bDAUs?\b"),
    "MAU": (r"monthly active users", r"\bMAUs?\b"),
    "ARPU": (r"average revenue per user", r"\bARPU\b"),
    "Churn": (r"\bchurn\b",),
    "Customer count": (r"total (?:customers|paying customers|subscribers)",),
    "Large customers": (r"customers? (?:with|generating) (?:more than|over|\$)",),
    "GMV": (r"gross merchandise (?:value|volume)", r"\bGMV\b"),
    "Take rate": (r"take rate",),
    "Same-store sales": (r"same[- ]store sales", r"comparable[- ]store sales"),
    "Gross margin": (r"gross margin",),
    "Adjusted EBITDA": (r"adjusted ebitda",),
    "Adjusted EPS": (r"adjusted (?:diluted )?eps", r"adjusted (?:diluted )?earnings per share"),
    "Free cash flow": (r"free cash flow",),
    "Unit volume": (r"unit (?:volume|shipments)",),
    "Utilization": (r"\butilization rate\b",),
}


def detect_kpis(text: str) -> set[str]:
    found: set[str] = set()
    for kpi, patterns in KPI_DICTIONARY.items():
        for pat in patterns:
            if re.search(pat, text, re.IGNORECASE):
                found.add(kpi)
                break
    return found


@dataclass
class KpiDriftAnalysis:
    periods_analyzed: int
    latest_kpis: set[str]
    added: set[str]
    removed: set[str]
    findings: list[NarrativeFinding]


def analyze(documents: list[DocumentRecord], prior_window: int = 2) -> KpiDriftAnalysis:
    """Compare latest documented period's KPI set against the union of the
    prior `prior_window` documented periods."""
    by_label: dict[str, str] = {}
    order: list[str] = []
    for doc in documents:
        if doc.fiscal_label not in by_label:
            by_label[doc.fiscal_label] = ""
            order.append(doc.fiscal_label)
        by_label[doc.fiscal_label] += "\n" + doc.text

    if len(order) < 2:
        return KpiDriftAnalysis(len(order), set(), set(), set(), [])

    latest_label = order[-1]
    prior_labels = order[-(prior_window + 1):-1]
    latest_kpis = detect_kpis(by_label[latest_label])
    prior_kpis: set[str] = set()
    for label in prior_labels:
        prior_kpis |= detect_kpis(by_label[label])

    added = latest_kpis - prior_kpis
    removed = prior_kpis - latest_kpis

    findings: list[NarrativeFinding] = []
    for kpi in sorted(removed):
        findings.append(
            NarrativeFinding(
                kind="kpi_removed",
                detail=(
                    f'KPI "{kpi}" was discussed in prior periods '
                    f"({', '.join(prior_labels)}) but is not mentioned in {latest_label}. "
                    "Reduced disclosure of a previously highlighted metric warrants review."
                ),
                fiscal_label=latest_label,
            )
        )
    for kpi in sorted(added):
        findings.append(
            NarrativeFinding(
                kind="kpi_added",
                detail=(
                    f'KPI "{kpi}" is newly introduced in {latest_label}. New metrics '
                    "introduced when established metrics soften warrant definitional review."
                ),
                fiscal_label=latest_label,
            )
        )

    return KpiDriftAnalysis(
        periods_analyzed=len(order),
        latest_kpis=latest_kpis,
        added=added,
        removed=removed,
        findings=findings,
    )
