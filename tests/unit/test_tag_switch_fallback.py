"""A filer's tag switch must not empty the newest quarter (checked fallback).

The mapper chooses one XBRL concept per single-tag field by reported-quarter
coverage and, until this fix, never read another concept for that series.
When a filer moved a field to a new concept, the tag with the longer history
won and the quarter reported only under the new concept went missing:

- CRM (`tests/corpus_drafts/crm_january_fye`): `interest_expense` selected
  `InterestExpenseDebt`, so FY2027Q1 (2026-04-30) was None although
  `InterestExpenseNonoperating` reports 317m for it — and the two concepts
  agree (68m) on the one quarter both report, FY2026Q1.
- KO as of 2025-03-31: `InterestExpenseNonoperating` selected, FY2024Q1
  (2024-03-29) empty, while `InterestExpense` (382m, filed 2024-05-02)
  agrees with it on every quarter both report.

The rule now: a quarter the selected concept HAS a value for is never taken
from another concept (that would fabricate period-over-period jumps); a
quarter it has NO value for is filled from the first other candidate that
is proven equal to it on every quarter both report (at least one), built
from the same point-in-time view and derivation machinery, cited in the
per-quarter provenance and named in a field note. A candidate that could
fill the gap but is not proven equal is named in a note with its value and
the reason.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.services.ingestion import companyfacts_mapper as mapper
from app.services.ingestion.companyfacts_mapper import (
    FieldDiagnostic,
    PeriodSource,
    build_dataset,
)
from app.services.ingestion.restatements import scan_restatements
from app.services.ingestion.selection import SeriesSelection

ROOT = Path(__file__).resolve().parents[2]
CRM_DRAFT = ROOT / "tests" / "corpus_drafts" / "crm_january_fye" / "companyfacts.json"
KO_REAL = ROOT / "tests" / "fixtures" / "real" / "companyfacts_KO_trimmed.json"

NONOP = "us-gaap:InterestExpenseNonoperating"
DEBT = "us-gaap:InterestExpenseDebt"
PLAIN = "us-gaap:InterestExpense"

Q_ENDS = ["2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31",
          "2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"]
Q_STARTS = ["2024-01-01", "2024-04-01", "2024-07-01", "2024-10-01",
            "2025-01-01", "2025-04-01", "2025-07-01", "2025-10-01"]
GAP = "2025-12-31"  # FY2025Q4: the quarter the selected concept does not report
SHARED = "2025-09-30"  # FY2025Q3


def _fact(start: str | None, end: str, val: float, filed: str = "2026-01-01",
          form: str = "10-Q", accn: str = "0000000000-26-000001") -> dict:
    row = {"end": end, "val": val, "filed": filed, "form": form, "accn": accn}
    if start:
        row["start"] = start
    return row


def _q(i: int, val: float, **kw) -> dict:
    return _fact(Q_STARTS[i], Q_ENDS[i], val, **kw)


def _payload(**concepts: list[dict]) -> dict:
    """Eight December quarters with assets, revenue (quarterly plus the two
    fiscal years, so labels are FY-based) and the given interest concepts."""
    base = {
        "Assets": [_fact(None, e, 1000.0) for e in Q_ENDS],
        "Revenues": [_q(i, 500.0) for i in range(8)]
        + [_fact("2024-01-01", "2024-12-31", 2000.0, form="10-K"),
           _fact("2025-01-01", "2025-12-31", 2000.0, form="10-K")],
    }
    base.update(concepts)
    return {"entityName": "Switch Co",
            "facts": {"us-gaap": {t: {"units": {"USD": rows}} for t, rows in base.items()}}}


def _selected(**kw) -> list[dict]:
    """InterestExpenseDebt for the first seven quarters (100 each): the
    best-covered concept, with FY2025Q4 missing."""
    return [_q(i, 100.0, **kw) for i in range(7)]


def _interest(facts: dict, **kw):
    ds, diag = build_dataset(facts, "SYN", **kw)
    by_end = {p.period_end.isoformat(): p for p in ds.periods}
    return by_end, diag.field_by_name("interest_expense"), diag


# --- the two real filers ------------------------------------------------------


class TestRealTagSwitches:
    def test_crm_fy2027q1_interest_comes_from_the_concept_the_filer_switched_to(self):
        """CRM's FY2027Q1 10-Q reports interest only as InterestExpenseNonoperating;
        the quarter was None because the mapper stayed on InterestExpenseDebt."""
        facts = json.loads(CRM_DRAFT.read_text())
        by_end, fd, diag = _interest(facts, as_of=date(2026, 5, 28))
        q = by_end["2026-04-30"]
        assert q.fiscal_label == "FY2027Q1"
        assert q.interest_expense == 317e6
        assert fd.tag_used == DEBT  # the series is still InterestExpenseDebt's
        src = fd.period_sources["2026-04-30"]
        assert src.components == [NONOP] and src.strategy == "single" and src.method == "direct"
        # Every other quarter is the selected concept's.
        assert {tuple(s.components) for k, s in fd.period_sources.items() if k != "2026-04-30"} == {(DEBT,)}
        assert fd.fallbacks == {"2026-04-30": NONOP}
        assert "FY2027Q1" not in fd.missing_periods
        assert (f"FY2027Q1 from {NONOP}: the filer switched concepts; it agrees with "
                f"{DEBT} on FY2026Q1.") in fd.notes
        # The value cites the fact it was read from.
        (ref,) = q.sources["interest_expense"].inputs
        assert (ref.concept, ref.value, ref.accession) == (NONOP, 317e6, "0001108524-26-000127")

    def test_ko_fy2024q1_interest_is_filled_in_the_point_in_time_cut(self):
        """KO at 2025-03-31: InterestExpenseNonoperating (selected) had no
        FY2024Q1 fact filed yet; InterestExpense reported 382m on 2024-05-02."""
        facts = json.loads(KO_REAL.read_text())
        by_end, fd, _ = _interest(facts, as_of=date(2025, 3, 31))
        q = by_end["2024-03-29"]
        assert q.fiscal_label == "FY2024Q1"
        assert q.interest_expense == 382e6
        assert fd.tag_used == NONOP
        assert fd.period_sources["2024-03-29"].components == [PLAIN]
        assert fd.fallbacks == {"2024-03-29": PLAIN}
        (ref,) = q.sources["interest_expense"].inputs
        assert (ref.concept, ref.filed) == (PLAIN, date(2024, 5, 2))
        assert any(n.startswith(f"FY2024Q1 from {PLAIN}: the filer switched concepts; "
                                f"it agrees with {NONOP} on ") for n in fd.notes)

    def test_a_filled_quarter_is_cited_by_the_ledger_under_its_own_concept(self):
        from app.services.formulas.registry import compute_metrics
        from app.services.provenance import sources_for
        from app.services.reporting.ledger import _fact_sources

        facts = json.loads(CRM_DRAFT.read_text())
        ds, _ = build_dataset(facts, "CRM", as_of=date(2026, 5, 28))
        bundle = compute_metrics(ds)
        m = next(m for m in bundle.history["interest_coverage"] if m.fiscal_label == "FY2027Q1")
        found = sources_for(ds, m, bundle=bundle)
        (sv,) = found["interest_expense"]
        assert [r.concept for r in sv.inputs] == [NONOP]
        prov, incomplete = _fact_sources(found)
        assert incomplete == 0
        cited = [p for p in prov if getattr(p, "concept", None) == NONOP]
        assert len(cited) == 1 and cited[0].accession == "0001108524-26-000127"


# --- the rule, on synthetic payloads -------------------------------------------


class TestCheckedFallback:
    def test_an_agreeing_alternative_fills_only_the_gap(self):
        """The alternative agrees within tolerance on the shared quarter (100
        vs 100.4) and reports 130 for the gap: the gap is filled, and the
        shared quarter keeps the SELECTED concept's 100 — no mixing where the
        selected tag has a value."""
        facts = _payload(
            InterestExpenseDebt=_selected(),
            InterestExpenseNonoperating=[_q(6, 100.4), _q(7, 130.0)],
        )
        by_end, fd, _ = _interest(facts)
        assert by_end[GAP].interest_expense == 130.0
        assert by_end[SHARED].interest_expense == 100.0
        assert all(by_end[e].interest_expense == 100.0 for e in Q_ENDS[:7])
        assert fd.period_sources[SHARED].components == [DEBT]
        assert fd.period_sources[GAP] == PeriodSource(strategy="single", components=[NONOP], method="direct")
        assert fd.fallbacks == {GAP: NONOP}
        assert fd.periods_filled == 8 and fd.missing_periods == []
        assert fd.notes == [f"FY2025Q4 from {NONOP}: the filer switched concepts; it agrees "
                            f"with {DEBT} on FY2025Q3."]

    def test_an_alternative_that_disagrees_on_a_shared_quarter_is_not_used(self):
        facts = _payload(
            InterestExpenseDebt=_selected(),
            InterestExpenseNonoperating=[_q(6, 110.0), _q(7, 130.0)],
        )
        by_end, fd, _ = _interest(facts)
        assert by_end[GAP].interest_expense is None
        assert GAP not in fd.period_sources and fd.fallbacks == {}
        assert fd.missing_periods == ["FY2025Q4"]
        assert fd.notes == [
            f"FY2025Q4: {NONOP} reports 130 but was not used: it disagrees with {DEBT} "
            "at FY2025Q3 (110 vs 100)."
        ]

    def test_an_alternative_sharing_no_quarter_is_not_used(self):
        """Nothing proves the two concepts measure the same thing."""
        facts = _payload(
            InterestExpenseDebt=_selected(),
            InterestExpenseNonoperating=[_q(7, 130.0)],
        )
        by_end, fd, _ = _interest(facts)
        assert by_end[GAP].interest_expense is None
        assert fd.fallbacks == {}
        assert fd.notes == [
            f"FY2025Q4: {NONOP} reports 130 but was not used: it shares no quarter with "
            f"{DEBT}, so agreement cannot be checked."
        ]

    def test_agreement_must_hold_on_every_shared_quarter(self):
        facts = _payload(
            InterestExpenseDebt=_selected(),
            InterestExpenseNonoperating=[_q(4, 100.0), _q(5, 90.0), _q(6, 100.0), _q(7, 130.0)],
        )
        by_end, fd, _ = _interest(facts)
        assert by_end[GAP].interest_expense is None
        assert fd.notes == [
            f"FY2025Q4: {NONOP} reports 130 but was not used: it disagrees with {DEBT} "
            "at FY2025Q2 (90 vs 100)."
        ]

    @pytest.mark.parametrize(("alt", "used"), [
        (100.5, True),  # exactly 0.5%: agrees
        (99.5, True),
        (100.50001, False),  # just over
        (99.49999, False),
    ])
    def test_the_agreement_tolerance_is_half_a_percent_of_the_selected_value(self, alt, used):
        assert mapper.FALLBACK_AGREEMENT_PCT == 0.005
        facts = _payload(
            InterestExpenseDebt=_selected(),
            InterestExpenseNonoperating=[_q(6, alt), _q(7, 130.0)],
        )
        by_end, fd, _ = _interest(facts)
        assert (by_end[GAP].interest_expense == 130.0) is used
        assert (fd.fallbacks == {GAP: NONOP}) is used

    def test_a_selected_zero_is_matched_only_by_zero(self):
        """Zero against zero agrees, and zero against anything else does not
        (the tolerance is relative) — but agreeing zeros are not proof on
        their own (TestWhatCountsAsProof)."""
        rows = [_q(i, 0.0) for i in range(6)] + [_q(6, 100.0)]
        agree = _payload(InterestExpenseDebt=rows,
                         InterestExpenseNonoperating=[_q(5, 0.0), _q(6, 100.0), _q(7, 5.0)])
        assert _interest(agree)[0][GAP].interest_expense == 5.0
        differ = _payload(InterestExpenseDebt=rows,
                          InterestExpenseNonoperating=[_q(5, 0.001), _q(6, 100.0), _q(7, 5.0)])
        assert _interest(differ)[0][GAP].interest_expense is None

    def test_candidates_are_tried_in_rank_order_and_rejections_are_named(self):
        """InterestExpense ranks first; when it disagrees, the next candidate
        that agrees fills the gap, and the rejected one is still named."""
        facts = _payload(
            InterestExpenseDebt=_selected(),
            InterestExpense=[_q(6, 120.0), _q(7, 999.0)],
            InterestExpenseNonoperating=[_q(6, 100.0), _q(7, 130.0)],
        )
        by_end, fd, _ = _interest(facts)
        assert by_end[GAP].interest_expense == 130.0
        assert fd.fallbacks == {GAP: NONOP}
        assert fd.notes == [
            f"FY2025Q4: {PLAIN} reports 999 but was not used: it disagrees with {DEBT} "
            "at FY2025Q3 (120 vs 100).",
            f"FY2025Q4 from {NONOP}: the filer switched concepts; it agrees with {DEBT} "
            "on FY2025Q3.",
        ]

    def test_the_first_agreeing_candidate_wins(self):
        facts = _payload(
            InterestExpenseDebt=_selected(),
            InterestExpense=[_q(6, 100.0), _q(7, 140.0)],
            InterestExpenseNonoperating=[_q(6, 100.0), _q(7, 130.0)],
        )
        by_end, fd, _ = _interest(facts)
        assert by_end[GAP].interest_expense == 140.0
        assert fd.fallbacks == {GAP: PLAIN}
        assert len(fd.notes) == 1

    def test_a_derived_fallback_is_built_by_the_same_machinery(self):
        """The alternative reports the gap only as a year-to-date figure: the
        quarter is its nine-months-to-year difference, exactly as it would be
        for the selected concept, and the provenance cites both facts."""
        facts = _payload(
            InterestExpenseDebt=_selected(),
            InterestExpenseNonoperating=[
                _q(6, 100.0),
                _fact("2025-01-01", "2025-09-30", 300.0),
                _fact("2025-01-01", "2025-12-31", 430.0, form="10-K"),
            ],
        )
        ds, diag = build_dataset(facts, "SYN")
        fd = diag.field_by_name("interest_expense")
        gap = ds.periods[-1]
        assert gap.interest_expense == 130.0
        assert fd.period_sources[GAP].method == "ytd_diff"
        assert fd.methods == {"direct": 7, "ytd_diff": 1}
        assert [(r.concept, r.value, r.sign) for r in gap.sources["interest_expense"].inputs] == [
            (NONOP, 430.0, 1), (NONOP, 300.0, -1)]

    def test_a_mixed_vintage_fallback_keeps_its_note(self):
        """A derived fallback quarter that mixes filing dates is flagged the
        way a selected one is (`_Series.mixed` travels with the value)."""
        facts = _payload(
            InterestExpenseDebt=_selected(),
            InterestExpenseNonoperating=[
                _q(6, 100.0, filed="2026-03-01"),
                _fact("2025-01-01", "2025-09-30", 300.0, filed="2026-03-01"),
                _fact("2025-01-01", "2025-12-31", 430.0, filed="2026-02-01", form="10-K"),
            ],
        )
        ds, diag = build_dataset(facts, "SYN")
        fd = diag.field_by_name("interest_expense")
        assert ds.periods[-1].interest_expense == 130.0
        assert ds.periods[-1].sources["interest_expense"].note is not None
        assert any(n.startswith("Derived from filings of different dates at FY2025Q4") for n in fd.notes)

    def test_a_field_with_no_gap_reads_no_other_concept(self):
        facts = _payload(
            InterestExpenseDebt=[_q(i, 100.0) for i in range(8)],
            InterestExpenseNonoperating=[_q(i, 100.0) for i in range(3)],
        )
        by_end, fd, diag = _interest(facts)
        assert fd.fallbacks == {} and fd.notes == []
        assert fd.selection == SeriesSelection.of("interest_expense", (DEBT,))

    def test_strategy_fields_are_left_to_their_own_resolution(self):
        """SG&A and D&A resolve each quarter from their registry strategies;
        the checked fallback is for single-concept fields only."""
        facts = _payload(
            DepreciationDepletionAndAmortization=[_q(i, 50.0) for i in range(7)],
            DepreciationAndAmortization=[_q(6, 50.0), _q(7, 60.0)],
        )
        ds, diag = build_dataset(facts, "SYN")
        fd = diag.field_by_name("depreciation_amortization")
        assert ds.periods[-1].depreciation_amortization is None
        assert fd.selection is not None and fd.selection.fallbacks == ()

    def test_instants_are_filled_too(self):
        facts = _payload(
            AccountsReceivableNetCurrent=[_fact(None, e, 70.0) for e in Q_ENDS[:7]],
            ReceivablesNetCurrent=[_fact(None, e, 70.0) for e in Q_ENDS[5:]],
        )
        ds, diag = build_dataset(facts, "SYN")
        fd = diag.field_by_name("receivables")
        assert [p.receivables for p in ds.periods] == [70.0] * 8
        assert fd.fallbacks == {GAP: "us-gaap:ReceivablesNetCurrent"}
        assert fd.notes == ["FY2025Q4 from us-gaap:ReceivablesNetCurrent: the filer switched "
                            "concepts; it agrees with us-gaap:AccountsReceivableNetCurrent on "
                            "FY2025Q2, FY2025Q3."]


class TestWhatCountsAsProof:
    """Independent review of 7a65130: two proofs of equality that prove
    nothing were accepted. Every shared quarter must still agree, AND at
    least one of them must be a reported-window quarter where the selected
    value is not zero."""

    def test_agreeing_only_on_zero_is_not_proof(self):
        """Both tags report 0 at FY2024Q1, the only quarter they share, and
        the alternative's 5000 filled FY2025Q4 against a series of 100s."""
        facts = _payload(
            InterestExpenseDebt=[_q(0, 0.0)] + [_q(i, 100.0) for i in range(1, 7)],
            InterestExpenseNonoperating=[_q(0, 0.0), _q(7, 5000.0)],
        )
        by_end, fd, _ = _interest(facts)
        assert by_end[GAP].interest_expense is None
        assert fd.fallbacks == {}
        assert fd.notes == [
            f"FY2025Q4: {NONOP} reports 5,000 but was not used: it agrees with {DEBT} only "
            "where both report zero (FY2024Q1), which does not show they measure the same figure."
        ]

    def test_a_zero_beside_a_nonzero_agreement_does_not_block_the_fill(self):
        facts = _payload(
            InterestExpenseDebt=[_q(0, 0.0)] + [_q(i, 100.0) for i in range(1, 7)],
            InterestExpenseNonoperating=[_q(0, 0.0), _q(6, 100.0), _q(7, 130.0)],
        )
        by_end, fd, _ = _interest(facts)
        assert by_end[GAP].interest_expense == 130.0
        assert fd.notes == [f"FY2025Q4 from {NONOP}: the filer switched concepts; it agrees "
                            f"with {DEBT} on FY2024Q1, FY2025Q3."]

    def test_a_proof_only_before_the_reported_window_is_not_proof(self):
        """n_quarters=4: the one shared quarter, FY2024Q1, is in the
        derivation buffer, not a quarter the report shows; the alternative's
        999 filled FY2025Q4 against ~100."""
        facts = _payload(
            InterestExpenseDebt=_selected(),
            InterestExpenseNonoperating=[_q(0, 100.0), _q(7, 999.0)],
        )
        by_end, fd, _ = _interest(facts, n_quarters=4)
        assert by_end[GAP].interest_expense is None
        assert fd.fallbacks == {}
        assert fd.notes == [
            f"FY2025Q4: {NONOP} reports 999 but was not used: it agrees with {DEBT} only "
            "before the reported window (FY2024Q1), so agreement is not shown on a quarter "
            "the report uses."
        ]

    def test_a_disagreement_in_the_buffer_still_rejects(self):
        """Disagreement is checked over every quarter, buffer included."""
        facts = _payload(
            InterestExpenseDebt=_selected(),
            InterestExpenseNonoperating=[_q(0, 150.0), _q(6, 100.0), _q(7, 130.0)],
        )
        by_end, fd, _ = _interest(facts, n_quarters=4)
        assert by_end[GAP].interest_expense is None
        assert fd.notes == [
            f"FY2025Q4: {NONOP} reports 130 but was not used: it disagrees with {DEBT} "
            "at FY2024Q1 (150 vs 100)."
        ]

    def test_a_window_proof_with_buffer_agreement_fills(self):
        facts = _payload(
            InterestExpenseDebt=_selected(),
            InterestExpenseNonoperating=[_q(0, 100.0), _q(6, 100.0), _q(7, 130.0)],
        )
        by_end, fd, _ = _interest(facts, n_quarters=4)
        assert by_end[GAP].interest_expense == 130.0
        assert fd.fallbacks == {GAP: NONOP}


