"""Synthetic companyfacts payloads, one per mapper selection branch.

The three committed real fixtures reach only part of what
`companyfacts_mapper.build_dataset` does: no FY-minus-three-quarters
derivation, no winning D&A composite, no finance-lease addition, no
lease-inclusive debt tag, no LongTermDebt fallback, no debt-free filer, no
same-day tie, no fiscal-year-end fallback. A snapshot of the real fixtures
alone would let a rewrite of the selection code break exactly the paths it
rewrites. Each payload below exists to reach named branches;
`tests/integration/test_selection_snapshot.py` checks that together with the
real fixtures they reach every derivation method and every mapper note.

Everything is deterministic: fixed dates, fixed values, fixed row order (row
order decides same-day ties). Values are distinct per tag so the snapshot
shows WHICH tag supplied a figure, not only that one did.

`CASES` maps a case name to a builder returning a fresh payload.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

# Twelve calendar quarter ends (fiscal year = calendar year): eight in the
# mapper's default window plus its four-quarter derivation buffer.
QUARTER_ENDS: tuple[date, ...] = tuple(
    date(y, m, d) for y in (2022, 2023, 2024) for (m, d) in ((3, 31), (6, 30), (9, 30), (12, 31))
)


def _iso(d: date) -> str:
    return d.isoformat()


def _accn(filed: date, seq: int = 1) -> str:
    return f"0000000001-{filed:%y}-{filed.timetuple().tm_yday * 10 + seq:06d}"


def _filed(end: date, form: str) -> date:
    return end + timedelta(days=60 if form.startswith("10-K") else 40)


def _q_start(qend: date) -> date:
    """First day of the calendar quarter ending `qend`."""
    return date(qend.year, qend.month - 2, 1)


def instant(end: date, val: float, *, filed: date | None = None, form: str = "10-Q") -> dict:
    filed = filed or _filed(end, form)
    return {"end": _iso(end), "val": val, "filed": _iso(filed), "form": form, "accn": _accn(filed)}


def duration(
    start: date, end: date, val: float, *, filed: date | None = None, form: str = "10-Q"
) -> dict:
    filed = filed or _filed(end, form)
    return {
        "start": _iso(start), "end": _iso(end), "val": val,
        "filed": _iso(filed), "form": form, "accn": _accn(filed),
    }


def quarter(qend: date, val: float, **kw: Any) -> dict:
    """A discrete three-month fact."""
    if qend.month == 12:
        kw.setdefault("form", "10-K")
    return duration(_q_start(qend), qend, val, **kw)


def ytd(qend: date, val: float, **kw: Any) -> dict:
    """A year-to-date fact from 1 January (12 months at December → 10-K)."""
    if qend.month == 12:
        kw.setdefault("form", "10-K")
    return duration(date(qend.year, 1, 1), qend, val, **kw)


def annual(year: int, val: float) -> dict:
    return duration(date(year, 1, 1), date(year, 12, 31), val, form="10-K")


class Payload:
    def __init__(self, name: str) -> None:
        self.data: dict = {"entityName": name, "facts": {}}

    def add(self, tag: str, rows: list[dict], *, taxonomy: str = "us-gaap", unit: str = "USD") -> Payload:
        concept = self.data["facts"].setdefault(taxonomy, {}).setdefault(tag, {"units": {}})
        concept["units"].setdefault(unit, []).extend(rows)
        return self


def _base(name: str, *, revenue: bool = True) -> Payload:
    """Assets at every quarter end (the quarter-end source) and discrete
    quarterly revenue plus annual totals (the fiscal-year-end source)."""
    p = Payload(name)
    p.add("Assets", [instant(q, 10_000.0 + 10 * i) for i, q in enumerate(QUARTER_ENDS)])
    if revenue:
        p.add(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            [quarter(q, 1_000.0 + i) for i, q in enumerate(QUARTER_ENDS)]
            + [annual(y, 4_000.0 + y - 2000) for y in (2022, 2023, 2024)],
        )
    return p


def _instants(tag_vals: float, ends: tuple[date, ...] = QUARTER_ENDS, step: float = 1.0) -> list[dict]:
    return [instant(q, tag_vals + step * i) for i, q in enumerate(ends)]


# ---------------------------------------------------------------------------
# Flows: direct, ytd_diff, fy_minus_3q, amendments, ties, non-additive


def flows() -> dict:
    p = Payload("Flows Co")
    p.add("Assets", _instants(10_000.0, step=10.0))
    q = QUARTER_ENDS
    # Revenue: discrete Q1–Q3 10-Q facts and an annual 10-K total, never a Q4
    # quarter → Q4 = FY − (Q1+Q2+Q3).
    p.add(
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        [quarter(e, 1_000.0 + 7 * i) for i, e in enumerate(q) if e.month != 12]
        + [annual(2022, 4_100.0), annual(2023, 4_300.0), annual(2024, 4_500.0)],
    )
    # CFO: year-to-date only → Q1 direct, Q2–Q4 by differencing.
    p.add(
        "NetCashProvidedByUsedInOperatingActivities",
        [ytd(e, 300.0 * (i % 4 + 1) + 11 * i) for i, e in enumerate(q)],
    )
    # Net income: every quarter direct; 2023-06-30 amended by a 10-Q/A and
    # then re-reported as a comparative — the latest filed value wins.
    ni = [quarter(e, 100.0 + i) for i, e in enumerate(q)]
    ni.append(quarter(q[5], 777.0, filed=date(2023, 11, 20), form="10-Q/A"))
    ni.append(quarter(q[5], 778.0, filed=date(2024, 8, 9)))
    p.add("NetIncomeLoss", ni)
    # Cost of revenue: a same-day tie on 2024-03-31 — two facts for one
    # period filed on one day; the first in payload order is kept.
    cor = [quarter(e, 600.0 + i) for i, e in enumerate(q)]
    cor.insert(9, quarter(q[8], 999.0))
    p.add("CostOfRevenue", cor)
    # Interest expense: discrete quarters with 2023-06-30 missing and an
    # annual total → 2023 Q4 cannot be derived (a prior quarter is absent).
    p.add(
        "InterestExpense",
        [quarter(e, 20.0 + i) for i, e in enumerate(q) if e.month != 12 and e != q[5]]
        + [annual(2022, 90.0), annual(2023, 95.0), annual(2024, 99.0)],
    )
    # Stock comp: year-to-date, with the 2024 H1 figure amended upward later
    # → the Q2 difference uses the amended YTD.
    sbc = [ytd(e, 50.0 * (i % 4 + 1)) for i, e in enumerate(q)]
    sbc.append(ytd(q[9], 130.0, filed=date(2024, 10, 15), form="10-Q/A"))
    p.add("ShareBasedCompensation", sbc)
    # Diluted shares: quarterly 10-Q facts, annual figure only at year end,
    # mis-filed under USD. Non-additive → Q4 stays missing, with its note.
    p.add(
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        [quarter(e, 500.0 - i) for i, e in enumerate(q) if e.month != 12]
        + [annual(y, 490.0) for y in (2022, 2023, 2024)],
    )
    # Buybacks: a row without a value (dropped), an undated row that loses to
    # a dated one for the same period, and an undated row that is the only
    # fact for its period (kept outside point-in-time mode).
    bb = [quarter(e, 40.0 + i) for i, e in enumerate(q) if e not in (q[9], q[10])]
    bad = quarter(q[9], 0.0)
    del bad["val"]
    undated_loser = quarter(q[4], 1.0)
    del undated_loser["filed"]
    undated_only = quarter(q[10], 55.0)
    del undated_only["filed"]
    p.add("PaymentsForRepurchaseOfCommonStock", [undated_loser, *bb, bad, undated_only])
    # Capex: year-to-date, except that 2023 H1 is filed as a five-month
    # figure whose start matches no earlier fact → 2023-06-30 cannot be
    # differenced, and neither can 2023-09-30 (no six-month figure to
    # subtract); 2023-12-31 differences against the nine-month figure.
    cap = [ytd(e, 80.0 * (i % 4 + 1)) for i, e in enumerate(q) if e != q[5]]
    cap.append(duration(date(2023, 2, 1), q[5], 45.0))
    p.add("PaymentsToAcquirePropertyPlantAndEquipment", cap)
    # Equity proceeds: year-to-date from 1 January, and a later filing that
    # re-presents 2024 H1 and Q1 from 1 December 2023 (a calendar
    # transition). Two year-to-date facts end on 2024-06-30 and both can be
    # differenced; the latest filed pair is used.
    ipo = [ytd(e, 10.0 * (i % 4 + 1)) for i, e in enumerate(q)]
    later = date(2024, 10, 1)
    ipo.append(duration(date(2023, 12, 1), q[9], 64.0, filed=later))
    ipo.append(duration(date(2023, 12, 1), q[8], 30.0, filed=later))
    p.add("ProceedsFromIssuanceOfCommonStock", ipo)
    return p.data


# ---------------------------------------------------------------------------
# Tag choice: coverage, candidate order, cover-page dates, composites


def tag_choice() -> dict:
    p = _base("Tag Choice Co")
    q = QUARTER_ENDS
    # Equal coverage → the earlier candidate wins.
    p.add("AccountsReceivableNetCurrent", _instants(300.0))
    p.add("ReceivablesNetCurrent", _instants(900.0))
    # Unequal coverage → the better-covered candidate wins over the earlier.
    p.add("InventoryNet", _instants(200.0, q[7:]))
    p.add("InventoryGross", _instants(250.0))
    # Coverage is ranked over the REPORTED window first: the first tag covers
    # 10 of 12 buffered quarters but only 6 of the 8 reported; the second
    # covers all 8 reported, and wins. (Ranked over the buffered window, the
    # abandoned tag won and two reported quarters were empty.)
    p.add("CashAndCashEquivalentsAtCarryingValue", _instants(500.0, q[:10]))
    p.add("CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", _instants(520.0, q[4:]))
    # Only the third candidate is filed.
    p.add("AccountsPayableTradeCurrent", _instants(150.0))
    # Same-day tie on an instant: the first row in payload order is kept.
    dr = _instants(70.0)
    dr.insert(9, instant(q[8], 71.5))
    p.add("ContractWithCustomerLiabilityCurrent", dr)
    # Goodwill: an undated duplicate loses to the dated fact.
    gw = _instants(800.0)
    undated = instant(q[6], 1.0)
    del undated["filed"]
    p.add("Goodwill", [undated, *gw])
    # Shares: dei cover-page counts 20 days after each quarter end (a second,
    # later one for 2023-09-30; none within 60 days for 2023-06-30), and a
    # us-gaap balance-sheet count at only six quarter ends. dei wins on
    # coverage via the nearest-cover-date match.
    dei = [instant(e + timedelta(days=20), 5_000.0 - i) for i, e in enumerate(q) if e != q[5]]
    dei.append(instant(q[6] + timedelta(days=45), 4_000.0))
    dei.append(instant(q[5] + timedelta(days=70), 4_990.0))
    p.add("EntityCommonStockSharesOutstanding", dei, taxonomy="dei", unit="shares")
    p.add("CommonStockSharesOutstanding", _instants(4_500.0, q[6:]), unit="shares")
    # SG&A: the single tag is reported for the last four quarters, S&M + G&A
    # for all twelve. Each quarter uses the single tag where it is reported
    # and the composite elsewhere.
    p.add("SellingGeneralAndAdministrativeExpense", [quarter(e, 330.0) for e in q[8:]])
    p.add("SellingAndMarketingExpense", [quarter(e, 200.0 + i) for i, e in enumerate(q)])
    p.add("GeneralAndAdministrativeExpense", [quarter(e, 100.0 + 2 * i) for i, e in enumerate(q)])
    # D&A: the aggregate tag is reported for the first 10 quarters (6 of the
    # reported ones), depreciation + amortization for the last 8. Each
    # quarter takes the aggregate where it is reported and the composite in
    # the last two — the aggregate is the filer's own total.
    p.add("DepreciationDepletionAndAmortization", [quarter(e, 60.0) for e in q[:10]])
    p.add("Depreciation", [quarter(e, 40.0 + i) for i, e in enumerate(q) if i >= 4])
    p.add("AmortizationOfIntangibleAssets", [quarter(e, 15.0) for e in q[4:]])
    return p.data


def composites_lose() -> dict:
    p = _base("Composites Lose Co")
    q = QUARTER_ENDS
    # SG&A: single tag and composite both cover everything → single wins.
    p.add("SellingGeneralAndAdministrativeExpense", [quarter(e, 310.0 + i) for i, e in enumerate(q)])
    p.add("SellingAndMarketingExpense", [quarter(e, 200.0) for e in q])
    p.add("GeneralAndAdministrativeExpense", [quarter(e, 100.0) for e in q])
    # D&A: only Depreciation is filed → used, with the amortization caveat.
    p.add("Depreciation", [quarter(e, 45.0 + i) for i, e in enumerate(q)])
    return p.data


def da_split_equal_coverage() -> dict:
    """Depreciation and amortization reported separately in every quarter:
    composed (20 + 10 = 30), not depreciation alone — which is what the
    mapper built when `Depreciation` was an aggregate candidate."""
    p = _base("DA Split Co")
    p.add("Depreciation", [quarter(e, 20.0 + i) for i, e in enumerate(QUARTER_ENDS)])
    p.add("AmortizationOfIntangibleAssets", [quarter(e, 10.0) for e in QUARTER_ENDS])
    return p.data


def da_aggregate_with_amortization() -> dict:
    """An aggregate D&A tag plus separately disclosed depreciation and
    amortization, all in every quarter (KO files all three). The aggregate
    already includes both: it wins every tie, and nothing is added to it."""
    p = _base("DA Aggregate Co")
    p.add("DepreciationDepletionAndAmortization", [quarter(e, 70.0 + i) for i, e in enumerate(QUARTER_ENDS)])
    p.add("Depreciation", [quarter(e, 55.0 + i) for i, e in enumerate(QUARTER_ENDS)])
    p.add("AmortizationOfIntangibleAssets", [quarter(e, 12.0) for e in QUARTER_ENDS])
    return p.data


def da_amortization_partial() -> dict:
    """Amortization reported only from 2023-12-31: those quarters are
    composed, the three before them are depreciation alone — each named as
    partial. (The choice used to be made for the whole window, and
    depreciation alone won everywhere.)"""
    p = _base("DA Partial Co")
    p.add("Depreciation", [quarter(e, 30.0 + i) for i, e in enumerate(QUARTER_ENDS)])
    p.add("AmortizationOfIntangibleAssets", [quarter(e, 5.0) for e in QUARTER_ENDS[7:]])
    return p.data


def da_google_pattern() -> dict:
    """Alphabet's history (Hermes re-audit): depreciation in every quarter,
    separate amortization only in the latest four. The latest four are
    complete (7,104 + 367 = 7,471 at the last quarter end, not 7,104); the
    older four are depreciation alone, flagged partial."""
    p = _base("DA Alphabet Pattern Co")
    q = QUARTER_ENDS
    p.add("Depreciation", [quarter(e, 5_000.0 + 191.0 * i) for i, e in enumerate(q[:-1])]
          + [quarter(q[-1], 7_104.0)])
    p.add("AmortizationOfIntangibleAssets", [quarter(e, 300.0 + 22.0 * i) for i, e in enumerate(q[8:-1])]
          + [quarter(q[-1], 367.0)])
    return p.data


# ---------------------------------------------------------------------------
# Total debt: composed per balance-sheet date (composition.compose_total_debt)


def debt_full_breakdown() -> dict:
    p = _base("Debt Full Co")
    q = QUARTER_ENDS
    p.add("LongTermDebtNoncurrent", _instants(2_000.0))
    # Current portion missing at one quarter end → counted as 0 there.
    p.add("LongTermDebtCurrent", [instant(e, 150.0 + i) for i, e in enumerate(q) if e != q[9]])
    # Short-term role: the first candidate is reported at three quarter ends,
    # the second at all twelve. At each date the first candidate reported AT
    # THAT DATE counts — CommercialPaper where ShortTermBorrowings is absent.
    p.add("ShortTermBorrowings", _instants(40.0, q[9:]))
    p.add("CommercialPaper", _instants(400.0))
    p.add("FinanceLeaseLiabilityNoncurrent", _instants(30.0))
    p.add("FinanceLeaseLiabilityCurrent", _instants(5.0, q[6:]))
    return p.data


def debt_hermes_double_count() -> dict:
    """Hermes's controlled filing: noncurrent 800, current portion 100,
    aggregate current debt 150. DebtCurrent already contains the current
    portion: 950, not 1,050."""
    p = _base("Debt Aggregate Current Co")
    p.add("LongTermDebtNoncurrent", _instants(800.0, step=0.0))
    p.add("LongTermDebtCurrent", _instants(100.0, step=0.0))
    p.add("DebtCurrent", _instants(150.0, step=0.0))
    p.add("FinanceLeaseLiabilityCurrent", _instants(7.0, step=0.0))  # inside DebtCurrent
    return p.data


def debt_intel_pattern() -> dict:
    """Intel's history: current portion and commercial paper in older
    quarters, aggregate current debt (DebtCurrent) in the latest. CommercialPaper
    used to be chosen for the whole series because it had values somewhere,
    and DebtCurrent was dropped from the latest quarters."""
    p = _base("Debt Intel Pattern Co")
    q = QUARTER_ENDS
    p.add("LongTermDebtNoncurrent", _instants(46_000.0, step=100.0))
    p.add("LongTermDebtCurrent", _instants(1_500.0, q[:9]))
    p.add("CommercialPaper", _instants(900.0, q[:9]))
    p.add("DebtCurrent", _instants(1_988.0, q[9:], step=16.0))
    return p.data


def debt_tag_migration() -> dict:
    """KO's history: the plain debt tags only in the buffer quarters, the
    lease-inclusive ones in every reported quarter. The buffer tags used to
    win (first with any value) and every reported quarter was empty."""
    p = _base("Debt Migration Co")
    q = QUARTER_ENDS
    p.add("LongTermDebtNoncurrent", _instants(38_000.0, q[:4]))
    p.add("LongTermDebtCurrent", _instants(1_200.0, q[:4]))
    p.add("LongTermDebtAndCapitalLeaseObligations", _instants(40_000.0, q[4:], step=50.0))
    p.add("LongTermDebtAndCapitalLeaseObligationsCurrent", _instants(1_300.0, q[4:]))
    p.add("CommercialPaper", _instants(4_000.0))
    return p.data


def debt_fallback_some_quarters() -> dict:
    """The split is reported only from 2023-09-30; before that only the
    LongTermDebt total. Each date uses what was reported at it, and the note
    names the fallback quarters."""
    p = _base("Debt Partial Split Co")
    q = QUARTER_ENDS
    p.add("LongTermDebtNoncurrent", _instants(900.0, q[6:]))
    p.add("LongTermDebtCurrent", _instants(60.0, q[6:]))
    p.add("LongTermDebt", _instants(955.0))
    return p.data


def debt_lease_inclusive() -> dict:
    """Lease-inclusive noncurrent tag with a plain current tag: only the
    current finance-lease liability is added."""
    p = _base("Debt Lease Inclusive Co")
    p.add("LongTermDebtAndCapitalLeaseObligations", _instants(3_000.0))
    p.add("LongTermDebtCurrent", _instants(120.0))
    p.add("FinanceLeaseLiabilityNoncurrent", _instants(60.0))
    p.add("FinanceLeaseLiabilityCurrent", _instants(9.0))
    return p.data


def debt_both_lease_inclusive() -> dict:
    """Both debt tags embed finance leases: none is added on top."""
    p = _base("Debt Both Inclusive Co")
    q = QUARTER_ENDS
    # The first noncurrent candidate has facts, but none at a quarter end →
    # empty series, so the lease-inclusive second candidate is taken.
    p.add("LongTermDebtNoncurrent", [instant(e - timedelta(days=15), 9.0) for e in q])
    p.add("LongTermDebtAndCapitalLeaseObligations", _instants(3_100.0))
    p.add("LongTermDebtAndCapitalLeaseObligationsCurrent", _instants(130.0))
    p.add("FinanceLeaseLiabilityNoncurrent", _instants(61.0))
    p.add("FinanceLeaseLiabilityCurrent", _instants(8.0))
    p.add("DebtCurrent", _instants(12.0))
    return p.data


def debt_noncurrent_only() -> dict:
    p = _base("Debt Noncurrent Only Co")
    p.add("LongTermDebtNoncurrent", _instants(1_500.0, QUARTER_ENDS[2:]))
    return p.data


def debt_total_fallback_with_leases() -> dict:
    p = _base("Debt Total Fallback Co")
    q = QUARTER_ENDS
    p.add("LongTermDebt", _instants(2_500.0))
    p.add("CommercialPaper", _instants(90.0, q[5:]))
    p.add("FinanceLeaseLiabilityNoncurrent", _instants(33.0))
    return p.data


def debt_total_fallback_plain() -> dict:
    p = _base("Debt Total Plain Co")
    p.add("LongTermDebt", _instants(2_600.0))
    return p.data


def debt_none() -> dict:
    return _base("Debt Free Co").data


# ---------------------------------------------------------------------------
# Quarter ends and fiscal labels


def quarter_ends_from_revenue() -> dict:
    """No Assets: quarter ends come from quarterly revenue. The first revenue
    candidate has only annual facts, so the second supplies them."""
    p = Payload("No Assets Co")
    q = QUARTER_ENDS
    p.add(
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        [annual(y, 4_000.0 + y - 2000) for y in (2022, 2023, 2024)],
    )
    p.add("Revenues", [quarter(e, 1_100.0 + i) for i, e in enumerate(q)])
    p.add("NetIncomeLoss", [quarter(e, 90.0 + i) for i, e in enumerate(q)])
    return p.data


def unknown_fiscal_year_end() -> dict:
    """No annual-duration facts: labels fall back to P<date>, a warning is
    raised, and the missing critical fields are warned about in order."""
    p = Payload("No Annual Co")
    q = QUARTER_ENDS
    p.add("Assets", _instants(10_000.0, step=10.0))
    p.add(
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        [quarter(e, 1_000.0 + i) for i, e in enumerate(q) if e.month != 12],
    )
    return p.data


def fifty_two_week() -> dict:
    """A 52/53-week calendar whose fiscal year ends on the first days of a
    month: ends on day ≤ 4 count toward the previous month, and 1–4
    January toward December of the previous year — 2023-01-01 is FY2023Q1,
    not FY2024Q1 (the label of 2023-12-31), which is what it was labelled
    until that was fixed."""
    p = Payload("Retail Weeks Co")
    # Year ends 2022-10-02, 2023-10-01, 2024-09-29: fiscal September.
    ends = [date(2021, 10, 3) + timedelta(weeks=13 * i) for i in range(1, 13)]
    p.add("Assets", [instant(e, 7_000.0 + i) for i, e in enumerate(ends)])
    rev = [
        duration(e - timedelta(weeks=13) + timedelta(days=1), e, 500.0 + i)
        for i, e in enumerate(ends)
    ]
    year_ends = [e for i, e in enumerate(ends) if i % 4 == 3]
    rev += [
        duration(e - timedelta(weeks=52) + timedelta(days=1), e, 2_100.0 + i, form="10-K")
        for i, e in enumerate(year_ends)
    ]
    p.add("Revenues", rev)
    return p.data


def mixed_vintages() -> dict:
    """Derived quarters rebuilt from one filing date (Hermes audit round 4).

    Operating income: 2024 Q1-Q3 are 100 each and the 10-K reports 400 for
    the year (filed 2025-03-01); a 10-Q/A then restates 2024 Q1 to 150
    (2025-03-10). Q4 is the year less the three quarters AS THE 10-K SAW
    THEM: 400 - 300 = 100, not 400 - 350 = 50.

    Cash from operations: year-to-date only in 2024 Q2, and the Q1 figure it
    subtracts was first filed a month AFTER the H1 total (a late
    re-presentation), so no single filing date has both. The latest values
    are used and the quarter is noted."""
    p = _base("Mixed Vintages Co")
    q = QUARTER_ENDS
    oi = [quarter(e, 90.0 + i) for i, e in enumerate(q[:8])]
    oi += [quarter(e, 100.0) for e in q[8:11]]
    oi += [annual(2022, 400.0), annual(2023, 420.0), annual(2024, 400.0)]
    oi.append(quarter(q[8], 150.0, filed=date(2025, 3, 10), form="10-Q/A"))
    p.add("OperatingIncomeLoss", oi)
    cfo = [quarter(e, 50.0 + i) for i, e in enumerate(q) if e not in (q[8], q[9])]
    cfo.append(quarter(q[8], 60.0, filed=date(2024, 9, 1)))
    cfo.append(ytd(q[9], 130.0, filed=date(2024, 8, 1)))
    p.add("NetCashProvidedByUsedInOperatingActivities", cfo)
    return p.data


CASES: dict[str, Callable[[], dict]] = {
    "flows": flows,
    "mixed_vintages": mixed_vintages,
    "tag_choice": tag_choice,
    "composites_lose": composites_lose,
    "da_split_equal_coverage": da_split_equal_coverage,
    "da_aggregate_with_amortization": da_aggregate_with_amortization,
    "da_amortization_partial": da_amortization_partial,
    "da_google_pattern": da_google_pattern,
    "debt_full_breakdown": debt_full_breakdown,
    "debt_hermes_double_count": debt_hermes_double_count,
    "debt_intel_pattern": debt_intel_pattern,
    "debt_tag_migration": debt_tag_migration,
    "debt_fallback_some_quarters": debt_fallback_some_quarters,
    "debt_lease_inclusive": debt_lease_inclusive,
    "debt_both_lease_inclusive": debt_both_lease_inclusive,
    "debt_noncurrent_only": debt_noncurrent_only,
    "debt_total_fallback_with_leases": debt_total_fallback_with_leases,
    "debt_total_fallback_plain": debt_total_fallback_plain,
    "debt_none": debt_none,
    "quarter_ends_from_revenue": quarter_ends_from_revenue,
    "unknown_fiscal_year_end": unknown_fiscal_year_end,
    "fifty_two_week": fifty_two_week,
}
