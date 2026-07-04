"""KPI-drift materiality adjudication (design stage 3 interface + fallback).

An adjudicator judges whether a candidate KPI-definition change is MATERIAL
(the metric now measures something different) or cosmetic. The deterministic
adjudicator here is the fallback and the Phase-2 baseline; the LLM adjudicator
(Phase 3) implements the same interface.

Nothing here searches documents or invents facts — an adjudicator receives one
already-extracted pair and returns a structured judgment over it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ValidationError

from app.services.narrative.grounding import (
    BANNED_TERMS,
    GroundedAnnotation,
    validate_annotations,
)
from app.schemas.report import NarrativeEvidence
from app.services.narrative.kpi_extraction import KpiDefinitionPair

logger = logging.getLogger(__name__)


class Materiality(str, Enum):
    NO_MATERIAL_CHANGE = "no_material_change"
    BROADENED_EXCLUSIONS = "broadened_exclusions"
    NARROWED_SCOPE = "narrowed_scope"
    CHANGED_BASIS = "changed_basis"
    AMBIGUOUS = "ambiguous_requires_review"


class Direction(str, Enum):
    MORE_FLATTERING = "more_flattering"
    LESS_FLATTERING = "less_flattering"
    NEUTRAL = "neutral"
    UNCLEAR = "unclear"


MATERIAL_LABELS = {
    Materiality.BROADENED_EXCLUSIONS,
    Materiality.NARROWED_SCOPE,
    Materiality.CHANGED_BASIS,
}


class Adjudication(BaseModel):
    evidence_id: str
    materiality: Materiality
    direction: Direction
    changed_clause: str = ""
    explanation: str = ""
    confidence: str = "low"

    @property
    def is_material(self) -> bool:
        return self.materiality in MATERIAL_LABELS


class Adjudicator(Protocol):
    def adjudicate(self, pair: KpiDefinitionPair) -> Adjudication | None: ...


class DeterministicAdjudicator:
    """The pre-LLM baseline: token-similarity threshold, reproducing the original
    `kpi_drift.detect_definition_changes` behavior (material if similarity < 0.55).
    This is what the LLM adjudicator must beat on precision."""

    def __init__(self, threshold: float = 0.55) -> None:
        self.threshold = threshold

    def adjudicate(self, pair: KpiDefinitionPair) -> Adjudication:
        if pair.change_type == "drop":
            materiality = Materiality.NARROWED_SCOPE
        elif pair.prefilter_similarity < self.threshold:
            materiality = Materiality.CHANGED_BASIS
        else:
            materiality = Materiality.NO_MATERIAL_CHANGE
        return Adjudication(
            evidence_id=pair.evidence_id,
            materiality=materiality,
            direction=Direction.UNCLEAR,
            explanation=f"deterministic token-similarity {pair.prefilter_similarity:.2f}",
            confidence="low",
        )