class TestRecordsNameTheFallbacks:
    """Independent review of 7a65130: the ledger and the corpus observation
    recorded `tag_used` alone, so CRM's ledger said interest_expense was
    InterestExpenseDebt although FY2027Q1 was read from
    InterestExpenseNonoperating."""

    CRM_LABEL = f"{DEBT}|2026-04-30:{NONOP}"

    def test_the_selection_label_carries_the_fallbacks(self):
        plain = SeriesSelection.of("interest_expense", (DEBT,))
        assert plain.label == DEBT == plain.tag_used
        filled = SeriesSelection.of("interest_expense", (DEBT,), (("2026-04-30", NONOP),))
        assert filled.label == self.CRM_LABEL

    def test_the_ledger_records_the_fallback(self):
        from app.core.pipeline import analyze
        from app.services.reporting.ledger import build_ledger

        facts = json.loads(CRM_DRAFT.read_text())
        ds, diag = build_dataset(facts, "CRM", as_of=date(2026, 5, 28))
        doc = build_ledger(result=analyze(ds), dataset=ds, ticker="CRM",
                           report_date=date(2026, 5, 28), field_tags=diag.selected_series())
        assert doc.selections["interest_expense"] == self.CRM_LABEL
        # A field without fallbacks reads exactly as before.
        assert doc.selections["revenue"] == diag.field_by_name("revenue").tag_used

    def test_the_corpus_observation_records_the_fallback(self):
        from app.services.corpus import load_case, observe

        case, facts, subs = load_case(CRM_DRAFT.parent)
        obs = observe(facts, subs, case.ticker, case.as_of, case.since)
        assert obs.selections["interest_expense"] == self.CRM_LABEL
        assert case.expected.selections["interest_expense"] == self.CRM_LABEL


