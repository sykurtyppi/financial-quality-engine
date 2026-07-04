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


# --- LLM adjudicator (design stage 3) --------------------------------------

SYSTEM_PROMPT = (
    "You are given the prior and current definitions of ONE disclosed financial "
    "metric for ONE company. Judge only whether, and how, the definition materially "
    "changed.\n"
    "Rules:\n"
    "- Use ONLY the text provided. Do not use any outside knowledge of the company.\n"
    "- You do not know and must not guess anything about the company's future, "
    "performance, or any restatement.\n"
    "- Do not assert intent, wrongdoing, fraud, or manipulation. Describe the change "
    "mechanically.\n"
    "- Quote, in changed_clause, the specific phrase that changed, copied verbatim from "
    "one of the provided definitions. Do not introduce numbers absent from the definitions.\n"
    "- materiality is one of: no_material_change, broadened_exclusions, narrowed_scope, "
    "changed_basis, ambiguous_requires_review.\n"
    "- direction is one of: more_flattering, less_flattering, neutral, unclear.\n"
    "- Output the structured fields only."
)

# materiality -> concern classification (must be in grounding ALLOWED_CLASSIFICATIONS)
_CLASSIFICATION = {
    Materiality.BROADENED_EXCLUSIONS: "presentation_risk",
    Materiality.CHANGED_BASIS: "presentation_risk",
    Materiality.NARROWED_SCOPE: "narrative_drift",
    Materiality.AMBIGUOUS: "requires_review",
    Materiality.NO_MATERIAL_CHANGE: "supportive",
}

CompletionFn = Callable[[str, str], dict | None]


def build_user_prompt(pair: KpiDefinitionPair) -> str:
    return (
        f"Metric: {pair.kpi}\n"
        f'Prior definition ({pair.prior_period}): "{pair.prior_definition}"\n'
        f'Current definition ({pair.current_period}): "{pair.current_definition}"'
    )


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


class LlmAdjudicator:
    """LLM materiality judge over a single pre-extracted pair. Blind to outcomes,
    grounded, cached, and degradable to the deterministic adjudicator.

    `complete(system, user) -> dict | None` is the injected, provider-agnostic
    model call (temperature 0, structured output). Returns a dict with keys
    materiality/direction/changed_clause/explanation/confidence, or None on failure.
    """

    def __init__(
        self,
        complete: CompletionFn,
        model_id: str,
        cache_dir: str | Path = "data/cache/kpi_adjudications",
        fallback: DeterministicAdjudicator | None = None,
    ) -> None:
        self.complete = complete
        self.model_id = model_id
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.fallback = fallback or DeterministicAdjudicator()

    def _cache_key(self, system: str, user: str) -> str:
        h = hashlib.sha256(f"{self.model_id}\n{system}\n{user}".encode()).hexdigest()
        return h

    def _cached(self, key: str) -> dict | None:
        p = self.cache_dir / f"{key}.json"
        return json.loads(p.read_text()) if p.exists() else None

    def _store(self, key: str, raw: dict) -> None:
        (self.cache_dir / f"{key}.json").write_text(json.dumps(raw, sort_keys=True))

    def _degrade(self, pair: KpiDefinitionPair, reason: str) -> Adjudication:
        logger.warning("kpi adjudicator degraded (%s) for %s: %s", self.model_id, pair.evidence_id, reason)
        adj = self.fallback.adjudicate(pair)
        return adj.model_copy(update={"confidence": "low"})

    def adjudicate(self, pair: KpiDefinitionPair) -> Adjudication:
        user = build_user_prompt(pair)
        key = self._cache_key(SYSTEM_PROMPT, user)
        raw = self._cached(key)
        if raw is None:
            try:
                raw = self.complete(SYSTEM_PROMPT, user)
            except Exception as e:  # noqa: BLE001 - any model failure -> fallback
                return self._degrade(pair, f"call failed: {e}")
            if raw is None:
                return self._degrade(pair, "no completion")
            self._store(key, raw)

        try:
            adj = Adjudication(
                evidence_id=pair.evidence_id,
                materiality=Materiality(raw["materiality"]),
                direction=Direction(raw.get("direction", "unclear")),
                changed_clause=raw.get("changed_clause", ""),
                explanation=raw.get("explanation", ""),
                confidence=raw.get("confidence", "medium"),
            )
        except (KeyError, ValueError, ValidationError) as e:
            return self._degrade(pair, f"parse: {e}")

        violation = self._validate(pair, adj)
        if violation:
            return self._degrade(pair, violation)
        return adj

    def _validate(self, pair: KpiDefinitionPair, adj: Adjudication) -> str | None:
        """Grounding + changed_clause substring. Returns a violation reason or None."""
        low_expl = adj.explanation.lower()
        hits = [t for t in BANNED_TERMS if t in low_expl]
        if hits:
            return f"banned vocabulary {hits}"

        # changed_clause must be copied from one of the definitions (anti-hallucination).
        if adj.materiality in MATERIAL_LABELS and adj.changed_clause:
            combined = _normalize(pair.prior_definition + " " + pair.current_definition)
            if _normalize(adj.changed_clause) not in combined:
                return "changed_clause not a substring of the definitions"

        # Number grounding via the existing validator: numbers in the explanation
        # must appear in the definitions.
        evidence = NarrativeEvidence(
            evidence_id=pair.evidence_id, detector="kpi_definition_change",
            fiscal_label=pair.current_period, comparison="qoq", source="period documents",
            excerpt=(pair.prior_definition + " | " + pair.current_definition)[:400],
            confidence=adj.confidence, detail="KPI definition pair",
        )
        ann = GroundedAnnotation(
            evidence_ids=[pair.evidence_id],
            classification=_CLASSIFICATION[adj.materiality],
            explanation=adj.explanation or "(none)",
        )
        errors = validate_annotations([ann], [evidence], metrics=None)
        if errors:
            return "; ".join(f"{e.rule}:{e.detail}" for e in errors)
        return None
