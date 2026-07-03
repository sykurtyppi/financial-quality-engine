"""v0.4 narrative layer tests: baselines, detectors, KPI definition changes,
mismatches, evidence ledger, and the LLM grounding validator."""

import pytest

from app.schemas.financials import DocumentRecord, DocumentType
from app.schemas.metrics import MetricResult, MetricStatus
from app.schemas.report import NarrativeEvidence
from app.services.narrative import kpi_drift
from app.services.narrative.baselines import build_comparison, group_documents
from app.services.narrative.detectors import (
    detect_defensive_tone,
    detect_guidance_shift,
    detect_risk_factor_changes,
)
from app.services.narrative.evidence import EvidenceLedger
from app.services.narrative.grounding import (
    GroundedAnnotation,
    validate_annotations,
)
from app.services.narrative.mismatch import detect_mismatches
from app.services.narrative.narrative_metrics import compute_narrative_layer


def doc(label: str, text: str, doc_type: DocumentType = DocumentType.EARNINGS_RELEASE) -> DocumentRecord:
    return DocumentRecord(fiscal_label=label, doc_type=doc_type, text=text)


def ok_metric(name: str, value: float) -> MetricResult:
    return MetricResult(name=name, formula="f", fiscal_label="FY2025Q4",
                        status=MetricStatus.OK, value=value)


class TestBaselines:
    def test_yoy_and_qoq_selection(self):
        docs = [doc(f"FY2024Q{q}", "text") for q in (1, 2, 3, 4)]
        docs += [doc(f"FY2025Q{q}", "text") for q in (1, 2, 3)]
        cmp = build_comparison(group_documents(docs))
        assert cmp.current.fiscal_label == "FY2025Q3"
        assert cmp.qoq.fiscal_label == "FY2025Q2"
        assert cmp.yoy.fiscal_label == "FY2024Q3"
        assert len(cmp.trailing) == 6

    def test_doc_types_kept_separate(self):
        docs = [
            doc("FY2025Q1", "mdna text here", DocumentType.MDNA),
            doc("FY2025Q1", "risk text here", DocumentType.RISK_FACTORS),
        ]
        periods = group_documents(docs)
        assert periods[0].by_type[DocumentType.MDNA] == "mdna text here"
        assert periods[0].by_type[DocumentType.RISK_FACTORS] == "risk text here"


class TestGuidanceShift:
    def test_deterioration_detected(self):
        docs = [
            doc("FY2025Q3", "We are raising our full-year guidance and reaffirm our outlook."),
            doc("FY2025Q4", "We are lowering our full-year guidance given the outlook."),
        ]
        cmp = build_comparison(group_documents(docs))
        g = detect_guidance_shift(cmp)
        assert g.guidance_discussed
        assert g.prior_stance > 0 > g.current_stance
        assert g.shift > 0
        assert g.excerpts


class TestDefensiveTone:
    def test_tone_increase_vs_trailing_baseline(self):
        calm = "Revenue grew and customers were happy. " * 20
        defensive = ("The macro environment remains challenging with significant headwinds "
                     "and uncertainty creating pressure. ") * 20
        docs = [doc(f"FY2024Q{q}", calm) for q in (1, 2, 3, 4)] + [doc("FY2025Q1", defensive)]
        cmp = build_comparison(group_documents(docs))
        t = detect_defensive_tone(cmp)
        assert t.change is not None and t.change > 1.0
        assert t.excerpts


class TestRiskFactors:
    def test_expansion_prefers_yoy(self):
        docs = [
            doc("FY2024Q4", "risk " * 1000, DocumentType.RISK_FACTORS),
            doc("FY2025Q3", "risk " * 500, DocumentType.RISK_FACTORS),
            doc("FY2025Q4", "risk " * 1500, DocumentType.RISK_FACTORS),
        ]
        cmp = build_comparison(group_documents(docs))
        r = detect_risk_factor_changes(cmp)
        assert r.comparison_basis == "yoy"
        assert r.expansion_ratio == pytest.approx(1.5)

    def test_high_severity_term_emergence(self):
        docs = [
            doc("FY2025Q3", "Ordinary risks apply to our business.", DocumentType.RISK_FACTORS),
            doc("FY2025Q4", "We identified a material weakness in internal control.", DocumentType.RISK_FACTORS),
        ]
        cmp = build_comparison(group_documents(docs))
        r = detect_risk_factor_changes(cmp)
        assert "material weakness" in r.new_high_severity_terms
        assert r.excerpts

    def test_preexisting_term_not_flagged_as_new(self):
        docs = [
            doc("FY2025Q3", "We previously disclosed a material weakness.", DocumentType.RISK_FACTORS),
            doc("FY2025Q4", "The material weakness remediation continues.", DocumentType.RISK_FACTORS),
        ]
        cmp = build_comparison(group_documents(docs))
        assert detect_risk_factor_changes(cmp).new_high_severity_terms == []


class TestKpiDefinitionChange:
    def test_changed_definition_flagged(self):
        prior = ("Adjusted EBITDA is defined as net income excluding interest, taxes, "
                 "depreciation and amortization.")
        current = ("Adjusted EBITDA is defined as net income excluding interest, taxes, "
                   "depreciation, amortization, restructuring charges, litigation costs, "
                   "impairments and stock-based compensation expense.")
        changes = kpi_drift.detect_definition_changes(
            {"Q1": prior, "Q2": current}, ["Q1", "Q2"]
        )
        assert len(changes) == 1
        assert changes[0].kpi == "Adjusted EBITDA"
        assert changes[0].similarity < 0.55

    def test_stable_definition_not_flagged(self):
        text = "Adjusted EBITDA is defined as net income excluding interest and taxes."
        assert kpi_drift.detect_definition_changes({"Q1": text, "Q2": text}, ["Q1", "Q2"]) == []


