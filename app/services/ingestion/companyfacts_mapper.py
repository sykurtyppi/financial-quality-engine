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
   candidate tag is scored and the one covering the most REPORTED quarters
   wins (tags are never mixed within one series). Total debt is the
   exception: it is composed per balance-sheet date from what was reported
   at that date (composition.compose_total_debt), because a debt role's tag
   can change without the series changing meaning (KO's migration to the
   lease-inclusive tags; Intel's move to aggregate current debt).
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
from app.services.ingestion.composition import (
    COMPOSITE,
    DEBT_TAGS,
    PARTIAL,
    SPLIT,
    SPLIT_AGGREGATE_CURRENT,
    TOTAL_FALLBACK,
    DebtComposition,
    Resolved,
    compose_total_debt,
    resolve_by_strategy,
)
from app.services.ingestion.fields import (
    COVER_DATE_TOLERANCE_DAYS,
    CRITICAL_FIELDS,
    FIELDS,
    Kind,
    candidate_table,
    composite_components,
    role_tags,
    unit_for,
)
from app.services.ingestion.fields import field as field_spec
from app.services.ingestion.precedence import Rank, current_conflict, latest
from app.services.ingestion.precedence import rank as fact_rank

QTD_DAYS = (70, 100)
ANNUAL_DAYS = (330, 380)
WINDOW_BUFFER_QUARTERS = 4  # extra history fetched so edge quarters can derive

# The field ontology lives in `fields.FIELDS`; everything below is a view of
# it under the name (and in the order) the mapper and its importers have
# always used. Add or reorder tags there, never here.
INSTANT_FIELDS: dict[str, tuple[tuple[str, str], ...]] = candidate_table(Kind.INSTANT)
FLOW_FIELDS: dict[str, tuple[tuple[str, str], ...]] = candidate_table(Kind.FLOW)

SGA_COMPONENTS = composite_components("sga_expense")
DA_COMPONENTS = composite_components("depreciation_amortization")

# LongTermDebt is a TOTAL (current + noncurrent): used only when the split is
# unavailable, never alongside it (double counting).
DEBT_NONCURRENT = role_tags("total_debt", "noncurrent")
DEBT_CURRENT = role_tags("total_debt", "current")
DEBT_TOTAL = role_tags("total_debt", "total")
DEBT_SHORT = role_tags("total_debt", "short")
DEBT_CURRENT_AGGREGATE = role_tags("total_debt", "current_aggregate")

# Finance (capital) lease liabilities are a financing obligation and belong in
# total debt (P0-10). Operating-lease liabilities are deliberately EXCLUDED —
# a different economic commitment that credit leverage conventions keep apart.
FINANCE_LEASE_NONCURRENT = role_tags("total_debt", "finance_lease_nc")
FINANCE_LEASE_CURRENT = role_tags("total_debt", "finance_lease_c")
# Debt tags that already embed capital/finance-lease obligations: adding the
# separately reported finance-lease liability on top would double-count.
LEASE_INCLUSIVE_DEBT_TAGS = field_spec("total_debt").lease_inclusive_tags

# Weighted-average share counts are not additive across quarters: no Q4
# derivation, direct facts only.
NON_ADDITIVE_FLOWS = {f.name for f in FIELDS if f.kind is Kind.FLOW and not f.additive}


@dataclass(frozen=True)
class RawFact:
    start: date | None  # None for instant facts
    end: date
    val: float
    filed: date
    form: str
    accn: str = ""

    @property
    def days(self) -> int | None:
        if self.start is None:
            return None
        return (self.end - self.start).days


class PeriodSource(BaseModel):
    """How one reported quarter's value of a field was built: the strategy
    (`single`, `composite`, `partial`, or total debt's `split` /
    `split_aggregate_current` / `total`), the qualified concepts it drew on,
    the derivation method, and whether it is knowingly incomplete."""

    strategy: str
    components: list[str]
    method: str
    partial: bool = False


class FieldDiagnostic(BaseModel):
    field_name: str
    tag_used: str | None
    periods_filled: int
    periods_total: int
    methods: dict[str, int] = Field(default_factory=dict, description="method -> period count")
    missing_periods: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    # Reported quarter end (ISO) -> how that quarter's value was built. The
    # series-level `tag_used` cannot say this once a field can be built
    # differently from one quarter to the next.
    period_sources: dict[str, PeriodSource] = Field(default_factory=dict)


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

    def selected_tags(self) -> dict[str, str | None]:
        """Which qualified XBRL tag actually backed each canonical field in
        this run. The mapper's choice is the only authority on what the engine
        scored; anything downstream that needs to name the same series (the
        restatement detector does) must read it from here rather than
        re-deriving it from the payload and hoping the two agree."""
        return {f.field_name: f.tag_used for f in self.fields}

    def field_notes(self) -> list[str]:
        """Every per-field mapping note, prefixed with its field, in mapper
        order. These record how a figure was BUILT — "total debt may
        understate", "only a depreciation tag was available" — and until now
        reached only `scripts/validate_real_data.py`; a report that scored the
        figure never said so. Rendered verbatim in the data-quality appendix."""
        return [f"{f.field_name}: {note}" for f in self.fields for note in f.notes]


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
                    accn=str(e.get("accn", "")),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _rank(f: RawFact) -> Rank:
    """The shared current-fact order (`precedence`): filed date, then an
    amendment over an original, then accession."""
    return fact_rank(f.filed, f.form, f.accn)


def _dedupe_latest_filed(facts: list[RawFact]) -> dict[tuple[date | None, date], RawFact]:
    """The current value per (start, end): amendments and comparative
    re-reports supersede originals, by the order in `precedence` — a same-day
    10-Q/A supersedes its 10-Q. A full tie keeps the first fact."""
    best: dict[tuple[date | None, date], RawFact] = {}
    for f in facts:
        key = (f.start, f.end)
        if key not in best or _rank(f) > _rank(best[key]):
            best[key] = f
    return best


def _unit_for(field_name: str) -> str:
    return unit_for(field_name)


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


def _effective_period(d: date) -> tuple[int, int]:
    """(year, month) a period end belongs to. A 52/53-week end on day <= 4
    belongs to the previous month — and 1-4 January to December of the
    previous YEAR."""
    if d.day <= 4:
        d = d.replace(day=1) - timedelta(days=1)
    return d.year, d.month


def _effective_month(d: date) -> int:
    return _effective_period(d)[1]


def _fiscal_label(qend: date, fye_month: int | None) -> str:
    """Structural fiscal label. FY numbering = calendar year in which the
    fiscal year ends (SEC convention; some companies brand differently).
    The year is the effective one: a quarter ending 2023-01-01 is the
    December 2022 quarter, not a 2023 one (it was once labelled with the
    same FY as 2023-12-31)."""
    if fye_month is None:
        return f"P{qend.isoformat()}"
    year, month = _effective_period(qend)
    delta = (fye_month - month) % 12
    quarter = 4 - delta // 3
    fy_year = year + (1 if month > fye_month else 0)
    return f"FY{fy_year}Q{quarter}"


# ---------------------------------------------------------------------------
# Series extraction
# ---------------------------------------------------------------------------


class _FlowSeries:
    """Quarterly value extraction for one duration-based concept.

    A derived quarter subtracts earlier periods from a longer figure: a
    year-to-date total (`ytd_diff`) or the fiscal year (`fy_minus_3q`). The
    earlier periods are taken AS THEY STOOD WHEN THAT LONGER FIGURE WAS
    FILED (`_as_of`), not as they stand today. A Q1 amended from 100 to 150
    after the 10-K reported 400 for the year did not change Q4: the 400
    still embeds the old 100, so Q4 is 400 - 100 - 100 - 100 = 100, where
    subtracting today's Q1 made it 50 (Hermes audit round 4). When nothing
    was revised after the longer figure was filed, the cut sees the same
    facts and the value is unchanged. Where an earlier period did not exist
    yet at that filing, the latest values are used and the quarter is
    listed in `mixed`."""

    def __init__(self, facts: list[RawFact], allow_derivation: bool = True):
        self.facts = [f for f in facts if f.start is not None]
        self.by_key = _dedupe_latest_filed(self.facts)
        self.allow_derivation = allow_derivation
        self.mixed: set[date] = set()
        self._cuts: dict[date, _FlowSeries] = {}

    def _as_of(self, cutoff: date) -> _FlowSeries:
        """This concept as filed on or before `cutoff`."""
        if cutoff not in self._cuts:
            self._cuts[cutoff] = _FlowSeries(
                [f for f in self.facts if f.filed <= cutoff], self.allow_derivation
            )
        return self._cuts[cutoff]

    def _find(self, qend: date, min_days: int, max_days: int) -> RawFact | None:
        matches = [
            f
            for f in self.by_key.values()
            if f.end == qend and f.days is not None and min_days <= f.days <= max_days
        ]
        return latest(matches, key=_rank) if matches else None

    def quarterly(self, quarter_ends: list[date]) -> tuple[dict[date, float], dict[date, str]]:
        values: dict[date, float] = {}
        methods: dict[date, str] = {}
        self.mixed = set()
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
                for f2 in sorted(ytd_cur, key=_rank, reverse=True):
                    f1 = self.by_key.get((f2.start, prev_end))
                    if f1 is None:
                        continue
                    implied_days = f2.days - (f1.days or 0)  # type: ignore[operator]
                    if QTD_DAYS[0] <= implied_days <= QTD_DAYS[1]:
                        # The earlier period as it stood when this
                        # year-to-date figure was filed (class docstring):
                        # a Q1 restated after H1 was filed is not what H1
                        # embeds.
                        then = self._as_of(f2.filed).by_key.get((f2.start, prev_end))
                        if then is None:
                            then = f1
                            self.mixed.add(qend)
                        values[qend] = f2.val - then.val
                        methods[qend] = "ytd_diff"
                        break
            if qend in values:
                continue
            annual = self._find(qend, *ANNUAL_DAYS)
            if annual is not None and i >= 3 and annual.start is not None:
                prior = quarter_ends[i - 3 : i]
                if all(p in values and annual.start <= p for p in prior):
                    # The three quarters as they stood when the annual figure
                    # was filed (class docstring).
                    then, _ = self._as_of(annual.filed).quarterly(quarter_ends[:i])
                    if all(p in then for p in prior):
                        values[qend] = annual.val - sum(then[p] for p in prior)
                    else:
                        values[qend] = annual.val - sum(values[p] for p in prior)
                        self.mixed.add(qend)
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
        if f.end not in by_end or _rank(f) > _rank(by_end[f.end]):
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
    window_ends: list[date] | None = None,
    mixed_out: set[date] | None = None,
) -> tuple[dict[date, float], dict[date, str], str | None]:
    """Evaluate every candidate tag; the one covering the most REPORTED
    quarters (`window_ends`) wins, then the most buffered quarters
    (`quarter_ends`, which derivations draw on), then candidate order. Tags
    are never mixed within a series — that would fabricate
    period-over-period jumps.

    Coverage used to be counted over the buffered window alone, so a tag a
    filer had abandoned could outrank the one it files today by covering
    more OLD quarters, leaving reported quarters empty.

    `mixed_out`, when given, receives the winning flow series' quarters that
    could not be derived from one filing date (`_FlowSeries.mixed`)."""
    window = window_ends if window_ends is not None else quarter_ends
    best_key: tuple[int, int, int] | None = None
    best: tuple[dict[date, float], dict[date, str], str | None] = ({}, {}, None)
    best_mixed: set[date] = set()
    for rank, (taxonomy, tag) in enumerate(candidates):
        facts = _collect(facts_json, taxonomy, tag, unit)
        if not facts:
            continue
        mixed: set[date] = set()
        if kind == "instant":
            values, methods = _instant_series(facts, quarter_ends, tolerance_days)
        else:
            series = _FlowSeries(facts, allow_derivation)
            values, methods = series.quarterly(quarter_ends)
            mixed = series.mixed
        key = (_score(values, window), _score(values, quarter_ends), -rank)
        if key[1] > 0 and (best_key is None or key > best_key):
            best_key, best, best_mixed = key, (values, methods, f"{taxonomy}:{tag}"), mixed
    if mixed_out is not None:
        mixed_out |= best_mixed
    return best


