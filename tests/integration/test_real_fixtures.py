"""Integration tests on real (trimmed, public-domain SEC) company data.

Fixtures cover three fiscal-calendar shapes:
- AAPL: September FYE, 52/53-week calendar
- KO:   calendar-year FYE
- CRM:  January FYE, 52/53-week calendar (the fy/fp-metadata bug case)

Expected values below are transcribed from the companies' filed XBRL facts
(verified against 10-K/10-Q accessions via scripts/verify_against_filings.py).
"""

import json
from pathlib import Path

import pytest

from app.core.pipeline import analyze
from app.services.ingestion.companyfacts_mapper import build_dataset

FIXTURES = Path(__file__).parent.parent / "fixtures" / "real"


def load(ticker: str) -> dict:
    return json.loads((FIXTURES / f"companyfacts_{ticker}_trimmed.json").read_text())


class TestAppleMapping:
    @pytest.fixture(scope="class")
    def built(self):
        return build_dataset(load("AAPL"), "AAPL", n_quarters=8)

    def test_spot_value_matches_filed_10q(self, built):
        ds, _ = built
        q = next(p for p in ds.periods if p.fiscal_label == "FY2026Q2")
        assert q.revenue == 111_184_000_000.0  # accession 0000320193-26-000013

    def test_fy2025_quarters_reconcile_to_filed_annual(self, built):
        ds, _ = built
        fy25 = [p for p in ds.periods if p.fiscal_label.startswith("FY2025")]
        assert len(fy25) == 4
        assert sum(p.revenue for p in fy25) == pytest.approx(416_161_000_000.0)
        assert sum(p.cfo for p in fy25) == pytest.approx(111_482_000_000.0)

    def test_september_fye_labels(self, built):
        ds, diag = built
        assert diag.fiscal_year_end_month == 9
        labels = [p.fiscal_label for p in ds.periods]
        assert labels == sorted(labels)
        assert len(labels) == len(set(labels))

    def test_legitimately_missing_fields_reported_not_guessed(self, built):
        """Apple does not separately disclose goodwill or interest expense in
        recent filings — these must surface as gaps, not fabrications."""
        _, diag = built
        assert diag.field_by_name("goodwill").periods_filled == 0
        assert diag.field_by_name("interest_expense").periods_filled == 0

    def test_pipeline_runs_end_to_end(self, built):
        ds, _ = built
        result = analyze(ds)
        assert result.overall is not None
        assert result.overall.score is not None
        assert not result.excluded


class TestCocaColaMapping:
    @pytest.fixture(scope="class")
    def built(self):
        return build_dataset(load("KO"), "KO", n_quarters=8)

    def test_calendar_fye_labels(self, built):
        ds, diag = built
        assert diag.fiscal_year_end_month == 12
        assert any(p.fiscal_label == "FY2025Q4" for p in ds.periods)

    def test_fy2025_revenue_reconciles(self, built):
        ds, _ = built
        fy25 = [p for p in ds.periods if p.fiscal_label.startswith("FY2025")]
        assert len(fy25) == 4
        assert sum(p.revenue for p in fy25) == pytest.approx(47_941_000_000.0)

    def test_cfo_fully_covered_via_ytd_differencing(self, built):
        _, diag = built
        fd = diag.field_by_name("cfo")
        assert fd.periods_filled == fd.periods_total


class TestSalesforceMapping:
    """CRM is the case that exposed SEC's unreliable fy/fp metadata: the
    FY-ending-2026-01-31 annual fact is stamped fy=2025 in companyfacts."""

    @pytest.fixture(scope="class")
    def built(self):
        return build_dataset(load("CRM"), "CRM", n_quarters=8)

    def test_january_fye_structural_labels_unique_and_ordered(self, built):
        ds, diag = built
        assert diag.fiscal_year_end_month == 1
        labels = [p.fiscal_label for p in ds.periods]
        assert len(labels) == len(set(labels)), f"duplicate labels: {labels}"
        assert labels == sorted(labels)

    def test_fiscal_year_ending_jan_2026_is_fy2026(self, built):
        ds, _ = built
        jan_2026 = next(p for p in ds.periods if p.period_end.year == 2026 and p.period_end.month == 1)
        assert jan_2026.fiscal_label == "FY2026Q4"

    def test_sga_composed_from_components(self, built):
        _, diag = built
        fd = diag.field_by_name("sga_expense")
        assert fd.periods_filled == fd.periods_total
        assert "composite" in fd.methods
