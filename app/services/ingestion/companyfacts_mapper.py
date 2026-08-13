"""Pure mapper: SEC companyfacts JSON -> CompanyDataset + IngestionDiagnostics.

No network code here: the entire mapping logic is testable offline against
committed fixtures.

Real-data problems handled (each validated against real filings — see
docs/real_data_validation.md — and covered by tests):

1. DURATION AMBIGUITY — 10-Qs report the same flow concept over multiple
   durations (quarter-to-date and year-to-date, same end date). Facts are
   classified by duration length; only ~quarter-length durations are used
   directly.
2. MISSING Q4 — companies file FY totals, not a Q4 period. Q4 flow values are
   derived as FY − (Q1 + Q2 + Q3) when the three prior quarters are known.
3. YTD-ONLY CASH-FLOW ITEMS — CFO, capex, buybacks, SBC are typically reported
   only cumulatively in 10-Qs; quarterly values are recovered by differencing
   consecutive YTD facts sharing a fiscal-year start.
4. AMENDMENTS & COMPARATIVES — the same period appears in multiple filings;
   the latest-filed value wins.
5. WINDOW EDGE — derivations need quarters before the requested window, so the
   mapper computes over a buffered window and trims.
6. TAG SWITCHES — filers change tags over time (e.g. XOM receivables); each
   candidate tag is scored and the one with the best period coverage wins
   (tags are never mixed within one series).
7. COVER-PAGE DATES — dei:EntityCommonStockSharesOutstanding is stamped with
   the cover date (weeks after quarter end); matched with bounded tolerance.
8. UNRELIABLE fy/fp METADATA — SEC's fy/fp fields can carry the wrong year on
   annual facts (observed on CRM). Fiscal labels are derived structurally from
   the fiscal-year-end month instead. FY numbering = calendar year in which
   the fiscal year ends.

Every canonical field records which XBRL tag supplied it and how each period's
value was obtained (direct / ytd_diff / fy_minus_3q / composite / nearest) in
IngestionDiagnostics.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from pydantic import BaseModel, Field

from app.schemas.financials import (
    CompanyDataset,
    CompanyProfile,
    PeriodFinancials,
    PeriodType,
)

QTD_DAYS = (70, 100)
ANNUAL_DAYS = (330, 380)
WINDOW_BUFFER_QUARTERS = 4  # extra history fetched so edge quarters can derive
COVER_DATE_TOLERANCE_DAYS = 60  # dei share counts are stamped with cover dates

INSTANT_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "total_assets": (("us-gaap", "Assets"),),
    "current_assets": (("us-gaap", "AssetsCurrent"),),
    "cash_and_equivalents": (
        ("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),
        ("us-gaap", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
    ),
    "receivables": (
        ("us-gaap", "AccountsReceivableNetCurrent"),
        ("us-gaap", "ReceivablesNetCurrent"),
        ("us-gaap", "AccountsAndOtherReceivablesNetCurrent"),
        ("us-gaap", "AccountsNotesAndLoansReceivableNetCurrent"),
    ),
    "inventory": (("us-gaap", "InventoryNet"), ("us-gaap", "InventoryGross")),
    "ppe_net": (
        ("us-gaap", "PropertyPlantAndEquipmentNet"),
        (
            "us-gaap",
            "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization",
        ),
    ),
    "intangible_assets": (
        ("us-gaap", "FiniteLivedIntangibleAssetsNet"),
        ("us-gaap", "IntangibleAssetsNetExcludingGoodwill"),
    ),
    "goodwill": (("us-gaap", "Goodwill"),),
    "current_liabilities": (("us-gaap", "LiabilitiesCurrent"),),
    "accounts_payable": (
        ("us-gaap", "AccountsPayableCurrent"),
        ("us-gaap", "AccountsPayableAndAccruedLiabilitiesCurrent"),
        ("us-gaap", "AccountsPayableTradeCurrent"),
    ),
    "deferred_revenue": (
        ("us-gaap", "ContractWithCustomerLiabilityCurrent"),
        ("us-gaap", "DeferredRevenueCurrent"),
    ),
    "shares_outstanding": (
        ("dei", "EntityCommonStockSharesOutstanding"),
        ("us-gaap", "CommonStockSharesOutstanding"),
    ),
}

FLOW_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "revenue": (
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
        ("us-gaap", "Revenues"),
        ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"),
        ("us-gaap", "SalesRevenueNet"),
    ),
    "cost_of_revenue": (
        ("us-gaap", "CostOfRevenue"),
        ("us-gaap", "CostOfGoodsAndServicesSold"),
        ("us-gaap", "CostOfGoodsSold"),
        ("us-gaap", "CostOfSales"),
    ),
    "sga_expense": (("us-gaap", "SellingGeneralAndAdministrativeExpense"),),
    "operating_income": (("us-gaap", "OperatingIncomeLoss"),),
    "ebit": (("us-gaap", "OperatingIncomeLoss"),),
    "depreciation_amortization": (
        ("us-gaap", "DepreciationDepletionAndAmortization"),
        ("us-gaap", "DepreciationAmortizationAndAccretionNet"),
        ("us-gaap", "DepreciationAndAmortization"),
        ("us-gaap", "Depreciation"),
    ),
    "interest_expense": (
        ("us-gaap", "InterestExpense"),
        ("us-gaap", "InterestExpenseNonoperating"),
        ("us-gaap", "InterestExpenseDebt"),
    ),
    "net_income": (("us-gaap", "NetIncomeLoss"), ("us-gaap", "ProfitLoss")),
    "stock_based_compensation": (
        ("us-gaap", "ShareBasedCompensation"),
        ("us-gaap", "AllocatedShareBasedCompensationExpense"),
    ),
    "cfo": (
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
    ),
    "capex": (
        ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),
        ("us-gaap", "PaymentsToAcquireProductiveAssets"),
        ("us-gaap", "PaymentsForCapitalImprovements"),
    ),
    "buybacks": (("us-gaap", "PaymentsForRepurchaseOfCommonStock"),),
    "share_issuance_proceeds": (
        ("us-gaap", "ProceedsFromIssuanceOfCommonStock"),
        ("us-gaap", "ProceedsFromIssuanceOrSaleOfEquity"),
    ),
    "shares_diluted": (
        ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
        ("us-gaap", "WeightedAverageNumberOfShareOutstandingBasicAndDiluted"),
    ),
}

SGA_COMPONENTS = (
    ("us-gaap", "SellingAndMarketingExpense"),
    ("us-gaap", "GeneralAndAdministrativeExpense"),
)
DA_COMPONENTS = (
    ("us-gaap", "Depreciation"),
    ("us-gaap", "AmortizationOfIntangibleAssets"),
)

# LongTermDebt is a TOTAL (current + noncurrent): used only when the split is
# unavailable, never alongside it (double counting).
DEBT_NONCURRENT = ("LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations")
DEBT_CURRENT = ("LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent")
DEBT_TOTAL = ("LongTermDebt",)
DEBT_SHORT = ("ShortTermBorrowings", "CommercialPaper", "DebtCurrent")

# Finance (capital) lease liabilities are a financing obligation and belong in
# total debt (P0-10). Operating-lease liabilities are deliberately EXCLUDED —
# a different economic commitment that credit leverage conventions keep apart.
FINANCE_LEASE_NONCURRENT = ("FinanceLeaseLiabilityNoncurrent",)
FINANCE_LEASE_CURRENT = ("FinanceLeaseLiabilityCurrent",)
# Debt tags that already embed capital/finance-lease obligations: adding the
# separately reported finance-lease liability on top would double-count.
LEASE_INCLUSIVE_DEBT_TAGS = frozenset(
    {"LongTermDebtAndCapitalLeaseObligations", "LongTermDebtAndCapitalLeaseObligationsCurrent"}
)

# Weighted-average share counts are not additive across quarters: no Q4
# derivation, direct facts only.
NON_ADDITIVE_FLOWS = {"shares_diluted"}


@dataclass(frozen=True)
class RawFact:
    start: date | None  # None for instant facts
    end: date
    val: float
    filed: date
    form: str

    @property
    def days(self) -> int | None:
        if self.start is None:
            return None
        return (self.end - self.start).days


class FieldDiagnostic(BaseModel):
    field_name: str
    tag_used: str | None
    periods_filled: int
    periods_total: int
    methods: dict[str, int] = Field(default_factory=dict, description="method -> period count")
    missing_periods: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class IngestionDiagnostics(BaseModel):
    ticker: str
    entity_name: str | None
    quarter_ends: list[str]
    fiscal_year_end_month: int | None
    fields: list[FieldDiagnostic]
    warnings: list[str] = Field(default_factory=list)

    def coverage(self) -> float:
        total = sum(f.periods_total for f in self.fields)
        filled = sum(f.periods_filled for f in self.fields)
        return filled / total if total else 0.0

    def field_by_name(self, name: str) -> FieldDiagnostic:
        return next(f for f in self.fields if f.field_name == name)


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _collect(facts_json: dict, taxonomy: str, tag: str, unit: str) -> list[RawFact]:
    concept = facts_json.get("facts", {}).get(taxonomy, {}).get(tag)
    if not concept:
        return []
    units = concept.get("units", {})
    entries = units.get(unit)
    if entries is None and unit == "shares":
        entries = units.get("USD")  # some filers mis-file share counts
    if entries is None:
        return []
    out: list[RawFact] = []
    for e in entries:
        try:
            out.append(
                RawFact(
                    start=_parse_date(e["start"]) if "start" in e else None,
                    end=_parse_date(e["end"]),
                    val=float(e["val"]),
                    filed=_parse_date(e.get("filed", "1900-01-01")),
                    form=e.get("form", ""),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _dedupe_latest_filed(facts: list[RawFact]) -> dict[tuple[date | None, date], RawFact]:
    """Latest-filed value wins per (start, end): amendments and comparative
    re-reports supersede originals."""
    best: dict[tuple[date | None, date], RawFact] = {}
    for f in facts:
        key = (f.start, f.end)
        if key not in best or f.filed > best[key].filed:
            best[key] = f
    return best


def _unit_for(field_name: str) -> str:
    return "shares" if field_name in ("shares_outstanding", "shares_diluted") else "USD"


# ---------------------------------------------------------------------------
# Quarter ends and fiscal labels
# ---------------------------------------------------------------------------


def select_quarter_ends(facts_json: dict, n_quarters: int) -> list[date]:
    """Canonical quarter-end dates from balance-sheet (Assets) instants — the
    most reliably filed concept; falls back to quarterly revenue ends."""
    assets = _dedupe_latest_filed(_collect(facts_json, "us-gaap", "Assets", "USD"))
    ends = sorted({end for (_, end) in assets})
    if not ends:
        for taxonomy, tag in FLOW_FIELDS["revenue"]:
            rev = _collect(facts_json, taxonomy, tag, "USD")
            ends = sorted(
                {f.end for f in rev if f.days is not None and QTD_DAYS[0] <= f.days <= QTD_DAYS[1]}
            )
            if ends:
                break
    return ends[-n_quarters:]


def fiscal_year_end_month(facts_json: dict) -> int | None:
    """Mode of the end month across annual-duration facts. 52/53-week
    calendars can end within ~4 days of a month boundary; ends on day <= 4 are
    attributed to the previous month."""
    months: list[int] = []
    for name in ("revenue", "net_income", "cfo"):
        for taxonomy, tag in FLOW_FIELDS[name]:
            for f in _collect(facts_json, taxonomy, tag, "USD"):
                if f.days is not None and ANNUAL_DAYS[0] <= f.days <= ANNUAL_DAYS[1]:
                    months.append(_effective_month(f.end))
    if not months:
        return None
    return Counter(months).most_common(1)[0][0]


def _effective_month(d: date) -> int:
    m = d.month - 1 if d.day <= 4 else d.month
    return 12 if m == 0 else m


def _fiscal_label(qend: date, fye_month: int | None) -> str:
    """Structural fiscal label. FY numbering = calendar year in which the
    fiscal year ends (SEC convention; some companies brand differently)."""
    if fye_month is None:
        return f"P{qend.isoformat()}"
    month = _effective_month(qend)
    delta = (fye_month - month) % 12
    quarter = 4 - delta // 3
    fy_year = qend.year + (1 if month > fye_month else 0)
    return f"FY{fy_year}Q{quarter}"


# ---------------------------------------------------------------------------
# Series extraction
# ---------------------------------------------------------------------------


class _FlowSeries:
    """Quarterly value extraction for one duration-based concept."""

    def __init__(self, facts: list[RawFact], allow_derivation: bool = True):
        self.by_key = _dedupe_latest_filed([f for f in facts if f.start is not None])
        self.allow_derivation = allow_derivation

    def _find(self, qend: date, min_days: int, max_days: int) -> RawFact | None:
        matches = [
            f
            for f in self.by_key.values()
            if f.end == qend and f.days is not None and min_days <= f.days <= max_days
        ]
        return max(matches, key=lambda f: f.filed) if matches else None

    def quarterly(self, quarter_ends: list[date]) -> tuple[dict[date, float], dict[date, str]]:
        values: dict[date, float] = {}
        methods: dict[date, str] = {}
        for i, qend in enumerate(quarter_ends):
            direct = self._find(qend, *QTD_DAYS)
            if direct is not None:
                values[qend] = direct.val
                methods[qend] = "direct"
                continue
            if not self.allow_derivation:
                continue
            prev_end = quarter_ends[i - 1] if i > 0 else None
            if prev_end is not None:
                ytd_cur = [
                    f for f in self.by_key.values() if f.end == qend and f.days and f.days > QTD_DAYS[1]
                ]
                for f2 in sorted(ytd_cur, key=lambda f: -f.filed.toordinal()):
                    f1 = self.by_key.get((f2.start, prev_end))
                    if f1 is None:
                        continue
                    implied_days = f2.days - (f1.days or 0)  # type: ignore[operator]
                    if QTD_DAYS[0] <= implied_days <= QTD_DAYS[1]:
                        # Round-14 finding 4 (known limitation): `f2` and `f1`
                        # are each the LATEST-filed value for their period, but
                        # they can come from different filings/accessions. If
                        # a company amends only the longer YTD (Q1-Q2 restated
                        # but Q1 alone unchanged), the diff mixes vintages.
                        # The scored-field impact is already caught by
                        # detect_restatements; a full fix requires per-value
                        # form/accession provenance (P1-A). Until then, no
                        # cross-vintage flag is emitted.
                        values[qend] = f2.val - f1.val
                        methods[qend] = "ytd_diff"
                        break
            if qend in values:
                continue
            annual = self._find(qend, *ANNUAL_DAYS)
            if annual is not None and i >= 3 and annual.start is not None:
                prior = quarter_ends[i - 3 : i]
                if all(p in values and annual.start <= p for p in prior):
                    values[qend] = annual.val - sum(values[p] for p in prior)
                    methods[qend] = "fy_minus_3q"
        return values, methods


def _instant_series(
    facts: list[RawFact],
    quarter_ends: list[date],
    tolerance_days: int = 0,
) -> tuple[dict[date, float], dict[date, str]]:
    by_end: dict[date, RawFact] = {}
    for f in facts:
        if f.start is not None:
            continue
        if f.end not in by_end or f.filed > by_end[f.end].filed:
            by_end[f.end] = f
    values: dict[date, float] = {}
    methods: dict[date, str] = {}
    for q in quarter_ends:
        if q in by_end:
            values[q] = by_end[q].val
            methods[q] = "direct"
        elif tolerance_days:
            # Cover-page dates trail the quarter end by days-to-weeks.
            window = [
                e for e in by_end if q < e <= q + timedelta(days=tolerance_days)
            ]
            if window:
                nearest = min(window)
                values[q] = by_end[nearest].val
                methods[q] = "nearest"
    return values, methods


def _score(values: dict[date, float], quarter_ends: list[date]) -> int:
    return sum(1 for q in quarter_ends if q in values)


def _best_series(
    facts_json: dict,
    candidates: tuple[tuple[str, str], ...],
    unit: str,
    quarter_ends: list[date],
    kind: str,
    allow_derivation: bool = True,
    tolerance_days: int = 0,
) -> tuple[dict[date, float], dict[date, str], str | None]:
    """Evaluate every candidate tag; the one covering the most quarters wins
    (ties break by candidate order). Tags are never mixed within a series —
    that would fabricate period-over-period jumps."""
    best: tuple[int, int, dict[date, float], dict[date, str], str | None] = (0, 0, {}, {}, None)
    for rank, (taxonomy, tag) in enumerate(candidates):
        facts = _collect(facts_json, taxonomy, tag, unit)
        if not facts:
            continue
        if kind == "instant":
            values, methods = _instant_series(facts, quarter_ends, tolerance_days)
        else:
            values, methods = _FlowSeries(facts, allow_derivation).quarterly(quarter_ends)
        coverage = _score(values, quarter_ends)
        if coverage > best[0] or (coverage == best[0] and coverage > 0 and -rank > best[1]):
            best = (coverage, -rank, values, methods, f"{taxonomy}:{tag}")
    return best[2], best[3], best[4]


def _composite_flow(
    facts_json: dict,
    components: tuple[tuple[str, str], ...],
    quarter_ends: list[date],
) -> tuple[dict[date, float], dict[date, str], str]:
    """Sum of component flows, only for quarters where ALL components exist."""
    parts: list[dict[date, float]] = []
    for taxonomy, tag in components:
        fs = _FlowSeries(_collect(facts_json, taxonomy, tag, "USD"))
        parts.append(fs.quarterly(quarter_ends)[0])
    common = set(parts[0]).intersection(*parts[1:]) if parts else set()
    values = {q: sum(p[q] for p in parts) for q in common}
    label = "+".join(tag for _, tag in components)
    return values, {q: "composite" for q in common}, label


def _total_debt_series(
    facts_json: dict, quarter_ends: list[date]
) -> tuple[dict[date, float], str | None, list[str]]:
    notes: list[str] = []

    def series(tags: tuple[str, ...]) -> tuple[dict[date, float], str | None]:
        for tag in tags:
            vals, _ = _instant_series(_collect(facts_json, "us-gaap", tag, "USD"), quarter_ends)
            if vals:
                return vals, tag
        return {}, None

    noncur, noncur_tag = series(DEBT_NONCURRENT)
    cur, cur_tag = series(DEBT_CURRENT)
    short, short_tag = series(DEBT_SHORT)
    fin_nc, fin_nc_tag = series(FINANCE_LEASE_NONCURRENT)
    fin_c, fin_c_tag = series(FINANCE_LEASE_CURRENT)

    if noncur:
        # Only add finance-lease liabilities the chosen debt tag does not
        # already embed (P0-10 double-count guard).
        add_fin_nc = fin_nc if noncur_tag not in LEASE_INCLUSIVE_DEBT_TAGS else {}
        add_fin_c = fin_c if cur_tag not in LEASE_INCLUSIVE_DEBT_TAGS else {}
        used_parts = [noncur_tag, cur_tag or "none", short_tag or "none"]
        out = {
            q: noncur[q]
            + cur.get(q, 0.0)
            + short.get(q, 0.0)
            + add_fin_nc.get(q, 0.0)
            + add_fin_c.get(q, 0.0)
            for q in quarter_ends
            if q in noncur
        }
        if not cur:
            notes.append("Current portion of long-term debt unavailable; total debt may understate.")
        if not short:
            notes.append("Short-term borrowings unavailable or zero; not included.")
        if add_fin_nc or add_fin_c:
            notes.append("Finance-lease liabilities added to total debt; operating leases excluded.")
            used_parts += [t for t in (fin_nc_tag if add_fin_nc else None, fin_c_tag if add_fin_c else None) if t]
        return out, "+".join(used_parts), notes

    total, total_tag = series(DEBT_TOTAL)
    if total:
        # Review finding 6: finance leases must be added on the fallback path too.
        # us-gaap:LongTermDebt is not one of the lease-inclusive tags, so add
        # them (with the standard caveat that the total tag can, for some filers,
        # already embed capital leases).
        used_parts = [total_tag, short_tag or "none"]
        out = {
            q: total[q] + short.get(q, 0.0) + fin_nc.get(q, 0.0) + fin_c.get(q, 0.0)
            for q in total
        }
        notes.append("Used LongTermDebt total (current/noncurrent split unavailable).")
        if fin_nc or fin_c:
            notes.append(
                "Finance-lease liabilities added to total debt; operating leases excluded "
                "(the LongTermDebt total may, for some filers, already embed capital leases)."
            )
            used_parts += [t for t in (fin_nc_tag if fin_nc else None, fin_c_tag if fin_c else None) if t]
        return out, "+".join(used_parts), notes

    return {}, None, ["No debt concepts found; company may be debt-free or use custom tags."]


# ---------------------------------------------------------------------------
# Dataset assembly
# ---------------------------------------------------------------------------


def build_dataset(
    facts_json: dict,
    ticker: str,
    n_quarters: int = 8,
    sector: str | None = None,
) -> tuple[CompanyDataset, IngestionDiagnostics]:
    # Extended window: edge quarters need earlier history for YTD differencing
    # and FY-minus-3Q derivation; the output is trimmed back afterwards.
    extended_ends = select_quarter_ends(facts_json, n_quarters + WINDOW_BUFFER_QUARTERS)
    if len(extended_ends) < 2:
        raise ValueError(
            f"Could not establish at least 2 quarter-end dates for {ticker}; "
            "the filer may use custom tags for Assets and revenue."
        )
    window_ends = extended_ends[-n_quarters:]
    fye_month = fiscal_year_end_month(facts_json)
    labels = {q: _fiscal_label(q, fye_month) for q in extended_ends}

    field_values: dict[str, dict[date, float]] = {}
    diagnostics: list[FieldDiagnostic] = []
    warnings: list[str] = []
    if fye_month is None:
        warnings.append(
            "Could not determine fiscal-year-end month (no annual-duration facts); "
            "period labels fall back to raw dates."
        )

    def record(
        name: str,
        values: dict[date, float],
        methods: dict[date, str],
        tag: str | None,
        notes: list[str],
    ) -> None:
        field_values[name] = values
        in_window = {q: m for q, m in methods.items() if q in window_ends}
        method_counts: dict[str, int] = {}
        for m in in_window.values():
            method_counts[m] = method_counts.get(m, 0) + 1
        diagnostics.append(
            FieldDiagnostic(
                field_name=name,
                tag_used=tag,
                periods_filled=sum(1 for q in window_ends if q in values),
                periods_total=len(window_ends),
                methods=method_counts,
                missing_periods=[labels[q] for q in window_ends if q not in values],
                notes=notes,
            )
        )

    for name, tags in INSTANT_FIELDS.items():
        tolerance = COVER_DATE_TOLERANCE_DAYS if name == "shares_outstanding" else 0
        values, methods, used = _best_series(
            facts_json, tags, _unit_for(name), extended_ends, "instant", tolerance_days=tolerance
        )
        notes = []
        if name == "shares_outstanding" and "nearest" in methods.values():
            notes.append(
                "Share counts matched from cover-page dates within "
                f"{COVER_DATE_TOLERANCE_DAYS} days after quarter end."
            )
        record(name, values, methods, used, notes)

    for name, tags in FLOW_FIELDS.items():
        values, methods, used = _best_series(
            facts_json,
            tags,
            _unit_for(name),
            extended_ends,
            "flow",
            allow_derivation=name not in NON_ADDITIVE_FLOWS,
        )
        notes: list[str] = []
        if name == "sga_expense":
            comp_values, comp_methods, comp_tag = _composite_flow(
                facts_json, SGA_COMPONENTS, extended_ends
            )
            if _score(comp_values, window_ends) > _score(values, window_ends):
                values, methods, used = comp_values, comp_methods, comp_tag
                notes.append("SG&A composed from separate S&M and G&A tags.")
        if name == "depreciation_amortization":
            comp_values, comp_methods, comp_tag = _composite_flow(
                facts_json, DA_COMPONENTS, extended_ends
            )
            if _score(comp_values, window_ends) > _score(values, window_ends):
                values, methods, used = comp_values, comp_methods, comp_tag
                notes.append("D&A composed from separate depreciation and amortization tags.")
            elif used == "us-gaap:Depreciation":
                notes.append(
                    "Only a depreciation tag was available; amortization may be excluded "
                    "(capex/D&A can overstate)."
                )
        if name in NON_ADDITIVE_FLOWS and _score(values, window_ends) < len(window_ends):
            notes.append(
                "Weighted-average share counts are not additive; quarters without a "
                "directly reported value stay missing (no Q4 derivation)."
            )
        record(name, values, methods, used, notes)

    debt_values, debt_tag, debt_notes = _total_debt_series(facts_json, extended_ends)
    record("total_debt", debt_values, {q: "composite" for q in debt_values}, debt_tag, debt_notes)

    coverage_by_field = {d.field_name: d.periods_filled for d in diagnostics}
    for critical in ("revenue", "net_income", "cfo", "total_assets"):
        missing_n = len(window_ends) - coverage_by_field.get(critical, 0)
        if missing_n:
            warnings.append(
                f"Critical field '{critical}' missing for {missing_n} of "
                f"{len(window_ends)} quarters."
            )

    periods = [
        PeriodFinancials(
            period_end=q,
            period_type=PeriodType.QUARTER,
            fiscal_label=labels[q],
            **{name: values.get(q) for name, values in field_values.items()},
        )
        for q in window_ends
    ]

    dataset = CompanyDataset(
        profile=CompanyProfile(
            ticker=ticker.upper(),
            name=facts_json.get("entityName"),
            sector=sector,
        ),
        periods=periods,
    )
    diag = IngestionDiagnostics(
        ticker=ticker.upper(),
        entity_name=facts_json.get("entityName"),
        quarter_ends=[q.isoformat() for q in window_ends],
        fiscal_year_end_month=fye_month,
        fields=diagnostics,
        warnings=warnings,
    )
    return dataset, diag
