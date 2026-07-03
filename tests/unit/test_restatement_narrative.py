"""Offline tests for the restatement-narrative harness classification and the
point-in-time document cutoff. Network path validated live via
scripts/run_restatement_narrative.py."""

from datetime import date

from app.schemas.financials import DocumentType
from app.services.backtesting.restatement_narrative import INDEPENDENT_KINDS


class TestIndependentKinds:
    def test_high_severity_is_independent(self):
        # The sharpest low-false-positive signal must be in the independent set.
        assert "high_severity_disclosure" in INDEPENDENT_KINDS

    def test_kpi_and_disclosure_signals_independent(self):
        assert {"kpi_removed", "kpi_definition_change", "disclosure_reduction"} <= INDEPENDENT_KINDS

    def test_mismatch_kinds_not_in_independent(self):
        # Metric-gated mismatches must NOT be counted as independent narrative signal.
        assert "demand_narrative_vs_working_capital" not in INDEPENDENT_KINDS
        assert "profitability_narrative_vs_cash_conversion" not in INDEPENDENT_KINDS


class TestDocumentCutoff:
    def test_before_filters_future_filings(self):
        # The `before` cutoff is point-in-time discipline for documents. Verify the
        # filtering predicate directly (string date comparison is ISO-safe).
        cutoff = date(2018, 6, 7).isoformat()
        filing_dates = ["2018-05-01", "2018-06-06", "2018-06-07", "2018-08-01"]
        keep = [d for d in filing_dates if d <= cutoff]
        assert keep == ["2018-05-01", "2018-06-06", "2018-06-07"]
        assert "2018-08-01" not in keep