class TestMismatches:
    def _cmp(self, text: str):
        return build_comparison(group_documents([doc("FY2025Q3", "prior text"), doc("FY2025Q4", text)]))

    def test_demand_vs_working_capital(self):
        cmp = self._cmp("Demand remains strong across all segments. Demand remains strong.")
        metrics = {"receivables_growth_spread": ok_metric("receivables_growth_spread", 0.30)}
        concerns = {"receivables_growth_spread": 74.0}
        ledger = EvidenceLedger()
        out = detect_mismatches(cmp, metrics, concerns, ledger)
        assert len(out) == 1
        assert out[0].kind == "demand_narrative_vs_working_capital"
        assert out[0].confidence == "high"  # concern >= 70 and multiple mentions
        assert out[0].narrative_evidence_id in ledger.ids()

    def test_buyback_vs_share_count_uses_raw_value(self):
        cmp = self._cmp("Our share repurchase program returned capital to shareholders.")
        metrics = {"net_share_count_change": ok_metric("net_share_count_change", 0.02)}
        out = detect_mismatches(cmp, metrics, {"net_share_count_change": 40.0}, EvidenceLedger())
        assert len(out) == 1
        assert out[0].metric_values["net_share_count_change"] == pytest.approx(0.02)

    def test_no_mismatch_when_shares_shrink(self):
        cmp = self._cmp("Our share repurchase program returned capital to shareholders.")
        metrics = {"net_share_count_change": ok_metric("net_share_count_change", -0.01)}
        assert detect_mismatches(cmp, metrics, {}, EvidenceLedger()) == []

    def test_immaterial_share_uptick_not_flagged(self):
        """Vesting-timing noise (live finding on AAPL: +0.04% quarter) must not
        contradict a buyback narrative."""
        cmp = self._cmp("Our share repurchase program returned capital to shareholders.")
        metrics = {"net_share_count_change": ok_metric("net_share_count_change", 0.0004)}
        assert detect_mismatches(cmp, metrics, {}, EvidenceLedger()) == []

    def test_no_mismatch_without_narrative(self):
        cmp = self._cmp("A quiet quarter with nothing notable said.")
        metrics = {"receivables_growth_spread": ok_metric("receivables_growth_spread", 0.30)}
        assert detect_mismatches(cmp, metrics, {"receivables_growth_spread": 90.0}, EvidenceLedger()) == []


class TestNarrativeLayer:
    def test_disclosure_reduction_finding(self):
        docs = [doc(f"FY2025Q{q}", "words " * 400) for q in (1, 2, 3)] + [doc("FY2025Q4", "words " * 100)]
        layer = compute_narrative_layer(docs)
        kinds = {f.kind for f in layer.findings}
        assert "disclosure_reduction" in kinds
        by_name = {m.name: m for m in layer.metrics}
        assert by_name["disclosure_volume_change"].value == pytest.approx(0.25)

    def test_evidence_ids_sequential_and_unique(self):
        docs = [
            doc(f"FY2025Q{q}", "One-time restructuring charges again. Net revenue retention was strong.")
            for q in (1, 2, 3)
        ] + [doc("FY2025Q4", "One-time restructuring charges again.")]
        layer = compute_narrative_layer(docs)
        ids = [e.evidence_id for e in layer.evidence]
        assert len(ids) == len(set(ids))
        assert ids == sorted(ids)

    def test_no_documents_reports_all_missing(self):
        layer = compute_narrative_layer([])
        assert all(m.status is MetricStatus.MISSING_DATA for m in layer.metrics)
        assert layer.evidence == []
        assert layer.mismatches == []


class TestGrounding:
    LEDGER = [
        NarrativeEvidence(
            evidence_id="NE-001", detector="adjustment_recurrence", fiscal_label="FY2025Q4",
            comparison="trailing8", source="docs", excerpt="restructuring charges of 42 million",
            confidence="high", detail="term recurred in 4 of 4 periods",
        )
    ]

    def test_valid_annotation_passes(self):
        ann = GroundedAnnotation(
            evidence_ids=["NE-001"], classification="narrative_drift",
            explanation="Restructuring charges of 42 million recurred across 4 periods.",
        )
        assert validate_annotations([ann], self.LEDGER) == []

    def test_unknown_evidence_id_rejected(self):
        ann = GroundedAnnotation(evidence_ids=["NE-999"], classification="narrative_drift", explanation="x")
        errors = validate_annotations([ann], self.LEDGER)
        assert any(e.rule == "unknown_evidence_id" for e in errors)

    def test_banned_vocabulary_rejected(self):
        ann = GroundedAnnotation(
            evidence_ids=["NE-001"], classification="narrative_drift",
            explanation="This suggests manipulation of results.",
        )
        errors = validate_annotations([ann], self.LEDGER)
        assert any(e.rule == "banned_vocabulary" for e in errors)

    def test_ungrounded_number_rejected(self):
        ann = GroundedAnnotation(
            evidence_ids=["NE-001"], classification="elevated_concern",
            explanation="Charges of 97 million recurred.",  # 97 appears nowhere
        )
        errors = validate_annotations([ann], self.LEDGER)
        assert any(e.rule == "ungrounded_number" for e in errors)

    def test_invalid_classification_rejected(self):
        ann = GroundedAnnotation(evidence_ids=["NE-001"], classification="fraud_risk", explanation="x")
        errors = validate_annotations([ann], self.LEDGER)
        assert any(e.rule == "invalid_classification" for e in errors)
