"""A metric that read a revised figure says so, naming the input.

Hermes's next-iteration item 6. The earnings-night drill's CRM run showed
the harm: a +10% 10-Q/A on revenue put "Receivables growing in line with
revenue" under Checked and clean, and nothing on the card tied it to the
restatement. The metric is not wrong; its input is restated, and a reader
must not take it as independent corroboration.

The marks are checked against `provenance.sources_for`, so a metric is
marked exactly when a value it read is one a revision touched.
"""

from __future__ import annotations

import copy
import json
import sys
from datetime import date
from pathlib import Path

import pytest

from app.core.pipeline import analyze
from app.schemas.metrics import MetricStatus
from app.services.formulas.registry import compute_metrics
from app.services.ingestion.companyfacts_mapper import build_dataset
from app.services.ingestion.restatements import scan_restatements
from app.services.ingestion.vintages import FactKey, VintageChange
from app.services.provenance import sources_for
from app.services.reporting.decision_card import render_decision_card
from app.services.reporting.revised_inputs import (
    card_notes,
    note,
    revised_inputs,
    revision_index,
)
from app.services.scoring.thermometer import compute_thermometer

ROOT = Path(__file__).resolve().parents[2]
REAL = ROOT / "tests" / "fixtures" / "real"
sys.path.insert(0, str(ROOT / "scripts"))

import drill  # noqa: E402  (the drill's own scenario edit: one source of truth)

ACCN = "0001108524-26-990001"


def _amended(ticker: str = "CRM", *, form: str = "10-Q/A", accn: str = ACCN):
    """The fixture, and the fixture with the newest direct revenue quarter
    re-filed +10% (the drill's step 3)."""
    facts = json.loads((REAL / f"companyfacts_{ticker}_trimmed.json").read_text())
    newer = copy.deepcopy(facts)
    cur = drill.current_revenue_fact(newer, ticker)
    drill.add_fact(newer, cur, val=round(cur.fact["val"] * 1.10), form=form,
                   filed="2026-09-26", accn=accn)
    return facts, newer, cur


def _scan(facts):
    return scan_restatements(facts, period_since=date(2023, 1, 1), as_of=date(2026, 9, 26))


@pytest.fixture(scope="module")
def crm():
    _, newer, cur = _amended()
    ds, _ = build_dataset(newer, "CRM")
    bundle = compute_metrics(ds)
    return ds, bundle, cur, revision_index(_scan(newer))


def _cites_revenue_at(ds, bundle, m, quarter: date) -> bool:
    by_id = {id(sv): (f, p.period_end) for p in ds.periods for f, sv in p.sources.items()}
    return any(by_id.get(id(sv)) == ("revenue", quarter)
               for group in sources_for(ds, m, bundle=bundle).values() for sv in group)


def test_a_metric_is_marked_exactly_when_it_read_the_revised_figure(crm):
    ds, bundle, cur, idx = crm
    marked = unmarked = 0
    for history in bundle.history.values():
        for m in history:
            if m.status is not MetricStatus.OK:
                continue
            revs = revised_inputs(ds, bundle, m, idx)
            reads = _cites_revenue_at(ds, bundle, m, cur.quarter)
            assert bool(revs) is reads, (m.name, m.fiscal_label)
            marked += reads
            unmarked += not reads
    assert marked >= 10 and unmarked >= 100  # the check has teeth both ways


def test_the_mark_names_the_input_its_move_and_the_amendment(crm):
    ds, bundle, cur, idx = crm
    m = next(x for x in bundle.history["receivables_growth_spread"] if x.fiscal_label == "FY2027Q1")
    (rev,) = revised_inputs(ds, bundle, m, idx)
    assert (rev.field, rev.period_end) == ("revenue", cur.quarter)
    assert rev.original == cur.fact["val"] and rev.current == round(cur.fact["val"] * 1.10)
    assert rev.how == f"amended by 10-Q/A {ACCN}"
    assert note([rev], {cur.quarter: "FY2027Q1"}) == (
        f"reads a revised figure: revenue FY2027Q1 {cur.fact['val']:,.0f} → "
        f"{round(cur.fact['val'] * 1.10):,.0f} (amended by 10-Q/A {ACCN})")


