"""A dated report must not carry evidence filed after its own date."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from app.services.reporting.report_builder import _pit_dates, _pit_footprints


def test_events_after_the_report_date_are_excluded():
    report_date = date(2025, 1, 15)
    dates = {date(2022, 6, 1), date(2024, 3, 3), date(2025, 1, 15), date(2026, 1, 20)}
    assert _pit_dates(dates, date(2023, 1, 15), report_date) == [date(2024, 3, 3), date(2025, 1, 15)]


def test_revisions_filed_after_the_report_date_are_excluded():
    report_date = date(2025, 1, 15)
    fps = [SimpleNamespace(current_filed=date(2024, 8, 1)),
           SimpleNamespace(current_filed=date(2025, 1, 15)),
           SimpleNamespace(current_filed=date(2025, 6, 1))]
    assert [f.current_filed for f in _pit_footprints(fps, report_date)] == [
        date(2024, 8, 1), date(2025, 1, 15)]
