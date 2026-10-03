"""Multiples over the bridge and the engine's own TTM figures — shown where
they mean something, with the reason on the line where they do not.

docs/thesis_monitor_architecture.md is right that earnings multiples are
undefined on exactly the distressed names the engine is validated on. This
module does not contradict it: a denominator that is missing, zero or
negative, a TTM window short of four quarters, or an EV the bridge did not
assert each leaves the multiple empty with that reason, and nothing here
reaches a score. The trailing figures come from `formulas/ttm.annualize`,
so the plane's TTM revenue, net income, EBIT, EBITDA and FCF are the ones
the engine's ratios read.

Own-history and peer ranges are not available in v1 (one observation, no
price history, no reference class) and are said to be unavailable rather
than approximated.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas.financials import CompanyDataset
from app.services.formulas.ttm import annualize
from app.services.valuation.bridge import Bridge

HISTORY_LINE = ("own-history range: not available (one price observation; no price history "
                "recorded)")
PEER_LINE = "peer range: no reference class (none defined)"


@dataclass(frozen=True)
class TrailingFigures:
    """The TTM window ending at the bridge's period: its label (None with
    `reason` when it could not be built) and the five figures the multiples
    and the expectations read."""

    label: str | None
    reason: str | None
    revenue: float | None = None
    net_income: float | None = None
    ebit: float | None = None
    ebitda: float | None = None
    fcf: float | None = None


@dataclass(frozen=True)
class Multiple:
    name: str
    value: float | None
    reason: str | None
    numerator_name: str
    numerator: float | None
    denominator_name: str
    denominator: float | None
    ttm_window: str | None


def trailing(dataset: CompanyDataset, bridge: Bridge) -> TrailingFigures:
    """`annualize` over the four quarters ending at the bridge's period."""
    if bridge.fiscal_label is None:
        return TrailingFigures(None, "TTM window not built: no period available at the observation")
    periods = dataset.sorted_periods()
    idx = next(i for i, p in enumerate(periods) if p.fiscal_label == bridge.fiscal_label)
    ttm = annualize(periods, idx)
    if ttm is None:
        return TrailingFigures(
            None, f"TTM window incomplete: fewer than 4 consecutive quarters ending "
                  f"{bridge.fiscal_label}")
    return TrailingFigures(ttm.fiscal_label, None, ttm.revenue, ttm.net_income, ttm.ebit,
                           ttm.ebitda, ttm.fcf)


def _not_positive(name: str, value: float, multiple: str) -> str:
    how = "negative" if value < 0 else "zero"
    return f"TTM {name} is {how} ({value:,.0f}): {multiple} undefined"


def compute_multiples(bridge: Bridge, ttm: TrailingFigures) -> tuple[Multiple, ...]:
    """P/E, EV/EBIT, EV/EBITDA, EV/Sales, P/S, P/FCF, FCF yield and earnings
    yield. A ratio's denominator must be a positive TTM figure; a yield's
    denominator is the market cap and its numerator may be negative (a
    negative yield is a number, a negative P/E is not)."""
    mcap = bridge.market_cap.value
    ev = bridge.ev
    specs: list[tuple[str, str, float | None, str, float | None, bool]] = [
        # name, numerator name, numerator, denominator name, denominator, is_yield
        ("P/E", "market cap", mcap, "net income", ttm.net_income, False),
        ("EV/EBIT", "EV", ev, "EBIT", ttm.ebit, False),
        ("EV/EBITDA", "EV", ev, "EBITDA", ttm.ebitda, False),
        ("EV/Sales", "EV", ev, "revenue", ttm.revenue, False),
        ("P/S", "market cap", mcap, "revenue", ttm.revenue, False),
        ("P/FCF", "market cap", mcap, "FCF", ttm.fcf, False),
        ("FCF yield", "FCF", ttm.fcf, "market cap", mcap, True),
        ("earnings yield", "net income", ttm.net_income, "market cap", mcap, True),
    ]
    out: list[Multiple] = []
    for name, num_name, num, den_name, den, is_yield in specs:
        reason: str | None = None
        if mcap is None or ((num_name == "EV" or den_name == "EV") and ev is None):
            reason = bridge.ev_reason
        elif ttm.reason is not None:
            reason = ttm.reason
        elif is_yield:
            if num is None:
                reason = f"TTM {num_name} missing"
        elif den is None:
            reason = f"TTM {den_name} missing"
        elif den <= 0:
            reason = _not_positive(den_name, den, name)
        value = None
        if reason is None and num is not None and den is not None:
            value = num / den
        out.append(Multiple(name, value, reason, num_name, num, den_name, den,
                            ttm.label if reason is None else None))
    return tuple(out)
