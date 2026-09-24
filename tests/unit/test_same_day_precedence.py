"""Same-day filing order (Hermes audit round 3, finding 1).

Companyfacts dates a filing but does not time it. An original and its
amendment filed on one day used to collapse into one state: the composite
restatement check keyed its vintages by date, kept the first fact on the
day, and a real 110 -> 160 amendment of summed SG&A produced no footprint.
Every reader now orders facts by `precedence` — date, then amendment over
original, then accession — and a same-day disagreement the order cannot
settle is reported, not silently chosen.
"""

from __future__ import annotations

from datetime import date

from hypothesis import given
from hypothesis import strategies as st

from app.services.ingestion import restatements as rs
from app.services.ingestion import vintages as vs
from app.services.ingestion.companyfacts_mapper import (
    _collect,
    _dedupe_latest_filed,
    build_dataset,
)
from app.services.ingestion.precedence import (
    conflicts,
    current_conflict,
    earliest,
    latest,
    level,
    rank,
)
from tests.fixtures.selection_cases import QUARTER_ENDS, _base, instant, quarter
from tests.strategies import same_day_trails

D = date(2025, 8, 1)
Q = QUARTER_ENDS[-1]  # 2024-12-31, a reported quarter
AMEND_DAY = date(2025, 3, 20)


def _key(f):
    return rank(f["filed"], f["form"], f["accn"])


class TestPrecedence:
    def test_date_then_amendment_then_accession(self):
        assert rank(D, "10-K", "z") < rank(date(2025, 8, 2), "10-Q", "a")
        assert rank(D, "10-Q", "z") < rank(D, "10-Q/A", "a")
        assert rank(D, "10-Q", "a") < rank(D, "10-Q", "b")
        assert level(D, "10-K/A") == (D, True)

    def test_a_full_tie_keeps_the_first(self):
        rows = [{"filed": D, "form": "10-Q", "accn": "a", "v": 1},
                {"filed": D, "form": "10-Q", "accn": "a", "v": 2}]
        assert latest(rows, key=_key)["v"] == 1
        assert earliest(rows, key=_key)["v"] == 1

    def test_a_malformed_form_is_not_an_amendment(self):
        assert rank(D, None, 7) == (D, False, "7")  # type: ignore[arg-type]

    def test_conflicts_group_by_level_not_accession(self):
        rows = [{"filed": D, "form": "10-Q", "accn": "a", "v": 1.0},
                {"filed": D, "form": "10-Q", "accn": "b", "v": 2.0},
                {"filed": D, "form": "10-Q/A", "accn": "c", "v": 3.0},
                {"filed": date(2025, 9, 1), "form": "10-Q", "accn": "d", "v": 4.0},
                {"filed": date(2025, 9, 1), "form": "10-Q", "accn": "e", "v": 4.0}]
        groups = conflicts(rows, key=_key, value=lambda r: r["v"])
        assert [[r["accn"] for r in g] for g in groups] == [["a", "b"]]
        # Only the current level decides the value; it agrees, so no conflict.
        assert current_conflict(rows, key=_key, value=lambda r: r["v"]) == []
        assert [r["accn"] for r in current_conflict(rows[:2], key=_key, value=lambda r: r["v"])] == ["a", "b"]


def _sga_payload(*, amended_first: bool, amend_ga: bool = False) -> dict:
    """Composite SG&A (no SG&A total tag): S&M + G&A per quarter. For the
    last quarter, S&M 100 and G&A 10 are filed, and ON THE SAME DAY a 10-Q/A
    restates S&M to 150 (and G&A to 30 when `amend_ga`)."""
    p = _base("Same Day Co")
    sm = [quarter(q, 50.0 + i) for i, q in enumerate(QUARTER_ENDS[:-1])]
    ga = [quarter(q, 5.0 + i) for i, q in enumerate(QUARTER_ENDS[:-1])]
    original_sm = quarter(Q, 100.0, filed=AMEND_DAY, form="10-Q")
    amended_sm = quarter(Q, 150.0, filed=AMEND_DAY, form="10-Q/A")
    amended_sm["accn"] = "0000000001-25-000002"
    original_sm["accn"] = "0000000001-25-000001"
    sm += [amended_sm, original_sm] if amended_first else [original_sm, amended_sm]
    ga.append(quarter(Q, 10.0, filed=AMEND_DAY, form="10-Q"))
    if amend_ga:
        g = quarter(Q, 30.0, filed=AMEND_DAY, form="10-Q/A")
        g["accn"] = "0000000001-25-000002"
        ga.append(g)
    p.add("SellingAndMarketingExpense", sm)
    p.add("GeneralAndAdministrativeExpense", ga)
    return p.data


def _sga_value_and_footprint(payload):
    ds, diag = build_dataset(payload, "SDC")
    mapped = next(p.sga_expense for p in ds.periods if p.period_end == Q)
    fps = [f for f in rs.detect_restatements(payload, selected_tags=diag.selected_tags())
           if f.field_name == "sga_expense" and f.period_end == Q]
    return mapped, fps


