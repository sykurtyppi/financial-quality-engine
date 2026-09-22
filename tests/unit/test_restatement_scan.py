"""PR 0.2 — a restatement check must account for every field it knows about.

A footprint list cannot say why it is empty. Before this, "No prior-period
revisions detected" rendered identically over a payload that mapped seven
fields and one that mapped twenty-seven — and the first is not a clean bill for
the other twenty. These tests pin the invariant that every canonical field
lands in exactly one of inspected / uninspected / excluded, that the empty
case is always scoped to the inspected set, and that the card header is
qualified whenever the check had holes.
"""

from __future__ import annotations

import json
import random
from datetime import date
from pathlib import Path

import pytest

from app.services.ingestion import restatements as mod
from app.services.ingestion.companyfacts_mapper import build_dataset
from app.services.ingestion.restatements import (
    SPLIT_ADJUSTED_FIELDS,
    RestatementScan,
    detect_restatements,
    render_restatements_section,
    scan_restatements,
)
from app.services.reporting.decision_card import render_decision_card

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"
TICKERS = ("AAPL", "KO", "CRM")
AS_OF = date(2026, 9, 22)
SINCE = date(2023, 1, 1)
LEGACY_CLEAN_BILL = "No prior-period revisions detected above the materiality threshold"


def _fixture(ticker: str) -> tuple[dict, dict[str, str | None]]:
    facts = json.loads((REAL / f"companyfacts_{ticker}_trimmed.json").read_text())
    _dataset, diag = build_dataset(facts, ticker, n_quarters=8)
    return facts, diag.selected_tags()


def _fact(end, val, filed, form, *, start=None, accn="A"):
    d = {"end": end, "val": val, "filed": filed, "form": form, "accn": accn, "fy": 2024, "fp": "FY"}
    if start:
        d["start"] = start
    return d


def _facts(tags: dict[str, list[dict]]) -> dict:
    return {"facts": {"us-gaap": {t: {"units": {"USD": rows}} for t, rows in tags.items()}}}


# --- accounting invariant -----------------------------------------------------


@pytest.mark.parametrize("ticker", TICKERS)
def test_every_field_lands_in_exactly_one_bucket(ticker):
    facts, tags = _fixture(ticker)
    scan = scan_restatements(facts, period_since=SINCE, as_of=AS_OF, selected_tags=tags)
    known = set(mod._fields_to_inspect(tags))
    buckets = [set(scan.inspected), set(scan.uninspected), set(scan.excluded)]
    assert buckets[0] | buckets[1] | buckets[2] == known
    assert sum(len(b) for b in buckets) == len(known), "a field was counted twice"
    assert scan.total == len(known)
    # The mapper's own field list is the universe: total_debt is in it.
    assert "total_debt" in known
    assert set(scan.excluded) == SPLIT_ADJUSTED_FIELDS


@pytest.mark.parametrize("ticker", TICKERS)
def test_every_unmapped_field_is_named_as_a_gap(ticker):
    """Random-subset invariant: blank any subset of the mapper's selections and
    every blanked field is named, with the mapper-gap reason, and never
    counted as inspected. Seeded so a failure reproduces."""
    facts, tags = _fixture(ticker)
    mapped = sorted(f for f, t in tags.items() if t and f not in SPLIT_ADJUSTED_FIELDS)
    rng = random.Random(f"{ticker}-0.2")
    for _ in range(12):
        blanked = set(rng.sample(mapped, rng.randint(0, len(mapped))))
        partial = {f: (None if f in blanked else t) for f, t in tags.items()}
        scan = scan_restatements(facts, period_since=SINCE, as_of=AS_OF, selected_tags=partial)
        for f in blanked:
            assert scan.uninspected.get(f) == "no series mapped this run", (f, blanked)
            assert f not in scan.inspected
        assert scan.incomplete is (bool(blanked) or bool(scan.uninspected))
        # The footprints are exactly those of the fields still mapped.
        assert {fp.field_name for fp in scan.footprints} <= set(scan.inspected)


@pytest.mark.parametrize("ticker", TICKERS)
def test_detect_is_the_scan_footprints(ticker):
    facts, tags = _fixture(ticker)
    kw = dict(period_since=SINCE, as_of=AS_OF, selected_tags=tags)
    assert detect_restatements(facts, **kw) == scan_restatements(facts, **kw).footprints


