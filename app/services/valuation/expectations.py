"""Implied expectations: what the observed price assumes about free cash
flow, under assumptions that are all on the page.

Everything here is a model assumption or arithmetic over one: the required
return, the terminal growth and the horizon come from the observation file
or, when it carries none, from `Assumptions()`'s defaults — then labelled
"default assumptions (not operator-supplied)" wherever they are used.

Two readings of the same EV:

- Gordon: g = r − FCF_ttm / EV, the perpetual growth a constant-growth
  perpetuity at r would need to be worth EV;
- reverse two-stage DCF: the constant FCF growth over `horizon_years`,
  with `terminal_growth` thereafter, that makes the present value of FCF at
  r equal EV. PV is increasing in that growth, so bisection over a wide
  bracket finds it or says there is none in it — deterministic, no numpy.

Sensitivities (r ± 1pt, price ± 10%) say which assumption moves the implied
growth more: "the main assumption that would change the conclusion". A
scenario is the operator's own FCF path, valued per share against the
price. None of it is a forecast, and none of it reaches a score.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

from app.services.valuation.bridge import Bridge
from app.services.valuation.multiples import TrailingFigures
from app.services.valuation.observation import Assumptions, MarketObservation, Scenario

# The growth bracket the bisection searches: FCF collapsing 99% a year to
# doubling every year. A price outside it is said to be, not pinned to an
# end of it.
GROWTH_LOW = -0.99
GROWTH_HIGH = 1.0
_ITERATIONS = 200
_TOLERANCE = 1e-12
NO_FCF = "implied growth not computable: TTM FCF ≤ 0"
NO_EV = "implied growth not computable: EV ≤ 0"
GORDON_FORMULA = "g = r − FCF_ttm / EV"


@dataclass(frozen=True)
class ImpliedGrowth:
    value: float | None
    reason: str | None
    formula: str


@dataclass(frozen=True)
class Sensitivity:
    """Implied growth at the low and high end of one assumption's move, and
    the larger distance from the base case."""

    name: str
    low: float
    high: float
    swing: float


@dataclass(frozen=True)
class ScenarioValue:
    name: str
    label: str
    value_per_share: float | None
    upside: float | None  # value per share over the price, minus one
    reason: str | None


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
    year's flow). Requires terminal_growth < required_return."""
    pv = 0.0
    flow = fcf
    discount = 1.0
    for _ in range(years):
        flow *= 1.0 + growth
        discount *= 1.0 + required_return
        pv += flow / discount
    terminal = flow * (1.0 + terminal_growth) / (required_return - terminal_growth)
    return pv + terminal / discount


def gordon_growth(fcf: float | None, ev: float | None, required_return: float) -> ImpliedGrowth:
    if fcf is None or fcf <= 0:
        return ImpliedGrowth(None, NO_FCF, GORDON_FORMULA)
    if ev is None or ev <= 0:
        return ImpliedGrowth(None, NO_EV, GORDON_FORMULA)
    return ImpliedGrowth(required_return - fcf / ev, None, GORDON_FORMULA)


def _two_stage_formula(a: Assumptions) -> str:
    return (f"PV(FCF_ttm growing g/yr for {a.horizon_years} years, then "
            f"{a.terminal_growth:.1%} in perpetuity, at r={a.required_return:.1%}) = EV; "
            f"g by bisection over [{GROWTH_LOW:+.0%}, {GROWTH_HIGH:+.0%}]")


def implied_growth(fcf: float | None, ev: float | None, a: Assumptions) -> ImpliedGrowth:
    """The reverse two-stage DCF's growth, by bisection; the reason when
    there is none (no positive FCF or EV, or a price outside the bracket)."""
    formula = _two_stage_formula(a)
    if fcf is None or fcf <= 0:
        return ImpliedGrowth(None, NO_FCF, formula)
    if ev is None or ev <= 0:
        return ImpliedGrowth(None, NO_EV, formula)

    def pv(g: float) -> float:
        return present_value(fcf, g, a.horizon_years, a.terminal_growth, a.required_return)

    lo, hi = GROWTH_LOW, GROWTH_HIGH
    if pv(lo) > ev:
        return ImpliedGrowth(None, f"implied growth not computable: below {GROWTH_LOW:+.0%}/yr "
                                   "(EV is under the PV of a collapsing FCF)", formula)
    if pv(hi) < ev:
        return ImpliedGrowth(None, f"implied growth not computable: above {GROWTH_HIGH:+.0%}/yr "
                                   "(EV exceeds the PV of FCF doubling every year)", formula)
    for _ in range(_ITERATIONS):
        mid = (lo + hi) / 2.0
        if pv(mid) < ev:
            lo = mid
        else:
            hi = mid
        if hi - lo < _TOLERANCE:
            break
    return ImpliedGrowth((lo + hi) / 2.0, None, formula)


