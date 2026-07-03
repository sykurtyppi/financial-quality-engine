"""Offline sanity tests for the clean-company narrative control. Network path
validated live via scripts/run_clean_narrative_control.py."""

from app.services.backtesting.clean_narrative_control import (
    CLEAN,
    RESTATEMENT_RATES,
)


class TestControlSet:
    def test_includes_serial_acquirers_to_match_confound(self):
        # The clean set MUST include adjustment-language-heavy names, or the
        # comparison is rigged (fraudsters-who-use-jargon vs simple clean cos).
        acquirers = [c for c in CLEAN if c.sector in ("serial_acquirer", "industrial")]
        assert len(acquirers) >= 4

    def test_sector_coverage_matches_restatement_set(self):
        sectors = {c.sector for c in CLEAN}
        # restatement cases spanned staples, tech, retail, medical, industrial
        assert {"staples", "tech", "retail", "medical"} <= sectors

    def test_reference_rates_present_for_key_detectors(self):
        assert "high_severity_disclosure" in RESTATEMENT_RATES
        assert "adjustment_recurrence" in RESTATEMENT_RATES
        # high-severity is the sharp signal; adjustment is the noise baseline.
        hs_n, hs_d = RESTATEMENT_RATES["high_severity_disclosure"]
        adj_n, adj_d = RESTATEMENT_RATES["adjustment_recurrence"]
        assert hs_n / hs_d < adj_n / adj_d  # sharp signal is rarer than the noise
