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

def _block(name: str, score: float | None) -> "BlockScore":
    from app.schemas.scoring import BlockScore, Confidence, Direction

    return BlockScore(
        name=name, score=score, direction=Direction.MIXED,
        confidence=Confidence.HIGH, rationale="fixture", components=[],
        data_coverage=1.0,
    )


class TestCapitalIntegrityOfferingsCaveat:
    """FPS-class consistency check (2026Q2's worst miss): a low-concern
    Capital Integrity score must not sit silently next to SELLING-STOCKHOLDER
    takedowns it cannot see. The caveat must fire only on classified
    secondary/mixed evidence — calling a debt 424B5 or an issuer-primary deal
    a "sponsor sale" states an attribution the data does not establish."""

    def _result(self, ci_score):
        from types import SimpleNamespace

        return SimpleNamespace(block_scores=[
            _block("Earnings Quality", 50.0),
            _block("Capital Integrity", ci_score),
        ])

    @staticmethod
    def _takedown(**over):
        from datetime import date as _date

        from app.services.ingestion.offerings import OfferingFiling

        base = dict(form="424B4", filing_date=_date(2026, 7, 6), accession="a",
                    primary_doc="d.htm", kind="takedown")
        base.update(over)
        return OfferingFiling(**base)

    def test_secondary_only_fires(self):
        from app.services.reporting.report_builder import (
            _capital_integrity_offerings_caveat,
        )

        tds = [self._takedown(security_type="equity", has_selling_stockholders=True,
                              secondary_shares=29_094_075)]
        caveat = _capital_integrity_offerings_caveat(self._result(10.0), tds)
        assert caveat is not None
        assert "blind" in caveat
        assert "1 selling-stockholder takedown(s)" in caveat

    def test_mixed_offering_fires(self):
        # FPS's actual July deal: sponsor shares AND issuer shares in one 424B4.
        from app.services.reporting.report_builder import (
            _capital_integrity_offerings_caveat,
        )

        tds = [self._takedown(security_type="equity", has_selling_stockholders=True,
                              primary_shares=14_555_925, secondary_shares=29_094_075)]
        assert _capital_integrity_offerings_caveat(self._result(10.0), tds) is not None

    def test_debt_takedown_is_silent(self):
        # A debt 424B5 is not a sponsor sale; labeling it one was the defect.
        from app.services.reporting.report_builder import (
            _capital_integrity_offerings_caveat,
        )

        tds = [self._takedown(form="424B5", security_type="debt",
                              has_selling_stockholders=False)]
        assert _capital_integrity_offerings_caveat(self._result(10.0), tds) is None

    def test_primary_only_is_silent(self):
        # Issuer-side dilution is exactly what the block DOES score — there is
        # no blind spot to caveat.
        from app.services.reporting.report_builder import (
            _capital_integrity_offerings_caveat,
        )

        tds = [self._takedown(security_type="equity", primary_shares=16_586_427,
                              secondary_shares=0, has_selling_stockholders=False)]
        assert _capital_integrity_offerings_caveat(self._result(10.0), tds) is None

    def test_unknown_unparsed_is_silent(self):
        # No parsed evidence of selling stockholders -> no sponsor attribution.
        from app.services.reporting.report_builder import (
            _capital_integrity_offerings_caveat,
        )

        tds = [self._takedown(security_type="unknown")]
        assert _capital_integrity_offerings_caveat(self._result(10.0), tds) is None

    def test_unknown_with_selling_stockholder_boilerplate_is_silent(self):
        # Review finding: an unrecognized instrument (preferred, warrants,
        # convertible left "unknown") whose boilerplate says "selling
        # stockholders" is still not a POSITIVELY classified equity secondary
        # — the caveat requires security_type == "equity", not "not debt".
        from app.services.reporting.report_builder import (
            _capital_integrity_offerings_caveat,
        )

        tds = [self._takedown(security_type="unknown", has_selling_stockholders=True,
                              secondary_shares=1_000_000)]
        assert _capital_integrity_offerings_caveat(self._result(10.0), tds) is None

    def test_no_takedowns_is_silent(self):
        from app.services.reporting.report_builder import (
            _capital_integrity_offerings_caveat,
        )

        assert _capital_integrity_offerings_caveat(self._result(10.0), []) is None

    def test_elevated_score_is_silent(self):
        # The block already reads concerning; the contradiction is gone.
        from app.services.reporting.report_builder import (
            _capital_integrity_offerings_caveat,
        )

        tds = [self._takedown(security_type="equity", has_selling_stockholders=True)]
        assert _capital_integrity_offerings_caveat(self._result(55.0), tds) is None

    def test_unscored_block_is_silent(self):
        from app.services.reporting.report_builder import (
            _capital_integrity_offerings_caveat,
        )

        tds = [self._takedown(security_type="equity", has_selling_stockholders=True)]
        assert _capital_integrity_offerings_caveat(self._result(None), tds) is None


class TestFundingContextNote:
    """AMKR-class misread: a cash-conversion red flag must carry the
    funding-context checklist line; unrelated flags must not."""

    def test_cash_conversion_flag_carries_the_note(self):
        from app.core.pipeline import analyze
        from app.services.reporting.markdown_report import render

        ds = stretch_dataset()  # stressed fixture fires cash-conversion flags
        result = analyze(ds)
        assert any(
            set(f.evidence_metrics) & {"cfo_to_net_income", "fcf_margin", "fcf_margin_trend"}
            for f in result.red_flags
        ), "fixture no longer fires a cash flag; pick another fixture"
        report = render(result, generated_on="2026-09-01")
        assert "Funding context" in report
        # Neutral wording only: the old note claimed these items sit "outside
        # CFO - capex", which is not generally true (a customer advance can BE
        # operating cash flow). The note must hedge on classification.
        assert "may affect reported operating cash flow" in report
        assert "outside CFO" not in report

    def test_cfo_only_flag_gets_no_capex_language(self):
        # Split-by-metric: a cfo_to_net_income flag involves no capex, so the
        # attached note must not steer the reader to a capex explanation.
        from app.services.reporting.markdown_report import (
            FUNDING_CONTEXT_NOTE_CFO,
        )

        assert "capital spending" not in FUNDING_CONTEXT_NOTE_CFO
        assert "investing cash flow" not in FUNDING_CONTEXT_NOTE_CFO

    def test_note_absent_without_cash_flags(self):
        from types import SimpleNamespace

        from app.services.reporting.markdown_report import (
            FUNDING_CONTEXT_METRICS,
        )

        # Pure containment check on the trigger set — a flag on any other
        # metric must not intersect.
        other = SimpleNamespace(evidence_metrics=["dso_trend"])
        assert not (set(other.evidence_metrics) & FUNDING_CONTEXT_METRICS)
