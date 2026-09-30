"""The restatement scan reads the facts the score read (Hermes audit, r25).

Two halves of one defect.

The live report (`scripts/generate_report.py`, the journal's report of
today) maps the WHOLE companyfacts payload — no as-of — but the restatement
scan ran as of the report's local date. EDGAR dates a filing accepted after
5:30pm ET the next business day, and the watcher runs the same evening: a
report written the night a 10-Q lands, dated today, scored that 10-Q while
the scan, cut at today, dropped it. A revision the new 10-Q carried moved the
score and was absent from the evidence; on AAPL the intangible-assets series
the score was built from read "not inspected".

Inside the scan, the derived-quarter check never saw the caller's selection:
it re-ran the mapper on the facts filed by `as_of` and read that run's own
choice. With the date skew the rebuild chose differently (AAPL
`intangible_assets` selected vs nothing; KO `total_debt` another
composition; every fixture lost its newest quarter), and whatever it found
was reported against a series or quarter the engine did not score.

Now the live scan runs through the payload's newest filing, and a rebuild
that drifted from the report's selection or quarters is withheld and named
as a gap on the "Restatement scan:" coverage line. A replay (and the corpus)
already mapped and scanned as of the same day; they are unchanged.
"""

from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.schemas.ledger import LedgerDocument
from app.services.ingestion.companyfacts_mapper import build_dataset
from app.services.ingestion.restatements import (
    render_restatements_section,
    scan_restatements,
)
from app.services.ingestion.selection import SeriesSelection
from app.services.journal import reporting as journal_reporting
from app.services.reporting.report_builder import ledger_path
from tests.integration.test_ledger_provenance import _Client, _submissions

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"
CRM_10Q = "0001108524-26-000127"  # CRM's newest filing: the 10-Q filed 2026-05-28
REVISED = ("2025-02-01", "2025-04-30")  # the prior-year quarter it re-presents


def _real(ticker: str) -> dict:
    return json.loads((REAL / f"companyfacts_{ticker}_trimmed.json").read_text())


def _last_filed(facts: dict) -> date:
    """The newest `filed` date in a payload, found independently of the
    module under test."""
    return max(
        date.fromisoformat(r["filed"])
        for tags in facts["facts"].values() for concept in tags.values()
        for rows in concept["units"].values() for r in rows
    )


def _crm_revised_by_its_newest_10q() -> dict:
    """CRM as filed, with the newest 10-Q's prior-year comparative revenue
    re-presented 5% higher: a revision that exists only in that 10-Q."""
    facts = copy.deepcopy(_real("CRM"))
    rows = facts["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"][
        "units"]["USD"]
    (row,) = [r for r in rows
              if r["accn"] == CRM_10Q and (r["start"], r["end"]) == REVISED]
    row["val"] = round(row["val"] * 1.05)
    return facts


def _report_the_evening_before(monkeypatch, tmp_path, ticker: str, facts: dict,
                               *, replay: bool = False):
    """The journal's report, run the evening the newest filing was accepted:
    the local date is the day BEFORE the `filed` date EDGAR gave it."""
    evening = _last_filed(facts) - timedelta(days=1)

    class _Evening(date):
        @classmethod
        def today(cls):
            return evening

    monkeypatch.setattr(journal_reporting, "SecClient", lambda fresh=False: _Client(facts))
    monkeypatch.setattr(journal_reporting, "fetch_submissions_snapshot",
                        lambda ticker, client: _submissions())
    monkeypatch.setattr(journal_reporting, "date", _Evening)
    out, _ = journal_reporting.build_report(
        ticker, with_docs=False, out_dir=tmp_path, vintage=False, replay=replay,
        report_day=evening.isoformat() if replay else None,
    )
    ledger = LedgerDocument.model_validate_json(ledger_path(out).read_text())
    return evening, out.read_text(), ledger


def _scan_line(report: str) -> str:
    """The coverage line, as the card (with a full stop) and the data-quality
    appendix (without) both print it."""
    (line,) = {ln.rstrip(".") for ln in report.splitlines()
               if ln.startswith("- Restatement scan: ")}
    return line


# --- the live report ---------------------------------------------------------


