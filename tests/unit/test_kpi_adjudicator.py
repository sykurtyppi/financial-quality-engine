"""Offline tests for the KPI adjudicator abstraction and deterministic fallback."""

from app.services.narrative.kpi_adjudicator import (
    Adjudication,
    DeterministicAdjudicator,
    Direction,
    Materiality,
)
from app.services.narrative.kpi_extraction import KpiDefinitionPair


def pair(sim: float, change_type: str = "redefinition") -> KpiDefinitionPair:
    return KpiDefinitionPair(
        evidence_id="KDP-001", kpi="Adjusted EBITDA", change_type=change_type,
        prior_period="Q1", prior_definition="a", current_period="Q2",
        current_definition="b" if change_type == "redefinition" else "",
        prefilter_similarity=sim,
    )


class TestDeterministicAdjudicator:
    def test_low_similarity_is_material(self):
        adj = DeterministicAdjudicator().adjudicate(pair(0.30))
        assert adj.materiality is Materiality.CHANGED_BASIS
        assert adj.is_material

    def test_high_similarity_not_material(self):
        adj = DeterministicAdjudicator().adjudicate(pair(0.80))
        assert adj.materiality is Materiality.NO_MATERIAL_CHANGE
        assert not adj.is_material

    def test_threshold_boundary(self):
        # 0.55 is the boundary; at/above is not material.
        assert not DeterministicAdjudicator().adjudicate(pair(0.55)).is_material
        assert DeterministicAdjudicator().adjudicate(pair(0.54)).is_material

    def test_drop_is_material_narrowed_scope(self):
        adj = DeterministicAdjudicator().adjudicate(pair(0.0, "drop"))
        assert adj.materiality is Materiality.NARROWED_SCOPE
        assert adj.is_material


class TestAdjudicationModel:
    def test_material_labels(self):
        for m in (Materiality.BROADENED_EXCLUSIONS, Materiality.NARROWED_SCOPE, Materiality.CHANGED_BASIS):
            a = Adjudication(evidence_id="x", materiality=m, direction=Direction.UNCLEAR)
            assert a.is_material
        for m in (Materiality.NO_MATERIAL_CHANGE, Materiality.AMBIGUOUS):
            a = Adjudication(evidence_id="x", materiality=m, direction=Direction.UNCLEAR)
            assert not a.is_material
