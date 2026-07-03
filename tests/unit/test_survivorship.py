"""Offline tests for the survivorship pilot's pure helpers. The network path
(CIK-direct companyfacts) is validated live via scripts/run_survivorship_pilot.py,
consistent with how the EDGAR adapter is treated."""

from datetime import date

from app.services.backtesting.survivorship import (
    PILOT,
    P80,
    P90,
    _months_before,
    band,
)


class TestMonthsBefore:
    def test_simple(self):
        assert _months_before(date(2023, 6, 15), 6) == date(2022, 12, 15)

    def test_year_wrap(self):
        assert _months_before(date(2023, 3, 16), 18) == date(2021, 9, 16)

    def test_day_clamped_to_28(self):
        # Feb has no 30th; day is clamped so the date is always valid.
        assert _months_before(date(2023, 8, 30), 6) == date(2023, 2, 28)


class TestBand:
    def test_bands(self):
        assert band(P90 + 1) == ">=p90"
        assert band(P80 + 1) == ">=p80"
        assert band(35.0) == ">=p50"
        assert band(20.0) == "<p50"


class TestPilotUniverse:
    def test_no_financials_in_curated_set(self):
        # The engine excludes SIC 6xxx; the curated pilot must not rely on names
        # that would be excluded (they'd score nothing).
        # (SIC is fetched live, but the curation intent is asserted here.)
        assert all("bank" not in dc.name.lower() for dc in PILOT)

    def test_every_entry_has_an_event_date_and_type(self):
        for dc in PILOT:
            assert dc.event_date.year >= 2009  # post-XBRL
            assert dc.event_type in ("bankruptcy", "non_reliance", "delisting")
