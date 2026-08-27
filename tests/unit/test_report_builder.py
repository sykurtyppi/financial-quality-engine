"""Shared report builder tests (review findings 1, 4, 6, and round-5 outage)."""

from datetime import date

from app.core.pipeline import analyze
from app.services.ingestion.restatements import RestatementFootprint
from app.services.ingestion.sec_client import SecClientError
from app.services.reporting.report_builder import (
    _restatement_tier1_lines,
    build_report,
    data_quality_section,
)
from tests.fixtures.companies import stretch_dataset


class _OutageClient:
    """Simulates an EDGAR submissions/companyfacts outage on every network call."""

    cache_dir = None

    def resolve_cik(self, ticker):
        return 320193

    def submissions_by_cik(self, cik):
        raise SecClientError("submissions 503")

    def company_facts(self, ticker):
        raise SecClientError("companyfacts 503")

    def _cached_json(self, *a, **k):
        raise SecClientError("submissions 503")

    def _get(self, *a, **k):
        raise SecClientError("archive 503")


class TestOfferingsOutage:
    def test_outage_is_not_checked_end_to_end(self):
        # Review finding 1 (round 5): a submissions OUTAGE must not read as
        # checked-clean on the card, and the appendix must surface the gap.
        ds = stretch_dataset()
        result = analyze(ds)
        report, _ = build_report(
            result, ds,
            generated_on="2026-08-10",
            coverage=1.0,
            client=_OutageClient(),
            ticker="AAPL",
            fetched_at="2026-08-10 00:00 UTC",
        )
        card = report.split("Full report (appendix)")[0]
        assert "Checked — no securities-offering activity" not in card
        assert "Capital-markets stream not checked" in card
        assert "UNAVAILABLE" in report  # appendix surfaces the outage
        assert "No offering-related filings found" not in report


def _fp(field: str, accn: str, period: date, form: str) -> RestatementFootprint:
    is_amend = form.endswith("/A")
    return RestatementFootprint(
        field_name=field,
        tag=f"us-gaap:{field}",
        period_end=period,
        period_start=None,
        original_value=100.0,
        original_filed=date(2025, 1, 1),
        original_form="10-K",
        original_accession="orig",
        current_value=120.0,
        current_filed=date(2025, 6, 1),
        current_form=form,
        current_accession=accn,
        amendment_value=120.0 if is_amend else None,
        amendment_filed=date(2025, 6, 1) if is_amend else None,
        amendment_form=form if is_amend else None,
        amendment_accession=accn if is_amend else None,
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


class TestReportScopeAndSnapshot:
    def test_complete_report_names_material_unmodeled_disclosures(self):
        ds = stretch_dataset()
        result = analyze(ds)
        report, _ = build_report(result, ds, generated_on="2026-08-27")
        assert "examples of material risks not analyzed" in report.lower()
        assert "purchase commitments and guarantees" in report.lower()
        assert "customer concentration" in report.lower()
        assert "export controls" in report.lower()
        assert "not exhaustive" in report.lower()

    def test_prefetched_company_facts_prevent_restatement_refetch(self):
        ds = stretch_dataset()
        result = analyze(ds)
        report, _ = build_report(
            result,
            ds,
            generated_on="2026-08-27",
            coverage=1.0,
            client=_OutageClient(),
            ticker="AAPL",
            fetched_at="2026-08-27 00:00 UTC",
            company_facts={"facts": {}},
        )

        # Other streams are unavailable, but restatements were checked against
        # the exact snapshot used for fundamentals instead of making a new call.
        assert "Restatement appendix UNAVAILABLE" not in report
