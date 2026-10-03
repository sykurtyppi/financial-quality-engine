"""The enterprise-value bridge: from one observed price to EV, one line per
component, each line saying which data class it is.

- filing: a balance-sheet figure of the latest period whose facts were all
  filed by the observation's moment (`select_period`), with the period's
  `SourcedValue` behind it — the ledger cites those filings;
- observation: the price;
- derived: arithmetic over the lines above (market cap, EV);
- assumption: a component the filer did not report and the bridge reads as
  zero. Said on the line, never folded in silently: short-term investments,
  minority interest and preferred stock are often genuinely absent, but
  "absent" and "zero" are not the same claim.

Operating-lease liabilities are shown beside EV and never added to it: the
engine's own total debt excludes them by design (`fields.py`, P0-10), and
lessee comparability across filers is the caveat on the line. Equity (book)
is shown for the same reason — context, not a bridge component.

A bridge is never guessed: a missing share count, debt or cash means no EV,
with the field and the period named.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import ClassVar, Literal

from app.schemas.financials import CompanyDataset, PeriodFinancials, SourcedValue
from app.services.valuation.observation import MarketObservation

Basis = Literal["filing", "observation", "derived", "assumption"]


@dataclass(frozen=True)
class BridgeComponent:
    """One line of the bridge. `sign` is its sign in EV (+1 added, -1
    subtracted, 0 not a component); `sources` the period's provenance for a
    filing line (empty when the dataset carries none, as the API's does)."""

    name: str
    label: str
    value: float | None
    basis: Basis
    sign: int = 0
    sources: tuple[SourcedValue, ...] = ()
    note: str | None = None

    @property
    def in_ev(self) -> bool:
        return self.sign != 0


@dataclass(frozen=True)
class Bridge:
    # The period fields the bridge reads; a period is "available" at an
    # observation when every fact behind these was filed by then.
    FILING_FIELDS: ClassVar[tuple[str, ...]] = (
        "shares_outstanding", "shares_diluted", "total_debt", "cash_and_equivalents",
        "short_term_investments", "minority_interest", "preferred_stock",
        "operating_lease_liabilities", "stockholders_equity",
    )

    fiscal_label: str | None
    period_end: date | None
    availability: str
    price: BridgeComponent
    shares: BridgeComponent
    market_cap: BridgeComponent
    debt: BridgeComponent
    cash: BridgeComponent
    short_term_investments: BridgeComponent
    minority_interest: BridgeComponent
    preferred_stock: BridgeComponent
    operating_leases: BridgeComponent
    equity: BridgeComponent
    ev: float | None
    ev_reason: str | None

    def components(self) -> tuple[BridgeComponent, ...]:
        """Every line, in the order the card shows them."""
        return (self.price, self.shares, self.market_cap, self.debt, self.cash,
                self.short_term_investments, self.minority_interest, self.preferred_stock,
                self.operating_leases, self.equity)

    def filing_components(self) -> tuple[BridgeComponent, ...]:
        """The filing-derived lines that carry a value: the claims the ledger
        sources to filings."""
        return tuple(c for c in self.components() if c.basis == "filing" and c.value is not None)


def _filed_by(period: PeriodFinancials) -> date | None:
    """The latest filing date among the facts behind the period's bridge
    fields (every field's, when none of those has a fact); None when the
    period carries no provenance at all."""
    refs = [ref for name, sv in period.sources.items() if name in Bridge.FILING_FIELDS
            for ref in sv.inputs]
    if not refs:
        refs = [ref for sv in period.sources.values() for ref in sv.inputs]
    return max((ref.filed for ref in refs), default=None)


def select_period(
    dataset: CompanyDataset, observed_at: datetime
) -> tuple[PeriodFinancials | None, str]:
    """The latest period whose bridge facts were all filed by the
    observation's day, and a sentence saying so (or why not). A dataset
    without per-value provenance (the API's, a synthetic one) cannot be
    checked: its latest period is used and the sentence says the check was
    not made — never a guess dressed as one."""
    periods = dataset.sorted_periods()
    if not periods:
        return None, "no periods in the dataset"
    if not any(p.sources for p in periods):
        latest = periods[-1]
        return latest, (f"availability not checked (no per-value provenance in this dataset): "
                        f"latest period {latest.fiscal_label} (ending {latest.period_end}) used")
    on = observed_at.astimezone(UTC).date()
    skipped: list[str] = []
    for period in reversed(periods):
        filed = _filed_by(period)
        if filed is None:
            skipped.append(f"{period.fiscal_label} (no dated fact)")
            continue
        if filed <= on:
            after = f"; filed after it: {', '.join(skipped)}" if skipped else ""
            return period, (f"filing-derived facts filed by {filed} (observation {on}): "
                            f"{period.fiscal_label}, ending {period.period_end}{after}")
        skipped.append(f"{period.fiscal_label} (filed {filed})")
    return None, (f"no period of the dataset was filed by {on}: "
                  f"{', '.join(skipped)}")


def _filing(period: PeriodFinancials, name: str, label: str, sign: int, *,
            assumed_zero: bool = False) -> BridgeComponent:
    """A balance-sheet line of the period. Missing: `assumed_zero` reads it
    as 0 and says so (a model assumption); otherwise the line stays empty
    and says it is not reported."""
    value = getattr(period, name)
    if value is None:
        if assumed_zero:
            return BridgeComponent(
                name, label, 0.0, "assumption", sign,
                note=f"not reported (assumed 0) for {period.fiscal_label}: model assumption",
            )
        return BridgeComponent(name, label, None, "filing", sign,
                               note=f"not reported for {period.fiscal_label}")
    sv = period.sources.get(name)
    note = None
    if sv is not None and sv.inputs:
        concepts = ", ".join(dict.fromkeys(ref.concept for ref in sv.inputs))
        note = f"read as {concepts}"
    return BridgeComponent(name, label, value, "filing", sign,
                           (sv,) if sv is not None else (), note)


def _share_count(period: PeriodFinancials) -> BridgeComponent:
    """The cover-page count (dei), dated by its cover date, else the
    weighted-average diluted count, labelled as such."""
    if period.shares_outstanding is not None:
        sv = period.sources.get("shares_outstanding")
        if sv is not None and sv.inputs:
            ref = sv.inputs[0]
            kind = "cover-page count" if ref.concept.startswith("dei:") else "balance-sheet count"
            label = f"{kind} dated {ref.end}"
        else:
            label = "cover-page count (cover date not recorded)"
        return BridgeComponent("shares_outstanding", label, period.shares_outstanding, "filing",
                               0, (sv,) if sv is not None else ())
    if period.shares_diluted is not None:
        sv = period.sources.get("shares_diluted")
        return BridgeComponent("shares_diluted",
                               f"weighted-average diluted, {period.fiscal_label}",
                               period.shares_diluted, "filing", 0, (sv,) if sv is not None else ())
    return BridgeComponent("shares_outstanding", "share count", None, "filing", 0,
                           note=f"not reported for {period.fiscal_label}")


def _empty(name: str, label: str, sign: int, note: str) -> BridgeComponent:
    return BridgeComponent(name, label, None, "filing", sign, note=note)


def enterprise_value_bridge(dataset: CompanyDataset, obs: MarketObservation) -> Bridge:
    """EV = market cap + total debt − cash − short-term investments
    + minority interest + preferred stock, over the period available at the
    observation; `ev` is None, with the reason naming the field and period,
    when the share count, debt or cash is missing."""
    period, availability = select_period(dataset, obs.observed_at)
    price = BridgeComponent(
        "price", f"price {obs.price:,.2f} {obs.currency} observed {obs.observed_at.isoformat()}",
        obs.price, "observation",
    )
    mcap_label = "market cap = price × share count"
    leases_label = "operating lease liabilities — not in EV (lessee comparability caveat)"
    equity_label = "stockholders' equity (book) — not in EV"
    if period is None:
        none = "no period available at the observation"
        return Bridge(
            None, None, availability, price,
            _empty("shares_outstanding", "share count", 0, none),
            BridgeComponent("market_cap", mcap_label, None, "derived", 1, note=none),
            _empty("total_debt", "total debt", 1, none),
            _empty("cash_and_equivalents", "cash and equivalents", -1, none),
            _empty("short_term_investments", "short-term investments", -1, none),
            _empty("minority_interest", "minority interest", 1, none),
            _empty("preferred_stock", "preferred stock", 1, none),
            _empty("operating_lease_liabilities", leases_label, 0, none),
            _empty("stockholders_equity", equity_label, 0, none),
            None, f"EV not asserted: {availability}",
        )
    shares = _share_count(period)
    market_cap = None if shares.value is None else obs.price * shares.value
    mcap = BridgeComponent(
        "market_cap", mcap_label, market_cap, "derived", 1,
        note=None if market_cap is not None else f"share count missing for {period.fiscal_label}",
    )
    debt = _filing(period, "total_debt", "total debt (incl. finance leases)", 1)
    cash = _filing(period, "cash_and_equivalents", "cash and equivalents", -1)
    sti = _filing(period, "short_term_investments", "short-term investments", -1, assumed_zero=True)
    mi = _filing(period, "minority_interest", "minority interest", 1, assumed_zero=True)
    pref = _filing(period, "preferred_stock", "preferred stock", 1, assumed_zero=True)
    leases = _filing(period, "operating_lease_liabilities", leases_label, 0)
    equity = _filing(period, "stockholders_equity", equity_label, 0)

    ev: float | None = None
    reason: str | None = None
    if market_cap is None:
        reason = f"EV not asserted: share count missing for {period.fiscal_label}"
    elif debt.value is None:
        reason = f"EV not asserted: total_debt missing for {period.fiscal_label}"
    elif cash.value is None:
        reason = f"EV not asserted: cash_and_equivalents missing for {period.fiscal_label}"
    else:
        ev = (market_cap + debt.value - cash.value
              - (sti.value or 0.0) + (mi.value or 0.0) + (pref.value or 0.0))
    return Bridge(
        period.fiscal_label, period.period_end, availability, price, shares, mcap, debt, cash,
        sti, mi, pref, leases, equity, ev, reason,
    )


EV_FORMULA = ("EV = market cap + total debt − cash − short-term investments + minority interest "
              "+ preferred stock")