class TestSameDayCompositeAmendment:
    def test_hermes_case_is_reported_in_either_input_order(self):
        for amended_first in (False, True):
            mapped, fps = _sga_value_and_footprint(_sga_payload(amended_first=amended_first))
            assert mapped == 160.0
            assert len(fps) == 1, amended_first
            fp = fps[0]
            assert (fp.original_value, fp.current_value) == (110.0, 160.0)
            assert fp.is_amendment and fp.amendment_form == "10-Q/A"
            assert fp.current_value == mapped  # the evidence describes the scored value

    def test_two_vintages_on_one_day(self):
        payload = _sga_payload(amended_first=True)
        series = [("us-gaap", "SellingAndMarketingExpense"), ("us-gaap", "GeneralAndAdministrativeExpense")]
        trail = rs._composite_vintages(payload, series, "USD", None)[(date(2024, 10, 1), Q)]
        assert [(v[0], v[1], v[2]) for v in trail] == [
            (AMEND_DAY, 110.0, "10-Q"), (AMEND_DAY, 160.0, "10-Q/A"),
        ]

    def test_an_amendment_of_both_components(self):
        mapped, fps = _sga_value_and_footprint(_sga_payload(amended_first=False, amend_ga=True))
        assert mapped == 180.0
        assert [(f.original_value, f.current_value) for f in fps] == [(110.0, 180.0)]


class TestSameDayDebtAmendment:
    def test_an_amended_debt_component_on_the_same_day(self):
        for amended_first in (True, False):
            p = _base("Debt Day Co")
            ltd = [instant(q, 800.0) for q in QUARTER_ENDS[:-1]]
            amended = instant(Q, 900.0, filed=AMEND_DAY, form="10-Q/A")
            amended["accn"] = "0000000001-25-000002"
            original = instant(Q, 800.0, filed=AMEND_DAY)
            ltd += [amended, original] if amended_first else [original, amended]
            p.add("LongTermDebtNoncurrent", ltd)
            p.add("LongTermDebtCurrent", [instant(q, 100.0) for q in QUARTER_ENDS])
            ds, diag = build_dataset(p.data, "DDC")
            assert next(x.total_debt for x in ds.periods if x.period_end == Q) == 1000.0
            fps = [f for f in rs.detect_restatements(p.data, selected_tags=diag.selected_tags())
                   if f.field_name == "total_debt" and f.period_end == Q]
            assert [(f.original_value, f.current_value, f.is_amendment) for f in fps] == [
                (900.0, 1000.0, True)
            ]


class TestSameDayConflictIsReported:
    def _payload(self, reverse: bool) -> dict:
        p = _base("Conflict Co")
        rows = [quarter(q, 600.0 + i) for i, q in enumerate(QUARTER_ENDS[:-1])]
        a = quarter(Q, 700.0, filed=AMEND_DAY)
        b = quarter(Q, 900.0, filed=AMEND_DAY)
        a["accn"], b["accn"] = "0000000001-25-000001", "0000000001-25-000009"
        rows += [b, a] if reverse else [a, b]
        p.add("CostOfRevenue", rows)
        return p.data

    def test_value_is_order_independent_and_the_choice_is_named(self):
        for reverse in (False, True):
            ds, diag = build_dataset(self._payload(reverse), "CC")
            assert next(x.cost_of_revenue for x in ds.periods if x.period_end == Q) == 900.0
            notes = diag.field_by_name("cost_of_revenue").notes
            assert len(notes) == 1 and notes[0].startswith(
                "Same-day conflicting facts for us-gaap:CostOfRevenue at FY2024Q4")
            assert "Not a revision" in notes[0]

    def test_the_scan_lists_it_and_does_not_promote_it(self):
        payload = self._payload(False)
        _ds, diag = build_dataset(payload, "CC")
        scan = rs.scan_restatements(payload, selected_tags=diag.selected_tags())
        (c,) = [c for c in scan.conflicts if c.field_name == "cost_of_revenue"]
        assert (c.period_end, c.filed, c.values, c.amended) == (Q, AMEND_DAY, (700.0, 900.0), False)
        assert c.accessions == ("0000000001-25-000001", "0000000001-25-000009")
        text = rs.render_restatements_section(scan)
        assert "### Same-day conflicting facts (not revisions)" in text
        assert "1 same-day conflict(s)" in text
        assert "700, 900" in text
        # A footprint needs two levels; one day's disagreement is not one.
        assert not [f for f in scan.footprints if f.field_name == "cost_of_revenue"]

    def test_no_conflict_no_section(self):
        payload = _sga_payload(amended_first=False)
        _ds, diag = build_dataset(payload, "SDC")
        scan = rs.scan_restatements(payload, selected_tags=diag.selected_tags())
        assert scan.conflicts == ()
        assert "Same-day conflicting" not in rs.render_restatements_section(scan)
        assert not any("Same-day" in n for n in diag.field_notes())


def _assets(rows):
    return {"entityName": "T", "facts": {"us-gaap": {"Assets": {"units": {"USD": rows}}}}}


@given(rows=same_day_trails(), data=st.data())
def test_the_current_value_depends_on_order_only_where_a_conflict_is_named(rows, data):
    """Every reader agrees, and shuffling the rows moves the value only when
    the current level holds a disagreement — the case the note names."""
    end = date(2025, 6, 30)
    shuffled = data.draw(st.permutations(rows))

    def current(rs_rows):
        facts = _collect(_assets(rs_rows), "us-gaap", "Assets", "USD")
        return _dedupe_latest_filed(facts)[(None, end)].val

    raw = [dict(r, filed=date.fromisoformat(r["filed"])) for r in rows]
    disputed = current_conflict(raw, key=_key, value=lambda r: r["val"])
    if not disputed:
        assert current(rows) == current(shuffled)
    for order in (rows, shuffled):
        mapped = current(order)
        stored = [v for k, v in vs._series(_assets(order), scored_only=True).items()
                  if k[0] == "total_assets"][0]
        assert stored["val"] == mapped
        fps = rs.detect_restatements(_assets(order), selected_tags={"total_assets": "us-gaap:Assets"})
        if fps:
            assert fps[0].current_value == mapped
