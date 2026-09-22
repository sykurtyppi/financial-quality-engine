"""A defect in this code must not be reported to the reader as an SEC outage,
and an SEC problem must not be reported as a defect.

Each evidence stream is contained so one failing stream never costs the whole
report. The old containment then guessed WHY from the exception class, and
the classes overlap: malformed SEC data raises the same AttributeError or
TypeError a bug does. A misplaced variable reached the reader as

    Restatement appendix UNAVAILABLE (fetch/parse failed: name 'field_tags'
    is not defined). Absence of that section is a data gap, ...

— blaming SEC for a bug and telling the reader to treat a section that was
never computed as an acquisition gap.

Now the parsers check payload shapes and raise `ExternalPayloadError`, so the
classification is by construction: SecClientError / ExternalPayloadError /
OSError are data failures; anything else is a defect. Defects propagate in
tests (conftest sets STRICT_STREAMS) and, in production, are rendered as an
INTERNAL error that explicitly is not a data gap.
"""

from __future__ import annotations

import logging
from datetime import date

import pytest

from app.services.ingestion.payloads import ExternalPayloadError
from app.services.ingestion.sec_client import SecClientError
from app.services.reporting import report_builder
from app.services.reporting.report_builder import (
    STREAM_DATA_ERRORS,
    StreamFailure,
    _collect_streams,
    data_quality_section,
)

DAY = date(2026, 9, 21)


class _Client:
    """A complete stub: an incomplete one raises AttributeError, which is a
    defect and now fails the test instead of reading as a data gap."""

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


def _errors(client=None, **kw):
    out = _collect_streams(client or _Client(), "AAPL", DAY, company_facts={"facts": {}}, **kw)
    return out[3]


def _break_restatements(monkeypatch, exc):
    import app.services.ingestion.restatements as restatements_mod

    def broken(*a, **k):
        raise exc

    monkeypatch.setattr(restatements_mod, "scan_restatements", broken)


# --- data failures degrade, with the data-gap wording ---------------------------

@pytest.mark.parametrize("exc", [
    SecClientError("SEC request failed: 503"),
    ExternalPayloadError("filings.recent.form is NoneType, expected a list"),
    OSError("disk full"),
])
def test_a_data_failure_is_recorded_as_data(monkeypatch, exc):
    _break_restatements(monkeypatch, exc)
    failure = _errors()["restatements"]
    assert failure == StreamFailure("data", str(exc))
    line = data_quality_section(
        fetched_at="t", fresh=False, coverage=1.0, warnings=[], doc_diagnostics=[],
        restatements_error=failure,
    )
    assert f"**Restatement appendix UNAVAILABLE** (fetch/parse failed: {exc})" in line
    assert "data gap, not evidence of no revisions" in line
    assert "internal error" not in line


def test_the_data_error_set_is_exactly_the_three_sources():
    """Widening this set is how a defect gets disguised as a data gap again."""
    assert set(STREAM_DATA_ERRORS) == {SecClientError, ExternalPayloadError, OSError}


def test_a_malformed_sec_payload_is_named_as_such():
    """`filings.recent = null` is bad input from SEC, not a bug here: the
    report still builds, and the notice names what was malformed instead of
    quoting an AttributeError about NoneType."""

    class _Malformed(_Client):
        def submissions(self, ticker):
            return {"filings": {"recent": None}}

        def submissions_by_cik(self, cik):
            return {"filings": {"recent": None}}

    errors = _errors(_Malformed())
    for stream in ("offerings", "events"):
        assert errors[stream].kind == "data"
        assert "submissions.filings.recent is NoneType" in errors[stream].message


def test_an_offerings_outage_recorded_without_raising_is_still_data():
    """`fetch_offerings` swallows a submissions outage into `acquisition_error`
    rather than raising, so it never reaches the classifier — it must still
    be labelled a data gap, not an internal error."""

    class _Outage(_Client):
        def submissions_by_cik(self, cik):
            raise SecClientError("SEC request failed: 503")

    failure = _errors(_Outage())["offerings"]
    assert failure == StreamFailure("data", "SEC request failed: 503")
    line = data_quality_section(
        fetched_at="t", fresh=False, coverage=1.0, warnings=[], doc_diagnostics=[],
        offerings_error=failure,
    )
    assert "**Capital-markets appendix UNAVAILABLE** (fetch/parse failed: SEC request failed: 503)" in line