class TestPointInTime:
    def _facts(self) -> dict:
        return _payload(
            InterestExpenseDebt=_selected(),
            InterestExpenseNonoperating=[
                _q(6, 100.0, filed="2026-03-01"),  # the proof of agreement
                _q(7, 130.0, filed="2026-02-01"),  # the gap
            ],
        )

    def test_a_cut_before_the_alternative_was_filed_does_not_fill(self):
        by_end, fd, _ = _interest(self._facts(), as_of=date(2026, 1, 31))
        assert by_end[GAP].interest_expense is None
        assert fd.fallbacks == {} and fd.notes == []

    def test_the_proof_of_agreement_must_be_visible_too(self):
        """The gap's fact is filed by the cut; the shared quarter that would
        prove the two concepts equal is not. A reader then could not have
        known they agree, so the quarter stays empty."""
        by_end, fd, _ = _interest(self._facts(), as_of=date(2026, 2, 28))
        assert by_end[GAP].interest_expense is None
        assert fd.notes == [
            f"FY2025Q4: {NONOP} reports 130 but was not used: it shares no quarter with "
            f"{DEBT}, so agreement cannot be checked."
        ]

    def test_filled_once_both_were_filed(self):
        by_end, fd, _ = _interest(self._facts(), as_of=date(2026, 3, 1))
        assert by_end[GAP].interest_expense == 130.0
        assert fd.fallbacks == {GAP: NONOP}


