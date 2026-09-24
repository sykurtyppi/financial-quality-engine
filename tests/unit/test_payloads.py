"""The validated accessors in `ingestion/payloads.py`: what each accepts, what
it rejects, and that valid shapes read exactly as the old ad-hoc `.get`
chains did."""

from __future__ import annotations

from datetime import date

import pytest

from app.services.ingestion.payloads import (
    ExternalPayloadError,
    SubmissionsMismatchError,
    check_aligned,
    concept_rows,
    recent_filings,
    sec_date,
)
from app.services.ingestion.sec_client import assert_submissions_match


@pytest.mark.parametrize("s, d", [("2024-02-29", date(2024, 2, 29)), ("2026-09-21", date(2026, 9, 21))])
def test_sec_date_accepts_real_iso_dates(s, d):
    assert sec_date(s, "filingDate") == d


@pytest.mark.parametrize("bad", ["2024-02-30", "2023-02-29", "20240101", "2024-1-01", "", None, 1, [], "2024-13-45"])
def test_sec_date_rejects_anything_else(bad):
    with pytest.raises(ExternalPayloadError, match="filingDate"):
        sec_date(bad, "filingDate")


def _subs(**recent):
    return {"filings": {"recent": recent}}


def test_recent_filings_reads_rows_in_column_order():
    subs = _subs(form=["8-K", "10-Q"], filingDate=["2026-01-02", "2026-02-03"], items=["4.02", None])
    assert recent_filings(subs, form=str, items=(str, type(None)), filingDate=str) == [
        ("8-K", "4.02", "2026-01-02"), ("10-Q", None, "2026-02-03"),
    ]


@pytest.mark.parametrize("subs", [{}, {"filings": {}}, _subs()])
def test_no_filings_is_an_empty_list_not_an_error(subs):
    assert recent_filings(subs, form=str) == []


def test_unequal_columns_are_refused_not_truncated():
    """Hermes audit round 3, finding 2: zip() dropped the rows past the
    shortest column, and a section then said "none found" about filings it
    never read. Nothing says which element is missing, so nothing can be
    re-aligned: the payload is malformed."""
    subs = _subs(form=["8-K", "10-Q", "S-3"], filingDate=["2026-01-02"])
    with pytest.raises(ExternalPayloadError, match=r"unequal lengths \(form=3, filingDate=1\)"):
        recent_filings(subs, form=str, filingDate=str)


def test_an_unrequested_column_does_not_have_to_line_up():
    subs = _subs(form=["8-K"], filingDate=["2026-01-02"], items=["2.02", "4.02"])
    assert recent_filings(subs, form=str, filingDate=str) == [("8-K", "2026-01-02")]


def test_optional_columns_absent_read_as_none():
    subs = _subs(form=["8-K", "10-Q"], filingDate=["2026-01-02", "2026-01-03"])
    assert recent_filings(subs, form=str, optional={"reportDate": (str, type(None))}) == [
        ("8-K", None), ("10-Q", None),
    ]


def test_optional_columns_present_must_line_up_and_type_check():
    subs = _subs(form=["8-K", "10-Q"], reportDate=["2025-12-31"])
    with pytest.raises(ExternalPayloadError, match="unequal lengths"):
        recent_filings(subs, form=str, optional={"reportDate": str})
    subs = _subs(form=["8-K"], reportDate=[7])
    with pytest.raises(ExternalPayloadError, match=r"reportDate\[0\] is int"):
        recent_filings(subs, form=str, optional={"reportDate": str})
    subs = _subs(form=["8-K"], reportDate=["2025-12-31"])
    assert recent_filings(subs, form=str, optional={"reportDate": str}) == [("8-K", "2025-12-31")]


def test_check_aligned():
    assert check_aligned({"a": [1, 2], "b": [3, 4]}, "x") == 2
    assert check_aligned({}, "x") == 0
    with pytest.raises(ExternalPayloadError, match="x columns have unequal lengths"):
        check_aligned({"a": [1], "b": []}, "x")


