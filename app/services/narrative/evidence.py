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

# `[FY2025Q1] ` — the period tag adjustment-language snippets carry. Only the
# two label shapes the mapper emits count: a filing's own "[1]" footnote
# marker at the start of a window is text, not a period.
_LABEL_PREFIX = re.compile(r"^\[(FY\d{4}Q[1-4]|P\d{4}-\d{2}-\d{2})\]\s*")

# Shortest centred core of a window worth attributing on its own: long
# enough that a match is the quoted passage, not a common phrase.
_MIN_CORE = 30


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _window(snippet: str) -> tuple[str | None, str]:
    """One verbatim window: its period tag (or None) and its normalized text.
    Detectors cut windows as `"…" + " ".join(text[a:b].split()) + "…"`. A
    window is never split further — a filing's own text may contain " | " or
    anything else."""
    snippet = snippet.strip()
    m = _LABEL_PREFIX.match(snippet)
    label = m.group(1) if m else None
    return label, _normalize(_LABEL_PREFIX.sub("", snippet).strip("…"))


def _cores(window: str) -> list[str]:
    """The window, then slices of it anchored at its centre and running right
    or left, longest first. Detectors centre a window on the matched term, so
    when the whole window straddles two concatenated documents (a match near
    a section boundary) the term — and a slice running away from the
    boundary — still lies inside the document it came from. No slice is
    shorter than _MIN_CORE: a short phrase matches anywhere."""
    out = [window]
    mid = len(window) // 2
    for size in (120, 90, 60, 45, _MIN_CORE):
        for back in (0, 4, 8):
            right = window[max(0, mid - back):max(0, mid - back) + size]
            left = window[max(0, mid + back - size):mid + back]
            out += [right, left]
    return [c for c in out if len(c) >= _MIN_CORE or c == window]


class EvidenceLedger:
    """Numbered ledger rows. Given the documents the detectors read, it also
    names which filing each row came from (`attribute`, `attribute_snippets`,
    `derived_from`) instead of the undifferentiated "period documents" every
    row used to claim — although each DocumentRecord already carried its form
    and accession."""

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

    def _locate(self, window: str, period: str | None) -> set[int] | None:
        """Indices of the documents of `period` holding the window — or, when
        no single document holds it whole, its centred core."""
        texts = self._texts()
        for core in _cores(window):
            hits = {
                i for i, (d, t) in enumerate(zip(self._documents, texts))
                if (period is None or d.fiscal_label == period) and core in t
            }
            if hits:
                return hits
        return None

    def _name(self, windows: list[tuple[str | None, str]], fiscal_label: str | None) -> str:
        matched: set[int] = set()
        for label, window in windows:
            if not window:
                continue
            hits = self._locate(window, label or fiscal_label)
            if hits is None:
                return NOT_LOCATED
            matched |= hits
        if not matched:
            return NOT_LOCATED
        docs = [self._documents[i] for i in sorted(matched)]
        if any(not d.source for d in docs):
            return NO_SOURCE_RECORDED
        return self._join(docs)

    def attribute(self, excerpt: str, fiscal_label: str | None = None) -> str:
        """The filing(s) one verbatim excerpt was cut from, looking only in
        the documents of `fiscal_label` — boilerplate repeats quarter to
        quarter, and a sentence found in every 10-Q was still cut from one. A
        window straddling two concatenated documents is attributed by its
        centre, where the detector's matched term sits; one that cannot be
        placed at all is reported as not located rather than guessed."""
        return self._name([_window(excerpt)], fiscal_label)

    def attribute_snippets(self, snippets: list[str]) -> str:
        """The filings behind several period-tagged snippets (`[FY2025Q1]
        …text…`), each looked up in its own period's documents and named in
        document order. Taking the snippets as a list — rather than
        re-splitting a joined excerpt — keeps a " | " inside a filing's text
        from being read as a boundary between snippets."""
        return self._name([_window(s) for s in snippets], None)

    def derived_from(self, fiscal_label: str, compared: list[str] | None = None) -> str:
        """For a row whose text is a computed statement, not a quotation (a
        KPI dropped, disclosure volume fell): the documents it was computed
        from — the period's own, and those of the periods it was compared
        against, which is where a dropped KPI's evidence actually is."""
        parts = []
        for label in [fiscal_label, *(compared or [])]:
            docs = [d for d in self._documents if d.fiscal_label == label]
            if not docs or any(not d.source for d in docs):
                parts.append(f"{label} documents (no source recorded)")
            else:
                parts.append(f"{label} documents: {self._join(docs)}")
        head, *rest = parts
        return "derived from " + head + "".join(f"; compared with {p}" for p in rest)

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