class TestSelectionAndDiagnostics:
    def test_the_selection_carries_the_fallbacks_and_tag_used_stays_primary(self):
        facts = _payload(
            InterestExpenseDebt=_selected(),
            InterestExpenseNonoperating=[_q(6, 100.0), _q(7, 130.0)],
        )
        _, fd, diag = _interest(facts)
        assert fd.selection is not None
        assert fd.selection.components == (DEBT,)
        assert fd.selection.concepts == [("us-gaap", "InterestExpenseDebt")]
        assert fd.selection.fallbacks == ((GAP, NONOP),)
        assert fd.tag_used == fd.selection.tag_used == DEBT
        assert diag.selected_tags()["interest_expense"] == DEBT

    def test_the_digest_says_a_quarter_came_from_another_concept(self):
        """Two runs with one digest mapped every field from the same concepts —
        a filled quarter is a different concept."""
        filled = _payload(InterestExpenseDebt=_selected(),
                          InterestExpenseNonoperating=[_q(6, 100.0), _q(7, 130.0)])
        unfilled = _payload(InterestExpenseDebt=_selected(),
                            InterestExpenseNonoperating=[_q(6, 110.0), _q(7, 130.0)])
        plain = _payload(InterestExpenseDebt=_selected())
        d_filled = build_dataset(filled, "SYN")[1].selections_digest()
        assert d_filled != build_dataset(unfilled, "SYN")[1].selections_digest()
        assert build_dataset(unfilled, "SYN")[1].selections_digest() == \
            build_dataset(plain, "SYN")[1].selections_digest()

    def test_a_fallback_must_be_another_concept_of_a_single_field(self):
        with pytest.raises(ValueError, match="selected concept"):
            SeriesSelection(field="interest_expense", composer="single",
                            components=(DEBT,), fallbacks=(("2025-12-31", DEBT),))
        with pytest.raises(ValueError, match="single-concept"):
            SeriesSelection(field="sga_expense", composer="strategy",
                            components=("us-gaap:SellingGeneralAndAdministrativeExpense",),
                            fallbacks=(("2025-12-31", "us-gaap:X"),))

    def test_the_diagnostic_rejects_a_fallback_its_provenance_does_not_show(self):
        sel = SeriesSelection(field="interest_expense", composer="single",
                              components=(DEBT,), fallbacks=((GAP, NONOP),))
        ok = PeriodSource(strategy="single", components=[NONOP], method="direct")
        FieldDiagnostic(field_name="interest_expense", tag_used=DEBT, periods_filled=1,
                        periods_total=1, selection=sel, period_sources={GAP: ok})
        wrong = PeriodSource(strategy="single", components=[DEBT], method="direct")
        with pytest.raises(ValueError, match="fallback"):
            FieldDiagnostic(field_name="interest_expense", tag_used=DEBT, periods_filled=1,
                            periods_total=1, selection=sel, period_sources={GAP: wrong})
        with pytest.raises(ValueError, match="fallback"):
            FieldDiagnostic(field_name="interest_expense", tag_used=DEBT, periods_filled=0,
                            periods_total=1, selection=sel, period_sources={})

    def test_without_a_selection_there_are_no_fallbacks(self):
        fd = FieldDiagnostic(field_name="interest_expense", tag_used=None,
                             periods_filled=0, periods_total=1)
        assert fd.fallbacks == {}


