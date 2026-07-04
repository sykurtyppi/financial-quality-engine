"""Phase 3: LLM adjudicator — grounding, substring check, cache, fallback.
Uses a stub completion function; no network."""

import pytest

from app.services.narrative.kpi_adjudicator import (
    LlmAdjudicator,
    Materiality,
)
from app.services.narrative.kpi_extraction import KpiDefinitionPair

PRIOR = "Adjusted EBITDA is net income before interest and taxes."
CURRENT = "Adjusted EBITDA is net income before interest, taxes, restructuring, and impairments of 42."


def make_pair() -> KpiDefinitionPair:
    return KpiDefinitionPair(
        evidence_id="KDP-001", kpi="Adjusted EBITDA", change_type="redefinition",
        prior_period="Q1", prior_definition=PRIOR, current_period="Q2",
        current_definition=CURRENT, prefilter_similarity=0.40,
    )


def adj(model_id, complete, tmp_path):
    return LlmAdjudicator(complete=complete, model_id=model_id, cache_dir=tmp_path)


class TestValidJudgments:
    def test_material_judgment_passes(self, tmp_path):
        def complete(system, user):
            return {"materiality": "broadened_exclusions", "direction": "more_flattering",
                    "changed_clause": "restructuring, and impairments",
                    "explanation": "Now also excludes restructuring and impairments.",
                    "confidence": "high"}
        a = adj("m", complete, tmp_path).adjudicate(make_pair())
        assert a.materiality is Materiality.BROADENED_EXCLUSIONS
        assert a.is_material
        assert a.confidence == "high"

    def test_cosmetic_judgment_suppressed(self, tmp_path):
        def complete(system, user):
            return {"materiality": "no_material_change", "direction": "neutral",
                    "changed_clause": "", "explanation": "Only wording changed."}
        a = adj("m", complete, tmp_path).adjudicate(make_pair())
        assert not a.is_material


class TestGroundingRejections:
    def test_banned_vocabulary_degrades(self, tmp_path):
        def complete(system, user):
            return {"materiality": "broadened_exclusions", "direction": "more_flattering",
                    "changed_clause": "restructuring, and impairments",
                    "explanation": "This is manipulation of the metric."}
        a = adj("m", complete, tmp_path).adjudicate(make_pair())
        # Degraded to deterministic fallback (low-sim pair -> changed_basis, low conf).
        assert a.confidence == "low"
        assert "manipulation" not in a.explanation.lower()

    def test_hallucinated_changed_clause_degrades(self, tmp_path):
        def complete(system, user):
            return {"materiality": "changed_basis", "direction": "more_flattering",
                    "changed_clause": "goodwill amortization never mentioned",
                    "explanation": "Basis changed."}
        a = adj("m", complete, tmp_path).adjudicate(make_pair())
        assert a.confidence == "low"  # degraded

    def test_ungrounded_number_degrades(self, tmp_path):
        def complete(system, user):
            return {"materiality": "changed_basis", "direction": "more_flattering",
                    "changed_clause": "restructuring, and impairments",
                    "explanation": "Excludes 999 of charges."}  # 999 not in definitions
        a = adj("m", complete, tmp_path).adjudicate(make_pair())
        assert a.confidence == "low"

    def test_grounded_number_passes(self, tmp_path):
        def complete(system, user):
            return {"materiality": "changed_basis", "direction": "more_flattering",
                    "changed_clause": "impairments of 42",
                    "explanation": "Now excludes impairments of 42."}  # 42 IS in current def
        a = adj("m", complete, tmp_path).adjudicate(make_pair())
        assert a.materiality is Materiality.CHANGED_BASIS
        assert a.confidence != "low" or a.explanation.startswith("Now")


class TestCacheAndFallback:
    def test_cache_avoids_second_call(self, tmp_path):
        calls = {"n": 0}
        def complete(system, user):
            calls["n"] += 1
            return {"materiality": "no_material_change", "direction": "neutral",
                    "changed_clause": "", "explanation": "wording"}
        a1 = adj("m", complete, tmp_path)
        a1.adjudicate(make_pair())
        a1.adjudicate(make_pair())  # same pair -> cache hit
        assert calls["n"] == 1

    def test_cache_key_includes_model_id(self, tmp_path):
        calls = {"n": 0}
        def complete(system, user):
            calls["n"] += 1
            return {"materiality": "no_material_change", "direction": "neutral", "changed_clause": "", "explanation": "x"}
        adj("model-A", complete, tmp_path).adjudicate(make_pair())
        adj("model-B", complete, tmp_path).adjudicate(make_pair())  # different model -> new call
        assert calls["n"] == 2

    def test_none_completion_falls_back(self, tmp_path):
        a = adj("m", lambda s, u: None, tmp_path).adjudicate(make_pair())
        assert a.confidence == "low"
        assert a.materiality is Materiality.CHANGED_BASIS  # fallback on sim=0.40

    def test_exception_falls_back(self, tmp_path):
        def boom(system, user):
            raise RuntimeError("model down")
        a = adj("m", boom, tmp_path).adjudicate(make_pair())
        assert a.confidence == "low"