def test_metrics_that_do_not_read_revenue_are_not_marked(crm):
    ds, bundle, _, idx = crm
    for name in ("current_ratio", "debt_to_assets", "cfo_to_net_income", "total_accruals"):
        for m in bundle.history.get(name, []):
            assert revised_inputs(ds, bundle, m, idx) == [], (name, m.fiscal_label)


def test_the_crm_card_marks_the_clean_line_that_rests_on_the_amendment(crm):
    ds, bundle, _, idx = crm
    result = analyze(ds)
    notes = card_notes(ds, bundle, [*result.red_flags, *result.green_flags], idx)
    card = render_decision_card(
        result, compute_thermometer(result.block_scores, ds.periods), generated_on="2026-09-26",
        change_notes=notes.changes, flag_notes=notes.flags)
    clean = card.split("## Checked and clean")[1].split("## Data quality")[0]
    line = next(x for x in clean.splitlines() if "Receivables growing in line with revenue" in x)
    assert "⚠ reads a revised figure: revenue FY2027Q1" in line and ACCN in line
    assert "Low accrual intensity" in clean
    assert all("⚠" not in x for x in clean.splitlines() if "Low accrual intensity" in x)
    changes = card.split("## Changes since last period")[1].split("## Distress")[0]
    assert all("⚠" not in x for x in changes.splitlines() if x.startswith("- Total accruals"))
    assert any("⚠" in x for x in changes.splitlines() if x.startswith("- Days sales outstanding"))


def test_without_revisions_the_card_is_byte_identical():
    facts, _, _ = _amended()
    ds, _ = build_dataset(facts, "CRM")
    bundle = compute_metrics(ds)
    idx = revision_index(_scan(facts))
    assert not idx  # the unamended fixture has no material revision
    result = analyze(ds)
    notes = card_notes(ds, bundle, [*result.red_flags, *result.green_flags], idx)
    assert notes.changes == {} and notes.flags == {}
    therm = compute_thermometer(result.block_scores, ds.periods)
    assert render_decision_card(result, therm, generated_on="d") == render_decision_card(
        result, therm, generated_on="d", change_notes={}, flag_notes={})


def test_a_later_ordinary_filing_is_named_as_such():
    _, newer, _ = _amended(form="10-Q", accn="0001108524-26-990002")
    idx = revision_index(_scan(newer))
    (rev,) = {r for r in idx.by_fact.values() if r.field == "revenue"}
    assert rev.how == "revised by a later 10-Q 0001108524-26-990002"


def test_a_silent_change_is_matched_by_its_fact_and_an_explained_one_is_not(crm):
    ds, bundle, cur, _ = crm
    base = next(x for x in ds.sorted_periods() if x.period_end == cur.quarter).sources["revenue"]
    (ref,) = base.inputs
    taxonomy, _, tag = ref.concept.partition(":")
    key = FactKey(taxonomy, tag, "USD", ref.start, ref.end)

    def change(old_accession=ref.accession, **kw):
        return VintageChange("revised", "revenue", key, 1.0, None, old_accession, ref.form,
                             ref.value, None, ref.accession, ref.form, 0.5, tag, **kw)

    class _Obs:
        def __init__(self, captured):
            self.captured = captured

    class _Rep:
        compared = True
        previous, newest = _Obs("2026-09-19"), _Obs("2026-09-20")

        def __init__(self, changes):
            self.changes_since_previous = changes

    silent = revision_index(vintage=_Rep([change()]))
    (rev,) = silent.by_fact.values()
    assert rev.how == "changed silently between snapshots 2026-09-19 → 2026-09-20"
    m = next(x for x in bundle.history["dso"] if x.fiscal_label == "FY2027Q1")
    assert revised_inputs(ds, bundle, m, silent) == [rev]
    explained = revision_index(
        vintage=_Rep([change(old_accession="0001108524-25-000001", original_retained=True)]))
    assert not explained  # a filed move is the scan's to report, not a silent one