def _where(quarters: list[date], among: list[date], labels: dict[date, str]) -> str:
    """" at FY…, FY…" naming the quarters a note applies to — or nothing when
    it applies to every quarter it could, so the note reads as it always
    has."""
    if quarters == among:
        return ""
    return " at " + ", ".join(labels[q] for q in quarters)


def _qualified(concept: str) -> str:
    return f"us-gaap:{concept}"


def _resolved_flow(
    facts_json: dict,
    name: str,
    quarter_ends: list[date],
    window_ends: list[date],
    labels: dict[date, str],
    single: tuple[dict[date, float], dict[date, str], str | None],
    mixed: set[date] | None = None,
) -> tuple[dict[date, float], dict[date, str], str | None, list[str], dict[date, PeriodSource]]:
    """A flow field with alternative strategies (SG&A, D&A), resolved per
    quarter by `composition.resolve_by_strategy` — the rule the restatement
    detector applies too. `single` is the field's own concept as
    `_best_series` selected it (tags are never mixed within it). `mixed`
    holds the single concept's quarters that could not be derived from one
    filing date; on return it holds the resolved field's."""
    single_values, single_methods, single_used = single
    single_tag = single_used.split(":", 1)[1] if single_used else None
    mixed_by: dict[str | None, set[date]] = {single_tag: set(mixed or ())}
    component_series: dict[str, tuple[dict[date, float], dict[date, str]]] = {}
    for taxonomy, tag in composite_components(name):
        series = _FlowSeries(_collect(facts_json, taxonomy, tag, "USD"))
        component_series[tag] = series.quarterly(quarter_ends)
        mixed_by[tag] = series.mixed

    values: dict[date, float] = {}
    methods: dict[date, str] = {}
    resolved: dict[date, Resolved] = {}
    for q in quarter_ends:
        present: dict[str, float] = {}
        if single_tag is not None and q in single_values:
            present[single_tag] = single_values[q]
        for tag, (series, _m) in component_series.items():
            if q in series:
                present[tag] = series[q]
        r = resolve_by_strategy(name, present)
        if r is None:
            continue
        values[q] = r.total
        resolved[q] = r
        if mixed is not None and any(q in mixed_by.get(t, ()) for t in r.used):
            mixed.add(q)
        elif mixed is not None:
            mixed.discard(q)
        if r.strategy == COMPOSITE:
            methods[q] = "composite"
        elif r.used == (single_tag,):
            methods[q] = single_methods[q]
        else:
            methods[q] = component_series[r.used[0]][1][q]

    reported = [q for q in window_ends if q in resolved]
    used = [t for t in ([single_tag] if single_tag else []) + list(component_series)
            if any(t in resolved[q].used for q in reported)]
    if not used:
        tag_used = None
    elif len(used) == 1:
        tag_used = _qualified(used[0])
    else:
        tag_used = "+".join(used)
    sources = {
        q: PeriodSource(
            strategy=resolved[q].strategy,
            components=[_qualified(t) for t in resolved[q].used],
            method=methods[q],
            partial=resolved[q].partial,
        )
        for q in reported
    }
    composite_q = [q for q in reported if resolved[q].strategy == COMPOSITE]
    partial_q = [q for q in reported if resolved[q].strategy == PARTIAL]
    notes: list[str] = []
    if name == "sga_expense" and composite_q:
        notes.append(f"SG&A composed from separate S&M and G&A tags{_where(composite_q, reported, labels)}.")
    if name == "depreciation_amortization":
        if composite_q:
            notes.append(
                "D&A composed from separate depreciation and amortization tags"
                f"{_where(composite_q, reported, labels)}."
            )
        if partial_q:
            notes.append(
                f"Partial D&A{_where(partial_q, reported, labels)}: only a depreciation tag is "
                "reported; amortization is not included (capex/D&A can overstate)."
            )
    return values, methods, tag_used, notes, sources