# --- downstream: the restatement scan -------------------------------------------


class TestRestatementScan:
    def _facts(self) -> dict:
        """The selected concept's FY2025Q3 is amended 100 -> 120; the
        alternative agrees with the amended value there and supplies
        FY2025Q4, which it amends 50 -> 60. Its FY2025Q3 row also carries an
        old, different value (90): a revision of a period it does NOT supply."""
        debt = [_q(i, 100.0) for i in range(6)] + [
            _q(6, 100.0, filed="2025-11-01", accn="a-orig"),
            _q(6, 120.0, filed="2026-02-01", form="10-Q/A", accn="a-amend"),
        ]
        nonop = [
            _q(6, 90.0, filed="2025-11-02", accn="b-orig"),
            _q(6, 120.0, filed="2026-02-02", accn="b-later"),
            _q(7, 50.0, filed="2026-02-02", accn="b-q4"),
            _q(7, 60.0, filed="2026-03-01", form="10-Q/A", accn="b-q4-amend"),
        ]
        return _payload(InterestExpenseDebt=debt, InterestExpenseNonoperating=nonop)

    def test_a_field_with_a_fallback_is_scanned_per_concept_and_never_summed(self):
        facts = self._facts()
        ds, diag = build_dataset(facts, "SYN")
        assert ds.periods[-1].interest_expense == 60.0
        assert ds.periods[-2].interest_expense == 120.0
        scan = scan_restatements(facts, selected_tags=diag.selected_series())
        mine = [f for f in scan.footprints if f.field_name == "interest_expense"]
        assert not any("+" in f.tag for f in mine), "a fallback concept was summed"
        got = {(f.tag, f.period_end.isoformat(), f.original_value, f.current_value) for f in mine}
        assert got == {
            (DEBT, SHARED, 100.0, 120.0),  # not 190 -> 240 (the two concepts summed)
            (NONOP, GAP, 50.0, 60.0),  # the filled quarter's own amendment
        }
        assert "interest_expense" in scan.inspected

    def test_same_day_conflicts_of_the_fallback_concept_are_listed_for_its_quarter_only(self):
        facts = self._facts()
        nonop = facts["facts"]["us-gaap"]["InterestExpenseNonoperating"]["units"]["USD"]
        nonop += [_q(7, 61.0, filed="2026-03-01", form="10-Q/A", accn="b-q4-amend"),
                  _q(6, 91.0, filed="2025-11-02", accn="b-orig")]
        _, diag = build_dataset(facts, "SYN")
        scan = scan_restatements(facts, selected_tags=diag.selected_series())
        mine = {(c.tag, c.period_end.isoformat()) for c in scan.conflicts
                if c.field_name == "interest_expense"}
        assert mine == {(NONOP, GAP)}

    def test_a_legacy_string_selection_scans_the_primary_concept_only(self):
        """A tag_used string carries no fallbacks; the scan then reads what it
        names and nothing else (never a sum)."""
        facts = self._facts()
        scan = scan_restatements(facts, selected_tags={"interest_expense": DEBT})
        mine = {(f.tag, f.period_end.isoformat()) for f in scan.footprints
                if f.field_name == "interest_expense"}
        assert mine == {(DEBT, SHARED)}


