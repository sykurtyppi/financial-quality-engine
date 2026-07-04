"""Phase 1: hardened KPI extraction — multi-sentence, table, missing, rename,
evidence IDs, pre-filter."""

from app.schemas.financials import DocumentRecord, DocumentType
from app.services.narrative.kpi_extraction import (
    PREFILTER_SIMILARITY,
    extract_kpi_definitions,
    pair_definition_changes,
)


def doc(label: str, text: str) -> DocumentRecord:
    return DocumentRecord(fiscal_label=label, doc_type=DocumentType.EARNINGS_RELEASE, text=text, source=f"src-{label}")


class TestMultiSentenceDefinitions:
    def test_definition_spans_continuation_sentences(self):
        text = ("Adjusted EBITDA is defined as net income before interest and taxes. "
                "It also excludes restructuring charges, impairments, and stock-based compensation. "
                "The weather was fine that day.")
        defs = extract_kpi_definitions([doc("Q1", text)])
        d = next(x for x in defs if x.kpi == "Adjusted EBITDA")
        assert d.status == "defined"
        assert "restructuring charges" in d.definition  # captured the 2nd sentence
        assert "weather" not in d.definition  # stopped at the non-continuation sentence


class TestMissingDefinition:
    def test_mentioned_without_definition_reported(self):
        defs = extract_kpi_definitions([doc("Q1", "Adjusted EBITDA grew 20% this quarter.")])
        d = next(x for x in defs if x.kpi == "Adjusted EBITDA")
        assert d.status == "mentioned_no_definition"
        assert d.definition is None

    def test_unmentioned_kpi_absent(self):
        defs = extract_kpi_definitions([doc("Q1", "Revenue grew.")])
        assert not any(x.kpi == "Adjusted EBITDA" for x in defs)


class TestTableDefinition:
    def test_reconciliation_context_captured(self):
        text = ("Revenue was strong. Reconciliation of Adjusted EBITDA to net income: "
                "net income 100, plus taxes 20, plus depreciation 30.")
        defs = extract_kpi_definitions([doc("Q1", text)])
        d = next(x for x in defs if x.kpi == "Adjusted EBITDA")
        assert d.status == "defined_table"


class TestEvidenceIds:
    def test_every_definition_has_unique_id(self):
        defs = extract_kpi_definitions([
            doc("Q1", "Adjusted EBITDA is defined as X. Free cash flow is defined as CFO less capex."),
        ])
        ids = [d.evidence_id for d in defs]
        assert all(i.startswith("KD-") for i in ids)
        assert len(ids) == len(set(ids))


class TestPairing:
    def _docs(self, prior_def: str, cur_def: str):
        return [doc("Q1", prior_def), doc("Q2", cur_def)]

    def test_changed_definition_becomes_candidate(self):
        pairs = pair_definition_changes(self._docs(
            "Adjusted EBITDA is defined as net income before interest and taxes.",
            "Adjusted EBITDA is defined as net income before interest, taxes, restructuring, "
            "impairments, litigation costs, and stock-based compensation.",
        ))
        assert len(pairs) == 1
        assert pairs[0].kpi == "Adjusted EBITDA"
        assert pairs[0].change_type == "redefinition"
        assert pairs[0].prefilter_similarity < PREFILTER_SIMILARITY
        assert pairs[0].evidence_id.startswith("KDP-")

    def test_unchanged_definition_prefiltered_out(self):
        same = "Adjusted EBITDA is defined as net income before interest and taxes."
        assert pair_definition_changes(self._docs(same, same)) == []

    def test_dropped_kpi_becomes_drop_candidate(self):
        pairs = pair_definition_changes([
            doc("Q1", "Adjusted EBITDA is defined as net income before interest and taxes."),
            doc("Q2", "Revenue grew this quarter."),
        ])
        drops = [p for p in pairs if p.change_type == "drop"]
        assert any(p.kpi == "Adjusted EBITDA" for p in drops)
        assert drops[0].current_definition == ""
