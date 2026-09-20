"""Standing assumptions per holding — the thesis without the journal.

The journal's blind thesis per print produced one case in a full season; the
holder has no time for it. What they can do is write down, once, the two or
three things they are assuming about a holding ("data-center revenue keeps
growing >50% YoY", "no dilution beyond the buyback offset"). Each brief then
maps the print's facts to those assumptions — held / challenged / no news —
with the evidence cited. Fifteen minutes per holding per year instead of an
hour per print, and the `useful:` tally has something concrete to score.

File: journal/assumptions/<TICKER>.md (private, gitignored — it reveals
holdings). One assumption per bullet; blank lines, `#` headings and comments
are ignored. Hand-edit it, or `scripts/earnings_brief.py assume TICKER "..."`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from app.services.journal.store import safe_ticker

ROOT = Path(__file__).resolve().parents[3]
ASSUMPTIONS = ROOT / "journal" / "assumptions"
MAX_ASSUMPTION_CHARS = 300
MAX_ASSUMPTIONS = 12
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*\S)\s*$")


def assumptions_path(ticker: str, root: Path | None = None) -> Path:
    return (root or ASSUMPTIONS) / f"{safe_ticker(ticker)}.md"


def parse_assumptions(text: str) -> list[str]:
    """Bulleted or numbered lines, in file order; everything else ignored.
    Each is one line, whitespace-collapsed, bounded — a long paragraph is a
    thesis, not an assumption, and the model gets these as data anyway."""
    out: list[str] = []
    for line in text.splitlines():
        m = _BULLET_RE.match(line)
        if not m:
            continue
        # A `|` would split the brief's table cell and drop the whole row,
        # which fails the brief for this holding every time until the file is
        # edited by hand. Neutralize it at the source, where both the file we
        # hand the model and the text we compare against come from.
        item = " ".join(m.group(1).replace("|", "/").split())
        if item[:3].lower() in ("[ ]", "[x]"):  # checkbox bullets, either case
            item = item[3:].strip()
        if item:
            out.append(item[:MAX_ASSUMPTION_CHARS])
    return out[:MAX_ASSUMPTIONS]


def load_assumptions(ticker: str, root: Path | None = None) -> list[str]:
    p = assumptions_path(ticker, root)
    if not p.is_file():
        return []
    return parse_assumptions(p.read_text(errors="replace"))


def add_assumption(ticker: str, text: str, root: Path | None = None) -> Path:
    """Append one assumption; creates the file with a header the first time."""
    item = " ".join(text.replace("|", "/").split())
    if not item:
        raise ValueError("an assumption needs some text")
    if len(item) > MAX_ASSUMPTION_CHARS:
        raise ValueError(f"keep an assumption under {MAX_ASSUMPTION_CHARS} characters — "
                         "that is a thesis; split it")
    p = assumptions_path(ticker, root)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(f"# {safe_ticker(ticker)} — standing assumptions\n"
                     "# One per bullet. Each brief reports held / challenged / no news.\n\n")
    existing = load_assumptions(ticker, root)
    if item in existing:
        return p
    if len(existing) >= MAX_ASSUMPTIONS:
        raise ValueError(f"{p.name} already holds {MAX_ASSUMPTIONS} assumptions — retire one first")
    with p.open("a") as fh:
        fh.write(f"- {item}\n")
    return p


HOLDER, DERIVED = "holder", "derived"


def render_for_brief(
    ticker: str,
    items: list[str],
    *,
    origin: str = HOLDER,
    details: Sequence[str] | None = None,
) -> str:
    """The numbered list handed to the brief as a data file.

    `origin` is DERIVED when `derived.py` supplied these instead of the holder.
    The provenance is stated in the file itself, not only in the source label:
    the brief's section is called "Your assumptions", and a reader must never
    be left thinking they wrote a claim the engine read off the filings.
    """
    ticker = safe_ticker(ticker)
    if origin == DERIVED:
        lines = [
            f"Standing assumptions for {ticker} — DERIVED BY THE ENGINE from the "
            "company's own filed quarterly history.",
            "",
            "These are NOT holder-authored and NOT predictions. Each is a continuity "
            "claim the filings supported through the last reported quarter, written so "
            "that this print either holds it, challenges it, or says nothing about it.",
            "",
            "Reproduce the numbered assumption text verbatim in the brief's table. An "
            "indented `basis:` line is the trailing history behind the claim — context "
            "for the evidence cell, never part of the assumption text.",
            "",
        ]
    else:
        lines = [f"Standing assumptions for {ticker} (holder-authored; numbered "
                 "for reference in the brief):", ""]
    for i, a in enumerate(items, 1):
        lines.append(f"{i}. {a}")
        if details is not None and i <= len(details) and details[i - 1]:
            lines.append(f"   basis: {details[i - 1]}")
    return "\n".join(lines) + "\n"