# --- downstream: the vintage diffs ----------------------------------------------


class TestVintages:
    def _snap(self, gap_value: float, filed: str) -> dict:
        return _payload(
            InterestExpenseDebt=_selected(),
            InterestExpenseNonoperating=[_q(6, 100.0), _q(7, gap_value, filed=filed, accn=filed)],
        )

    def test_a_silent_revision_of_a_filled_quarter_is_seen_and_attributed(self):
        from app.services.ingestion.vintages import diff_scored

        old, new = self._snap(130.0, "2026-02-01"), self._snap(160.0, "2026-04-01")
        diff = diff_scored(old, new)
        (c,) = [c for c in diff.changes if c.field_name == "interest_expense"]
        assert (c.kind, c.key.end.isoformat(), c.old_value, c.new_value) == ("revised", GAP, 130.0, 160.0)
        assert (c.key.taxonomy, c.key.tag) == ("us-gaap", "InterestExpenseNonoperating")
        assert (c.old_accession, c.new_accession) == ("2026-02-01", "2026-04-01")

    def test_the_raw_diff_names_the_quarters_it_cannot_see(self):
        from app.services.ingestion.vintages import raw_diff_blind_spots

        old, new = self._snap(130.0, "2026-02-01"), self._snap(160.0, "2026-04-01")
        assert raw_diff_blind_spots(old, new) == [
            f"interest_expense at {GAP} is read from {NONOP} (the selected concept, {DEBT}, "
            "does not report it); the raw fact diff follows one concept per field and did "
            "not inspect it"
        ]
        plain = _payload(InterestExpenseDebt=[_q(i, 100.0) for i in range(8)])
        assert raw_diff_blind_spots(plain, plain) == []
        assert raw_diff_blind_spots({"facts": {}}, plain) == []
