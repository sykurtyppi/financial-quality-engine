from app.schemas.financials import DocumentRecord, DocumentType
from app.schemas.metrics import MetricStatus
from app.services.narrative import adjustment_language, kpi_drift
from app.services.narrative.narrative_metrics import compute_narrative_metrics


def doc(label: str, text: str) -> DocumentRecord:
    return DocumentRecord(fiscal_label=label, doc_type=DocumentType.EARNINGS_RELEASE, text=text)


RESTRUCTURING_TEXT = (
    "This quarter includes one-time restructuring charges. Adjusted EBITDA excludes "
    "these non-recurring items."
)
CLEAN_TEXT = "Revenue grew and cash flow was strong. Customer demand remained healthy."


class TestAdjustmentLanguage:
    def test_recurrence_detected_across_periods(self):
        docs = [doc(f"Q{i}", RESTRUCTURING_TEXT) for i in range(1, 5)]
        analysis = adjustment_language.analyze(docs)
        assert analysis.periods_analyzed == 4
        assert analysis.recurrence_ratio == 1.0
        assert "restructuring" in analysis.recurring_terms
        assert analysis.recurring_terms["restructuring"] == 4
        assert any(f.kind == "adjustment_recurrence" for f in analysis.findings)

    def test_findings_carry_evidence_snippets(self):
        docs = [doc(f"Q{i}", RESTRUCTURING_TEXT) for i in range(1, 5)]
        analysis = adjustment_language.analyze(docs)
        finding = analysis.findings[0]
        assert finding.evidence_snippets
        assert any("Q1" in s for s in finding.evidence_snippets)

    def test_clean_text_produces_no_findings(self):
        docs = [doc(f"Q{i}", CLEAN_TEXT) for i in range(1, 5)]
        analysis = adjustment_language.analyze(docs)
        assert analysis.recurring_terms == {}
        assert analysis.findings == []

    def test_single_mention_is_not_recurrence(self):
        docs = [doc("Q1", RESTRUCTURING_TEXT)] + [doc(f"Q{i}", CLEAN_TEXT) for i in range(2, 5)]
        analysis = adjustment_language.analyze(docs)
        assert analysis.recurring_terms == {}

    def test_no_documents(self):
        analysis = adjustment_language.analyze([])
        assert analysis.periods_analyzed == 0
        assert analysis.recurrence_ratio is None


class TestKpiDrift:
    def test_kpi_removal_detected(self):
        docs = [
            doc("Q1", "Net revenue retention was 115 percent. Billings grew."),
            doc("Q2", "Net revenue retention was 112 percent. Billings grew."),
            doc("Q3", "Billings grew this quarter."),
        ]
        analysis = kpi_drift.analyze(docs)
        assert "Net revenue retention" in analysis.removed
        assert any(f.kind == "kpi_removed" for f in analysis.findings)

    def test_kpi_addition_detected(self):
        docs = [
            doc("Q1", "Billings grew."),
            doc("Q2", "Billings grew."),
            doc("Q3", "Billings grew. Adjusted EBITDA was positive for the first time."),
        ]
        analysis = kpi_drift.analyze(docs)
        assert "Adjusted EBITDA" in analysis.added

    def test_needs_two_periods(self):
        analysis = kpi_drift.analyze([doc("Q1", "Billings grew.")])
        assert analysis.findings == []


class TestNarrativeMetrics:
    def test_no_documents_reports_missing(self):
        metrics, findings = compute_narrative_metrics([])
        assert findings == []
        assert all(m.status is MetricStatus.MISSING_DATA for m in metrics)

    def test_metrics_computed_with_documents(self):
        docs = [doc(f"Q{i}", RESTRUCTURING_TEXT) for i in range(1, 5)]
        metrics, findings = compute_narrative_metrics(docs)
        by_name = {m.name: m for m in metrics}
        assert by_name["adjustment_recurrence_ratio"].value == 1.0
        assert by_name["recurring_adjustment_terms"].value >= 3
        assert findings
