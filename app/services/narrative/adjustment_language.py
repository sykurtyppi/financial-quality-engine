"""Deterministic recurring-adjustment-language detector.

Counts adjustment-related terms across period documents with word-boundary
matching, and measures recurrence: 'one-time' language that appears period
after period is the signal, a single mention is not.

No LLM involvement: every finding carries the exact source snippets.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from app.schemas.financials import DocumentRecord
from app.schemas.report import NarrativeFinding

ADJUSTMENT_TERMS: tuple[str, ...] = (
    "restructuring",
    "integration",
    "transformation",
    "optimization",
    "impairment",
    "strategic repositioning",
    "one-time",
    "one time",
    "non-recurring",
    "nonrecurring",
    "unusual",
    "adjusted ebitda",
    "adjusted eps",
    "excluded costs",
)

_SNIPPET_RADIUS = 70


def _pattern(term: str) -> re.Pattern[str]:
    return re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)


@dataclass
class PeriodTermCounts:
    fiscal_label: str
    counts: dict[str, int] = field(default_factory=dict)
    snippets: dict[str, list[str]] = field(default_factory=dict)
    total_words: int = 0


def count_terms_by_period(documents: list[DocumentRecord]) -> list[PeriodTermCounts]:
    """Aggregate term counts per fiscal period, ascending by appearance order
    of labels in the document list (caller supplies chronological documents)."""
    by_label: dict[str, PeriodTermCounts] = {}
    order: list[str] = []
    for doc in documents:
        if doc.fiscal_label not in by_label:
            by_label[doc.fiscal_label] = PeriodTermCounts(fiscal_label=doc.fiscal_label)
            order.append(doc.fiscal_label)
        ptc = by_label[doc.fiscal_label]
        ptc.total_words += len(doc.text.split())
        for term in ADJUSTMENT_TERMS:
            matches = list(_pattern(term).finditer(doc.text))
            if not matches:
                continue
            ptc.counts[term] = ptc.counts.get(term, 0) + len(matches)
            snips = ptc.snippets.setdefault(term, [])
            if len(snips) < 2:  # cap evidence per term per period
                m = matches[0]
                start = max(0, m.start() - _SNIPPET_RADIUS)
                end = min(len(doc.text), m.end() + _SNIPPET_RADIUS)
                snips.append("…" + " ".join(doc.text[start:end].split()) + "…")
    return [by_label[label] for label in order]


@dataclass
class AdjustmentAnalysis:
    periods_analyzed: int
    recurring_terms: dict[str, int]  # term -> number of periods it appeared in
    latest_density_per_1k_words: float | None
    recurrence_ratio: float | None  # fraction of periods with >= 1 adjustment term
    findings: list[NarrativeFinding]


def analyze(documents: list[DocumentRecord], recurrence_threshold: int = 3) -> AdjustmentAnalysis:
    period_counts = count_terms_by_period(documents)
    n = len(period_counts)
    if n == 0:
        return AdjustmentAnalysis(0, {}, None, None, [])

    term_periods: dict[str, int] = defaultdict(int)
    for ptc in period_counts:
        for term in ptc.counts:
            term_periods[term] += 1

    recurring = {t: c for t, c in term_periods.items() if c >= recurrence_threshold}
    periods_with_any = sum(1 for p in period_counts if p.counts)
    latest = period_counts[-1]
    density = (
        sum(latest.counts.values()) / latest.total_words * 1000
        if latest.total_words > 0
        else None
    )

    findings: list[NarrativeFinding] = []
    for term, period_hits in sorted(recurring.items(), key=lambda kv: -kv[1]):
        evidence: list[str] = []
        for ptc in period_counts:
            for s in ptc.snippets.get(term, [])[:1]:
                evidence.append(f"[{ptc.fiscal_label}] {s}")
        findings.append(
            NarrativeFinding(
                kind="adjustment_recurrence",
                detail=(
                    f'Adjustment term "{term}" appears in {period_hits} of {n} analyzed '
                    "periods; repeated presentation of items as non-recurring warrants review."
                ),
                fiscal_label=latest.fiscal_label,
                evidence_snippets=evidence[:4],
            )
        )

    return AdjustmentAnalysis(
        periods_analyzed=n,
        recurring_terms=recurring,
        latest_density_per_1k_words=density,
        recurrence_ratio=periods_with_any / n,
        findings=findings,
    )