def test_a_live_report_the_evening_a_10q_lands_scans_the_revision_it_scored(
    monkeypatch, tmp_path
):
    """The defect: scored from the 10-Q filed "tomorrow", scanned without it.
    The revised comparative moved the scored revenue and the report said
    nothing about it."""
    facts = _crm_revised_by_its_newest_10q()
    ds, _ = build_dataset(facts, "CRM")
    scored = next(p.revenue for p in ds.periods if p.period_end.isoformat() == REVISED[1])
    evening, report, ledger = _report_the_evening_before(monkeypatch, tmp_path, "CRM", facts)

    assert ledger.generated_on == evening  # still the report of that evening
    (fp,) = [i for i in ledger.items
             if i.kind == "restatement_footprint" and i.subject == "revenue"]
    assert fp.claim.startswith(f"revenue for {REVISED[0]} → {REVISED[1]} ")
    assert fp.claim.endswith(f"now {scored:,.0f} (10-Q filed {evening + timedelta(days=1)})")
    assert CRM_10Q in {p.accession for p in fp.provenance}
    assert "derived-quarter check NOT inspected" not in report


def test_a_live_report_inspects_the_series_it_scored_from_the_newest_10q(
    monkeypatch, tmp_path
):
    """AAPL as filed: the intangible-assets series the score read is backed
    only by the newest 10-Q's facts, and read "no eligible facts filed by"
    the evening's date — a field the report scored, called uninspectable."""
    facts = _real("AAPL")
    _ds, diag = build_dataset(facts, "AAPL")
    assert diag.selected_series()["intangible_assets"] is not None  # the score read it
    _evening, report, _ledger = _report_the_evening_before(monkeypatch, tmp_path, "AAPL", facts)

    line = _scan_line(report)
    assert "intangible_assets" not in line
    assert line.startswith("- Restatement scan: inspected 23 of 27 fields")


@pytest.mark.parametrize("ticker", ["AAPL", "KO", "CRM"])
def test_the_live_scan_is_the_scan_of_everything_the_score_read(monkeypatch, tmp_path, ticker):
    """Whatever the evening, the live report's scan equals a scan through
    the payload's newest filing, and its derived check rebuilt the scored
    series over the scored quarters (no drift to disclose)."""
    facts = _real(ticker)
    ds, diag = build_dataset(facts, ticker)
    evening, report, _ledger = _report_the_evening_before(monkeypatch, tmp_path, ticker, facts)
    full = scan_restatements(
        facts, period_since=date(evening.year - 3, 1, 1), as_of=_last_filed(facts),
        selected_tags=diag.selected_series(), n_quarters=len(ds.periods),
        scored_quarters=[p.period_end for p in ds.periods],
    )
    assert full.derived_gap is None
    assert _scan_line(report) == f"- Restatement scan: {full.coverage_line()}"


# --- the replay is unchanged -------------------------------------------------


def test_a_replay_of_that_evening_still_stops_at_its_day(monkeypatch, tmp_path):
    """The other direction: a replay dated the evening mapped as of that day,
    so its scan must not read the 10-Q EDGAR dated the next day — even
    handed today's payload."""
    facts = _crm_revised_by_its_newest_10q()
    evening, report, ledger = _report_the_evening_before(
        monkeypatch, tmp_path, "CRM", facts, replay=True)
    assert report.startswith(f"> **HISTORICAL REPLAY — as of {evening}.**")
    assert not [i for i in ledger.items if i.kind == "restatement_footprint"]
    assert CRM_10Q not in report
    assert "derived-quarter check NOT inspected" not in report  # its rebuild is its score


def test_the_scan_date_is_lifted_only_for_an_uncut_score():
    """`uncut_fundamentals` lifts the scan to the newest filing when that is
    after the report's day; never lowers it; and a caller that did not ask
    (the replay, a direct build) keeps the report's day."""
    import app.services.ingestion.restatements as rs
    from app.services.reporting.report_builder import _collect_streams

    facts = _real("CRM")
    newest = _last_filed(facts)
    seen: list[date] = []
    real = rs.scan_restatements

    def spy(*args, **kwargs):
        seen.append(kwargs["as_of"])
        return real(*args, **kwargs)

    mp = pytest.MonkeyPatch()
    mp.setattr(rs, "scan_restatements", spy)
    try:
        for day, uncut in [(newest - timedelta(days=1), True), (newest - timedelta(days=1), False),
                           (newest + timedelta(days=3), True), (newest, True)]:
            _collect_streams(_Client(facts), "CRM", day, company_facts=facts,
                             submissions=_submissions(), uncut_fundamentals=uncut)
    finally:
        mp.undo()
    assert seen == [newest, newest - timedelta(days=1), newest + timedelta(days=3), newest]


