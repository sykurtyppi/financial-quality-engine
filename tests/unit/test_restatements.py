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
        assert fp.current_value == 36_171_000_000
        assert fp.current_form == "10-K/A"
        assert fp.current_accession == "B"
        assert fp.amendment_value == 36_171_000_000  # the /A event
        assert fp.amendment_form == "10-K/A"
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
    def test_amendment_superseded_by_regular_filing_stays_tier1(self):
        # Round-7 finding: 10-K(100) -> 10-K/A(120) -> 10-K(105). The CURRENT
        # value is 105 (what the mapper scores, via an ordinary 10-K), but the
        # /A amendment event (120) must still be captured -> Tier-1 eligible.
        fj = _facts({"Assets": [
            _fact("2024-12-31", 100.0, "2025-02-01", "10-K", accn="A"),
            _fact("2024-12-31", 120.0, "2025-05-01", "10-K/A", accn="B"),
            _fact("2024-12-31", 105.0, "2025-08-01", "10-K", accn="C"),
        ]})
        fps = detect_restatements(fj)
        assert len(fps) == 1
        fp = fps[0]
        assert fp.current_value == 105.0  # matches scoring (latest-filed)
        assert fp.current_form == "10-K"
        assert fp.amendment_value == 120.0  # the /A event, not lost
        assert fp.amendment_form == "10-K/A"
        assert fp.amendment_accession == "B"
        assert fp.is_amendment is True  # -> promotes to Tier-1

    def test_amendment_revert_current_matches_scoring(self):
        # 10-K(100) -> 10-K/A(120) -> 10-K(100 revert). Current = 100 (what the
        # mapper scores), NOT the historical 120; the /A event still surfaces.
        fj = _facts({"Assets": [
            _fact("2024-12-31", 1000.0, "2025-02-01", "10-K", accn="A"),
            _fact("2024-12-31", 1200.0, "2025-05-01", "10-K/A", accn="B"),
            _fact("2024-12-31", 1000.0, "2025-08-01", "10-K", accn="C"),
        ]})
        fps = detect_restatements(fj)
        assert len(fps) == 1
        fp = fps[0]
        assert fp.current_value == 1000.0  # NOT 1200 — matches scoring
        assert fp.pct_change == 0.0  # no net change vs original
        assert fp.amendment_value == 1200.0
        assert fp.is_amendment is True

    def test_multi_amendment_current_is_latest(self):
        # 10-K(100) -> 10-K/A(120) -> 10-K/A(105): current = 105 (latest-filed),
        # amendment event = the latest /A (105 here).
        fj = _facts({"Assets": [
            _fact("2024-12-31", 100.0, "2025-02-01", "10-K", accn="A"),
            _fact("2024-12-31", 120.0, "2025-05-01", "10-K/A", accn="B"),
            _fact("2024-12-31", 105.0, "2025-08-01", "10-K/A", accn="C"),
        ]})
        fps = detect_restatements(fj)
        assert len(fps) == 1
        assert fps[0].current_value == 105.0
        assert fps[0].current_accession == "C"
        assert fps[0].is_amendment is True

    def test_reverted_regular_transient_is_noise_skipped(self):
        # A -> B -> A via ORDINARY filings (no /A): current back at original and
        # no amendment -> low-signal transient, not surfaced (round-7 model).
        fj = _facts({"Assets": [
            _fact("2024-12-31", 1000.0, "2025-02-01", "10-K", accn="A"),
            _fact("2024-12-31", 1300.0, "2025-05-01", "10-Q", accn="B"),
            _fact("2024-12-31", 1000.0, "2025-08-01", "10-Q", accn="C"),
        ]})
        assert detect_restatements(fj) == []

    def test_ongoing_regular_revision_surfaces_as_current(self):
        # A -> B via an ordinary later filing (net change, no /A): current = B,
        # no amendment -> surfaced as an "other" revision.
        fj = _facts({"Assets": [
            _fact("2024-12-31", 1000.0, "2025-02-01", "10-K", accn="A"),
            _fact("2024-12-31", 1300.0, "2025-05-01", "10-Q", accn="B"),
        ]})
        fps = detect_restatements(fj)
        assert len(fps) == 1
        assert fps[0].current_value == 1300.0
        assert fps[0].is_amendment is False


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
