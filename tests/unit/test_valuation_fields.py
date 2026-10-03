"""Five balance-sheet fields the valuation plane reads and no metric does
(Hermes review of 02c2aac, valuation plane). They are mapped like any other
field, with provenance, and marked `scored=False` in the registry: the
field-coverage figure, the restatement scan and the silent-revision diff
are over the scored fields only, so a run on a filer that reports no
preferred stock reads exactly as it did before these fields existed.

The committed real fixtures were trimmed to the registry's tags before
these fields existed, so none of them carries these concepts; the mapping
is checked on synthetic payloads (`selection_cases` helpers)."""

from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from app.schemas.financials import PeriodFinancials
from app.services.ingestion import fields as F
from app.services.ingestion import vintages
from app.services.ingestion.companyfacts_mapper import INSTANT_FIELDS, build_dataset
from app.services.ingestion.restatements import scan_restatements
from app.services.ingestion.vintages import diff_scored, silent_revision_tier1_lines
from tests.fixtures.selection_cases import QUARTER_ENDS, _base, _instants

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"
NEW = ("short_term_investments", "operating_lease_liabilities", "minority_interest",
       "preferred_stock", "stockholders_equity")
TAGS = {
    "short_term_investments": "ShortTermInvestments",
    "operating_lease_liabilities": "OperatingLeaseLiability",
    "minority_interest": "MinorityInterest",
    "preferred_stock": "PreferredStockValue",
    "stockholders_equity": "StockholdersEquity",
}
GOLDEN = Path(__file__).resolve().parents[1] / "golden_reports" / "selection_snapshot.json"