def test_both_live_entry_points_say_their_score_was_uncut(monkeypatch, tmp_path):
    """The CLI's report is always of today; the journal's is unless it is a
    replay."""
    from scripts import generate_report
    from tests.fixtures.staged import write_ledger
    from tests.unit.test_report_entrypoint_snapshot import (
        _documents,
        _NoRefetchClient,
        _payloads,
        _snapshot,
    )

    seen: list[bool] = []

    def fake_build_report(*args, **kwargs):
        write_ledger(kwargs)
        seen.append(kwargs["uncut_fundamentals"])
        return "report", SimpleNamespace(reading=None, regime_flags=[], hottest_cluster=None)

    company_facts, submissions = _payloads()
    monkeypatch.setattr(generate_report, "ROOT", tmp_path)
    monkeypatch.setattr(generate_report, "SecClient",
                        lambda fresh=False: _NoRefetchClient(submissions))
    monkeypatch.setattr(generate_report, "fetch_dataset_snapshot",
                        lambda *a, **k: _snapshot(company_facts))
    monkeypatch.setattr(generate_report, "fetch_documents", lambda *a, **k: _documents())
    monkeypatch.setattr(generate_report, "build_report", fake_build_report)
    monkeypatch.setattr(generate_report.sys, "argv", ["generate_report.py", "AAPL"])
    assert generate_report.main() == 0
    assert seen == [True]


# --- the derived check refuses a rebuild that drifted ------------------------


def _drifted(selection: dict, field: str, to: SeriesSelection | str | None) -> dict:
    out = dict(selection)
    out[field] = to
    return out


def _ytd_filer_scan(**kw):
    from tests.unit.test_restatement_scan_truth import AS_OF, SINCE, _ytd_filer

    facts = _ytd_filer(101.9)  # a derived Q2 operating income that moved 1 -> 1.9
    ds, diag = build_dataset(facts, "T")
    kw.setdefault("selected_tags", diag.selected_series())
    return facts, ds, diag, scan_restatements(
        facts, period_since=SINCE, as_of=AS_OF, n_quarters=len(ds.periods), **kw)


def test_a_rebuild_that_selected_another_series_reports_no_derived_rows():
    """The report scored total assets from `us-gaap:Assets`; the scan's
    rebuild of the same facts cannot have chosen what the report says, so
    its derived quarters — the operating-income move included — are some
    other run's, and are not reported."""
    _f, _ds, diag, clean = _ytd_filer_scan()
    assert [d.field_name for d in clean.derived] == ["operating_income"]
    assert clean.derived_gap is None

    other = SeriesSelection.of("total_assets", ("us-gaap:AssetsNet",))
    _f, _ds, _d, scan = _ytd_filer_scan(
        selected_tags=_drifted(diag.selected_series(), "total_assets", other))
    assert scan.derived == ()
    assert scan.derived_gap == (
        "selection drift: the scan's rebuild selected us-gaap:Assets for total_assets "
        "where the report used us-gaap:AssetsNet"
    )
    assert scan.incomplete
    assert scan.coverage_line().endswith(
        f"; derived-quarter check NOT inspected ({scan.derived_gap})")
    md = render_restatements_section(scan)
    assert f"- Coverage: {scan.coverage_line()}." in md
    assert (f"- ⚠ Incomplete: derived quarters were not checked ({scan.derived_gap}). "
            "A derived quarter that moved would not appear below.") in md
    assert "Derived quarters that moved" not in md


def test_every_drifted_field_is_named_and_legacy_strings_compare_as_strings():
    _f, _ds, diag, _clean = _ytd_filer_scan()
    tags = _drifted(diag.selected_tags(), "total_assets", None)
    tags = _drifted(tags, "operating_income", "us-gaap:OperatingIncomeLoss")  # unchanged
    tags = _drifted(tags, "revenue", "us-gaap:SalesRevenueNet")
    _f, _ds, _d, scan = _ytd_filer_scan(selected_tags=tags)
    assert scan.derived == ()
    assert scan.derived_gap == (
        "selection drift: the scan's rebuild selected "
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax for revenue where the "
        "report used us-gaap:SalesRevenueNet, and selected us-gaap:Assets for total_assets "
        "where the report used nothing"
    )


def test_a_selection_the_rebuild_agrees_with_is_not_drift():
    """Objects, legacy strings, and a sum's components in another order all
    name the rebuild's own choice: the derived rows stand."""
    facts = _real("KO")
    ds, diag = build_dataset(facts, "KO")
    debt = diag.selected_series()["total_debt"]
    assert len(debt.components) > 1  # a composite: its order is not a choice
    reordered = debt.model_copy(update={"components": tuple(reversed(debt.components))})
    legacy = diag.selected_tags()
    legacy["total_debt"] = "+".join(reversed(legacy["total_debt"].split("+")))
    for selected in (diag.selected_series(), _drifted(diag.selected_series(), "total_debt",
                                                      reordered), legacy):
        scan = scan_restatements(
            facts, period_since=date(2023, 1, 1), as_of=_last_filed(facts),
            selected_tags=selected, n_quarters=len(ds.periods),
            scored_quarters=[p.period_end for p in ds.periods],
        )
        assert scan.derived_gap is None