# --- defects propagate in tests ----------------------------------------------------

@pytest.mark.parametrize("exc", [
    NameError("name 'field_tags' is not defined"),
    TypeError("unsupported operand type(s)"),
    AttributeError("'NoneType' object has no attribute 'get'"),
    KeyError("cik"),
    RuntimeError("anything that is not a SecClientError"),
    ValueError("a plain ValueError is not a payload error"),
])
def test_a_defect_propagates_under_strict_streams(monkeypatch, exc):
    _break_restatements(monkeypatch, exc)
    with pytest.raises(type(exc)):
        _errors()


# --- defects in production: contained, and labelled as defects ----------------------

def test_in_production_a_defect_renders_as_an_internal_error(monkeypatch, caplog):
    from app.core.pipeline import analyze
    from app.services.reporting.report_builder import build_report
    from tests.fixtures.companies import stretch_dataset

    monkeypatch.setattr(report_builder, "STRICT_STREAMS", False)
    _break_restatements(monkeypatch, TypeError("unsupported operand type(s)"))
    ds = stretch_dataset()
    with caplog.at_level(logging.ERROR, logger=report_builder.__name__):
        report, _ = build_report(
            analyze(ds), ds, generated_on="2026-09-21", coverage=1.0,
            client=_Client(), ticker="AAPL", company_facts={"facts": {}},
            fetched_at="2026-09-21T00:00:00",
        )
    assert (
        "**Restatement appendix UNAVAILABLE — internal error** "
        "(TypeError: unsupported operand type(s)). This is a defect in this tool, "
        "not a data gap"
    ) in report
    assert "not evidence of no revisions" in report
    assert "fetch/parse failed: unsupported" not in report
    assert "restatement footprints (internal error)" in report
    # The other streams still ran.
    assert "## Capital Markets Activity" in report
    records = [r for r in caplog.records if "defect" in r.getMessage()]
    assert len(records) == 1 and records[0].exc_info is not None


def test_in_production_a_data_failure_keeps_the_plain_card_wording(monkeypatch):
    from app.core.pipeline import analyze
    from app.services.reporting.report_builder import build_report
    from tests.fixtures.companies import stretch_dataset

    monkeypatch.setattr(report_builder, "STRICT_STREAMS", False)
    _break_restatements(monkeypatch, SecClientError("SEC request failed: 503"))
    ds = stretch_dataset()
    report, _ = build_report(
        analyze(ds), ds, generated_on="2026-09-21", coverage=1.0,
        client=_Client(), ticker="AAPL", company_facts={"facts": {}},
            fetched_at="2026-09-21T00:00:00",
    )
    assert "not checked this run: restatement footprints," in report
    assert "**Restatement appendix UNAVAILABLE** (fetch/parse failed: SEC request failed: 503)" in report
    assert "internal error" not in report


@pytest.mark.parametrize("env, strict", [("1", True), ("0", False), (None, False)])
def test_strict_mode_follows_the_operator_env_when_not_overridden(monkeypatch, env, strict):
    monkeypatch.setattr(report_builder, "STRICT_STREAMS", None)
    if env is None:
        monkeypatch.delenv("FQE_STRICT_STREAMS", raising=False)
    else:
        monkeypatch.setenv("FQE_STRICT_STREAMS", env)
    _break_restatements(monkeypatch, TypeError("boom"))
    if strict:
        with pytest.raises(TypeError):
            _errors()
    else:
        assert _errors()["restatements"] == StreamFailure("internal", "TypeError: boom")


def test_a_plain_string_error_still_renders_as_data():
    """Callers that pass a message rather than a StreamFailure keep today's
    wording."""
    line = data_quality_section(
        fetched_at="t", fresh=False, coverage=1.0, warnings=[], doc_diagnostics=[],
        events_error="SEC request failed: 503",
    )
    assert "**Event (8-K 4.02) appendix UNAVAILABLE** (fetch/parse failed: SEC request failed: 503)" in line
