"""A dated report must not carry evidence filed after its own date."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from app.services.reporting.report_builder import _pit_dates, _pit_footprints


def test_events_after_the_report_date_are_excluded():
    report_date = date(2025, 1, 15)
    dates = {date(2022, 6, 1), date(2024, 3, 3), date(2025, 1, 15), date(2026, 1, 20)}
    assert _pit_dates(dates, date(2023, 1, 15), report_date) == [date(2024, 3, 3), date(2025, 1, 15)]


def test_pit_footprints_is_deprecated_and_no_longer_on_the_report_path():
    # Kept only for out-of-tree callers: filtering finished footprints erased
    # amendments a later comparative touched (see
    # tests/unit/test_restatements.py::TestPointInTime). The report now passes
    # `as_of` into detection instead.
    import inspect

    from app.services.reporting import report_builder

    assert "_pit_footprints(" not in inspect.getsource(report_builder._collect_streams)
    assert "as_of=report_date" in inspect.getsource(report_builder._collect_streams)
    fps = [SimpleNamespace(current_filed=date(2024, 8, 1)),
           SimpleNamespace(current_filed=date(2025, 6, 1))]
    assert [f.current_filed for f in _pit_footprints(fps, date(2025, 1, 15))] == [date(2024, 8, 1)]
