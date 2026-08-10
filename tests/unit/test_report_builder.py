"""Shared report builder tests (review findings 1, 4, 6)."""

from datetime import date

from app.services.ingestion.restatements import RestatementFootprint
from app.services.reporting.report_builder import _restatement_tier1_lines, data_quality_section


def _fp(field: str, accn: str, period: date, form: str) -> RestatementFootprint:
    return RestatementFootprint(
        field_name=field,
        tag=f"us-gaap:{field}",
        period_end=period,
        period_start=None,
        original_value=100.0,
        original_filed=date(2025, 1, 1),
        original_form="10-K",
        original_accession="orig",
        restated_value=120.0,
        restated_filed=date(2025, 6, 1),
        restated_form=form,
        restated_accession=accn,
    )


class TestRestatementAggregation:
    def test_one_line_per_event_not_per_field(self):
        # Review finding 6: three fields revised in ONE amendment (same accession
        # + period) must collapse to a single Tier-1 line, not three.
        p = date(2024, 12, 31)
        footprints = [
            _fp("total_assets", "accnA", p, "10-K/A"),
            _fp("revenue", "accnA", p, "10-K/A"),
            _fp("net_income", "accnA", p, "10-K/A"),
        ]
        lines = _restatement_tier1_lines(footprints)
        assert len(lines) == 1
        assert "3 figure(s) revised" in lines[0]

    def test_distinct_events_produce_distinct_lines(self):
        footprints = [
            _fp("total_assets", "accnA", date(2024, 12, 31), "10-K/A"),
            _fp("revenue", "accnB", date(2023, 12, 31), "10-K/A"),
        ]
        assert len(_restatement_tier1_lines(footprints)) == 2

    def test_non_amendments_excluded(self):
        footprints = [_fp("revenue", "accnC", date(2024, 12, 31), "10-Q")]  # not /A
        assert _restatement_tier1_lines(footprints) == []


class TestDataQualityEventError:
    def test_event_failure_is_visible(self):
        # Review finding 4: an event-stream failure must render, not vanish.
        section = data_quality_section(
            fetched_at="2026-08-10 00:00 UTC",
            fresh=False,
            coverage=0.9,
            warnings=[],
            doc_diagnostics=[],
            events_error="timeout",
        )
        assert "Event (8-K 4.02) appendix UNAVAILABLE" in section
        assert "not evidence of no events" in section
