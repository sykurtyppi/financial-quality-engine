"""Period-comparison discipline for narrative analysis.

Selects comparison periods from the documented set:
- QoQ:       the immediately preceding documented period
- YoY:       same fiscal quarter one year earlier (label-matched, FYnnnnQq)
- trailing8: up to 8 documented periods preceding the current one

Documents are grouped by fiscal label; text within a label is concatenated
per document type so detectors can compare like with like (a 10-K risk-factor
section is compared to the prior 10-K's, not to an earnings release).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.schemas.financials import DocumentRecord, DocumentType

_LABEL_RE = re.compile(r"^FY(\d{4})Q([1-4])$")


@dataclass
class PeriodDocs:
    fiscal_label: str
    by_type: dict[DocumentType, str] = field(default_factory=dict)
    sources: dict[DocumentType, str] = field(default_factory=dict)

    @property
    def all_text(self) -> str:
        return "\n".join(self.by_type.values())

    def word_count(self, doc_type: DocumentType | None = None) -> int:
        if doc_type is None:
            return len(self.all_text.split())
        return len(self.by_type.get(doc_type, "").split())


def group_documents(documents: list[DocumentRecord]) -> list[PeriodDocs]:
    """Group by fiscal label, preserving first-appearance order (callers
    supply documents chronologically)."""
    by_label: dict[str, PeriodDocs] = {}
    order: list[str] = []
    for doc in documents:
        if doc.fiscal_label not in by_label:
            by_label[doc.fiscal_label] = PeriodDocs(fiscal_label=doc.fiscal_label)
            order.append(doc.fiscal_label)
        pd = by_label[doc.fiscal_label]
        existing = pd.by_type.get(doc.doc_type, "")
        pd.by_type[doc.doc_type] = (existing + "\n" + doc.text).strip()
        if doc.source and doc.doc_type not in pd.sources:
            pd.sources[doc.doc_type] = doc.source
    return [by_label[label] for label in order]


@dataclass
class ComparisonSet:
    current: PeriodDocs
    qoq: PeriodDocs | None
    yoy: PeriodDocs | None
    trailing: list[PeriodDocs]  # up to 8 periods before current, oldest first


def _yoy_label(label: str) -> str | None:
    m = _LABEL_RE.match(label)
    if not m:
        return None
    return f"FY{int(m.group(1)) - 1}Q{m.group(2)}"


def build_comparison(periods: list[PeriodDocs]) -> ComparisonSet | None:
    """Comparison set for the latest documented period."""
    if not periods:
        return None
    current = periods[-1]
    prior = periods[:-1]
    qoq = prior[-1] if prior else None
    yoy_label = _yoy_label(current.fiscal_label)
    yoy = next((p for p in prior if p.fiscal_label == yoy_label), None)
    return ComparisonSet(current=current, qoq=qoq, yoy=yoy, trailing=prior[-8:])
