"""Restatement-footprint detector tests (P0-5). The AAPL numbers are the real
2008-09-27 Assets restatement (10-K -> 10-K/A) observed in cached companyfacts."""

import pytest

from app.services.ingestion.restatements import (
    detect_restatements,
    render_restatements_section,
)


def _fact(end, val, filed, form="10-K", start=None, accn="0000000000-00-000000"):
    e = {"end": end, "val": val, "filed": filed, "form": form, "accn": accn}
    if start:
        e["start"] = start
    return e


def _facts(concepts, taxonomy="us-gaap"):
    return {
        "entityName": "Test Co",
        "facts": {taxonomy: {tag: {"units": {"USD": rows}} for tag, rows in concepts.items()}},
    }


class TestDetection:
    def test_detects_material_downward_restatement(self):
        fj = _facts({"Assets": [
            _fact("2008-09-27", 39_572_000_000, "2009-10-27", "10-K", accn="A"),
            _fact("2008-09-27", 36_171_000_000, "2010-01-25", "10-K/A", accn="B"),
        ]})
        fps = detect_restatements(fj)
        assert len(fps) == 1
        fp = fps[0]
        assert fp.field_name == "total_assets"
        assert fp.original_value == 39_572_000_000
        assert fp.restated_value == 36_171_000_000
        assert fp.restated_form == "10-K/A"
        assert fp.restated_accession == "B"
        assert fp.direction == "down"
        assert fp.is_amendment is True
        assert fp.pct_change == pytest.approx(-0.0859, abs=1e-3)

    def test_immaterial_change_ignored(self):
        # $1M revision on $3.5B = 0.03% < 1% threshold: rounding/reclassification.
        fj = _facts({"NetIncomeLoss": [
            _fact("2007-09-29", 3_496_000_000, "2009-10-27", "10-K", start="2006-10-01"),
            _fact("2007-09-29", 3_495_000_000, "2010-01-25", "10-K/A", start="2006-10-01"),
        ]})
        assert detect_restatements(fj) == []

    def test_single_filing_no_footprint(self):
        fj = _facts({"Assets": [_fact("2024-12-31", 1000.0, "2025-01-15")]})
        assert detect_restatements(fj) == []

    def test_repeated_identical_value_no_footprint(self):
        # A comparative re-report with an unchanged value is not a restatement.
        fj = _facts({"Assets": [
            _fact("2024-12-31", 1000.0, "2025-01-15", "10-K", accn="A"),
            _fact("2024-12-31", 1000.0, "2025-04-15", "10-Q", accn="B"),
        ]})
        assert detect_restatements(fj) == []

    def test_tag_switch_not_flagged(self):
        # Same period under two DIFFERENT tags: a tag switch, not a restatement.
        fj = _facts({
            "AccountsReceivableNetCurrent": [_fact("2024-12-31", 100.0, "2025-01-15")],
            "ReceivablesNetCurrent": [_fact("2024-12-31", 200.0, "2025-04-15")],
        })
        assert detect_restatements(fj) == []

    def test_flow_restatement_keyed_by_period(self):
        fj = _facts({"Revenues": [
            _fact("2024-03-31", 1000.0, "2024-05-01", "10-Q", start="2024-01-01", accn="A"),
            _fact("2024-03-31", 1100.0, "2025-05-01", "10-Q", start="2024-01-01", accn="B"),
        ]})
        fps = detect_restatements(fj)
        assert len(fps) == 1
        assert fps[0].field_name == "revenue"
        assert fps[0].direction == "up"
        assert fps[0].pct_change == pytest.approx(0.10)

    def test_shared_tag_reported_once(self):
        # OperatingIncomeLoss maps to both ebit and operating_income; a single
        # underlying revision must be reported once, not double-counted.
        fj = _facts({"OperatingIncomeLoss": [
            _fact("2024-03-31", 500.0, "2024-05-01", start="2024-01-01", accn="A"),
            _fact("2024-03-31", 450.0, "2025-05-01", "10-K/A", start="2024-01-01", accn="B"),
        ]})
        assert len(detect_restatements(fj)) == 1

    def test_materiality_threshold_configurable(self):
        fj = _facts({"NetIncomeLoss": [
            _fact("2007-09-29", 3_496_000_000, "2009-10-27", "10-K", start="2006-10-01"),
            _fact("2007-09-29", 3_495_000_000, "2010-01-25", "10-K/A", start="2006-10-01"),
        ]})
        # A near-zero threshold surfaces even the $1M reclassification.
        assert len(detect_restatements(fj, materiality_pct=0.0)) == 1

    def test_share_splits_excluded(self):
        # A 7-for-1 split retroactively "revises" prior share counts +600%; this
        # is a corporate action, not a restatement, and must not be flagged.
        fj = _facts({"CommonStockSharesOutstanding": [
            _fact("2014-03-29", 900_000_000, "2014-04-24", "10-Q", accn="A"),
            _fact("2014-03-29", 6_300_000_000, "2015-04-24", "10-Q", accn="B"),
        ]}, taxonomy="us-gaap")
        assert detect_restatements(fj) == []

    def test_period_since_scopes_to_recent(self):
        fj = _facts({"Assets": [
            _fact("2010-12-31", 1000.0, "2011-01-15", "10-K", accn="A"),
            _fact("2010-12-31", 1200.0, "2012-01-15", "10-K/A", accn="B"),  # old restatement
            _fact("2024-12-31", 5000.0, "2025-01-15", "10-K", accn="C"),
            _fact("2024-12-31", 5500.0, "2025-05-15", "10-K/A", accn="D"),  # recent restatement
        ]})
        from datetime import date
        recent = detect_restatements(fj, period_since=date(2022, 1, 1))
        assert len(recent) == 1
        assert recent[0].period_end == date(2024, 12, 31)


