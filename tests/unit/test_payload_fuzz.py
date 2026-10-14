"""Malformed SEC payloads reach every evidence-stream parser as
`ExternalPayloadError`, and as nothing else.

That is the property the report's failure labels rest on: once a parser can
only fail on bad input with `ExternalPayloadError`, any other exception out of
a stream is a defect in this code, and the report can say so instead of
calling it a data gap. Example tests cannot establish it — the defects that
motivated it were shapes nobody wrote an example for — so this mutates valid
payloads at random nodes (hypothesis, `tests/strategies.mutated`) and checks
every outcome. A failure shrinks to a single minimal mutation.
"""

from __future__ import annotations

import tempfile
from datetime import date, datetime
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.backtesting.events import fetch_entity_events
from app.services.ingestion import vintages
from app.services.ingestion.filing_events import filing_events
from app.services.ingestion.offerings import fetch_offerings
from app.services.ingestion.payloads import ExternalPayloadError
from app.services.ingestion.restatements import scan_restatements
from tests.strategies import mutated, mutated_column_element

CIK = 320193

_PROSPECTUS = (
    b"<html><body>PROSPECTUS SUPPLEMENT 10,000,000 Shares of Common Stock "
    b"at a public offering price of $12.50 per share</body></html>"
)


def _submissions() -> dict:
    forms = ["424B5", "S-3", "8-K", "10-Q", "8-K", "424B3", "S-1"]
    return {
        "cik": str(CIK),
        "name": "TestCo",
        "sic": "3674",
        "sicDescription": "Semiconductors",
        "filings": {"recent": {
            "form": forms,
            "filingDate": ["2026-08-01", "2026-05-02", "2026-04-03", "2026-03-04",
                           "2025-12-05", "2025-11-06", "2025-10-07"],
            "accessionNumber": [f"0000320193-26-00000{i}" for i in range(len(forms))],
            "primaryDocument": ["p424.htm", "s3.htm", "8k.htm", "q.htm", "8k2.htm",
                                "p3.htm", "s1.htm"],
            "items": ["", "", "4.02,9.01", "", "2.02", "", ""],
        }},
    }


def _flow(start, end, val, filed, form="10-Q", accn="a1"):
    return {"start": start, "end": end, "val": val, "filed": filed, "form": form, "accn": accn}


def _inst(end, val, filed, form="10-Q", accn="a1"):
    return {"end": end, "val": val, "filed": filed, "form": form, "accn": accn}


def _companyfacts() -> dict:
    return {"cik": CIK, "entityName": "TestCo", "facts": {
        "us-gaap": {
            "Assets": {"units": {"USD": [
                _inst("2025-12-31", 1000.0, "2026-02-01"),
                _inst("2026-03-31", 1100.0, "2026-05-01"),
                _inst("2025-12-31", 1050.0, "2026-05-01", "10-Q/A", "a2"),
            ]}},
            "Revenues": {"units": {"USD": [
                _flow("2026-01-01", "2026-03-31", 500.0, "2026-05-01"),
                _flow("2025-10-01", "2025-12-31", 450.0, "2026-02-01"),
                _flow("2025-10-01", "2025-12-31", 470.0, "2026-05-01", "10-Q/A", "a2"),
            ]}},
            "LongTermDebtNoncurrent": {"units": {"USD": [
                _inst("2026-03-31", 300.0, "2026-05-01"),
            ]}},
        },
        "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
            _inst("2026-04-20", 1.0e9, "2026-05-01"),
        ]}}},
    }}


def _check(target: str, fn) -> None:
    try:
        fn()
    except ExternalPayloadError:
        pass
    except Exception as e:  # noqa: BLE001 - the property under test
        pytest.fail(
            f"{target} raised {type(e).__name__}: {e} — a malformed payload must "
            "surface as ExternalPayloadError"
        )


class _Client:
    def resolve_cik(self, ticker):
        return CIK

    def _get(self, url):
        return _PROSPECTUS

    def archive_text(self, cik, accession, doc, *, honor_fresh=True):
        return _PROSPECTUS.decode()