def test_the_registry_marks_them_unscored_and_nothing_else():
    for name in NEW:
        spec = F.field(name)
        assert spec.scored is False and spec.kind is F.Kind.INSTANT and spec.unit == "USD"
        assert spec.additive and not spec.split_adjusted
        assert (("us-gaap", TAGS[name]) in spec.strategies[0].tags), name
    assert all(spec.scored for spec in F.FIELDS if spec.name not in NEW)
    assert F.scored_fields() == tuple(s.name for s in F.FIELDS if s.name not in NEW)
    assert F.is_scored("revenue") and not F.is_scored("preferred_stock")
    assert F.is_scored("not_a_field")  # an unknown name is held to the scored rules
    # Mapped by the mapper, read by nothing scored: outside the candidate
    # tables the scans iterate, inside the tags a payload is trimmed to.
    assert not set(NEW) & set(INSTANT_FIELDS)
    assert all(("us-gaap", tag) in F.every_tag() for tag in TAGS.values())
    assert not any(("us-gaap", tag) in F.all_tags() for tag in TAGS.values())  # PIT's set
    assert F.field("stockholders_equity").strategies[0].tags == F._g(
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")
    assert F.field("short_term_investments").strategies[0].tags == F._g(
        "ShortTermInvestments", "MarketableSecuritiesCurrent",
        "AvailableForSaleSecuritiesDebtSecuritiesCurrent")


def test_period_financials_carry_them():
    p = PeriodFinancials(period_end=date(2026, 1, 1), period_type="Q", fiscal_label="x")
    assert all(getattr(p, name) is None for name in NEW)
    assert PeriodFinancials(period_end=date(2026, 1, 1), period_type="Q", fiscal_label="x",
                            preferred_stock=5.0).preferred_stock == 5.0


def _payload(*, every: bool = True) -> dict:
    p = _base("Valuation Co")
    p.add("CashAndCashEquivalentsAtCarryingValue", _instants(500.0))
    p.add("LongTermDebtNoncurrent", _instants(2_000.0))
    if every:
        for n, tag in enumerate(TAGS.values()):
            p.add(tag, _instants(100.0 * (n + 1), step=0.5))
    return p.data


def test_each_field_maps_with_provenance_on_a_synthetic_payload():
    ds, diag = build_dataset(_payload(), "VAL")
    last = ds.sorted_periods()[-1]
    for n, (name, tag) in enumerate(TAGS.items()):
        assert getattr(last, name) == pytest.approx(100.0 * (n + 1) + 0.5 * 11), name
        src = last.sources[name]
        assert src.field == name and src.method == "direct"
        assert [r.concept for r in src.inputs] == [f"us-gaap:{tag}"]
        d = diag.field_by_name(name)
        assert d.tag_used == f"us-gaap:{tag}" and d.periods_filled == d.periods_total == 8


def test_alternative_concepts_are_named_by_the_selection():
    p = _base("Alt Co")
    p.add("MarketableSecuritiesCurrent", _instants(10.0))
    p.add("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
          _instants(900.0))
    ds, diag = build_dataset(p.data, "ALT")
    last = ds.sorted_periods()[-1]
    assert last.short_term_investments == pytest.approx(21.0)
    assert diag.field_by_name("short_term_investments").tag_used == "us-gaap:MarketableSecuritiesCurrent"
    assert last.stockholders_equity == pytest.approx(911.0)
    assert diag.field_by_name("stockholders_equity").tag_used == (
        "us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")
    assert diag.selected_series()["stockholders_equity"].label == (
        "us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")


def test_coverage_is_over_the_scored_fields_only():
    _, with_them = build_dataset(_payload(every=True), "VAL")
    _, without = build_dataset(_payload(every=False), "VAL")
    assert with_them.coverage() == without.coverage()
    scored = [d for d in with_them.fields if F.is_scored(d.field_name)]
    assert with_them.coverage() == (sum(d.periods_filled for d in scored)
                                    / sum(d.periods_total for d in scored))
    assert {d.field_name for d in with_them.fields} >= set(NEW)
    # On a real fixture the figure is the one the scored fields give.
    facts = json.loads((REAL / "companyfacts_KO_trimmed.json").read_text())
    _, diag = build_dataset(facts, "KO")
    scored = [d for d in diag.fields if F.is_scored(d.field_name)]
    assert diag.coverage() == (sum(d.periods_filled for d in scored)
                               / sum(d.periods_total for d in scored))
    assert all(diag.field_by_name(n).periods_filled == 0 for n in NEW)  # trimmed fixture


def test_the_restatement_scan_neither_inspects_nor_lists_them():
    facts = _payload()
    newer = copy.deepcopy(facts)
    for tag in TAGS.values():
        rows = newer["facts"]["us-gaap"][tag]["units"]["USD"]
        rows.append(dict(rows[-1], val=rows[-1]["val"] * 2, filed="2025-02-01", form="10-K/A",
                         accn="0000000001-25-000099"))
    _, diag = build_dataset(newer, "VAL")
    scan = scan_restatements(
        newer, period_since=date(2022, 1, 1), as_of=date(2025, 3, 1),
        selected_tags=diag.selected_series(), n_quarters=8,
        scored_quarters=tuple(QUARTER_ENDS[-8:]),
    )
    names = set(scan.inspected) | set(scan.uninspected) | set(scan.excluded)
    assert not names & set(NEW)
    assert not {fp.field_name for fp in scan.footprints} & set(NEW)
    assert scan.total == len(names)


def test_the_silent_revision_diff_ignores_them():
    older = _payload()
    newer = copy.deepcopy(older)
    for tag in TAGS.values():
        for row in newer["facts"]["us-gaap"][tag]["units"]["USD"]:
            row["val"] *= 3
    changes = diff_scored(older, newer).changes
    assert not {c.field_name for c in changes} & set(NEW)
    assert changes == []
    assert silent_revision_tier1_lines(changes, "a", "b", period_since=date(2022, 1, 1)) == []
    # ...and the stored-snapshot report says nothing moved.
    root = Path(vintages.VINTAGES)
    vintages.store_snapshot(1, older, now=datetime(2026, 9, 1, tzinfo=UTC), root=root)
    vintages.store_snapshot(1, newer, now=datetime(2026, 9, 2, tzinfo=UTC), root=root)
    rep = vintages.report_diff(1, as_of=date(2026, 9, 3), since=date(2022, 1, 1), root=root)
    assert rep.compared and rep.changes_since_previous == []


def test_a_scored_field_still_moves_the_diff():
    older = _payload()
    newer = copy.deepcopy(older)
    for row in newer["facts"]["us-gaap"]["LongTermDebtNoncurrent"]["units"]["USD"]:
        if row["end"] == QUARTER_ENDS[-2].isoformat():
            row["val"] *= 2
    assert {c.field_name for c in diff_scored(older, newer).changes} == {"total_debt"}


def test_the_selection_snapshot_records_them_for_every_case():
    golden = json.loads(GOLDEN.read_text())
    for name, case in golden.items():
        if "fields" not in case:
            continue  # a refused payload records its error only
        assert {f["field"] for f in case["fields"]} >= set(NEW), name


def test_a_point_in_time_cut_drops_a_late_filed_value():
    facts = _payload()
    rows = facts["facts"]["us-gaap"]["StockholdersEquity"]["units"]["USD"]
    late = next(r for r in rows if r["end"] == QUARTER_ENDS[-1].isoformat())
    late["filed"] = (QUARTER_ENDS[-1] + timedelta(days=200)).isoformat()
    ds, _ = build_dataset(facts, "VAL", as_of=QUARTER_ENDS[-1] + timedelta(days=100))
    assert ds.sorted_periods()[-1].stockholders_equity is None
