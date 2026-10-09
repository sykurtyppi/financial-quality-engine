"""Implied expectations: what the observed price assumes about free cash
flow, under assumptions that are all on the page.

Everything here is a model assumption or arithmetic over one: the required
return, the terminal growth and the horizon come from the observation file
or, when it carries none, from `Assumptions()`'s defaults — then labelled
"default assumptions (not operator-supplied)" wherever they are used.

The FCF is the engine's (CFO − capex): after interest, a flow to EQUITY. So
every solve here equates its present value to the MARKET CAP, never to EV
(review of 48b1f04, F7: a levered flow against an unlevered value counts
the debt twice). EV serves the EV multiples only; unlevered FCF and NOPAT
are phase 2 (docs/valuation_spec.md). Two readings of the same market cap:

- Gordon: g = r − FCF_ttm / market cap, the perpetual growth a
  constant-growth perpetuity at r would need to be worth it;
- reverse two-stage DCF: the constant FCF growth over `horizon_years`,
  with `terminal_growth` thereafter, that makes the present value of FCF at
  r equal the market cap. PV is increasing in that growth, so bisection
  over a wide bracket finds it or says there is none in it —
  deterministic, no numpy.

Sensitivities (r ± 1pt, price ± 10%) say which assumption moves the implied
growth more: "the main assumption that would change the conclusion". One
that cannot be valued is listed as withheld with the reason, never dropped
(F9). A scenario is the operator's own FCF path, valued per share against
the price. None of it is a forecast, and none of it reaches a score. A
present value that overflows a double is said to be not computable (F3).
A price in another currency than the filing figures' values nothing: the
implied growth and every scenario say so (`bridge.currency_mismatch`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.services.valuation.bridge import Bridge, currency_mismatch
from app.services.valuation.multiples import TrailingFigures
from app.services.valuation.observation import (
    GROWTH_HIGH,
    GROWTH_LOW,
    Assumptions,
    MarketObservation,
    Scenario,
)

_ITERATIONS = 200
_TOLERANCE = 1e-12
NO_FCF = "implied growth not computable: TTM FCF ≤ 0"
NO_MCAP = "implied growth not computable: market cap ≤ 0"
OVERFLOW = "implied growth not computable: overflow (present value not finite)"
GORDON_FORMULA = "g = r − FCF_ttm / market cap"
RATE_SENSITIVITY = "required return ± 1pt"
PRICE_SENSITIVITY = "price ± 10%"


@dataclass(frozen=True)
class ImpliedGrowth:
    value: float | None
    reason: str | None
    formula: str


@dataclass(frozen=True)
class Sensitivity:
    """Implied growth at the low and high end of one assumption's move, and
    the larger distance from the base case; or, with the three empty,
    `reason` says why the move was withheld."""

    name: str
    low: float | None
    high: float | None
    swing: float | None
    reason: str | None = None


@dataclass(frozen=True)
class ScenarioValue:
    name: str
    terms: str  # the path: "FCF +5.0%/yr for 5 years, terminal 2.5%, r=9.0%"
    value_per_share: float | None
    upside: float | None  # value per share over the price, minus one
    reason: str | None

    @property
    def label(self) -> str:
        return f"model assumption: {self.name} — {self.terms}"


@dataclass(frozen=True)
class Expectations:
    assumptions: Assumptions
    defaulted: bool
    gordon: ImpliedGrowth
    reverse: ImpliedGrowth
    sensitivities: tuple[Sensitivity, ...]
    main_assumption: str | None
    scenarios: tuple[ScenarioValue, ...]


def present_value(fcf: float, growth: float, years: int, terminal_growth: float,
                  required_return: float) -> float:
    """PV at `required_return` of `fcf` growing at `growth` for `years`
    years, then at `terminal_growth` in perpetuity (Gordon on the last
    year's flow). Requires terminal_growth < required_return. Pure
    arithmetic: inf when a double overflows, which the callers refuse."""
    pv = 0.0
    flow = fcf
    discount = 1.0
    for _ in range(years):
        flow *= 1.0 + growth
        discount *= 1.0 + required_return
        pv += flow / discount
    terminal = flow * (1.0 + terminal_growth) / (required_return - terminal_growth)
    return pv + terminal / discount


def gordon_growth(fcf: float | None, mcap: float | None, required_return: float) -> ImpliedGrowth:
    if fcf is None or fcf <= 0:
        return ImpliedGrowth(None, NO_FCF, GORDON_FORMULA)
    if mcap is None or mcap <= 0:
        return ImpliedGrowth(None, NO_MCAP, GORDON_FORMULA)
    return ImpliedGrowth(required_return - fcf / mcap, None, GORDON_FORMULA)


def _two_stage_formula(a: Assumptions) -> str:
    return (f"PV(FCF_ttm growing g/yr for {a.horizon_years} years, then "
            f"{a.terminal_growth:.1%} in perpetuity, at r={a.required_return:.1%}) = market cap; "
            f"g by bisection over [{GROWTH_LOW:+.0%}, {GROWTH_HIGH:+.0%}]")


def implied_growth(fcf: float | None, mcap: float | None, a: Assumptions) -> ImpliedGrowth:
    """The reverse two-stage DCF's growth, by bisection; the reason when
    there is none (no positive FCF or market cap, a price outside the
    bracket, or a present value past a double)."""
    formula = _two_stage_formula(a)
    if fcf is None or fcf <= 0:
        return ImpliedGrowth(None, NO_FCF, formula)
    if mcap is None or mcap <= 0:
        return ImpliedGrowth(None, NO_MCAP, formula)

    def pv(g: float) -> float:
        return present_value(fcf, g, a.horizon_years, a.terminal_growth, a.required_return)

    lo, hi = GROWTH_LOW, GROWTH_HIGH
    at_lo, at_hi = pv(lo), pv(hi)
    if not (math.isfinite(at_lo) and math.isfinite(at_hi)):
        return ImpliedGrowth(None, OVERFLOW, formula)
    if at_lo > mcap:
        return ImpliedGrowth(None, f"implied growth not computable: below {GROWTH_LOW:+.0%}/yr "
                                   "(market cap is under the PV of a collapsing FCF)", formula)
    if at_hi < mcap:
        return ImpliedGrowth(None, f"implied growth not computable: above {GROWTH_HIGH:+.0%}/yr "
                                   "(market cap exceeds the PV of FCF doubling every year)", formula)
    for _ in range(_ITERATIONS):
        mid = (lo + hi) / 2.0
        if pv(mid) < mcap:
            lo = mid
        else:
            hi = mid
        if hi - lo < _TOLERANCE:
            break
    return ImpliedGrowth((lo + hi) / 2.0, None, formula)


def _valued(name: str, base: float, lower: ImpliedGrowth, upper: ImpliedGrowth) -> Sensitivity:
    """The sensitivity over two solves, or withheld with the reason the
    first failing one gave."""
    if lower.value is None or upper.value is None:
        why = lower.reason if lower.value is None else upper.reason
        return Sensitivity(name, None, None, None, f"withheld: {why}")
    return Sensitivity(name, lower.value, upper.value,
                       max(abs(lower.value - base), abs(upper.value - base)))


def _sensitivities(fcf: float, mcap: float, base: float,
                   a: Assumptions) -> tuple[Sensitivity, ...]:
    r_lo, r_hi = a.required_return - 0.01, a.required_return + 0.01
    # r − 1pt at or below the terminal growth (or at or below zero) is no
    # assumption set; so is r + 1pt at or past 100%. Withheld, and said.
    if r_lo <= a.terminal_growth:
        rate = Sensitivity(RATE_SENSITIVITY, None, None, None,
                           f"withheld: r − 1pt ({r_lo:.1%}) is not above the terminal growth "
                           f"({a.terminal_growth:.1%})")
    elif r_lo <= 0.0:
        rate = Sensitivity(RATE_SENSITIVITY, None, None, None,
                           f"withheld: r − 1pt ({r_lo:.1%}) is not a valid required return")
    elif r_hi >= 1.0:
        rate = Sensitivity(RATE_SENSITIVITY, None, None, None,
                           f"withheld: r + 1pt ({r_hi:.1%}) is not a valid required return")
    else:
        rate = _valued(
            RATE_SENSITIVITY, base,
            implied_growth(fcf, mcap, Assumptions(required_return=r_lo,
                                                  terminal_growth=a.terminal_growth,
                                                  horizon_years=a.horizon_years)),
            implied_growth(fcf, mcap, Assumptions(required_return=r_hi,
                                                  terminal_growth=a.terminal_growth,
                                                  horizon_years=a.horizon_years)),
        )
    price = _valued(PRICE_SENSITIVITY, base, implied_growth(fcf, 0.9 * mcap, a),
                    implied_growth(fcf, 1.1 * mcap, a))
    return (rate, price)


def _main_assumption(sensitivities: tuple[Sensitivity, ...]) -> str | None:
    """The valued sensitivity with the larger swing, named; the withheld
    ones named too, so a one-sided answer never reads as the whole."""
    if not sensitivities:
        return None
    valued = [s for s in sensitivities if s.swing is not None]
    withheld = [s.name for s in sensitivities if s.swing is None]
    if not valued:
        return "none: every sensitivity withheld"
    ranked = sorted(valued, key=lambda s: s.swing or 0.0, reverse=True)
    first = ranked[0]
    others = "; ".join(f"{s.swing:.1%} for {s.name}" for s in ranked[1:])
    text = (f"{first.name}: moves the implied growth by up to {first.swing:.1%}"
            + (f" (vs {others})" if others else ""))
    if withheld:
        text += f" ({', '.join(withheld)} withheld)"
    return text


def _scenario(s: Scenario, a: Assumptions, fcf: float | None, bridge: Bridge,
              mismatch: str | None = None) -> ScenarioValue:
    r = s.required_return if s.required_return is not None else a.required_return
    tg = s.terminal_growth if s.terminal_growth is not None else a.terminal_growth
    terms = f"FCF {s.fcf_growth:+.1%}/yr for {s.years} years, terminal {tg:.1%}, r={r:.1%}"
    reason: str | None = None
    if mismatch is not None:
        reason = f"not computable: {mismatch}"
    elif tg >= r:
        reason = f"not computable: terminal growth {tg:.1%} is not below r={r:.1%}"
    elif fcf is None:
        reason = "not computable: TTM FCF missing"
    elif fcf <= 0:
        reason = "not computable: TTM FCF ≤ 0"
    elif bridge.shares.value is None:
        reason = "not computable: share count missing"
    if reason is not None:
        return ScenarioValue(s.name, terms, None, None, reason)
    assert fcf is not None and bridge.shares.value is not None and bridge.price.value is not None
    # Equity value is the PV of the flow to equity: per share directly, no
    # bridge claims taken off (F7).
    pv = present_value(fcf, s.fcf_growth, s.years, tg, r)
    if not math.isfinite(pv):
        return ScenarioValue(s.name, terms, None, None,
                             "not computable: overflow (present value not finite)")
    per_share = pv / bridge.shares.value
    return ScenarioValue(s.name, terms, per_share, per_share / bridge.price.value - 1.0, None)


def compute_expectations(bridge: Bridge, ttm: TrailingFigures, obs: MarketObservation) -> Expectations:
    a = obs.assumptions if obs.assumptions is not None else Assumptions()
    fcf = ttm.fcf
    mcap = bridge.market_cap.value
    gordon = gordon_growth(fcf, mcap, a.required_return)
    reverse = implied_growth(fcf, mcap, a)
    mismatch = currency_mismatch(obs)
    why = mismatch if mismatch is not None else ttm.reason
    if why is not None:
        gordon = ImpliedGrowth(None, f"implied growth not computable: {why}", gordon.formula)
        reverse = ImpliedGrowth(None, f"implied growth not computable: {why}", reverse.formula)
    sensitivities: tuple[Sensitivity, ...] = ()
    if reverse.value is not None and fcf is not None and mcap is not None:
        sensitivities = _sensitivities(fcf, mcap, reverse.value, a)
    scenarios = tuple(_scenario(s, a, fcf, bridge, mismatch) for s in obs.scenarios)
    return Expectations(a, obs.assumptions is None, gordon, reverse, sensitivities,
                        _main_assumption(sensitivities), scenarios)