def _total_debt_series(
    facts_json: dict,
    quarter_ends: list[date],
    window_ends: list[date],
    labels: dict[date, str],
) -> tuple[dict[date, float], str | None, list[str], dict[date, PeriodSource]]:
    """Total debt at each quarter end, composed from the concepts reported
    AT THAT DATE by `composition.compose_total_debt` — the one rule the
    restatement detector applies too. Notes name the reported quarters where
    a role was missing (counted as zero) or a fallback was used."""
    by_tag: dict[str, dict[date, float]] = {}
    for tag in DEBT_TAGS:
        values, _ = _instant_series(_collect(facts_json, "us-gaap", tag, "USD"), quarter_ends)
        if values:
            by_tag[tag] = values

    out: dict[date, float] = {}
    composed: dict[date, DebtComposition] = {}
    for q in quarter_ends:
        present = {tag: values[q] for tag, values in by_tag.items() if q in values}
        comp = compose_total_debt(present)
        if comp is not None:
            out[q] = comp.total
            composed[q] = comp

    reported = [q for q in window_ends if q in composed]
    if not reported:
        return out, None, ["No debt concepts found; company may be debt-free or use custom tags."], {}
    used = {tag for q in reported for tag in composed[q].used}
    tag_used = "+".join(tag for tag in DEBT_TAGS if tag in used)

    def where(quarters: list[date], among: list[date]) -> str:
        return _where(quarters, among, labels)

    split = [q for q in reported if composed[q].strategy != TOTAL_FALLBACK]
    parts = [q for q in split if composed[q].strategy == SPLIT]
    aggregate = [q for q in split if composed[q].strategy == SPLIT_AGGREGATE_CURRENT]
    fallback = [q for q in reported if composed[q].strategy == TOTAL_FALLBACK]
    notes: list[str] = []
    no_current = [q for q in parts if "current" in composed[q].missing]
    if no_current:
        notes.append(
            f"Current portion of long-term debt unavailable{where(no_current, parts)}; "
            "total debt may understate."
        )
    no_short = [q for q in parts if "short" in composed[q].missing]
    if no_short:
        notes.append(f"Short-term borrowings unavailable or zero{where(no_short, parts)}; not included.")
    if aggregate:
        notes.append(
            f"Aggregate current debt (DebtCurrent) used{where(aggregate, split)}; the current "
            "portion of long-term debt and short-term borrowings are not added separately."
        )
    if any(composed[q].finance_lease_added for q in split):
        notes.append("Finance-lease liabilities added to total debt; operating leases excluded.")
    if fallback:
        notes.append(
            f"Used LongTermDebt total{where(fallback, reported)} (current/noncurrent split unavailable)."
        )
        if any(composed[q].finance_lease_added for q in fallback):
            notes.append(
                "Finance-lease liabilities added to total debt; operating leases excluded "
                "(the LongTermDebt total may, for some filers, already embed capital leases)."
            )
    sources = {
        q: PeriodSource(
            strategy=composed[q].strategy,
            components=[_qualified(t) for t in composed[q].used],
            method="composite",
            partial=bool(composed[q].missing),
        )
        for q in reported
    }
    return out, tag_used, notes, sources