def test_the_skewed_rebuild_of_each_real_filer_is_refused():
    """The defect's own evidence: scanned the evening before its newest
    filing, each fixture's rebuild drifts from what the uncut score chose —
    and is now withheld, with the drift on the coverage line."""
    expected = {
        "AAPL": "selection drift: the scan's rebuild selected nothing for intangible_assets "
                "where the report used us-gaap:IntangibleAssetsNetExcludingGoodwill",
        "KO": "selection drift: the scan's rebuild selected LongTermDebtNoncurrent+",
        # The evening before CRM's newest 10-Q the rebuild has neither that
        # quarter nor the tag-switch fallback (#103) the report read it from;
        # the selection is compared first, and names the fallback.
        "CRM": "selection drift: the scan's rebuild selected us-gaap:InterestExpenseDebt for "
               "interest_expense where the report used us-gaap:InterestExpenseDebt"
               "|2026-04-30:us-gaap:InterestExpenseNonoperating",
    }
    for ticker, reason in expected.items():
        facts = _real(ticker)
        ds, diag = build_dataset(facts, ticker)
        scan = scan_restatements(
            facts, period_since=date(2023, 1, 1), as_of=_last_filed(facts) - timedelta(days=1),
            selected_tags=diag.selected_series(), n_quarters=len(ds.periods),
            scored_quarters=[p.period_end for p in ds.periods],
        )
        assert scan.derived == (), ticker
        assert scan.derived_gap is not None and scan.derived_gap.startswith(reason), ticker
        assert f"derived-quarter check NOT inspected ({scan.derived_gap})" in scan.coverage_line()


def test_quarters_the_rebuild_could_not_establish_are_a_gap_only_when_named():
    """A payload too thin to map: with the report's quarters named, the
    empty result is a gap; without them it stays what it was (nothing to
    derive)."""
    from tests.unit.test_restatement_scan_truth import _assets

    facts = _assets((100.0, "2024-05-01", "10-Q", "a1"))
    named = scan_restatements(facts, scored_quarters=[date(2024, 3, 31)])
    assert named.derived == ()
    assert named.derived_gap == (
        "quarter drift: the scan's rebuild could not establish the report's quarters")
    assert scan_restatements(facts).derived_gap is None


def test_the_quarters_are_compared_whole_and_in_date_order():
    _f, ds, _diag, _scan = _ytd_filer_scan()
    ends = [p.period_end for p in ds.periods]
    _f, _ds, _d, scan = _ytd_filer_scan(scored_quarters=list(reversed(ends)))
    assert scan.derived_gap is None and scan.derived  # order is not drift
    fewer = ends[1:]
    _f, _ds, _d, short = _ytd_filer_scan(scored_quarters=fewer)
    assert short.derived == ()
    assert short.derived_gap == (
        f"quarter drift: the scan's rebuild covers {len(ends)} quarter(s) ending {ends[0]} "
        f"to {ends[-1]} where the report scored {len(fewer)} quarter(s) ending "
        f"{fewer[0]} to {fewer[-1]}"
    )
    _f, _ds, _d, none = _ytd_filer_scan(scored_quarters=[])
    assert none.derived_gap is not None and none.derived_gap.endswith("the report scored no quarters")


# --- the card ----------------------------------------------------------------


def test_the_card_qualifies_checked_and_clean_when_derived_quarters_were_not_checked():
    from app.core.pipeline import analyze
    from app.services.reporting.decision_card import render_decision_card
    from app.services.scoring.thermometer import compute_thermometer
    from tests.fixtures.companies import stretch_dataset

    ds = stretch_dataset()
    result = analyze(ds)
    t = compute_thermometer(result.block_scores, ds.periods)
    only = render_decision_card(result, t, generated_on="2026-09-22", restatement_derived_gap=True)
    assert ("## Checked and clean (incomplete: derived quarters not checked for revisions "
            "— see data quality)\n") in only
    both = render_decision_card(result, t, generated_on="2026-09-22", restatement_gaps=2,
                                restatement_derived_gap=True)
    assert ("## Checked and clean (incomplete: 2 field(s) not inspectable for revisions; "
            "derived quarters not checked for revisions — see data quality)\n") in both


