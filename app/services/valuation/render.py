"""The shadow card as an appendix section: three headed blocks, in the order
of the data classes, every line tagged with its class.

    [F] filing-derived fact   [O] market observation
    [A] model assumption      [D] derived (arithmetic over the above)

Rendered only when the caller asked for the plane; with no observation on
disk the section is one line saying so and how to record one. Nothing here
is read back by anything that scores.

The operator's own text (source, note, scenario names) is emitted as text:
the model refuses control characters, and `operator_text` still escapes
what could open markdown structure, so the one file an operator writes
cannot forge a heading or a table cell of the report (review of 48b1f04,
F4).
"""

from __future__ import annotations

import re

from app.services.valuation.bridge import EV_FORMULA, BridgeComponent
from app.services.valuation.expectations import ImpliedGrowth, ScenarioValue
from app.services.valuation.multiples import HISTORY_LINE, PEER_LINE, Multiple
from app.services.valuation.observation import STALE_AFTER_DAYS
from app.services.valuation.plane import ValuationPlane

SECTION_TITLE = "## Valuation shadow card (non-scoring)"
PREAMBLE = (
    "_This section does not feed any score or flag. Three data classes are kept separate on "
    "every line: [F] filing-derived facts, [O] the market observation, [A] model assumptions; "
    "[D] marks arithmetic over them. The card is a lens on the price, not a verdict._"
)
_TAG = {"filing": "F", "observation": "O", "assumption": "A", "derived": "D"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# What opens a heading, a list item, a quote: escaped where a line could
# have started. `|` ends a table cell anywhere.
_OPENERS = "#-*>+"


def operator_text(text: str) -> str:
    """Operator-written text as one line of plain markdown. Control
    characters become spaces (belt and braces: the model refuses them), a
    `|` is escaped, and a piece that starts with a structural character —
    the start of the text, or wherever a control character was — has it
    escaped."""
    pieces = []
    for piece in _CONTROL.split(text):
        if piece and piece[0] in _OPENERS:
            piece = "\\" + piece
        pieces.append(piece.replace("|", "\\|"))
    return " ".join(pieces)


def not_produced_line(ticker: str) -> str:
    """The section when the caller asked for the plane and no observation is
    recorded for the ticker."""
    return (f"Valuation shadow card: not produced (no market observation recorded for {ticker}; "
            f"record one with `scripts/market.py record {ticker} --price P --at ISO8601 "
            f"--source \"…\"`)")


def _money(v: float | None) -> str:
    return "—" if v is None else f"{v:,.0f}"


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v:+.1%}"


def _days(n: int) -> str:
    return f"{n} day" if n == 1 else f"{n} days"


def _filing_cell(c: BridgeComponent) -> str:
    """The filings behind a line: form, accession and filed date per fact."""
    refs = [ref for sv in c.sources for ref in sv.inputs]
    if not refs:
        return c.note or "(no per-value provenance)"
    seen = ", ".join(dict.fromkeys(f"{r.form} {r.accession} filed {r.filed}" for r in refs))
    return seen if c.note is None else f"{seen}; {c.note}"


def _bridge_row(c: BridgeComponent, period: str | None) -> str:
    value = "—" if c.value is None else f"{c.value:,.2f}" if c.name == "price" else _money(c.value)
    detail = _filing_cell(c) if c.basis == "filing" else (c.note or "")
    where = period or ""
    if c.basis == "observation":
        where, detail = "", "the observation above"
    return f"| {c.label} | {_TAG[c.basis]} | {value} | {where} | {detail} |"


def _multiple_row(m: Multiple) -> str:
    if m.value is None:
        return f"| {m.name} | — | | | {m.reason} |"
    shown = f"{m.value:.1%}" if m.name.endswith("yield") else f"{m.value:.2f}x"
    return (f"| {m.name} | {shown} | {m.numerator_name} {_money(m.numerator)} | "
            f"{m.denominator_name} {_money(m.denominator)} | |")


def _growth_line(what: str, g: ImpliedGrowth) -> str:
    if g.value is None:
        return f"- [D] {what}: {g.reason} ({g.formula})"
    return f"- [D] {what}: {g.value:+.1%}/yr ({g.formula})"