def _same_day_conflict_notes(
    facts_json: dict,
    name: str,
    sources: dict[date, PeriodSource],
    labels: dict[date, str],
) -> list[str]:
    """A note per concept whose value at a reported quarter end was chosen
    between facts filed on ONE day, at one amendment level, that disagree.
    Companyfacts records filing dates, not times, so which of them is current
    is the `precedence` convention (higher accession, else the first listed)
    — said here rather than chosen silently. A cover-date match (`nearest`) is not checked: its
    fact does not end on the quarter end."""
    unit = _unit_for(name)
    facts_by: dict[str, list[RawFact]] = {}
    hits: dict[str, list[date]] = {}
    for q, src in sorted(sources.items()):
        if src.method == "nearest":
            continue
        for concept in src.components:
            if concept not in facts_by:
                taxonomy, _, tag = concept.partition(":")
                facts_by[concept] = _collect(facts_json, taxonomy, tag, unit)
            by_period: dict[date | None, list[RawFact]] = {}
            for f in facts_by[concept]:
                if f.end == q:
                    by_period.setdefault(f.start, []).append(f)
            if any(current_conflict(g, key=_rank, value=lambda f: f.val) for g in by_period.values()):
                hits.setdefault(concept, []).append(q)
    notes: list[str] = []
    for concept, quarters in hits.items():
        where = ", ".join(labels[q] for q in quarters)
        notes.append(
            f"Same-day conflicting facts for {concept} at {where}: one day's filings report "
            "different values for the period, and companyfacts does not record their order, "
            "so the value used is a convention (the higher accession number, else the first "
            "listed). Not a revision."
        )
    return notes


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
        sources: dict[date, PeriodSource] | None = None,
    ) -> None:
        field_values[name] = values
        if sources is None:
            # One concept for the whole series.
            sources = {
                q: PeriodSource(strategy="single", components=[tag], method=methods[q])
                for q in window_ends if q in values and tag is not None
            }
        notes = notes + _same_day_conflict_notes(facts_json, name, sources, labels)
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
                period_sources={q.isoformat(): src for q, src in sources.items()},
            )
        )

    for name, tags in INSTANT_FIELDS.items():
        tolerance = COVER_DATE_TOLERANCE_DAYS if name == "shares_outstanding" else 0
        values, methods, used = _best_series(
            facts_json, tags, _unit_for(name), extended_ends, "instant",
            tolerance_days=tolerance, window_ends=window_ends,
        )
        notes = []
        if name == "shares_outstanding" and "nearest" in methods.values():
            notes.append(
                "Share counts matched from cover-page dates within "
                f"{COVER_DATE_TOLERANCE_DAYS} days after quarter end."
            )
        record(name, values, methods, used, notes)

    for name, tags in FLOW_FIELDS.items():
        mixed: set[date] = set()
        values, methods, used = _best_series(
            facts_json,
            tags,
            _unit_for(name),
            extended_ends,
            "flow",
            allow_derivation=name not in NON_ADDITIVE_FLOWS,
            window_ends=window_ends,
            mixed_out=mixed,
        )
        notes: list[str] = []
        sources: dict[date, PeriodSource] | None = None
        if name in ("sga_expense", "depreciation_amortization"):
            values, methods, used, notes, sources = _resolved_flow(
                facts_json, name, extended_ends, window_ends, labels, (values, methods, used),
                mixed,
            )
        mixed_q = [q for q in window_ends if q in mixed and q in values]
        if mixed_q:
            where = ", ".join(labels[q] for q in mixed_q)
            notes.append(
                f"Derived from filings of different dates at {where}: an earlier period "
                "did not exist yet when the year-to-date or annual figure it is subtracted "
                "from was filed, so the latest values were used."
            )
        if name in NON_ADDITIVE_FLOWS and _score(values, window_ends) < len(window_ends):
            notes.append(
                "Weighted-average share counts are not additive; quarters without a "
                "directly reported value stay missing (no Q4 derivation)."
            )
        record(name, values, methods, used, notes, sources)

    debt_values, debt_tag, debt_notes, debt_sources = _total_debt_series(
        facts_json, extended_ends, window_ends, labels
    )
    record(
        "total_debt", debt_values, {q: "composite" for q in debt_values}, debt_tag, debt_notes,
        debt_sources,
    )

    coverage_by_field = {d.field_name: d.periods_filled for d in diagnostics}
    for critical in CRITICAL_FIELDS:
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
