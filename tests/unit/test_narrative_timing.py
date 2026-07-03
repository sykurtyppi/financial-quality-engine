"""Offline tests for the narrative-timing classification. Network path validated
live via scripts/run_narrative_timing.py."""

from datetime import date

from app.services.backtesting.narrative_timing import (
    CONTEMPORANEOUS_DAYS,
    EARLY_WARNING_DAYS,
    _accession,
    _lead_days,
    classify,
)


class TestLeadClassification:
    def test_early_warning(self):
        assert classify(EARLY_WARNING_DAYS) == "EARLY WARNING"
        assert classify(315) == "EARLY WARNING"

    def test_contemporaneous(self):
        assert classify(0) == "contemporaneous"
        assert classify(CONTEMPORANEOUS_DAYS - 1) == "contemporaneous"

    def test_one_quarter(self):
        assert classify(90) == "one-quarter lead"

    def test_unknown(self):
        assert classify(None) == "unknown"


class TestLeadDays:
    def test_computes_days_to_event(self):
        # 4.02 on 2018-06-07, filing on 2018-01-08 -> 150 days (the MiMedx case).
        assert _lead_days(date(2018, 6, 7), "2018-01-08") == 150

    def test_none_filing(self):
        assert _lead_days(date(2018, 6, 7), None) is None


class TestAccessionParsing:
    def test_parses_accession_from_source(self):
        assert _accession("10-K 0000024545-19-000009") == "0000024545-19-000009"
        assert _accession("8-K 0000320193-25-000071 ex99.htm") == "0000320193-25-000071"

    def test_handles_missing(self):
        assert _accession(None) is None
        assert _accession("") is None
