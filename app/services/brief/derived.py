"""Standing assumptions derived from a holding's own filed history.

`assumptions.py` asks the holder to write down, once, the two or three things
they are assuming about a name. That input is the one thing the automation
cannot supply, and an empty `journal/assumptions/` turns a whole brief section
into a single UNAVAILABLE line for every holding, every quarter.

This module fills that gap without inventing a thesis. It reads the filed
quarterly history the engine already ingests and restates it as continuity
claims — the things that have been true of this business for the last year,
phrased so that the next print either holds them, challenges them, or says
nothing about them. "Revenue keeps growing year over year." "Gross margin
stays at or above 71.4%." Not predictions, and emphatically not the holder's
thesis: a derived assumption is the filings' own recent past, held up against
the quarter that just landed.

Deliberately conservative. A rule fires only when the trailing window actually
supports a stable claim; an erratic series yields nothing, because a floor
drawn under noise is challenged every quarter and teaches the reader to ignore
the section. Five rules, one per dimension the brief already assesses —
growth, profitability, cash generation, dilution, balance sheet — so the
derived set never stacks five variations of the same observation.

Deterministic: no model, no judgement, no network beyond the companyfacts
document the brief run already needs. Holder-authored assumptions always win;
these apply only when there are none.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from math import isclose

from app.schemas.financials import CompanyDataset, PeriodFinancials, PeriodType
from app.services.backtesting.pit import filter_as_of
from app.services.brief.assumptions import MAX_ASSUMPTION_CHARS
from app.services.formulas.ttm import MAX_GAP_DAYS, MIN_GAP_DAYS
from app.services.ingestion.companyfacts_mapper import build_dataset
from app.services.ingestion.edgar_adapter import fetch_dataset_snapshot
from app.services.ingestion.sec_client import SecClient

LEVEL_QUARTERS = 4  # trailing window for a level claim (a floor or a ceiling)
YOY_LAG = 4  # quarters back for a year-over-year comparison
YOY_QUARTERS = LEVEL_QUARTERS + YOY_LAG  # four YoY pairs need eight contiguous quarters
DERIVE_QUARTERS = 12  # fetched depth: YOY_QUARTERS plus slack for a gap or a stub period
MAX_DERIVED = 5

# A margin that swings more than this across four quarters has no meaningful
# floor: the trailing low is a sampling artifact, not a property of the
# business, and a claim built on it is challenged by ordinary seasonality.
MARGIN_SPREAD_MAX = 0.10

# Share counts are restated wholesale by splits and reverse splits (the same
# corporate action `restatements.SPLIT_ADJUSTED_FIELDS` exists to exclude), and
# a split read as dilution puts a fiction in front of the holder: a 1-for-2
# reverse split reads as "the share count did not grow" about a company that
# just halved it.
#
# Magnitude alone cannot separate the two — a real equity raise can run +22%
# year over year, squarely among the common split ratios. Shape can: issuance
# accumulates over quarters, while a split is one clean step. So the test is
# the largest QUARTER-over-quarter move in the window.
#
# The threshold does not try to tell a split from a large one-off raise, and it
# does not need to: neither is a RATE, and "grows no more than X% a year" is the
# wrong sentence for both. Boeing's 2024 raise (+21.3% in a quarter) is declined
# on exactly the same ground as a 5-for-4 split (+25%) — one step is not a
# trend, and the trailing figure it would produce describes nothing the next
# quarter can meaningfully hold or break.
#
# What that costs: a name whose share count moved in one step gets no dilution
# claim at all that year. Losing a row is the cheap error; printing a fabricated
# one in front of the holder is not. Honest limit in the other direction: an
# 11-for-10 split moves 10% and sits below any threshold that still admits
# ordinary issuance, so it would be reported as dilution.
SPLIT_SUSPECT_QOQ = 0.20


@dataclass(frozen=True)
class Derived:
    """One derived assumption: the claim, and the numbers behind it."""

    key: str
    text: str  # self-contained; what the brief reproduces verbatim in its table
    detail: str  # the trailing quarters that produced it, for the reader


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _signed_pct(x: float) -> str:
    return f"{'+' if x >= 0 else ''}{x * 100:.1f}%"


def _money(v: float) -> str:
    sign = "-" if v < 0 else ""
    a = abs(v)
    for cut, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= cut:
            return f"{sign}${a / cut:.2f}{suffix}"
    return f"{sign}${a:,.0f}"


def _gross_margin(p: PeriodFinancials) -> float | None:
    if p.gross_profit is None or p.revenue is None or p.revenue <= 0:
        return None
    return p.gross_profit / p.revenue


def _operating_margin(p: PeriodFinancials) -> float | None:
    if p.operating_income is None or p.revenue is None or p.revenue <= 0:
        return None
    return p.operating_income / p.revenue


def _quarters(dataset: CompanyDataset) -> list[PeriodFinancials]:
    return [p for p in dataset.sorted_periods() if p.period_type is PeriodType.QUARTER]


def _tail(quarters: Sequence[PeriodFinancials], n: int) -> list[PeriodFinancials] | None:
    """The last `n` quarters, when they are a contiguous run.

    "Four quarters ago" only means a year back when nothing in between is
    missing; a hole in the history silently shifts every horizon. Bounds match
    app/services/formulas/ttm.py.
    """
    if len(quarters) < n:
        return None
    window = list(quarters[-n:])
    for a, b in zip(window, window[1:]):
        if not (MIN_GAP_DAYS <= (b.period_end - a.period_end).days <= MAX_GAP_DAYS):
            return None
    return window


def _yoy(
    window: Sequence[PeriodFinancials], value: Callable[[PeriodFinancials], float | None]
) -> list[tuple[PeriodFinancials, float]] | None:
    """Year-over-year change for the most recent LEVEL_QUARTERS quarters of an
    eight-quarter window, or None when any input is missing or non-positive."""
    out: list[tuple[PeriodFinancials, float]] = []
    for i in range(YOY_LAG, len(window)):
        now, then = value(window[i]), value(window[i - YOY_LAG])
        if now is None or then is None or then <= 0:
            return None
        out.append((window[i], now / then - 1.0))
    return out


def _bound_quarter(
    window: Sequence[PeriodFinancials], values: Sequence[float], bound: float
) -> tuple[str, bool]:
    """(quarter that set this floor or ceiling, whether it is the latest one).

    The distinction decides how the claim may be phrased. A bound an older
    quarter set has been survived since; one the newest quarter just set has
    not been tested at all, and "stays at or above X" would promise a durability
    that nothing in the history supports.
    """
    values = list(values)
    matches = [i for i, value in enumerate(values) if value == bound]
    latest = len(values) - 1
    fresh = matches == [latest]
    # A latest-quarter tie is not a newly set bound: cite the most recent
    # earlier occurrence that demonstrates the level has already been seen.
    i = matches[-2] if matches[-1] == latest and len(matches) > 1 else matches[-1]
    return window[i].fiscal_label, fresh


def _trail(window: Sequence[PeriodFinancials], rendered: Sequence[str]) -> str:
    return ", ".join(
        f"{p.fiscal_label} {r}" for p, r in zip(reversed(window), reversed(rendered))
    )


# Diluted share count is a weighted average the mapper cannot derive for a
# fiscal Q4 backed out of the 10-K, so an eight-quarter window always holds at
# least one gap and a diluted-only rule never fires for anyone. Period-end
# shares outstanding covers every quarter, and for a dilution claim it is the
# more direct measure anyway. One measure for the whole window, never a mix:
# a weighted average compared against a point-in-time count is a fabricated
# year-over-year move.
@dataclass(frozen=True)
class _ShareMeasure:
    """One share-count series, with the grammar its name takes.

    "Shares outstanding does not grow" reads as sloppily machine-made, which is
    the one thing a brief in front of a holder must never read as.
    """

    label: str
    plural: bool
    get: Callable[[PeriodFinancials], float | None]

    @property
    def grows(self) -> str:
        return "grow" if self.plural else "grows"

    @property
    def not_grow(self) -> str:
        return "do not grow" if self.plural else "does not grow"

    @property
    def has(self) -> str:
        return "they have" if self.plural else "it has"

    @property
    def their(self) -> str:
        return "their" if self.plural else "its"


SHARE_MEASURES: tuple[_ShareMeasure, ...] = (
    _ShareMeasure("Diluted share count", False, lambda p: p.shares_diluted),
    _ShareMeasure("Shares outstanding", True, lambda p: p.shares_outstanding),
)


def _has_split_step(counts: Sequence[float | None]) -> bool:
    """True when one quarter-over-quarter step is too large to be issuance.

    Checked across the WHOLE window, not just the year-over-year pairs: a split
    anywhere in it corrupts every comparison that spans it.
    """
    for a, b in zip(counts, counts[1:]):
        if a is None or b is None or a <= 0:
            continue
        move = abs(b / a - 1.0)
        if move > SPLIT_SUSPECT_QOQ or isclose(move, SPLIT_SUSPECT_QOQ):
            return True
    return False


def _revenue_growth(quarters: Sequence[PeriodFinancials]) -> Derived | None:
    window = _tail(quarters, YOY_QUARTERS)
    if window is None:
        return None
    pairs = _yoy(window, lambda p: p.revenue)
    if pairs is None:
        return None
    rates = [g for _, g in pairs]
    if min(rates) <= 0:
        return None
    return Derived(
        "revenue_growth",
        f"Revenue keeps growing year over year — it has in each of the last four "
        f"quarters, by {_pct(min(rates))} to {_pct(max(rates))}.",
        _trail([p for p, _ in pairs], [_signed_pct(g) for _, g in pairs]) + " YoY",
    )


def _margin_floor(quarters: Sequence[PeriodFinancials]) -> Derived | None:
    window = _tail(quarters, LEVEL_QUARTERS)
    if window is None:
        return None
    # Gross margin is the cleaner claim; many filers never tag cost of revenue,
    # and for them operating margin carries the same idea.
    for label, fn in (("Gross margin", _gross_margin), ("Operating margin", _operating_margin)):
        values = [fn(p) for p in window]
        if any(v is None for v in values):
            continue
        floor, ceiling = min(values), max(values)  # type: ignore[type-var]
        if floor <= 0 or ceiling - floor > MARGIN_SPREAD_MAX:
            continue
        at, fresh = _bound_quarter(window, values, floor)  # type: ignore[arg-type]
        text = (
            f"{label} does not fall further — it just set a four-quarter low of {_pct(floor)}."
            if fresh else
            f"{label} stays at or above {_pct(floor)} — its low over the last four "
            f"quarters ({at})."
        )
        return Derived(
            "margin_floor", text,
            _trail(window, [_pct(v) for v in values]),  # type: ignore[arg-type]
        )
    return None


def _cash_generation(quarters: Sequence[PeriodFinancials]) -> Derived | None:
    window = _tail(quarters, LEVEL_QUARTERS)
    if window is None:
        return None
    values = [p.fcf for p in window]
    if any(v is None for v in values):
        return None
    detail = _trail(window, [_money(v) for v in values])  # type: ignore[arg-type]
    worst, best = min(values), max(values)  # type: ignore[type-var]
    if worst > 0:
        return Derived(
            "cash_generation",
            "Free cash flow stays positive — it has been in each of the last four quarters.",
            detail,
        )
    if best < 0:
        # A cash-burning holding gets the claim that actually matters to it:
        # the burn does not deepen past what the last year already showed.
        at, fresh = _bound_quarter(window, values, worst)  # type: ignore[arg-type]
        text = (
            "Quarterly free cash flow burn does not deepen — it just set a four-quarter "
            f"worst of {_money(abs(worst))}."
            if fresh else
            f"Quarterly free cash flow burn stays under {_money(abs(worst))} — its worst "
            f"over the last four quarters ({at})."
        )
        return Derived("cash_generation", text, detail)
    return None  # crossed zero: no stable claim either way


def _dilution(quarters: Sequence[PeriodFinancials]) -> Derived | None:
    window = _tail(quarters, YOY_QUARTERS)
    if window is None:
        return None
    for m in SHARE_MEASURES:
        pairs = _yoy(window, m.get)
        if pairs is None:
            continue
        counts = [m.get(p) for p in window]
        if _has_split_step(counts):
            # Abandons the rule rather than trying the other measure: a split
            # restates BOTH series, so a step in the first complete one means
            # either a real split or a series not to be trusted. Neither is
            # something to publish a dilution number from.
            return None
        rates = [g for _, g in pairs]
        quarters_used = [p for p, _ in pairs]
        detail = _trail(quarters_used, [_signed_pct(g) for _, g in pairs]) + " YoY"
        fastest = max(rates)
        if fastest <= 0:
            return Derived(
                "dilution",
                f"{m.label} {m.not_grow} year over year — {m.has} not in any of the last "
                "four quarters.",
                detail,
            )
        at, fresh = _bound_quarter(quarters_used, rates, fastest)
        text = (
            f"{m.label} {m.not_grow} faster than the {_pct(fastest)} just posted — "
            f"{m.their} fastest year-over-year rise in four quarters."
            if fresh else
            f"{m.label} {m.grows} no more than {_pct(fastest)} year over year — "
            f"{m.their} fastest over the last four quarters ({at})."
        )
        return Derived("dilution", text, detail)
    return None


def _balance_sheet(quarters: Sequence[PeriodFinancials]) -> Derived | None:
    window = _tail(quarters, LEVEL_QUARTERS)
    if window is None:
        return None
    cash = [p.cash_and_equivalents for p in window]
    debt = [p.total_debt for p in window]
    if any(c is None for c in cash):
        return None
    cash_detail = _trail(window, [_money(c) for c in cash])  # type: ignore[arg-type]
    if not any(d is None for d in debt):
        if all(c > d for c, d in zip(cash, debt)):  # type: ignore[operator]
            return Derived(
                "balance_sheet",
                "Cash and equivalents stay above total debt — they have at each of the "
                "last four quarter ends.",
                _trail(
                    window,
                    [f"cash {_money(c)} vs debt {_money(d)}" for c, d in zip(cash, debt)],  # type: ignore[arg-type]
                ),
            )
        ceiling = max(debt)  # type: ignore[type-var]
        at, fresh = _bound_quarter(window, debt, ceiling)  # type: ignore[arg-type]
        text = (
            "Total debt does not rise further — it just set a four-quarter high of "
            f"{_money(ceiling)}."
            if fresh else
            f"Total debt stays at or below {_money(ceiling)} — its high over the last "
            f"four quarter ends ({at})."
        )
        return Derived(
            "balance_sheet", text,
            _trail(window, [_money(d) for d in debt]),  # type: ignore[arg-type]
        )
    floor = min(cash)  # type: ignore[type-var]
    at, fresh = _bound_quarter(window, cash, floor)  # type: ignore[arg-type]
    text = (
        "Cash and equivalents do not fall further — they just set a four-quarter low "
        f"of {_money(floor)}."
        if fresh else
        f"Cash and equivalents stay at or above {_money(floor)} — their low over the "
        f"last four quarter ends ({at})."
    )
    return Derived("balance_sheet", text, cash_detail)


# One rule per dimension, in the order they appear in a brief's assessment.
RULES: tuple[Callable[[Sequence[PeriodFinancials]], Derived | None], ...] = (
    _revenue_growth,
    _margin_floor,
    _cash_generation,
    _dilution,
    _balance_sheet,
)


def derive_assumptions(dataset: CompanyDataset) -> list[Derived]:
    """Continuity claims the filed history supports, at most MAX_DERIVED.

    An empty list is a legitimate result: a short or broken history supports
    nothing, and saying nothing beats asserting a claim the numbers do not
    carry.
    """
    quarters = _quarters(dataset)
    out = [d for d in (rule(quarters) for rule in RULES) if d is not None]
    # `parse_assumptions` truncates at MAX_ASSUMPTION_CHARS, and the brief
    # contract pins every table row against that re-parsed text. A claim long
    # enough to be truncated would fail validation on every retry and cost the
    # holding its brief for the quarter, so drop it here instead: one lost row
    # beats a season of failed briefs for that name.
    out = [d for d in out if len(d.text) <= MAX_ASSUMPTION_CHARS]
    return out[:MAX_DERIVED]


def derive_for_ticker(
    ticker: str, *, as_of: date, client: SecClient | None = None
) -> list[Derived]:
    """Derive from history filed on or before ``as_of``.

    The client carries the run's cache policy: on a print night the brief runs
    `fresh`, and derivation must see the same EDGAR snapshot the rest of the
    brief does rather than quietly reading a day-old companyfacts document.
    The cutoff is mandatory because deriving from the print being assessed
    would turn the current result into its own standing assumption.
    """
    snapshot = fetch_dataset_snapshot(ticker, n_quarters=DERIVE_QUARTERS, client=client)
    facts = filter_as_of(snapshot.company_facts, as_of)
    dataset, _ = build_dataset(facts, ticker=ticker, n_quarters=DERIVE_QUARTERS)
    return derive_assumptions(dataset)