def test_a_derived_quarter_that_moved_is_matched_by_its_cell(crm):
    from app.services.ingestion.restatements import DerivedRevision, RestatementScan

    ds, bundle, _, _ = crm
    q = next(p for p in ds.sorted_periods() if p.sources["revenue"].method != "direct")
    d = DerivedRevision("revenue", None, q.period_end, "ytd_diff", 1.0, date(2025, 1, 1), 2.0,
                        date(2025, 6, 1), (("10-Q/A", "X-1"),))
    scan = RestatementScan([], (), {}, {}, None, None, 0.01, derived=(d,))
    idx = revision_index(scan)
    spread = [m for h in ("receivables_growth_spread", "dso") for m in bundle.history[h]
              if m.fiscal_label == q.fiscal_label and m.status is MetricStatus.OK]
    assert spread and all(
        revised_inputs(ds, bundle, m, idx)[0].how == "derived quarter moved by 10-Q/A X-1"
        for m in spread)


def test_a_failed_or_absent_check_marks_nothing(crm):
    ds, bundle, _, _ = crm
    empty = revision_index(None, None)
    assert not empty
    m = bundle.history["dso"][-1]
    assert revised_inputs(ds, bundle, m, empty) == []


def test_the_ledger_item_of_a_metric_that_read_it_says_so(crm):
    from app.services.reporting.ledger import build_ledger

    ds, _, cur, _ = crm
    _, newer, _ = _amended()
    doc = build_ledger(result=analyze(ds), dataset=ds, ticker="CRM",
                       report_date=date(2026, 9, 26),
                       streams={"ran": True, "restatements": _scan(newer)}, errors={})
    metrics = {i.subject: i for i in doc.items if i.kind == "metric"}
    dso = metrics["dso"]
    assert dso.change_state == "reads_revised_input"
    assert "reads a revised figure: revenue FY2027Q1" in dso.note and ACCN in dso.note
    assert metrics["current_ratio"].change_state is None
    assert "revised" not in (metrics["current_ratio"].note or "")
    # the sources still cite the current filing: the input is marked, not dropped
    assert any(p.accession == ACCN for p in dso.provenance)


def test_the_note_lists_two_inputs_in_full_and_counts_the_rest():
    from app.services.reporting.revised_inputs import Revision

    revs = [Revision("revenue", date(2026, 3, 31 - k), 1.0, 2.0, "x") for k in range(3)]
    assert "more" not in note(revs[:1]) and note(revs[:1]).startswith("reads a revised figure:")
    two = note(revs[:2])
    assert "more" not in two
    assert two.startswith("reads a revised figures: ") and two.count("revenue") == 2
    three = note(revs)
    assert three.endswith("; +1 more") and three.count("revenue") == 2


@pytest.mark.parametrize("keep, marked", [(2, True), (1, False), (0, False)])
def test_a_change_line_needs_two_periods_and_marks_either(crm, keep, marked):
    """`_what_changed` compares the last two OK values; with fewer there is
    no line to mark, and nothing must fail."""
    ds, bundle, _, idx = crm
    b = copy.copy(bundle)
    b.history = dict(bundle.history)
    dso = list(bundle.history["dso"])
    for k in range(len(dso) - keep):
        dso[k] = dso[k].model_copy(update={"status": MetricStatus.MISSING_DATA, "value": None})
    b.history["dso"] = dso
    notes = card_notes(ds, b, [], idx)
    assert ("Days sales outstanding" in notes.changes) is marked