# --- the valid baselines are valid ---------------------------------------------------

def test_the_base_payloads_parse_cleanly():
    """Otherwise every mutation below "passes" by failing for the same reason."""
    timeline = fetch_offerings(_Client(), "T", as_of=date(2026, 9, 21), submissions=_submissions())
    assert timeline.acquisition_error is None and timeline.filings
    events = fetch_entity_events(_Client(), "T", submissions=_submissions())
    assert events.non_reliance_8k_dates == [date(2026, 4, 3)] and events.sic == 3674
    scan = scan_restatements(_companyfacts(), period_since=date(2024, 1, 1), as_of=date(2026, 9, 21))
    assert scan.footprints


# --- the property ---------------------------------------------------------------

@settings(max_examples=150)
@given(payload=mutated(_submissions()))
def test_offerings_reject_malformed_submissions_only_as_payload_errors(payload):
    _check("fetch_offerings", lambda: fetch_offerings(
        _Client(), "T", as_of=date(2026, 9, 21), submissions=payload))


@settings(max_examples=150)
@given(payload=mutated_column_element(_submissions()))
def test_offerings_reject_a_malformed_filing_row_only_as_a_payload_error(payload):
    """Targeted: every example corrupts one filing-row element, so the date
    and form paths of offering rows are exercised on every run rather than
    by chance (PR 1.6's whole-tree mutation stopped reaching them)."""
    _check("fetch_offerings", lambda: fetch_offerings(
        _Client(), "T", as_of=date(2026, 9, 21), submissions=payload))


@settings(max_examples=150)
@given(payload=mutated_column_element(_submissions()))
def test_events_reject_a_malformed_filing_row_only_as_a_payload_error(payload):
    _check("fetch_entity_events", lambda: fetch_entity_events(_Client(), "T", submissions=payload))


@settings(max_examples=150)
@given(payload=mutated(_submissions()))
def test_events_reject_malformed_submissions_only_as_payload_errors(payload):
    _check("fetch_entity_events", lambda: fetch_entity_events(_Client(), "T", submissions=payload))


def _with_periods(subs: dict) -> dict:
    """The base index with period dates, so the filing-lag path is fuzzed too."""
    recent = subs["filings"]["recent"]
    recent["reportDate"] = ["2026-06-30" if f.startswith("10-") else "" for f in recent["form"]]
    return subs


@settings(max_examples=150)
@given(payload=mutated(_with_periods(_submissions())))
def test_filing_events_reject_malformed_submissions_only_as_payload_errors(payload):
    _check("filing_events", lambda: filing_events(
        payload, since=date(2024, 9, 21), as_of=date(2026, 9, 21)))


@settings(max_examples=150)
@given(payload=mutated_column_element(_with_periods(_submissions())))
def test_filing_events_reject_a_malformed_filing_row_only_as_a_payload_error(payload):
    _check("filing_events", lambda: filing_events(
        payload, since=date(2024, 9, 21), as_of=date(2026, 9, 21)))


@settings(max_examples=150)
@given(payload=mutated(_companyfacts()), selected=st.booleans())
def test_restatements_reject_malformed_companyfacts_only_as_payload_errors(payload, selected):
    tags = {"total_assets": "us-gaap:Assets", "revenue": "us-gaap:Revenues",
            "total_debt": "LongTermDebtNoncurrent+none+none"} if selected else None
    _check("scan_restatements", lambda: scan_restatements(
        payload, period_since=date(2024, 1, 1), as_of=date(2026, 9, 21), selected_tags=tags))


@settings(max_examples=60)
@given(payload=mutated(_companyfacts()))
def test_vintage_diff_rejects_a_malformed_snapshot_only_as_a_payload_error(payload):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        vintages.store_snapshot(CIK, _companyfacts(), now=datetime(2026, 9, 19, 12), root=root)
        vintages.store_snapshot(CIK, payload, now=datetime(2026, 9, 20, 12), root=root)
        _check("report_diff", lambda: vintages.report_diff(
            CIK, as_of=date(2026, 9, 21), since=date(2023, 1, 1), root=root))