class TestFilingTrail:
    def test_amendment_in_middle_labeled_amendment(self):
        # Review finding 7: 10-K -> 10-K/A (restates) -> 10-K (reverts). The
        # amendment must be surfaced and labeled, not collapsed to earliest-vs-latest.
        fj = _facts({"Assets": [
            _fact("2024-12-31", 1000.0, "2025-02-01", "10-K", accn="A"),
            _fact("2024-12-31", 1200.0, "2025-05-01", "10-K/A", accn="B"),
            _fact("2024-12-31", 1000.0, "2025-08-01", "10-K", accn="C"),
        ]})
        fps = detect_restatements(fj)
        assert len(fps) == 1
        assert fps[0].is_amendment is True
        assert fps[0].restated_form == "10-K/A"
        assert fps[0].restated_value == 1200.0
        assert fps[0].restated_accession == "B"

    def test_multi_amendment_reports_latest_not_largest(self):
        # Round-6 finding: 10-K(100) -> 10-K/A(120) -> 10-K/A(105). The restated
        # value must be the LATEST-filed (105 = what the mapper scores), not the
        # largest transient deviation (120).
        fj = _facts({"Assets": [
            _fact("2024-12-31", 100.0, "2025-02-01", "10-K", accn="A"),
            _fact("2024-12-31", 120.0, "2025-05-01", "10-K/A", accn="B"),
            _fact("2024-12-31", 105.0, "2025-08-01", "10-K/A", accn="C"),
        ]})
        fps = detect_restatements(fj)
        assert len(fps) == 1
        assert fps[0].restated_value == 105.0
        assert fps[0].restated_accession == "C"
        assert fps[0].is_amendment is True

    def test_reverted_revision_still_surfaces(self):
        # A -> B -> A via regular filings: the revision happened; it must not
        # vanish just because the latest value equals the original.
        fj = _facts({"Assets": [
            _fact("2024-12-31", 1000.0, "2025-02-01", "10-K", accn="A"),
            _fact("2024-12-31", 1300.0, "2025-05-01", "10-Q", accn="B"),
            _fact("2024-12-31", 1000.0, "2025-08-01", "10-Q", accn="C"),
        ]})
        fps = detect_restatements(fj)
        assert len(fps) == 1
        assert fps[0].restated_value == 1300.0
        assert fps[0].restated_accession == "B"


class TestRender:
    def test_render_section(self):
        fj = _facts({"Assets": [
            _fact("2008-09-27", 39_572_000_000, "2009-10-27", "10-K", accn="A"),
            _fact("2008-09-27", 36_171_000_000, "2010-01-25", "10-K/A", accn="B"),
        ]})
        md = render_restatements_section(detect_restatements(fj))
        assert "Restatement" in md
        assert "total_assets" in md
        assert "10-K/A" in md
        assert "-8.6%" in md
        # The AAPL Assets revision is an amendment -> high-confidence section.
        assert "high confidence" in md

    def test_render_separates_amendments_from_representations(self):
        fj = _facts({
            "Assets": [  # amendment -> high confidence
                _fact("2024-12-31", 1000.0, "2025-01-15", "10-K", accn="A"),
                _fact("2024-12-31", 1200.0, "2025-05-15", "10-K/A", accn="B"),
            ],
            "Revenues": [  # non-amendment revision -> "other" with caveat
                _fact("2024-03-31", 500.0, "2024-05-01", "10-Q", start="2024-01-01", accn="C"),
                _fact("2024-03-31", 300.0, "2025-05-01", "10-Q", start="2024-01-01", accn="D"),
            ],
        })
        md = render_restatements_section(detect_restatements(fj))
        assert "Amended-filing restatements" in md
        assert "Other prior-period revisions" in md
        assert "discontinued-operations or spinoff re-presentation" in md

    def test_render_empty(self):
        assert "No prior-period revisions" in render_restatements_section([])
