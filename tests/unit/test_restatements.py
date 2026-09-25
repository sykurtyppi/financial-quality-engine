"""Restatement-footprint detector tests (P0-5). The AAPL numbers are the real
2008-09-27 Assets restatement (10-K -> 10-K/A) observed in cached companyfacts."""

from datetime import date

import pytest

from app.services.ingestion.restatements import (
    detect_restatements,
    render_restatements_section,
    scan_restatements,
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

    def test_a_reversed_amendment_is_reported_as_unchanged_not_down(self):
        # The /A moved the figure and a later filing moved it back: the
        # amendment event is still reported, but the figure did not go down.
        fj = _facts({"Assets": [
            _fact("2024-12-31", 1000.0, "2025-02-15", "10-K", accn="A"),
            _fact("2024-12-31", 1200.0, "2025-03-15", "10-K/A", accn="B"),
            _fact("2024-12-31", 1000.0, "2025-05-01", "10-Q", accn="C"),
        ]})
        (fp,) = detect_restatements(fj)
        assert (fp.original_value, fp.current_value, fp.amendment_value) == (1000.0, 1000.0, 1200.0)
        assert fp.is_amendment and fp.pct_change == 0.0
        assert fp.direction == "unchanged"

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

    def test_only_reports_mapper_scored_tag(self):
        # Round-9 finding: net_income candidates are NetIncomeLoss (rank 0) then
        # ProfitLoss (rank 1). If ProfitLoss covers more periods, the mapper
        # scores it — a revision on the low-coverage NetIncomeLoss must NOT be
        # reported (its current_value would disagree with scoring), and the field
        # must not be double-reported across both tags.

        ends = ["2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31",
                "2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"]
        profit = [_fact(e, 450.0, "2026-01-01", "10-K") for e in ends]  # covers 8 (mapper's pick)
        # NetIncomeLoss covers only 1 period, but with a same-period revision:
        nil = [
            _fact("2024-12-31", 500.0, "2025-02-01", "10-K", accn="A"),
            _fact("2024-12-31", 430.0, "2025-05-01", "10-K/A", accn="B"),
        ]
        fj = _facts({"ProfitLoss": profit, "NetIncomeLoss": nil})
        fps = [f for f in detect_restatements(fj) if f.field_name == "net_income"]
        # The mapper scores ProfitLoss (no revision there) -> nothing reported on
        # net_income, and definitely no duplicate footprint.
        tags = {f.tag for f in fps}
        assert "us-gaap:NetIncomeLoss" not in tags
        assert len(fps) <= 1

    def test_same_day_tie_matches_mapper(self):
        # Round-8 finding: when the two latest facts share a filed date, the
        # reported current_value must equal what the mapper actually keeps.
        # Both follow `precedence`: same day and form, so the higher
        # accession — whichever order the rows arrive in.
        from datetime import date as _date

        from app.services.ingestion.companyfacts_mapper import (
            _collect,
            _dedupe_latest_filed,
        )

        entries = [
            _fact("2024-12-31", 90.0, "2025-02-01", "10-K", accn="A"),
            _fact("2024-12-31", 100.0, "2025-08-01", "10-Q", accn="B"),  # same day, first
            _fact("2024-12-31", 120.0, "2025-08-01", "10-Q", accn="C"),  # same day, second
        ]
        fj = _facts({"Assets": entries})
        # Exactly what scoring keeps for this period (the mapper's selector):
        best = _dedupe_latest_filed(_collect(fj, "us-gaap", "Assets", "USD"))
        mapper_value = best[(None, _date(2024, 12, 31))].val
        fps = detect_restatements(fj)
        assert len(fps) == 1
        assert fps[0].current_value == mapper_value  # report agrees with scoring
        assert fps[0].current_value == 120.0  # accession C over B
        swapped = _facts({"Assets": [entries[0], entries[2], entries[1]]})
        assert detect_restatements(swapped)[0].current_value == 120.0

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
        md = render_restatements_section(scan_restatements(fj))
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
        md = render_restatements_section(scan_restatements(fj))
        assert "Amended-filing restatements" in md
        assert "Other prior-period revisions" in md
        assert "discontinued-operations or spinoff re-presentation" in md

    def test_render_empty(self):
        md = render_restatements_section(scan_restatements(_facts({"Assets": [
            _fact("2024-12-31", 1000.0, "2025-01-15", "10-K", accn="A"),
        ]})))
        assert "No revisions detected" in md
        assert "among the 1 inspected field" in md


class TestPointInTime:
    """`as_of` must filter the FACTS. Filtering finished footprints erased an
    amendment that WAS known at the report date whenever a later comparative
    touched the same figure again."""

    @staticmethod
    def _facts(rows):
        return {"facts": {"us-gaap": {"Assets": {"units": {"USD": [
            {"end": e, "filed": f, "val": v, "form": fm, "accn": a, "fy": 2024, "fp": "Q1"}
            for e, f, v, fm, a in rows]}}}}}

    ROWS = [
        ("2024-03-31", "2024-05-01", 1000.0, "10-Q", "acc-orig"),
        ("2024-03-31", "2024-11-01", 1200.0, "10-Q/A", "acc-amend"),   # known by 2025
        ("2024-03-31", "2026-02-01", 1210.0, "10-K", "acc-later"),     # a later comparative
    ]

    def test_amendment_survives_a_later_comparative(self):
        fps = detect_restatements(self._facts(self.ROWS), period_since=date(2021, 1, 1),
                                  as_of=date(2025, 1, 15))
        assert len(fps) == 1
        assert fps[0].current_filed == date(2024, 11, 1) and fps[0].current_value == 1200.0

    def test_nothing_before_the_revision_was_filed(self):
        assert detect_restatements(self._facts(self.ROWS), period_since=date(2021, 1, 1),
                                   as_of=date(2024, 6, 1)) == []

    def test_without_as_of_the_latest_filing_still_wins(self):
        fps = detect_restatements(self._facts(self.ROWS), period_since=date(2021, 1, 1))
        assert len(fps) == 1 and fps[0].current_filed == date(2026, 2, 1)
