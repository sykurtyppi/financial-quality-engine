"""A defect in this code must not be reported to the reader as an SEC outage.

Each evidence stream is wrapped in a broad `except Exception` so that one
failing stream never costs the whole report. That is right, and it was also
labelling every failure a fetch/parse problem. A misplaced variable in the
restatement stream reached the reader as:

    Restatement appendix UNAVAILABLE (fetch/parse failed: name 'field_tags'
    is not defined). Absence of that section is a data gap, not evidence of
    no revisions.

Two harms at once: it blames SEC for a bug, and it tells the reader to treat
a section that was never computed as an acquisition gap — which is exactly
the "missing data is not clean data" confusion the report is built to avoid.
The bug was caught by an unrelated assertion; nothing about the message would
have led anyone to the cause.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.reporting.report_builder import _collect_streams, _stream_failure


class _Client:
    """A complete stub. It has to be complete now — an incomplete one raises
    AttributeError, which this change deliberately stops treating as a data
    gap. That is the trade-off working as intended."""

    def company_facts(self, ticker):
        return {"facts": {}}

    def company_facts_by_cik(self, cik):
        return {"facts": {}}

    def resolve_cik(self, ticker):
        return 320193

    def submissions(self, ticker):
        return {"filings": {"recent": {}}}

    def submissions_by_cik(self, cik):
        return {"filings": {"recent": {}}}


@pytest.mark.parametrize("exc", [
    NameError("name 'field_tags' is not defined"),
    ImportError("cannot import name 'gone'"),
    AssertionError("invariant broken"),
])
def test_a_defect_is_raised_not_described_as_a_data_gap(exc):
    with pytest.raises(type(exc)):
        _stream_failure(exc)


@pytest.mark.parametrize("exc", [
    OSError("connection reset"),
    ValueError("payload is not valid JSON"),
    KeyError("cik"),
    RuntimeError("SEC request failed: 403 fair-access throttle"),
    # Narrowed after review: these three are what MALFORMED SEC DATA raises —
    # a null `filings.recent` makes `.get` an AttributeError — so propagating
    # them traded a misleading notice for an outage on someone else's bad
    # input. Nothing left in _PROGRAMMING_ERRORS can be caused by a payload.
    AttributeError("'NoneType' object has no attribute 'get'"),
    TypeError("string indices must be integers"),
    IndexError("list index out of range"),
])
def test_an_acquisition_failure_is_still_recorded(exc):
    # These are genuinely about what came back from SEC (or did not), and a
    # report that degrades gracefully is better than one that does not build.
    assert _stream_failure(exc) == str(exc)


def test_a_broken_stream_surfaces_the_defect_rather_than_a_notice(monkeypatch):
    """End to end through the real `_collect_streams` body: a stream that
    raises a defect must propagate, not land in `errors` as a fetch failure."""
    import app.services.ingestion.restatements as restatements_mod

    def broken(*a, **k):
        raise NameError("name 'field_tags' is not defined")

    monkeypatch.setattr(restatements_mod, "scan_restatements", broken)
    with pytest.raises(NameError):
        _collect_streams(_Client(), "AAPL", date(2026, 9, 21), company_facts={"facts": {}})


def test_an_sec_outage_still_degrades_gracefully(monkeypatch):
    """The property the broad catch exists for, unchanged: a real acquisition
    failure is recorded and the other streams still produce their sections."""
    import app.services.ingestion.restatements as restatements_mod

    def outage(*a, **k):
        raise RuntimeError("SEC request failed: 503")

    monkeypatch.setattr(restatements_mod, "scan_restatements", outage)
    _sections, _events, _tier1, errors, _takedowns, _scan = _collect_streams(
        _Client(), "AAPL", date(2026, 9, 21), company_facts={"facts": {}}
    )
    assert errors["restatements"] == "SEC request failed: 503"


def test_a_malformed_sec_payload_degrades_rather_than_breaking_the_report():
    """The reliability case the first version regressed. SEC returning
    `filings.recent = null` is bad input, not a bug in this code; the report
    must still build and say which streams it could not read."""
    class _Malformed(_Client):
        def submissions(self, ticker):
            return {"filings": {"recent": None}}

        def submissions_by_cik(self, cik):
            return {"filings": {"recent": None}}

    _sections, _events, _tier1, errors, _takedowns, _scan = _collect_streams(
        _Malformed(), "AAPL", date(2026, 9, 21), company_facts={"facts": {}}
    )
    assert errors["offerings"] and "NoneType" in errors["offerings"]
    assert errors["events"] and "NoneType" in errors["events"]


def test_the_propagating_set_cannot_be_raised_by_data():
    """The rule, stated as a test: a payload cannot produce any of these.
    Anything that a malformed SEC response CAN raise must degrade instead."""
    from app.services.reporting.report_builder import _PROGRAMMING_ERRORS

    assert set(_PROGRAMMING_ERRORS) == {NameError, ImportError, AssertionError}