@pytest.mark.parametrize("subs, needle", [
    (None, "submissions payload is NoneType"),
    ([], "submissions payload is list"),
    ({"filings": None}, "submissions.filings is NoneType"),
    ({"filings": {"recent": []}}, "submissions.filings.recent is list"),
    (_subs(form=["8-K"]), "no 'filingDate' column"),
    (_subs(form="8-K", filingDate=["2026-01-02"]), "filings.recent.form is str, expected a list"),
    (_subs(form=["8-K", 7], filingDate=["2026-01-02", "2026-01-03"]), "filings.recent.form[1] is int, expected str"),
])
def test_recent_filings_names_what_is_malformed(subs, needle):
    with pytest.raises(ExternalPayloadError) as e:
        recent_filings(subs, form=str, filingDate=str)
    assert needle in str(e.value)


def _facts(units):
    return {"facts": {"us-gaap": {"Assets": {"units": units}}}}


def test_concept_rows_reads_rows_and_absent_levels_as_empty():
    row = {"end": "2026-03-31", "val": 1.0, "filed": "2026-05-01"}
    assert concept_rows(_facts({"USD": [row]}), "us-gaap", "Assets", "USD") == [row]
    assert concept_rows({}, "us-gaap", "Assets", "USD") == []
    assert concept_rows({"facts": {}}, "us-gaap", "Assets", "USD") == []
    assert concept_rows(_facts({}), "us-gaap", "Assets", "USD") == []
    assert concept_rows({"facts": {"us-gaap": {"Assets": {}}}}, "us-gaap", "Assets", "USD") == []


def test_concept_rows_keeps_the_mis_filed_share_count_fallback():
    row = {"end": "2026-03-31", "val": 1e9, "filed": "2026-05-01"}
    assert concept_rows(_facts({"USD": [row]}), "us-gaap", "Assets", "shares") == [row]
    assert concept_rows(_facts({"USD": [row]}), "us-gaap", "Assets", "EUR") == []


def test_row_level_problems_drop_the_row():
    good = {"end": "2026-03-31", "val": 1.0, "filed": "2026-05-01"}
    unparseable = {"end": "not-a-date", "val": "NaN-ish", "filed": "2026-05-01"}
    rows = concept_rows(_facts({"USD": [good, "not a fact", unparseable]}), "us-gaap", "Assets", "USD")
    assert rows == [good, unparseable]  # string fields that do not parse are the callers' drop


@pytest.mark.parametrize("payload, needle", [
    (None, "companyfacts payload is NoneType"),
    ({"facts": []}, "companyfacts.facts is list"),
    ({"facts": {"us-gaap": 0}}, "companyfacts.facts.us-gaap is int"),
    ({"facts": {"us-gaap": {"Assets": "x"}}}, "us-gaap:Assets is str"),
    (_facts([]), "us-gaap:Assets.units is list"),
    (_facts({"USD": {}}), "USD rows are dict"),
    (_facts({"USD": [{"end": {"a": 1}}]}), "end={'a': 1}"),
    (_facts({"USD": [{"end": "2026-03-31", "form": None}]}), "form=None"),
    (_facts({"USD": [{"end": "2026-03-31", "filed": 20260501}]}), "filed=20260501"),
])
def test_concept_rows_rejects_malformed_structure(payload, needle):
    with pytest.raises(ExternalPayloadError) as e:
        concept_rows(payload, "us-gaap", "Assets", "USD")
    assert needle in str(e.value)


def test_a_null_start_is_an_instant_not_an_error():
    row = {"start": None, "end": "2026-03-31", "val": 1.0, "filed": "2026-05-01"}
    assert concept_rows(_facts({"USD": [row]}), "us-gaap", "Assets", "USD") == [row]


def test_a_submissions_cik_mismatch_is_typed_and_keeps_its_message():
    with pytest.raises(SubmissionsMismatchError) as e:
        assert_submissions_match({"cik": "1045810"}, 320193)
    assert isinstance(e.value, ValueError)  # existing `except ValueError` callers
    assert str(e.value) == "submissions payload is for CIK '1045810', not the pinned CIK 320193"
    assert_submissions_match({"cik": "0000320193"}, 320193)
    assert_submissions_match(["not", "a", "dict"], None)  # unpinned: nothing to check
    with pytest.raises(ExternalPayloadError, match="submissions payload is list"):
        assert_submissions_match(["not", "a", "dict"], 320193)
