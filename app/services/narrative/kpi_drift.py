"""Deterministic KPI drift detection.

A curated dictionary of common disclosed KPIs is matched per period;
additions and removals are computed against the union of the prior two
documented periods.

v0.4 adds DEFINITION-CHANGE CANDIDATES: when a KPI's surrounding definitional
sentence (one containing cues like "defined as", "excludes", "represents")
differs materially between the current period and the most recent prior
period that defined it, the pair of excerpts is surfaced for analyst review.
This is a candidate flagger — token overlap, not semantics — and is labeled
medium/low confidence accordingly.
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


DEFINITION_CUES = ("defined as", "we define", "we calculate", "calculated as", "represents", "excludes", "excluding", "includes")
_DEFINITION_SIMILARITY_THRESHOLD = 0.55
_STOPWORDS = frozenset(
    "the a an and or of to in for as is are we our by with on that which".split()
)


def _definition_sentence(text: str, kpi: str) -> str | None:
    """First sentence mentioning the KPI that also contains a definitional cue."""
    patterns = KPI_DICTIONARY[kpi]
    for sentence in re.split(r"(?<=[.;])\s+", text):
        low = sentence.lower()
        if not any(cue in low for cue in DEFINITION_CUES):
            continue
        if any(re.search(pat, sentence, re.IGNORECASE) for pat in patterns):
            return " ".join(sentence.split())
    return None


def _token_similarity(a: str, b: str) -> float:
    ta = {w for w in re.findall(r"[a-z]+", a.lower()) if w not in _STOPWORDS}
    tb = {w for w in re.findall(r"[a-z]+", b.lower()) if w not in _STOPWORDS}
    if not ta or not tb:
        return 1.0
    return len(ta & tb) / len(ta | tb)


@dataclass
class DefinitionChange:
    kpi: str
    current_definition: str
    prior_definition: str
    prior_label: str
    similarity: float


def detect_definition_changes(
    texts_by_label: dict[str, str], order: list[str]
) -> list[DefinitionChange]:
    """Compare the latest period's KPI definition sentences against the most
    recent prior period that defined the same KPI."""
    if len(order) < 2:
        return []
    latest_label = order[-1]
    latest_text = texts_by_label[latest_label]
    out: list[DefinitionChange] = []
    for kpi in sorted(detect_kpis(latest_text)):
        cur_def = _definition_sentence(latest_text, kpi)
        if cur_def is None:
            continue
        for label in reversed(order[:-1]):
            prior_def = _definition_sentence(texts_by_label[label], kpi)
            if prior_def is None:
                continue
            sim = _token_similarity(cur_def, prior_def)
            if sim < _DEFINITION_SIMILARITY_THRESHOLD:
                out.append(
                    DefinitionChange(
                        kpi=kpi,
                        current_definition=cur_def[:300],
                        prior_definition=prior_def[:300],
                        prior_label=label,
                        similarity=sim,
                    )
                )
            break  # only compare against the most recent period that defined it
    return out


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
