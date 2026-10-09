"""The enterprise-value bridge: from one observed price to EV, one line per
component, each line saying which data class it is.

- filing: a balance-sheet figure of the latest period that carries the
  bridge inputs (`select_period`), with the period's `SourcedValue` behind
  it — the ledger cites those filings;
- observation: the price;
- derived: arithmetic over the lines above (market cap, EV);
- assumption: a component the filer did not report and the bridge reads as
  zero. Said on the line, never folded in silently: short-term investments,
  minority interest and preferred stock are often genuinely absent, but
  "absent" and "zero" are not the same claim.

Which facts a period carries is decided before the bridge sees it (review
of 48b1f04, F1): the plane maps the raw payload AS FILED by the observation
through the mapper's point-in-time cut (`plane.as_filed_dataset`), so a
figure re-filed later — a comparative in the next 10-Q, an amendment — is
read as it stood on the observation's day, and the live dataset's
latest-filed-wins dates never decide availability. The cut is EDGAR's day
(`available_through`): a filing dated the observation's own US/Eastern day
is not yet available (F5). A bridge over a dataset alone (no raw facts)
cannot check any of this, and its availability line says so rather than
guessing.

Operating-lease liabilities are shown beside EV and never added to it: the
engine's own total debt excludes them by design (`fields.py`, P0-10), and
lessee comparability across filers is the caveat on the line. Equity (book)
is shown for the same reason — context, not a bridge component.

A bridge is never guessed: a missing share count, debt or cash means no EV,
with the field and the period named. Nor is a currency converted: the
filing figures are in `fields.FILING_CURRENCY`, and a price recorded in any
other currency asserts no market cap and no EV (`currency_mismatch`; Hermes
audit of PR #118, finding 2: a EUR price was multiplied by the share count
and added to USD debt). The filing lines are still shown: they are facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

from app.schemas.financials import CompanyDataset, PeriodFinancials, SourcedValue
from app.services.ingestion.fields import FILING_CURRENCY
from app.services.valuation.observation import EASTERN, MarketObservation

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


def available_through(observed_at: datetime) -> date:
    """The last filing date an observation could have seen: the day before
    its US/Eastern calendar day. EDGAR dates a filing by its Eastern day and
    accepts it at any hour of it, so a filing dated the observation's own
    day is treated as not yet available — a close at 16:00 and a 10-Q
    accepted at 17:30 the same day are the common case, and the bridge must
    not read that 10-Q into that price (F5). The mapper's cut is inclusive
    (`filed <= as_of`), so this is the day before."""
    return observed_at.astimezone(EASTERN).date() - timedelta(days=1)


def currency_mismatch(obs: MarketObservation) -> str | None:
    """Why the price cannot be set against the filing figures, or None: a
    price in another currency than theirs, which nothing here converts.
    Every line that would mix the two (market cap, EV, the multiples, the
    implied growth, the scenarios) carries this instead of a number."""
    if obs.currency == FILING_CURRENCY:
        return None
    return f"price in {obs.currency}, filing figures in {FILING_CURRENCY} — no FX conversion"


def _missing_input(period: PeriodFinancials) -> str | None:
    """The first bridge input the period lacks: a share count (either
    kind), total debt or cash."""
    if period.shares_outstanding is None and period.shares_diluted is None:
        return "share count"
    for name in ("total_debt", "cash_and_equivalents"):
        if getattr(period, name) is None:
            return name
    return None


def select_period(dataset: CompanyDataset) -> tuple[PeriodFinancials | None, list[str]]:
    """The latest period carrying the bridge inputs, and the later periods
    skipped for lacking one, each named with the field. When no period is
    complete the latest is returned (the bridge then names what it lacks);
    None only for a dataset without periods."""
    periods = dataset.sorted_periods()
    if not periods:
        return None, []
    skipped: list[str] = []
    for period in reversed(periods):
        missing = _missing_input(period)
        if missing is None:
            return period, skipped
        skipped.append(f"{period.fiscal_label} ({missing} missing)")
    return periods[-1], []


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


def _availability(dataset: CompanyDataset | None, period: PeriodFinancials | None,
                  skipped: list[str], obs: MarketObservation, as_filed_by: date | None) -> str:
    """The sentence over the filing block: which facts the bridge read and
    whether their availability at the observation was checked."""
    cut = f"filings dated {obs.eastern_day} treated as not yet available"
    if dataset is None:
        # The point-in-time cut mapped nothing: fewer than two quarter ends
        # were filed by then.
        return f"no period can be built from the facts filed by {as_filed_by} ({cut})"
    if period is None:
        return "no periods in the dataset"
    if as_filed_by is None:
        which = "latest period " if not skipped else ""
        text = (f"filing availability at the observation date not checked (no raw facts): "
                f"{which}{period.fiscal_label} (ending {period.period_end}) used")
    else:
        text = (f"filing-derived facts as filed by {as_filed_by} ({cut}): {period.fiscal_label}, "
                f"ending {period.period_end}")
    if skipped:
        text += f"; skipped (bridge inputs missing): {', '.join(skipped)}"
    return text


def enterprise_value_bridge(
    dataset: CompanyDataset | None, obs: MarketObservation, *, as_filed_by: date | None = None
) -> Bridge:
    """EV = market cap + total debt − cash − short-term investments
    + minority interest + preferred stock, over the latest period of
    `dataset` that carries the bridge inputs; `ev` is None, with the reason
    naming the field and period, when the share count, debt or cash is
    missing, or when the price is not in the filing currency
    (`currency_mismatch`: no market cap either). `as_filed_by` is the
    point-in-time cut the dataset was mapped through (the plane's; None for
    a dataset alone, whose availability is then said to be unchecked);
    `dataset` is None when that cut mapped no period at all."""
    period, skipped = select_period(dataset) if dataset is not None else (None, [])
    availability = _availability(dataset, period, skipped, obs, as_filed_by)
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
    mismatch = currency_mismatch(obs)
    market_cap = None if shares.value is None or mismatch else obs.price * shares.value
    if mismatch:
        mcap_note: str | None = f"market cap not asserted: {mismatch}"
    elif market_cap is None:
        mcap_note = f"share count missing for {period.fiscal_label}"
    else:
        mcap_note = None
    mcap = BridgeComponent("market_cap", mcap_label, market_cap, "derived", 1, note=mcap_note)
    debt = _filing(period, "total_debt", "total debt (incl. finance leases)", 1)
    cash = _filing(period, "cash_and_equivalents", "cash and equivalents", -1)
    sti = _filing(period, "short_term_investments", "short-term investments", -1, assumed_zero=True)
    mi = _filing(period, "minority_interest", "minority interest", 1, assumed_zero=True)
    pref = _filing(period, "preferred_stock", "preferred stock", 1, assumed_zero=True)
    leases = _filing(period, "operating_lease_liabilities", leases_label, 0)
    equity = _filing(period, "stockholders_equity", equity_label, 0)

    ev: float | None = None
    reason: str | None = None
    if mismatch:
        reason = f"EV not asserted: {mismatch}"
    elif market_cap is None:
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
