"""Narrative evidence ledger assembly.

Central rule (docs/evidence_policy.md): every narrative claim the engine makes
must be traceable to a ledger entry carrying the verbatim excerpt, source,
period, comparison basis, and any linked deterministic metrics. Detectors emit
raw analyses; this module turns them into numbered ledger entries.
"""

from __future__ import annotations

import re

from app.schemas.financials import DocumentRecord
from app.schemas.report import NarrativeEvidence

# Labels for a row whose provenance cannot be named precisely. Each says WHY,
# so none reads as if a filing had been identified.
NO_SOURCE_RECORDED = "period documents (no source recorded)"
NOT_LOCATED = "period documents (excerpt not located in a single document)"

# `[FY2025Q1] ` — the period tag adjustment-language snippets carry.
_LABEL_PREFIX = re.compile(r"^\[([^\]]+)\]\s*")


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _fragments(excerpt: str) -> list[tuple[str | None, str]]:
    """The verbatim windows inside an excerpt, each with the period it is
    tagged with (or None). Detectors cut windows as
    `"…" + " ".join(text[a:b].split()) + "…"`; adjustment evidence joins
    several, each prefixed `[FY2025Q1] `, with " | "."""
    out = []
    for part in excerpt.split(" | "):
        part = part.strip()
        m = _LABEL_PREFIX.match(part)
        label = m.group(1) if m else None
        text = _normalize(_LABEL_PREFIX.sub("", part).strip("…"))
        if text:
            out.append((label, text))
    return out


class EvidenceLedger:
    """Numbered ledger rows. Given the documents the detectors read, it also
    names which filing each row came from (`attribute`, `derived_from`)
    instead of the undifferentiated "period documents" every row used to
    claim — although each DocumentRecord already carried its form and
    accession."""

    def __init__(self, documents: list[DocumentRecord] | None = None) -> None:
        self._entries: list[NarrativeEvidence] = []
        self._documents = list(documents or [])
        self._normalized: list[str] | None = None

    def _texts(self) -> list[str]:
        if self._normalized is None:
            self._normalized = [_normalize(d.text) for d in self._documents]
        return self._normalized

    @staticmethod
    def _join(docs: list[DocumentRecord]) -> str:
        """Distinct sources in document order. Callers check first that every
        document has one: naming only some would claim more than is known."""
        sources: list[str] = []
        for d in docs:
            if d.source and d.source not in sources:
                sources.append(d.source)
        return "; ".join(sources)

    def attribute(self, excerpt: str, fiscal_label: str | None = None) -> str:
        """The filings a verbatim excerpt was taken from. Each window is
        looked for only in the documents of its own period — the `[label]`
        it is tagged with, else `fiscal_label` — because boilerplate repeats
        quarter to quarter and a sentence found in every 10-Q was still cut
        from one. The row's source is every document holding one of its
        windows, in document order. A window found in no single document of
        its period (one straddling two concatenated sections) is reported as
        not located rather than guessed."""
        fragments = _fragments(excerpt)
        if not fragments:
            return NOT_LOCATED
        texts = self._texts()
        matched: set[int] = set()
        for label, fragment in fragments:
            period = label or fiscal_label
            hits = {
                i for i, (d, t) in enumerate(zip(self._documents, texts))
                if fragment in t and (period is None or d.fiscal_label == period)
            }
            if not hits:
                return NOT_LOCATED
            matched |= hits
        docs = [self._documents[i] for i in sorted(matched)]
        if any(not d.source for d in docs):
            return NO_SOURCE_RECORDED
        return self._join(docs)

    def derived_from(self, fiscal_label: str) -> str:
        """For a row whose text is a computed statement about a period (a KPI
        dropped, disclosure volume fell), not a quotation: the documents of
        that period it was computed from."""
        docs = [d for d in self._documents if d.fiscal_label == fiscal_label]
        if not docs or any(not d.source for d in docs):
            return f"derived from {fiscal_label} documents (no source recorded)"
        return f"derived from {fiscal_label} documents: {self._join(docs)}"

    def add(
        self,
        detector: str,
        fiscal_label: str,
        comparison: str,
        source: str,
        excerpt: str,
        detail: str,
        confidence: str,
        linked_metrics: list[str] | None = None,
    ) -> NarrativeEvidence:
        entry = NarrativeEvidence(
            evidence_id=f"NE-{len(self._entries) + 1:03d}",
            detector=detector,
            fiscal_label=fiscal_label,
            comparison=comparison,
            source=source,
            excerpt=excerpt[:400],
            linked_metrics=linked_metrics or [],
            confidence=confidence,
            detail=detail,
        )
        self._entries.append(entry)
        return entry

    @property
    def entries(self) -> list[NarrativeEvidence]:
        return list(self._entries)

    def ids(self) -> set[str]:
        return {e.evidence_id for e in self._entries}
