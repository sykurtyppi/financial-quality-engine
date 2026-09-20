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

from app.schemas.financials import CompanyDataset, PeriodFinancials, PeriodType
from app.services.formulas.ttm import MAX_GAP_DAYS, MIN_GAP_DAYS
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
# corporate action `restatements.SPLIT_ADJUSTED_FIELDS` exists to exclude). A
# year-over-year move beyond this is a split, not dilution, and no honest
# dilution claim can be read off it — the rule declines instead.
SPLIT_SUSPECT_YOY = 0.50


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


def _bound_at(
    window: Sequence[PeriodFinancials], values: Sequence[float], bound: float
) -> str:
    """Names the quarter that set a floor or ceiling, and says when that is the
    latest one — a bound the newest quarter just set has no headroom in it, and
    a reader who cannot see that reads stability into a jump."""
    i = list(values).index(bound)
    label = window[i].fiscal_label
    return f"{label}, the most recent" if i == len(window) - 1 else label


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
# (label, "grows", "does not grow", "it has", accessor) — the measure names
# differ in number, and a brief that says "Shares outstanding does not grow"
# reads as sloppily machine-made, which is exactly what it must not read as.
SHARE_MEASURES: tuple[
    tuple[str, str, str, str, Callable[[PeriodFinancials], float | None]], ...
] = (
    ("Diluted share count", "grows", "does not grow", "it has", lambda p: p.shares_diluted),
    ("Shares outstanding", "grow", "do not grow", "they have", lambda p: p.shares_outstanding),
)


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
        return Derived(
            "margin_floor",
            f"{label} stays at or above {_pct(floor)} — its low over the last four "
            f"quarters ({_bound_at(window, values, floor)}).",  # type: ignore[arg-type]
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
        return Derived(
            "cash_generation",
            f"Quarterly free cash flow burn stays under {_money(abs(worst))} — its worst "
            f"over the last four quarters ({_bound_at(window, values, worst)}).",  # type: ignore[arg-type]
            detail,
        )
    return None  # crossed zero: no stable claim either way


def _dilution(quarters: Sequence[PeriodFinancials]) -> Derived | None:
    window = _tail(quarters, YOY_QUARTERS)
    if window is None:
        return None
    for label, grows, not_grow, has, measure in SHARE_MEASURES:
        pairs = _yoy(window, measure)
        if pairs is None:
            continue
        rates = [g for _, g in pairs]
        if max(abs(g) for g in rates) > SPLIT_SUSPECT_YOY:
            return None  # a split restated the series; nothing here is dilution
        quarters_used = [p for p, _ in pairs]
        detail = _trail(quarters_used, [_signed_pct(g) for _, g in pairs]) + " YoY"
        fastest = max(rates)
        if fastest <= 0:
            return Derived(
                "dilution",
                f"{label} {not_grow} year over year — {has} not in any of the last "
                "four quarters.",
                detail,
            )
        return Derived(
            "dilution",
            f"{label} {grows} no more than {_pct(fastest)} year over year — its fastest "
            f"over the last four quarters ({_bound_at(quarters_used, rates, fastest)}).",
            detail,
        )
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
        return Derived(
            "balance_sheet",
            f"Total debt stays at or below {_money(ceiling)} — its high over the last "
            f"four quarter ends ({_bound_at(window, debt, ceiling)}).",  # type: ignore[arg-type]
            _trail(window, [_money(d) for d in debt]),  # type: ignore[arg-type]
        )
    floor = min(cash)  # type: ignore[type-var]
    return Derived(
        "balance_sheet",
        f"Cash and equivalents stay at or above {_money(floor)} — their low over the "
        f"last four quarter ends ({_bound_at(window, cash, floor)}).",  # type: ignore[arg-type]
        cash_detail,
    )


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
    return out[:MAX_DERIVED]


def derive_for_ticker(ticker: str, *, client: SecClient | None = None) -> list[Derived]:
    """Derive from `ticker`'s filed history, reusing the caller's SEC client.

    The client carries the run's cache policy: on a print night the brief runs
    `fresh`, and derivation must see the same EDGAR snapshot the rest of the
    brief does rather than quietly reading a day-old companyfacts document.
    """
    snapshot = fetch_dataset_snapshot(ticker, n_quarters=DERIVE_QUARTERS, client=client)
    return derive_assumptions(snapshot.dataset)