def test_a_field_the_mapper_recorded_as_unmapped_is_a_gap_not_a_silence():
    """`total_debt` is outside the candidate tables. Before, a None selection
    for it dropped it from the field list entirely, so its absence was
    indistinguishable from a checked-and-clean field."""
    scan = scan_restatements({"facts": {}}, selected_tags={"total_debt": None})
    assert scan.uninspected["total_debt"] == "no series mapped this run"


def test_without_a_selection_the_reason_is_the_approximation_finding_nothing():
    scan = scan_restatements({"facts": {}})
    assert scan.inspected == ()
    assert set(scan.uninspected) == set(mod._fields_to_inspect(None)) - SPLIT_ADJUSTED_FIELDS
    assert all(why == "no candidate tag with facts" for why in scan.uninspected.values())
    dated = scan_restatements({"facts": {}}, as_of=AS_OF)
    assert dated.uninspected["revenue"] == f"no candidate tag with facts filed by {AS_OF}"


def test_a_selected_series_with_nothing_visible_as_of_the_date_is_a_gap():
    """A series exists but every fact was filed after `as_of` (or under
    another unit): nothing was compared, so the field is not 'inspected'."""
    facts = _facts({"Assets": [_fact("2025-12-31", 1000.0, "2026-02-01", "10-K")]})
    scan = scan_restatements(
        facts, as_of=date(2026, 1, 1), selected_tags={"total_assets": "us-gaap:Assets"}
    )
    assert scan.uninspected["total_assets"] == (
        "selected series has no eligible facts filed by 2026-01-01"
    )
    assert "total_assets" not in scan.inspected
    # The same series, seen after the filing, IS inspected — one filing is
    # enough to compare against nothing and conclude "not revised".
    later = scan_restatements(
        facts, as_of=date(2026, 3, 1), selected_tags={"total_assets": "us-gaap:Assets"}
    )
    assert "total_assets" in later.inspected


def test_excluded_by_design_is_disclosed_but_not_a_hole():
    facts = _facts({"Assets": [_fact("2025-12-31", 1000.0, "2026-02-01", "10-K")]})
    # Every table field resolved to the one series that has facts, so the
    # only thing left to account for is the by-design exclusion.
    scan = scan_restatements(
        facts, selected_tags={f: "us-gaap:Assets" for f in mod._fields_to_inspect(None)}
    )
    assert len(scan.inspected) == scan.total - 2
    assert scan.excluded == {
        "shares_diluted": "split-adjusted share count",
        "shares_outstanding": "split-adjusted share count",
    }
    assert scan.incomplete is False
    assert "excluded by design: shares_diluted, shares_outstanding" in scan.coverage_line()
    assert "NOT inspected" not in scan.coverage_line()


# --- rendering: the unqualified clean bill never appears ----------------------


def _empty_scan(inspected=("revenue",), uninspected=None, excluded=None) -> RestatementScan:
    return RestatementScan(
        footprints=[],
        inspected=tuple(inspected),
        uninspected=dict(uninspected or {}),
        excluded=dict(excluded or {}),
        as_of=AS_OF,
        period_since=SINCE,
        materiality_pct=0.01,
    )


@pytest.mark.parametrize(
    "scan",
    [
        _empty_scan(),
        _empty_scan(inspected=()),
        _empty_scan(uninspected={"goodwill": "no series mapped this run"}),
        _empty_scan(inspected=(), uninspected={"revenue": "no candidate tag with facts"}),
        _empty_scan(excluded={"shares_diluted": "split-adjusted share count"}),
    ],
    ids=["clean", "nothing-inspected", "one-gap", "all-gaps", "excluded-only"],
)
def test_the_empty_case_is_always_scoped_to_the_inspected_set(scan):
    md = render_restatements_section(scan)
    assert LEGACY_CLEAN_BILL not in md
    assert f"among the {len(scan.inspected)} inspected field(s)" in md
    assert "Coverage: " + scan.coverage_line() in md
    for f in scan.uninspected:
        assert f in md
    assert ("⚠ Incomplete" in md) is scan.incomplete


