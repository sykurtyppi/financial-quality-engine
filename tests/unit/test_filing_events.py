"""Dated filing events: what a filer did, and when, from its filing index.

Each event kind is recognised from the index alone, only filings inside
`[since, as_of]` count (a replay never lists a later one), and a filer that
suddenly files later than its own habit is named. On the report, an auditor
change and a late-filing notice join 8-K 4.02 on the card's Tier 1, and
every event reaches the ledger with its accession.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.ingestion.filing_events import (
    AUDITOR_CHANGE,
    LAG_DRIFT_DAYS,
    LATE_FILING,
    NON_RELIANCE,
    filing_events,
    render_filing_events_section,
)
from app.services.ingestion.payloads import ExternalPayloadError

SINCE, AS_OF = date(2024, 9, 1), date(2026, 9, 1)


def _index(rows: list[tuple], *, report_dates: bool = True) -> dict:
    """rows: (form, filed, items, accession[, reportDate])."""
    recent = {
        "form": [r[0] for r in rows], "filingDate": [r[1] for r in rows],
        "items": [r[2] for r in rows], "accessionNumber": [r[3] for r in rows],
    }
    if report_dates:
        recent["reportDate"] = [r[4] if len(r) > 4 else "" for r in rows]
    return {"cik": "1", "filings": {"recent": recent}}


def _kinds(fe) -> list[tuple[str, str]]:
    return [(e.kind, e.accession) for e in fe.events]


def test_each_event_kind_is_recognised():
    fe = filing_events(_index([
        ("8-K", "2026-03-02", "4.02,9.01", "a-402"),
        ("8-K", "2026-02-02", "4.01", "a-401"),
        ("8-K", "2026-01-05", "2.06, 9.01", "a-206"),
        ("8-K", "2026-01-06", "2.02,9.01", "a-202"),  # earnings: not an event
        ("NT 10-K", "2026-03-03", "", "a-nt"),
        ("NT 10-Q", "2025-11-12", None, "a-ntq"),
        ("10-K/A", "2026-04-20", "", "a-ka"),
        ("10-Q/A", "2025-12-01", "", "a-qa"),
        ("S-8", "2026-01-01", "", "a-s8"),
    ]), since=SINCE, as_of=AS_OF)
    assert sorted(_kinds(fe)) == sorted([
        ("non_reliance", "a-402"), ("auditor_change", "a-401"), ("impairment", "a-206"),
        ("late_filing_notice", "a-nt"), ("late_filing_notice", "a-ntq"),
        ("amendment", "a-ka"), ("amendment", "a-qa"),
    ])
    signals = {e.accession: e.signal for e in fe.events}
    assert signals["a-402"] == NON_RELIANCE and signals["a-401"] == AUDITOR_CHANGE
    assert signals["a-nt"] == signals["a-ntq"] == LATE_FILING
    assert signals["a-206"] is None and signals["a-ka"] is None
    assert [e.filed for e in fe.events] == sorted((e.filed for e in fe.events), reverse=True)


def test_the_window_is_inclusive_and_a_later_filing_is_invisible():
    fe = filing_events(_index([
        ("8-K", SINCE.isoformat(), "4.01", "first-day"),
        ("8-K", AS_OF.isoformat(), "4.01", "last-day"),
        ("8-K", (SINCE - timedelta(days=1)).isoformat(), "4.01", "before"),
        ("8-K", (AS_OF + timedelta(days=1)).isoformat(), "4.01", "after"),
    ]), since=SINCE, as_of=AS_OF)
    assert {a for _k, a in _kinds(fe)} == {"first-day", "last-day"}


def _quarterlies(lags: list[int], *, start=date(2023, 3, 31)) -> list[tuple]:
    rows = []
    for n, lag in enumerate(lags):
        period = start + timedelta(days=91 * n)
        rows.append(("10-Q", (period + timedelta(days=lag)).isoformat(), "", f"q{n}",
                     period.isoformat()))
    return rows


@pytest.mark.parametrize(("extra", "fires"), [(LAG_DRIFT_DAYS, True), (LAG_DRIFT_DAYS - 1, False)])
def test_a_filing_later_than_the_filers_habit_is_named(extra, fires):
    lags = [40, 41, 39, 40, 40, 41, 40 + extra]
    fe = filing_events(_index(_quarterlies(lags)), since=SINCE, as_of=AS_OF)
    hits = [e for e in fe.events if e.kind == "filing_lag"]
    assert bool(hits) is fires
    if fires:
        assert hits[0].accession == "q6" and "against a median of 40" in hits[0].detail


def test_lag_drift_needs_a_habit_and_period_dates():
    short = filing_events(_index(_quarterlies([40, 40, 40, 90])), since=date(2023, 1, 1),
                          as_of=AS_OF)
    assert not [e for e in short.events if e.kind == "filing_lag"]  # three priors
    blind = filing_events(_index(_quarterlies([40] * 6 + [90]), report_dates=False),
                          since=SINCE, as_of=AS_OF)
    assert not blind.lag_checked and not blind.events
    assert "Filing-lag drift not checked" in render_filing_events_section(blind)
    # A 10-Q filed after the report date neither counts as drift nor as history.
    late = _quarterlies([40] * 6 + [90])
    cut = date.fromisoformat(late[-1][1]) - timedelta(days=1)
    assert not filing_events(_index(late), since=SINCE, as_of=cut).events


def test_a_malformed_index_is_a_payload_error():
    with pytest.raises(ExternalPayloadError):
        filing_events(_index([("8-K", "2026-13-40", "4.01", "x")]), since=SINCE, as_of=AS_OF)
    with pytest.raises(ExternalPayloadError):
        filing_events({"filings": {"recent": {"form": ["8-K"]}}}, since=SINCE, as_of=AS_OF)


def test_the_section_lists_every_event_or_says_there_are_none():
    fe = filing_events(_index([("8-K", "2026-02-02", "4.01", "a-401"),
                               ("10-Q/A", "2025-12-01", "", "a-qa")]),
                       since=SINCE, as_of=AS_OF)
    text = render_filing_events_section(fe)
    assert "| 2026-02-02 | 8-K | Auditor change (8-K 4.01) | a-401 |" in text
    assert "| 2025-12-01 | 10-Q/A | Amendment | a-qa |" in text
    empty = render_filing_events_section(filing_events(_index([]), since=SINCE, as_of=AS_OF))
    assert "No non-reliance, auditor-change" in empty and f"between {SINCE} and {AS_OF}" in empty


# --- on the report ------------------------------------------------------------------


def _report(index: dict, tmp_path, day: str = "2026-09-01"):
    from app.core.pipeline import analyze
    from app.schemas.ledger import LedgerDocument
    from app.services.reporting.report_builder import build_report
    from tests.fixtures.companies import stretch_dataset

    class Client:
        def resolve_cik(self, ticker):
            return 1

        def company_facts(self, ticker):
            return {"facts": {}}

        def submissions_by_cik(self, cik):
            return index

        def _get(self, url):
            return b""

        def archive_text(self, *a, **k):
            return ""

    ds = stretch_dataset()
    out = tmp_path / "ledger.json"
    report, _ = build_report(
        analyze(ds), ds, generated_on=day, client=Client(), ticker="X",
        fetched_at="2026-09-01 09:00 UTC", company_facts={"facts": {}}, submissions=index,
        ledger_out=out,
    )
    return report, LedgerDocument.model_validate_json(out.read_text())


def test_the_card_promotes_4_01_and_nt_and_lists_4_02_once(tmp_path):
    index = _index([
        ("8-K", "2026-03-02", "4.02", "0001-26-000402"),
        ("8-K", "2026-02-02", "4.01", "0001-26-000401"),
        ("NT 10-Q", "2026-05-15", "", "0001-26-000099"),
        ("10-Q/A", "2025-12-01", "", "0001-25-000077"),
    ])
    report, ledger = _report(index, tmp_path)
    card = report.split("# Full report (appendix)")[0]
    assert card.count("8-K Item 4.02 non-reliance (restatement announced) filed 2026-03-02") == 1
    assert "8-K Item 4.01 change of auditor filed 2026-02-02" in card
    assert "NT 10-Q (notice of late filing) filed 2026-05-15" in card
    assert "10-Q/A amendment" not in card  # listed in the appendix, not promoted
    assert "## Filing Behavior (dated events — not scored)" in report
    assert "| 2025-12-01 | 10-Q/A | Amendment | 0001-25-000077 |" in report
    by_kind = {i.kind: i for i in ledger.items if i.plane.value == "filing_behavior"}
    assert set(by_kind) == {"non_reliance_8k_402", "auditor_change_8k_401",
                            "missed_deadline_nt", "periodic_report_amendment"}
    assert all(i.accessions() for i in by_kind.values())
    assert by_kind["auditor_change_8k_401"].validation_status.value == "validated"
    assert by_kind["periodic_report_amendment"].validation_status.value == "directional"


def test_a_report_dated_in_the_past_lists_no_later_filing(tmp_path):
    index = _index([("8-K", "2026-02-02", "4.01", "0001-26-000401"),
                    ("NT 10-K", "2026-08-15", "", "0001-26-000500")])
    report, ledger = _report(index, tmp_path, day="2026-06-01")
    assert "0001-26-000500" not in report
    assert "0001-26-000401" in report
    assert all(p.filed is None or p.filed <= date(2026, 6, 1)
               for i in ledger.items for p in i.provenance)


def test_a_malformed_period_date_costs_filing_behaviour_not_the_402_alert(tmp_path):
    """Hermes audit round 3, finding 3. A valid 8-K 4.02 beside a malformed
    `reportDate` used to leave the 4.02 on Tier 1 while the same (single)
    events stream was marked unavailable, with no filing-behaviour evidence.
    The two are separate streams now: the 4.02 stands, its evidence is in
    the ledger, and filing behaviour — which alone reads the period dates —
    is unavailable and contributes nothing."""
    index = _index([
        ("8-K", "2026-03-02", "4.02", "0001-26-000402", ""),
        ("8-K", "2026-02-02", "4.01", "0001-26-000401", ""),
        ("10-Q", "2026-05-01", "", "0001-26-000010", "2026-13-45"),
        ("8-K", "2023-01-10", "4.02", "0001-23-000402", ""),  # before the card's window
    ])
    report, ledger = _report(index, tmp_path)
    card = report.split("# Full report (appendix)")[0]
    assert "8-K Item 4.02 non-reliance (restatement announced) filed 2026-03-02" in card
    assert "8-K Item 4.01 change of auditor" not in report  # never read, never claimed
    assert "## Filing Behavior (dated events" not in report
    assert "Filing-behavior (8-K 4.01/2.06, NT, amendments, lag) appendix UNAVAILABLE" in report
    assert "Event (8-K 4.02) appendix UNAVAILABLE" not in report
    assert "8-K 4.01 and NT filing events" in card  # named as not checked
    assert ledger.streams["events"] == "checked"
    assert ledger.streams["filing_events"].startswith("data failure")
    behaviour = [i for i in ledger.items if i.plane.value == "filing_behavior"]
    # From the 4.02 stream only, and only inside the card's window.
    assert [(i.kind, i.accessions()) for i in behaviour] == [
        ("non_reliance_8k_402", ["0001-26-000402"])
    ]


def test_a_misaligned_period_date_column_is_refused():
    index = _index([("8-K", "2026-03-02", "4.02", "0001-26-000402", "")])
    index["filings"]["recent"]["reportDate"] = ["2026-01-01", "2026-02-01"]  # one too many
    with pytest.raises(ExternalPayloadError, match="unequal lengths"):
        filing_events(index, since=SINCE, as_of=AS_OF)


def test_items_are_whole_comma_separated_codes():
    """An item code inside a longer token is not that item: a malformed
    `14.02` is not a 4.02 non-reliance, and `4.011` is not a 4.01."""
    from app.services.ingestion.filing_events import _items

    assert _items(" 4.01, 2.06 ,") == {"4.01", "2.06"}
    assert _items("14.02") == {"14.02"}
    assert _items("4.011,9.01") == {"4.011", "9.01"}
    assert _items(None) == set() and _items("") == set()
