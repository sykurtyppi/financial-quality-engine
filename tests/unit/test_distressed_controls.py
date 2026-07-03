"""Offline sanity tests for the distressed-survivor control set. The network
path is validated live via scripts/run_distressed_controls.py."""

from datetime import date

from app.services.backtesting.distressed_controls import CONTROLS, HORIZONS_MONTHS


class TestControlSet:
    def test_horizons_span_at_and_before_anchor(self):
        assert 0 in HORIZONS_MONTHS  # must score AT peak distress, not only before
        assert max(HORIZONS_MONTHS) >= 12

    def test_sector_mix_matches_dead_set_shape(self):
        # Dead set is retail- and energy-heavy; the control must be too, or the
        # comparison is confounded by sector.
        sectors = [c.sector for c in CONTROLS]
        assert sectors.count("retail") >= 5
        assert sectors.count("energy") >= 4

    def test_anchors_are_real_dates_pre_pandemic_or_covid_era(self):
        for c in CONTROLS:
            assert isinstance(c.anchor_date, date)
            assert c.anchor_date.year >= 2016  # post-XBRL, documented-distress era

    def test_no_obvious_financials(self):
        assert all("bank" not in c.name.lower() for c in CONTROLS)