def _scenario_label(sc: ScenarioValue) -> str:
    return f"model assumption: {operator_text(sc.name)} — {sc.terms}"


def render_valuation_section(plane: ValuationPlane) -> str:
    obs = plane.observation
    b = plane.bridge
    e = plane.expectations
    lines = [SECTION_TITLE, "", PREAMBLE, "", "### Market observation", ""]
    age = plane.age_days
    lines.append(f"- [O] price {obs.price:,.2f} {obs.currency} observed "
                 f"{obs.observed_at.isoformat()} (age {_days(age)} on {plane.generated_on})")
    if plane.stale:
        lines.append(f"- **STALE**: the observation is older than {STALE_AFTER_DAYS} days; the "
                     "filing facts below are as of the report, the price is not")
    lines.append(f"- [O] source: {operator_text(obs.source)}")
    if obs.note:
        lines.append(f"- [O] note: {operator_text(obs.note)}")
    lines.append(f"- [O] recorded {obs.recorded_at.isoformat()}; observation sha256 "
                 f"{plane.loaded.sha256[:12]}…")

    lines += ["", "### Filing-derived facts", "", f"_{b.availability}._", "",
              "| Line | Class | Value | Period | Filing / note |", "|---|---|---|---|---|"]
    period = b.fiscal_label
    for c in (b.price, b.shares, b.market_cap, b.debt, b.cash, b.short_term_investments,
              b.minority_interest, b.preferred_stock):
        lines.append(_bridge_row(c, period))
    if b.ev is None:
        lines.append(f"| **enterprise value** | D | — | {period or ''} | {b.ev_reason} |")
    else:
        lines.append(f"| **enterprise value** | D | {_money(b.ev)} | {period} | {EV_FORMULA} |")
    for c in (b.operating_leases, b.equity):
        lines.append(_bridge_row(c, period))
    lines.append("")
    if plane.ttm.reason is not None:
        lines.append(f"- [F] TTM figures: {plane.ttm.reason}")
    else:
        t = plane.ttm
        lines.append(f"- [F] TTM figures ({t.label}, the engine's own window): revenue "
                     f"{_money(t.revenue)}, net income {_money(t.net_income)}, EBIT "
                     f"{_money(t.ebit)}, EBITDA {_money(t.ebitda)}, FCF {_money(t.fcf)}")
    lines += ["", "| Multiple [D] | Value | Numerator | Denominator | Not meaningful because |",
              "|---|---|---|---|---|"]
    lines += [_multiple_row(m) for m in plane.multiples]
    lines += ["", f"- {HISTORY_LINE}", f"- {PEER_LINE}"]

    lines += ["", "### Model assumptions", ""]
    a = e.assumptions
    label = "default assumptions (not operator-supplied)" if e.defaulted else "operator-supplied"
    lines.append(f"- [A] required return {a.required_return:.1%}, terminal growth "
                 f"{a.terminal_growth:.1%}, horizon {a.horizon_years} years — {label}")
    lines.append(_growth_line("Gordon implied perpetual FCF growth", e.gordon))
    lines.append(_growth_line("reverse two-stage DCF implied FCF growth", e.reverse))
    for s in e.sensitivities:
        if s.low is None or s.high is None or s.swing is None:
            lines.append(f"- [D] sensitivity, {s.name}: {s.reason}")
        else:
            lines.append(f"- [D] sensitivity, {s.name}: implied growth {_pct(s.low)} to "
                         f"{_pct(s.high)}/yr (swing up to {s.swing:.1%})")
    if e.main_assumption is not None:
        lines.append(f"- [D] the main assumption that would change the conclusion: "
                     f"{e.main_assumption}")
    if not e.scenarios:
        lines.append("- [A] scenarios: no scenarios recorded")
    for sc in e.scenarios:
        if sc.value_per_share is None:
            lines.append(f"- [A] {_scenario_label(sc)}: {sc.reason}")
        else:
            lines.append(f"- [A] {_scenario_label(sc)} → [D] value per share "
                         f"{sc.value_per_share:,.2f} {obs.currency} vs price {obs.price:,.2f} "
                         f"({_pct(sc.upside)})")
    return "\n".join(lines)