def test_findings_render_beside_the_coverage_line():
    facts = _facts({"Assets": [
        _fact("2024-12-31", 1000.0, "2025-01-15", "10-K", accn="A"),
        _fact("2024-12-31", 1200.0, "2025-05-15", "10-K/A", accn="B"),
    ]})
    scan = scan_restatements(facts, selected_tags={"total_assets": "us-gaap:Assets", "goodwill": None})
    md = render_restatements_section(scan)
    assert "+20.0%" in md and "10-K/A" in md
    assert "NOT inspected: " in md and "goodwill (no series mapped this run)" in md
    assert f"⚠ Incomplete: {len(scan.uninspected)} field(s)" in md


# --- the card ------------------------------------------------------------------


def test_card_header_is_qualified_iff_the_scan_had_gaps():
    from app.core.pipeline import analyze
    from app.services.scoring.thermometer import compute_thermometer
    from tests.fixtures.companies import stretch_dataset

    ds = stretch_dataset()
    result = analyze(ds)
    t = compute_thermometer(result.block_scores, ds.periods)
    line = "inspected 25 of 27 fields for revisions; NOT inspected: goodwill (no series mapped this run)"
    with_gaps = render_decision_card(
        result, t, generated_on="2026-09-22", restatement_scan=line, restatement_gaps=2
    )
    assert "## Checked and clean (incomplete: 2 field(s) not inspectable for revisions" in with_gaps
    assert f"- Restatement scan: {line}." in with_gaps
    clean = render_decision_card(
        result, t, generated_on="2026-09-22", restatement_scan=line, restatement_gaps=0
    )
    assert "## Checked and clean\n" in clean and "incomplete" not in clean
    assert f"- Restatement scan: {line}." in clean
    absent = render_decision_card(result, t, generated_on="2026-09-22")
    assert "Restatement scan" not in absent and "## Checked and clean\n" in absent


class _Client:
    """Complete on purpose (see test_restatement_pit_and_tag): a missing
    method would read as a stream failure, not as this test's own gap."""

    def __init__(self, facts):
        self._facts = facts

    def company_facts(self, ticker):
        return self._facts

    def company_facts_by_cik(self, cik):
        return self._facts

    def resolve_cik(self, ticker):
        return 320193

    def submissions(self, ticker):
        return {"filings": {"recent": {}}}

    def submissions_by_cik(self, cik):
        return {"filings": {"recent": {}}}


def test_the_real_report_carries_the_scan_on_card_and_appendix():
    """End to end through build_report on real AAPL data: the two fields the
    mapper could not map are named on the card header, in the card's data
    quality, in the appendix data-quality section and in the section itself."""
    from app.core.pipeline import analyze
    from app.services.reporting.report_builder import build_report

    facts, tags = _fixture("AAPL")
    ds, diag = build_dataset(facts, "AAPL", n_quarters=8)
    report, _ = build_report(
        analyze(ds), ds,
        generated_on=AS_OF.isoformat(),
        coverage=diag.coverage(),
        field_tags=tags,
        client=_Client(facts),
        ticker="AAPL",
        fetched_at="2026-09-22 09:00 UTC",
        company_facts=facts,
    )
    gaps = sorted(f for f, t in tags.items() if t is None)
    assert gaps == ["goodwill", "share_issuance_proceeds"], "fixture drifted; re-read the gap set"
    assert f"## Checked and clean (incomplete: {len(gaps)} field(s) not inspectable" in report
    assert report.count("Restatement scan: inspected 23 of 27 fields") == 2, "card + appendix"
    assert "Coverage: inspected 23 of 27 fields" in report
    assert LEGACY_CLEAN_BILL not in report
    assert "among the 23 inspected field(s)" in report


def test_a_failed_scan_leaves_the_header_plain_and_the_tier1_notice_speaking(monkeypatch):
    """When the stream itself fails the card already says 'not checked this
    run'; the header must not ALSO claim a gap count it does not have."""
    from app.core.pipeline import analyze
    from app.services.reporting.report_builder import build_report
    from tests.fixtures.companies import stretch_dataset

    def outage(*a, **k):
        raise RuntimeError("SEC request failed: 503")

    monkeypatch.setattr(mod, "scan_restatements", outage)
    ds = stretch_dataset()
    report, _ = build_report(
        analyze(ds), ds, generated_on="2026-09-22", coverage=1.0,
        client=_Client({"facts": {}}), ticker="AAPL", fetched_at="x",
        company_facts={"facts": {}}, field_tags={"revenue": "us-gaap:Revenues"},
    )
    assert "not checked this run: restatement footprints" in report
    assert "## Checked and clean\n" in report
    assert "Restatement scan:" not in report
