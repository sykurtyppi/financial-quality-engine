"""LLM grounding contract for narrative annotation.

The LLM layer (future, vendor-agnostic) may EXPLAIN and CLASSIFY evidence; it
may not invent facts. This module makes that rule enforceable rather than
aspirational: any annotation an LLM produces must pass `validate_annotations`
before it can enter a report. Violations are rejected with reasons — they are
never silently repaired.

Enforced rules:
1. Every annotation must cite at least one KNOWN evidence-ledger id.
2. Classifications must come from the fixed vocabulary.
3. Banned vocabulary (accusatory language) is rejected outright.
4. Every number appearing in the explanation must appear in the cited
   evidence excerpts/details or in the linked metric values — numbers from
   nowhere are the canonical hallucination and are rejected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.schemas.metrics import MetricResult
from app.schemas.report import NarrativeEvidence
from pydantic import BaseModel, Field

ALLOWED_CLASSIFICATIONS = frozenset(
    {
        "requires_review",
        "elevated_concern",
        "narrative_drift",
        "presentation_risk",
        "industry_normal_candidate",
        "model_artifact_candidate",
        "supportive",
    }
)

BANNED_TERMS = (
    "fraud",
    "fraudulent",
    "manipulation",
    "manipulating",
    "manipulated",
    "deceptive",
    "deceit",
    "cooking the books",
    "engineered beat",
    "misleading investors",
)

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")


class GroundedAnnotation(BaseModel):
    """One LLM-produced annotation over ledger evidence."""

    evidence_ids: list[str] = Field(min_length=1)
    classification: str
    explanation: str


@dataclass(frozen=True)
class ValidationError:
    annotation_index: int
    rule: str
    detail: str


def _numbers_in(text: str) -> set[str]:
    # Normalize by stripping separators so "1,234" == "1234" and "38%" -> "38".
    return {m.group().replace(",", "") for m in _NUMBER_RE.finditer(text)}


def validate_annotations(
    annotations: list[GroundedAnnotation],
    ledger_entries: list[NarrativeEvidence],
    metrics: list[MetricResult] | None = None,
) -> list[ValidationError]:
    """Return all violations; an empty list means every annotation is usable."""
    errors: list[ValidationError] = []
    by_id = {e.evidence_id: e for e in ledger_entries}
    metric_number_pool: set[str] = set()
    for m in metrics or []:
        if m.value is not None:
            metric_number_pool |= _numbers_in(f"{m.value:.6g}")
            metric_number_pool |= _numbers_in(f"{m.value:.2f}")
            metric_number_pool |= _numbers_in(f"{m.value:.1%}")
        for v in m.inputs.values():
            if v is not None:
                metric_number_pool |= _numbers_in(f"{v:.6g}")

    for i, ann in enumerate(annotations):
        unknown = [eid for eid in ann.evidence_ids if eid not in by_id]
        if unknown:
            errors.append(
                ValidationError(i, "unknown_evidence_id", f"cites nonexistent ids: {unknown}")
            )
            continue

        if ann.classification not in ALLOWED_CLASSIFICATIONS:
            errors.append(
                ValidationError(
                    i, "invalid_classification", f'"{ann.classification}" not in allowed vocabulary'
                )
            )

        low = ann.explanation.lower()
        hits = [t for t in BANNED_TERMS if t in low]
        if hits:
            errors.append(
                ValidationError(i, "banned_vocabulary", f"contains banned terms: {hits}")
            )

        allowed_numbers = set(metric_number_pool)
        for eid in ann.evidence_ids:
            entry = by_id[eid]
            allowed_numbers |= _numbers_in(entry.excerpt)
            allowed_numbers |= _numbers_in(entry.detail)
        foreign = _numbers_in(ann.explanation) - allowed_numbers
        if foreign:
            errors.append(
                ValidationError(
                    i,
                    "ungrounded_number",
                    f"numbers not present in cited evidence or metrics: {sorted(foreign)}",
                )
            )
    return errors