def test_the_report_carries_the_derived_gap_to_the_card(monkeypatch):
    from app.core.pipeline import analyze
    from app.services.reporting import report_builder
    from tests.fixtures.companies import stretch_dataset

    seen = {}

    def card(*args, **kwargs):
        seen.update(kwargs)
        return "card"

    facts = _real("CRM")
    monkeypatch.setattr(report_builder, "render_decision_card", card)
    ds = stretch_dataset()  # not CRM's quarters: the rebuild drifts
    report_builder.build_report(
        analyze(ds), ds, generated_on="2026-09-22", client=_Client(facts), ticker="CRM",
        company_facts=facts, submissions=_submissions(),
    )
    assert seen["restatement_derived_gap"] is True
    assert "quarter drift" in seen["restatement_scan"]


# --- newest_filed ------------------------------------------------------------


def test_newest_filed_is_the_latest_dated_row_and_skips_what_it_cannot_read():
    from app.services.ingestion.restatements import newest_filed

    rows = [{"end": "2024-03-31", "val": 1, "filed": "2024-05-01"},
            {"end": "2024-06-30", "val": 2, "filed": "2024-08-01"},
            {"end": "2024-06-30", "val": 3},  # undated
            {"end": "2024-06-30", "val": 4, "filed": "not a date"},
            "not a row"]
    facts = {"facts": {
        "us-gaap": {"Assets": {"units": {"USD": rows, "EUR": "not rows"}},
                    "Broken": {"units": "not units"}, "Bare": "not a concept"},
        "dei": "not a taxonomy",
        "ifrs-full": {"Revenue": {"units": {"USD": [
            {"end": "2024-06-30", "val": 5, "filed": "2024-07-15"}]}}},
    }}
    assert newest_filed(facts) == date(2024, 8, 1)
    assert newest_filed({"facts": "nothing"}) is None
    assert newest_filed({}) is None
    assert newest_filed({"facts": {"us-gaap": {"Assets": {"units": {"USD": [
        {"end": "2024-03-31", "val": 1}]}}}}}) is None
    assert newest_filed(_real("CRM")) == _last_filed(_real("CRM"))
    # A unit whose rows are not a list at all (null, a number) is skipped
    # too, not iterated: "never raised on" (complete mutation run of ace7cd8,
    # restatements.py `continue` -> `pass` survived on the string alone).
    assert newest_filed({"facts": {"us-gaap": {"Assets": {"units": {
        "USD": None, "EUR": 7,
        "GBP": [{"end": "2024-06-30", "val": 1, "filed": "2024-07-01"}]}}}}}) == date(2024, 7, 1)


def test_a_quarter_filled_from_another_concept_is_part_of_the_selection_compared():
    """Review of #106: the drift comparison looked at the composer and the
    components only, so a rebuild that dropped a tag-switch fallback (a
    quarter the report read from another concept) compared as equal."""
    from app.services.ingestion.restatements import _unordered
    from app.services.ingestion.selection import SeriesSelection

    plain = SeriesSelection.of("interest_expense", ("us-gaap:InterestExpenseDebt",))
    filled = SeriesSelection.of("interest_expense", ("us-gaap:InterestExpenseDebt",))
    # Set directly: the field arrives with the tag-switch fallback (#103).
    object.__setattr__(filled, "fallbacks",
                       (("2026-04-30", "us-gaap:InterestExpenseNonoperating"),))
    assert _unordered(plain) != _unordered(filled)
    assert _unordered(plain) == _unordered(
        SeriesSelection.of("interest_expense", ("us-gaap:InterestExpenseDebt",)))


def test_the_ledger_says_when_the_scan_was_incomplete():
    """Review of #106: the report said "derived-quarter check NOT inspected"
    and qualified "Checked and clean", while the ledger recorded the stream
    as a bare "checked"."""
    from types import SimpleNamespace

    from app.services.reporting.ledger import _stream_state

    whole = SimpleNamespace(incomplete=False, coverage_line=lambda: "inspected 20 of 20 fields")
    held = SimpleNamespace(
        incomplete=True,
        coverage_line=lambda: "inspected 20 of 20 fields; derived-quarter check NOT inspected "
                              "(selection drift: …)",
    )
    assert _stream_state("restatements", True, {}, None, whole) == "checked"
    assert _stream_state("restatements", True, {}, None, held) == (
        "checked (incomplete: inspected 20 of 20 fields; derived-quarter check NOT "
        "inspected (selection drift: …))"
    )
    # Only the restatement stream reads the scan.
    assert _stream_state("offerings", True, {}, None, held) == "checked"
    assert _stream_state("restatements", False, {}, None, held) == "not run"
