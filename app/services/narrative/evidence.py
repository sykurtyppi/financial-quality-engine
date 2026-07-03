"""Narrative evidence ledger assembly.

Central rule (docs/evidence_policy.md): every narrative claim the engine makes
must be traceable to a ledger entry carrying the verbatim excerpt, source,
period, comparison basis, and any linked deterministic metrics. Detectors emit
raw analyses; this module turns them into numbered ledger entries.
"""

from __future__ import annotations

from app.schemas.report import NarrativeEvidence


class EvidenceLedger:
    def __init__(self) -> None:
        self._entries: list[NarrativeEvidence] = []

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
