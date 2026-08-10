"""Restatement-footprint detector (P0-5).

The companyfacts mapping keeps the latest-filed value per period
(`_dedupe_latest_filed`), which silently discards the value a company
ORIGINALLY reported when a later filing revises it — exactly the footprint a
deteriorating company can hide. This module recovers it.

TIER 1 (this module) — WITHIN-SNAPSHOT diff. A single companyfacts document
already accumulates every fact a filer has reported, each stamped with its
accession, filed date and form. When two filings report the SAME period
(same duration and end) with a materially different value, that difference is
a restatement footprint — recoverable today, no persistence required. An
amended form (10-K/A, 10-Q/A) is the strongest signal; a revised comparative
in a later original filing is the quieter one.

Tag switches are NOT restatements: two different tags reporting the same period
live in different series and are never compared (that would fabricate a
"restatement" at every taxonomy change).

TIER 2 (separate follow-up) — a company that revises a number WITHOUT
re-presenting the original leaves companyfacts holding only the new value.
Catching those needs an append-only vintage store captured over time. This
module delivers the detectable majority now.

Evidence only: nothing here feeds a score. Output is a provenance-linked
timeline rendered as a report section (matches ROADMAP_2026Q3 P1-F done-when).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.services.ingestion.companyfacts_mapper import (
    FLOW_FIELDS,
    INSTANT_FIELDS,
    _parse_date,
    _unit_for,
)

# Relative change below which a same-period revision is treated as rounding or
# an immaterial reclassification rather than a restatement. The XBRL survey
# measured a 3.4-6.7% base rate of benign changes; 1% cleanly separates the
# 2008 AAPL Assets restatement (8.6%) from a $1M/$3.5B (0.03%) reclassification.
DEFAULT_MATERIALITY_PCT = 0.01

# Share counts are routinely restated by stock splits / reverse splits — a
# neutral corporate action, not an accounting restatement. Excluded to avoid a
# flood of split-adjustment false positives (Apple's 2014 7-for-1 split makes
# every prior share count appear "revised" +600%).
SPLIT_ADJUSTED_FIELDS = frozenset({"shares_diluted", "shares_outstanding"})


@dataclass(frozen=True)
class RestatementFootprint:
    """One previously reported figure that a later filing revised.

    Round-7 finding: the CURRENT value (latest-filed, what the mapper scores) and
    the amendment EVENT are distinct and must not be conflated. `current_*` is the
    value as it now stands; `amendment_*` is the material /A event in the filing
    trail, if any (None when the revision came only via ordinary comparatives).
    `pct_change`/`direction` describe original -> current, matching scoring.
    Tier-1 promotion keys on `is_amendment` (an amendment event exists), never on
    the form carrying the current value.
    """

    field_name: str  # canonical field, e.g. "total_assets"
    tag: str  # XBRL tag, e.g. "us-gaap:Assets"
    period_end: date
    period_start: date | None  # None for instant (balance-sheet) facts
    # As originally reported (earliest filing):
    original_value: float
    original_filed: date
    original_form: str
    original_accession: str
    # As it now stands — latest-filed, i.e. exactly what the mapper scores:
    current_value: float
    current_filed: date
    current_form: str
    current_accession: str
    # The material amendment (/A) event in the trail, if any (Tier-1 provenance):
    amendment_value: float | None = None
    amendment_filed: date | None = None
    amendment_form: str | None = None
    amendment_accession: str | None = None

    @property
    def abs_change(self) -> float:
        return self.current_value - self.original_value

    @property
    def pct_change(self) -> float | None:
        if self.original_value == 0:
            return None
        return (self.current_value - self.original_value) / abs(self.original_value)

    @property
    def direction(self) -> str:
        return "up" if self.current_value > self.original_value else "down"

    @property
    def is_amendment(self) -> bool:
        """True iff a material /A amendment touched this period (regardless of
        the form carrying the current value)."""
        return self.amendment_accession is not None


def _rows(facts_json: dict, taxonomy: str, tag: str, unit: str) -> list[dict]:
    concept = facts_json.get("facts", {}).get(taxonomy, {}).get(tag)
    if not concept:
        return []
    units = concept.get("units", {})
    rows = units.get(unit)
    if rows is None and unit == "shares":
        rows = units.get("USD")  # some filers mis-file share counts under USD
    return rows or []


def detect_restatements(
    facts_json: dict,
    materiality_pct: float = DEFAULT_MATERIALITY_PCT,
    period_since: date | None = None,
) -> list[RestatementFootprint]:
    """Find same-period figures a later filing revised beyond `materiality_pct`.

    For each canonical field's candidate tags, group facts by (start, end) and
    compare the earliest-filed value (as originally reported) with the
    latest-filed value (as it now stands). Materially different pairs become
    provenance-linked footprints. Restatements sharing one XBRL tag (e.g.
    OperatingIncomeLoss backing both `ebit` and `operating_income`) are reported
    once.

    `period_since` restricts to periods ending on/after that date — the live
    monitor cares about revisions to recent periods, not a 2010 reclassification.
    Split-adjusted share fields are excluded (see SPLIT_ADJUSTED_FIELDS).
    """
    footprints: list[RestatementFootprint] = []
    seen: set[tuple[str, date | None, date]] = set()  # (tag, start, end) dedupe

    for field_name, candidates in {**INSTANT_FIELDS, **FLOW_FIELDS}.items():
        if field_name in SPLIT_ADJUSTED_FIELDS:
            continue
        unit = _unit_for(field_name)
        for taxonomy, tag in candidates:
            qualified_tag = f"{taxonomy}:{tag}"
            by_key: dict[tuple[date | None, date], list[tuple[date, float, str, str]]] = {}
            for e in _rows(facts_json, taxonomy, tag, unit):
                try:
                    start = _parse_date(e["start"]) if "start" in e else None
                    end = _parse_date(e["end"])
                    filed = _parse_date(e["filed"])
                    val = float(e["val"])
                except (KeyError, ValueError, TypeError):
                    continue
                by_key.setdefault((start, end), []).append(
                    (filed, val, e.get("form", ""), e.get("accn", ""))
                )

            for (start, end), filings in by_key.items():
                if len(filings) < 2 or (qualified_tag, start, end) in seen:
                    continue
                if period_since is not None and end < period_since:
                    continue
                # `filings` is in companyfacts order (same source the mapper
                # reads). `current` MUST resolve same-day filed ties the way the
                # mapper's _dedupe_latest_filed does — it keeps the FIRST fact at
                # the latest filed date (`>` not `>=`). Python's max()/min()
                # return the FIRST extremal element, so max(...key=filed) on the
                # unsorted list reproduces exactly what the mapper scores
                # (round-8 finding: sort()+filings[-1] picked the LAST same-day
                # fact and diverged from scoring).
                orig = min(filings, key=lambda f: f[0])  # earliest filed
                current = max(filings, key=lambda f: f[0])  # latest filed = mapper's value

                # Keep the CURRENT value and the amendment EVENT separate (round-7).
                # `material` = every filing that materially deviates from the
                # originally reported value; the amendment event is the latest
                # material /A filing in the trail, independent of which form
                # carries the current value.
                def _pct(v: float) -> float | None:
                    return None if orig[1] == 0 else abs(v - orig[1]) / abs(orig[1])

                material = [
                    f
                    for f in filings
                    if f[1] != orig[1] and ((p := _pct(f[1])) is None or p >= materiality_pct)
                ]
                if not material:
                    continue
                material_amendments = [f for f in material if f[2].endswith("/A")]
                amendment = max(material_amendments, key=lambda f: f[0]) if material_amendments else None
                # Emit iff there is a NET change (current materially differs from
                # original) OR a formal /A amendment. A transient non-amendment
                # revision that fully reverted (current back at ~original, no /A)
                # is low-signal noise and is skipped.
                if current not in material and amendment is None:
                    continue
                seen.add((qualified_tag, start, end))
                footprints.append(
                    RestatementFootprint(
                        field_name=field_name,
                        tag=qualified_tag,
                        period_end=end,
                        period_start=start,
                        original_value=orig[1],
                        original_filed=orig[0],
                        original_form=orig[2],
                        original_accession=orig[3],
                        current_value=current[1],
                        current_filed=current[0],
                        current_form=current[2],
                        current_accession=current[3],
                        amendment_value=amendment[1] if amendment else None,
                        amendment_filed=amendment[0] if amendment else None,
                        amendment_form=amendment[2] if amendment else None,
                        amendment_accession=amendment[3] if amendment else None,
                    )
                )

    footprints.sort(key=lambda f: (f.period_end, f.field_name), reverse=True)
    return footprints


_MAX_ROWS = 20  # spinoff/discontinued-ops re-presentation can produce many rows


def _table(footprints: list[RestatementFootprint]) -> list[str]:
    # Original -> Current (latest-filed = what the scorer uses), with the /A
    # amendment event shown separately so the two are never conflated (round-7).
    rows = ["| Period | Field | Original | Current | Change | Amendment event |", "|---|---|---|---|---|---|"]
    for f in footprints[:_MAX_ROWS]:
        pct = f"{f.pct_change * 100:+.1f}%" if f.pct_change is not None else "n/a"
        amend = (
            f"{f.amendment_value:,.0f} via {f.amendment_form} {f.amendment_filed}"
            if f.amendment_value is not None
            else "—"
        )
        rows.append(
            f"| {f.period_end} | {f.field_name} | {f.original_value:,.0f} "
            f"| {f.current_value:,.0f} | {pct} | {amend} |"
        )
    if len(footprints) > _MAX_ROWS:
        rows.append(f"| … | +{len(footprints) - _MAX_ROWS} more | | | | |")
    return rows


def render_restatements_section(footprints: list[RestatementFootprint]) -> str:
    """Markdown section for the report. Evidence framing only — no scoring.

    Amended-filing (/A) revisions are surfaced as high-confidence restatements;
    other same-period revisions are surfaced separately with a caveat, because a
    large non-amendment swing on a flow item is often a discontinued-operations
    or spinoff re-presentation rather than an accounting-error correction.
    """
    lines = ["## Prior-Period Restatements (evidence — not scored)", ""]
    lines.append(
        "Revisions to previously reported figures, recovered from the companyfacts "
        "filing history (original vs latest-filed value for the same period). "
        "Share counts are excluded (stock-split noise)."
    )
    lines.append("")
    if not footprints:
        lines.append("- No prior-period revisions detected above the materiality threshold.")
        return "\n".join(lines)

    amended = [f for f in footprints if f.is_amendment]
    other = [f for f in footprints if not f.is_amendment]

    lines.append("### Amended-filing restatements (10-K/A, 10-Q/A) — high confidence")
    lines.append("")
    if amended:
        lines.extend(_table(amended))
    else:
        lines.append("- None.")
    lines.append("")

    lines.append("### Other prior-period revisions")
    lines.append("")
    lines.append(
        "_A large non-amendment revision to a flow item (revenue, income, CFO) is "
        "often a discontinued-operations or spinoff re-presentation, not an "
        "accounting-error correction. Treat as context, not an alarm._"
    )
    lines.append("")
    if other:
        lines.extend(_table(other))
    else:
        lines.append("- None.")
    lines.append("")

    summary = f"- **{len(footprints)} revised figure(s)** recovered from filing history"
    if amended:
        summary += f"; **{len(amended)} via amended (/A) filings**"
    lines.append(summary + ".")
    return "\n".join(lines)