def _sensitivities(fcf: float, ev: float, mcap: float, base: float,
                   a: Assumptions) -> tuple[Sensitivity, ...]:
    out: list[Sensitivity] = []
    try:
        # Validated anew: r − 1pt at or below the terminal growth is no
        # assumption set, and the sensitivity is withheld rather than valued.
        lower = implied_growth(fcf, ev, Assumptions(
            required_return=a.required_return - 0.01, terminal_growth=a.terminal_growth,
            horizon_years=a.horizon_years))
        upper = implied_growth(fcf, ev, Assumptions(
            required_return=a.required_return + 0.01, terminal_growth=a.terminal_growth,
            horizon_years=a.horizon_years))
    except ValidationError:
        lower = upper = ImpliedGrowth(None, "withheld", "")
    if lower.value is not None and upper.value is not None:
        out.append(Sensitivity("required return ± 1pt", lower.value, upper.value,
                               max(abs(lower.value - base), abs(upper.value - base))))
    cheaper = implied_growth(fcf, ev - 0.1 * mcap, a)
    dearer = implied_growth(fcf, ev + 0.1 * mcap, a)
    if cheaper.value is not None and dearer.value is not None:
        out.append(Sensitivity("price ± 10%", cheaper.value, dearer.value,
                               max(abs(cheaper.value - base), abs(dearer.value - base))))
    return tuple(out)


def _main_assumption(sensitivities: tuple[Sensitivity, ...]) -> str | None:
    if not sensitivities:
        return None
    ranked = sorted(sensitivities, key=lambda s: s.swing, reverse=True)
    first = ranked[0]
    others = "; ".join(f"{s.swing:.1%} for {s.name}" for s in ranked[1:])
    return (f"{first.name}: moves the implied growth by up to {first.swing:.1%}"
            + (f" (vs {others})" if others else ""))


def _scenario(s: Scenario, a: Assumptions, fcf: float | None, bridge: Bridge) -> ScenarioValue:
    r = s.required_return if s.required_return is not None else a.required_return
    tg = s.terminal_growth if s.terminal_growth is not None else a.terminal_growth
    label = (f"model assumption: {s.name} — FCF {s.fcf_growth:+.1%}/yr for {s.years} years, "
             f"terminal {tg:.1%}, r={r:.1%}")
    reason: str | None = None
    if tg >= r:
        reason = f"not computable: terminal growth {tg:.1%} is not below r={r:.1%}"
    elif fcf is None:
        reason = "not computable: TTM FCF missing"
    elif fcf <= 0:
        reason = "not computable: TTM FCF ≤ 0"
    elif bridge.shares.value is None:
        reason = "not computable: share count missing"
    elif bridge.ev is None or bridge.market_cap.value is None:
        reason = f"not computable: {bridge.ev_reason}"
    if reason is not None:
        return ScenarioValue(s.name, label, None, None, reason)
    assert fcf is not None and bridge.ev is not None and bridge.market_cap.value is not None
    assert bridge.shares.value is not None and bridge.price.value is not None
    pv = present_value(fcf, s.fcf_growth, s.years, tg, r)
    # Equity = PV of the firm's FCF less what the bridge adds on top of the
    # market cap (debt − cash − STI + MI + preferred).
    equity = pv - (bridge.ev - bridge.market_cap.value)
    per_share = equity / bridge.shares.value
    return ScenarioValue(s.name, label, per_share, per_share / bridge.price.value - 1.0, None)


def compute_expectations(bridge: Bridge, ttm: TrailingFigures, obs: MarketObservation) -> Expectations:
    a = obs.assumptions if obs.assumptions is not None else Assumptions()
    fcf = ttm.fcf
    gordon = gordon_growth(fcf, bridge.ev, a.required_return)
    reverse = implied_growth(fcf, bridge.ev, a)
    if ttm.reason is not None:
        gordon = ImpliedGrowth(None, f"implied growth not computable: {ttm.reason}", gordon.formula)
        reverse = ImpliedGrowth(None, f"implied growth not computable: {ttm.reason}", reverse.formula)
    sensitivities: tuple[Sensitivity, ...] = ()
    if (reverse.value is not None and fcf is not None and bridge.ev is not None
            and bridge.market_cap.value is not None):
        sensitivities = _sensitivities(fcf, bridge.ev, bridge.market_cap.value, reverse.value, a)
    scenarios = tuple(_scenario(s, a, fcf, bridge) for s in obs.scenarios)
    return Expectations(a, obs.assumptions is None, gordon, reverse, sensitivities,
                        _main_assumption(sensitivities), scenarios)
