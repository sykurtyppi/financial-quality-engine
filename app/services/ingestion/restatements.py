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

from collections.abc import Mapping
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


@dataclass(frozen=True)
class RestatementScan:
    """What one restatement check actually covered, alongside what it found.

    A list of footprints cannot say why it is empty. "No revisions detected"
    over the seven fields the payload happened to map reads identically to the
    same sentence over all twenty-eight — and the first is not a clean bill for
    the other twenty-one. Every field the check knows about is therefore
    accounted for in exactly one of three places:

    - `inspected`: a series was resolved and its filing history compared;
    - `uninspected`: the field COULD NOT be checked (no series mapped this
      run, no candidate tag with facts, or a resolved series with no eligible
      facts) — its silence is a data gap, never evidence;
    - `excluded`: skipped by design (split-adjusted share counts).

    Only `uninspected` makes the scan `incomplete`; a by-design exclusion is
    disclosed but is not a hole.
    """

    footprints: list[RestatementFootprint]
    inspected: tuple[str, ...]
    uninspected: dict[str, str]  # field -> reason
    excluded: dict[str, str]  # field -> reason
    as_of: date | None
    period_since: date | None
    materiality_pct: float

    @property
    def total(self) -> int:
        return len(self.inspected) + len(self.uninspected) + len(self.excluded)

    @property
    def incomplete(self) -> bool:
        return bool(self.uninspected)

    def coverage_line(self) -> str:
        """One sentence naming the coverage, for the card and the appendix."""
        line = f"inspected {len(self.inspected)} of {self.total} fields for revisions"
        if self.uninspected:
            gaps = "; ".join(f"{f} ({why})" for f, why in sorted(self.uninspected.items()))
            line += f"; NOT inspected: {gaps}"
        if self.excluded:
            line += "; excluded by design: " + ", ".join(sorted(self.excluded))
        return line


def _rows(facts_json: dict, taxonomy: str, tag: str, unit: str) -> list[dict]:
    concept = facts_json.get("facts", {}).get(taxonomy, {}).get(tag)
    if not concept:
        return []
    units = concept.get("units", {})
    rows = units.get(unit)
    if rows is None and unit == "shares":
        rows = units.get("USD")  # some filers mis-file share counts under USD
    return rows or []


def _active_tag(
    facts_json: dict,
    candidates: tuple[tuple[str, str], ...],
    unit: str,
    as_of: date | None = None,
) -> tuple[str, str] | None:
    """Approximate the tag the mapper would SCORE: the candidate covering the
    most distinct periods, ties broken by candidate order.

    This is a FALLBACK, used only when the caller cannot supply the mapper's
    real selection (see `selected_tags` on `detect_restatements`). It is a
    genuine approximation and not equivalent to `_best_series`, which scores
    coverage of the report's chosen quarter ends AFTER period reconstruction
    and derivation. A legacy tag carrying many irrelevant old periods can win
    here while losing there, which would report a revision on a tag the engine
    never scored.

    `as_of` must be honored HERE and not only when rows are later filtered:
    counting coverage over the whole payload lets facts filed after `as_of`
    decide which tag a dated report inspects, so adding a future taxonomy
    series changes — or erases — a historical result that was already known at
    the report date. Returns (taxonomy, tag) or None if no candidate has data.
    """
    best: tuple[str, str] | None = None
    best_cov = 0
    for taxonomy, tag in candidates:  # candidates are in mapper priority order
        rows = _eligible_rows(facts_json, taxonomy, tag, unit, as_of)
        cov = len({(r.get("start"), r["end"]) for r in rows if "end" in r})
        if cov > best_cov:  # strict > => ties keep the earlier (higher-priority) tag
            best_cov = cov
            best = (taxonomy, tag)
    return best


def _eligible_rows(
    facts_json: dict, taxonomy: str, tag: str, unit: str, as_of: date | None
) -> list[dict]:
    """Rows a report dated `as_of` was entitled to see. One filter, applied
    before anything reads the data — tag selection included."""
    rows = _rows(facts_json, taxonomy, tag, unit)
    if as_of is None:
        return rows
    out = []
    for r in rows:
        try:
            filed = _parse_date(r["filed"])
        except (KeyError, ValueError, TypeError):
            continue  # a fact with no usable filed date cannot be dated; drop it
        if filed <= as_of:
            out.append(r)
    return out


def is_composite_selection(selected: str) -> bool:
    """True when the mapper BUILT this field by summing several tags rather
    than reading one. Recorded as bare tag names joined by `+`, e.g.
    `SellingAndMarketingExpense+GeneralAndAdministrativeExpense`."""
    return "+" in selected


def _parse_selection(selected: str) -> list[tuple[str, str]]:
    """Expand `FieldDiagnostic.tag_used` into the series it names.

    A composite returns all of its components, which are then AGGREGATED (see
    `_composite_vintages`) rather than reported individually. Reporting one
    component as a revision of the derived field states a number that was
    never scored: with SG&A = S&M 1000 + G&A 10, a G&A move of 10 -> 11 reads
    as `sga_expense: 10 -> 11`, a 10% revision clearing the 1% materiality
    bar, while the sga_expense the engine scored went 1010 -> 1011, 0.099%.
    """
    parts: list[tuple[str, str]] = []
    for piece in selected.split("+"):
        piece = piece.strip()
        if not piece or piece == "none":
            continue
        taxonomy, sep, tag = piece.partition(":")
        if sep:
            if taxonomy and tag:
                parts.append((taxonomy, tag))
        else:
            # Composite components are recorded unqualified; every tag the
            # mapper composes from is us-gaap (SGA_COMPONENTS, DA_COMPONENTS,
            # the debt tags), pinned by test so the assumption cannot rot.
            parts.append(("us-gaap", piece))
    return parts


def _composite_vintages(
    facts_json: dict,
    components: list[tuple[str, str]],
    unit: str,
    as_of: date | None,
) -> dict[tuple[date | None, date], list[tuple[date, float, str, str, frozenset[str]]]]:
    """Rebuild a summed field's value as it stood at each filing vintage.

    For every period, a vintage is any date on which some component was filed.
    The aggregate at that vintage sums each component's latest value filed on
    or before it — so a component the amendment did not re-report carries
    forward, which is what the mapper does and therefore what was scored.

    The contributing component set travels with each vintage. A vintage where
    a component is simply ABSENT (a tag the filer had not started using) is a
    change in how the figure is COMPOSED, not a revision of it, and comparing
    across that boundary would manufacture a restatement out of a taxonomy
    change — the failure this module's header already warns about for single
    tags. The caller drops such pairs.
    """
    per_component: dict[tuple[str, str], dict[tuple[date | None, date], list]] = {}
    for taxonomy, tag in components:
        rows: dict[tuple[date | None, date], list] = {}
        for e in _eligible_rows(facts_json, taxonomy, tag, unit, as_of):
            try:
                key = (_parse_date(e["start"]) if "start" in e else None, _parse_date(e["end"]))
                rows.setdefault(key, []).append(
                    (_parse_date(e["filed"]), float(e["val"]),
                     e.get("form", ""), e.get("accn", ""))
                )
            except (KeyError, ValueError, TypeError):
                continue
        per_component[(taxonomy, tag)] = rows

    out: dict[tuple[date | None, date], list] = {}
    keys = {k for rows in per_component.values() for k in rows}
    for key in keys:
        vintages = sorted({f[0] for rows in per_component.values() for f in rows.get(key, [])})
        for vintage in vintages:
            total = 0.0
            present: set[str] = set()
            form = accn = ""
            filed_today: list[tuple[str, str]] = []
            # Summed in a fixed tag order, not the order the selection string
            # happened to list the components. Float addition is not
            # associative, so iteration order moved the aggregate by ~1e-13 —
            # never enough to flip a materiality decision, but enough that the
            # same report did not reproduce byte-identically, which is the one
            # property a point-in-time artifact is supposed to have.
            for (taxonomy, tag), rows in sorted(per_component.items()):
                # Latest value filed on or before this vintage. Same tie rule
                # as the mapper: max() keeps the FIRST fact at the latest date.
                eligible = [f for f in rows.get(key, []) if f[0] <= vintage]
                if not eligible:
                    continue
                latest = max(eligible, key=lambda f: f[0])
                total += latest[1]
                present.add(f"{taxonomy}:{tag}")
                if latest[0] == vintage:
                    filed_today.append((latest[2], latest[3]))
            # Provenance across EVERY component filed on this date, not the
            # first one encountered. Several components can move a summed
            # figure on the same day through different filings, and taking
            # whichever happened to be iterated first would attribute the
            # aggregate to an ordinary 10-Q while the /A that actually moved
            # it was dropped — downgrading a high-confidence amendment event
            # to a routine comparative revision. An amendment wins; ties among
            # equals break on accession so the choice is deterministic.
            if filed_today:
                form, accn = min(
                    filed_today, key=lambda fa: (not fa[0].endswith("/A"), fa[1])
                )
            if present:
                out.setdefault(key, []).append(
                    (vintage, total, form, accn, frozenset(present))
                )
    return out


def _resolve_tags(
    facts_json: dict,
    field_name: str,
    candidates: tuple[tuple[str, str], ...],
    unit: str,
    as_of: date | None,
    selected_tags: Mapping[str, str | None] | None,
) -> list[tuple[str, str]]:
    """The series to inspect for `field_name`: the mapper's own selection when
    the caller supplied one, else the coverage approximation.

    A supplied selection is authoritative even when it is None — the mapper
    found no usable series for that field, so there is nothing the engine
    scored and nothing to report a revision against. Falling back to the
    approximation there would resurrect precisely the mismatch this argument
    exists to prevent.
    """
    if selected_tags is not None and field_name in selected_tags:
        selected = selected_tags[field_name]
        return _parse_selection(selected) if selected else []
    active = _active_tag(facts_json, candidates, unit, as_of)
    return [active] if active is not None else []


def _fields_to_inspect(
    selected_tags: Mapping[str, str | None] | None,
) -> dict[str, tuple[tuple[str, str], ...]]:
    """Every canonical field whose revision history should be checked.

    The candidate tables are not the whole set the engine scores. `total_debt`
    is assembled by `_total_debt_series` from the debt and finance-lease tag
    groups and recorded like any other field, but it appears in neither
    INSTANT_FIELDS nor FLOW_FIELDS — so iterating those tables alone meant the
    engine could score a revised debt total while the restatement appendix
    reported nothing for it. Silence there reads as "no revisions" for one of
    the most consequential figures on the balance sheet.

    Any field the mapper reports a selection for is therefore inspected, with
    no candidate tags of its own: the mapper's choice is the only authority on
    what a field outside the tables was built from, so there is nothing for
    the coverage fallback to approximate. A field the mapper recorded but
    mapped NOTHING for is listed too — `_resolve_tags` yields no series for
    it, so it produces no footprint, but the scan must name it as a gap
    rather than let its absence read as "checked".
    """
    fields: dict[str, tuple[tuple[str, str], ...]] = {**INSTANT_FIELDS, **FLOW_FIELDS}
    for name in selected_tags or {}:
        if name not in fields:
            fields[name] = ()
    return fields


def detect_restatements(
    facts_json: dict,
    materiality_pct: float = DEFAULT_MATERIALITY_PCT,
    period_since: date | None = None,
    as_of: date | None = None,
    selected_tags: Mapping[str, str | None] | None = None,
) -> list[RestatementFootprint]:
    """The footprints of `scan_restatements` alone, for callers that only
    consume revisions (the vintage store, tests). Anything that RENDERS a
    result must take the scan: the footprint list cannot say what it did not
    cover, and an empty list is not a clean bill."""
    return scan_restatements(
        facts_json, materiality_pct, period_since, as_of=as_of, selected_tags=selected_tags
    ).footprints


def scan_restatements(
    facts_json: dict,
    materiality_pct: float = DEFAULT_MATERIALITY_PCT,
    period_since: date | None = None,
    as_of: date | None = None,
    selected_tags: Mapping[str, str | None] | None = None,
) -> RestatementScan:
    """Find same-period figures a later filing revised beyond `materiality_pct`,
    and account for every field the check could or could not cover.

    For each canonical field's candidate tags, group facts by (start, end) and
    compare the earliest-filed value (as originally reported) with the
    latest-filed value (as it now stands). Materially different pairs become
    provenance-linked footprints. Restatements sharing one XBRL tag (e.g.
    OperatingIncomeLoss backing both `ebit` and `operating_income`) are reported
    once.

    `period_since` restricts to periods ending on/after that date — the live
    monitor cares about revisions to recent periods, not a 2010 reclassification.
    `as_of` drops every fact FILED after that date, so a dated report sees the
    trail exactly as it stood then. It must filter the facts, not the finished
    footprints: a later comparative moves `current_filed` forward, and
    discarding the footprint afterwards would erase an amendment that WAS
    known at the report date. The filter applies before TAG SELECTION too, or
    a series filed years later decides which tag a historical report inspects.

    `selected_tags` maps a canonical field name to the qualified tag the mapper
    actually scored (`FieldDiagnostic.tag_used`). Supply it whenever the caller
    has run the mapper: the evidence then names the same series as the score,
    which is the contract this module claims. Without it, `_active_tag`
    approximates the choice and can diverge — a legacy tag with a long history
    of periods outside the report window beats the tag actually scored.
    Split-adjusted share fields are excluded (see SPLIT_ADJUSTED_FIELDS).
    """
    footprints: list[RestatementFootprint] = []
    seen: set[tuple[str, date | None, date]] = set()  # (tag, start, end) dedupe
    inspected: list[str] = []
    uninspected: dict[str, str] = {}
    excluded: dict[str, str] = {}

    for field_name, candidates in _fields_to_inspect(selected_tags).items():
        if field_name in SPLIT_ADJUSTED_FIELDS:
            excluded[field_name] = "split-adjusted share count"
            continue
        unit = _unit_for(field_name)
        # Only inspect the tag the mapper actually SCORES for this field — the
        # best-coverage candidate (round-9 finding). Reporting a revision on a
        # non-scored candidate tag would show a current_value that disagrees with
        # the engine, and iterating every candidate would double-report a field
        # when two tags both carry a same-period revision.
        series = _resolve_tags(facts_json, field_name, candidates, unit, as_of, selected_tags)
        if not series:
            # Nothing was compared, so nothing can be said. Which kind of
            # nothing matters to the reader: the mapper scored without this
            # field (a coverage gap upstream) vs. the approximation found no
            # tag with facts (the engine may still have scored it).
            if selected_tags is not None and field_name in selected_tags:
                uninspected[field_name] = "no series mapped this run"
            else:
                uninspected[field_name] = (
                    "no candidate tag with facts"
                    + (f" filed by {as_of}" if as_of is not None else "")
                )
            continue
        # A field the mapper SUMMED is compared as the sum. Its components are
        # never reported individually: a 10% move in a small component is not
        # a 10% revision of the figure the engine scored, and materiality
        # applied to the part rather than the whole promotes rounding into a
        # restatement. `_composite_vintages` rebuilds the total at each filing
        # date; from here the comparison is identical to a single tag's.
        composite = len(series) > 1
        groups: list[tuple[str, dict]] = []
        if composite:
            qualified = "+".join(f"{tax}:{tag}" for tax, tag in series)
            groups.append((qualified, _composite_vintages(facts_json, series, unit, as_of)))
        else:
            for taxonomy, tag in series:
                by_key: dict[tuple[date | None, date], list] = {}
                for e in _eligible_rows(facts_json, taxonomy, tag, unit, as_of):
                    try:
                        key = (_parse_date(e["start"]) if "start" in e else None,
                               _parse_date(e["end"]))
                        by_key.setdefault(key, []).append(
                            (_parse_date(e["filed"]), float(e["val"]),
                             e.get("form", ""), e.get("accn", ""))
                        )
                    except (KeyError, ValueError, TypeError):
                        continue
                groups.append((f"{taxonomy}:{tag}", by_key))

        if not any(by_key for _tag, by_key in groups):
            # A resolved series with no eligible fact at all (unit mismatch,
            # or every fact filed after `as_of`) compared nothing.
            uninspected[field_name] = (
                "selected series has no eligible facts"
                + (f" filed by {as_of}" if as_of is not None else "")
            )
            continue
        inspected.append(field_name)

        for qualified_tag, by_key in groups:
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
                current = max(filings, key=lambda f: f[0])  # latest filed = mapper's value

                if composite:
                    # A vintage where some component did not yet exist
                    # describes a differently COMPOSED figure, not a revised
                    # one; comparing across that boundary would turn a filer
                    # adopting a new tag into a restatement.
                    #
                    # But discarding the whole period on that basis threw away
                    # every later revision too: adopt G&A (1000 -> 1500), then
                    # amend it via 10-Q/A (1500 -> 1700), and the earliest and
                    # latest vintages have different component sets, so a
                    # genuine 13% amendment vanished. The comparison belongs
                    # INSIDE the stable segment — the trailing run of vintages
                    # that share the composition the mapper scores today.
                    stable = [f for f in filings if f[4] == current[4]]
                    if len(stable) < 2:
                        continue
                    filings = stable

                orig = min(filings, key=lambda f: f[0])  # earliest filed

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
    return RestatementScan(
        footprints=footprints,
        inspected=tuple(inspected),
        uninspected=uninspected,
        excluded=excluded,
        as_of=as_of,
        period_since=period_since,
        materiality_pct=materiality_pct,
    )


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


def render_restatements_section(scan: RestatementScan) -> str:
    """Markdown section for the report. Evidence framing only — no scoring.

    Takes the SCAN, not its footprints: the section must say which fields were
    compared before it can say none of them moved. An unqualified "no revisions
    detected" over a partial inspection is the false clean bill this module
    exists to prevent, so the empty case is always scoped to the inspected set
    and the uninspected fields are named on every run, findings or not.

    Amended-filing (/A) revisions are surfaced as high-confidence restatements;
    other same-period revisions are surfaced separately with a caveat, because a
    large non-amendment swing on a flow item is often a discontinued-operations
    or spinoff re-presentation rather than an accounting-error correction.
    """
    footprints = scan.footprints
    lines = ["## Prior-Period Restatements (evidence — not scored)", ""]
    lines.append(
        "Revisions to previously reported figures, recovered from the companyfacts "
        "filing history (original vs latest-filed value for the same period). "
        "Share counts are excluded (stock-split noise)."
    )
    lines.append("")
    lines.append(f"- Coverage: {scan.coverage_line()}.")
    if scan.incomplete:
        lines.append(
            f"- ⚠ Incomplete: {len(scan.uninspected)} field(s) could not be inspected. "
            "Their absence below is a data gap, not evidence of no revision."
        )
    lines.append("")
    if not footprints:
        lines.append(
            f"- No revisions detected above the {scan.materiality_pct:.0%} materiality "
            f"threshold among the {len(scan.inspected)} inspected field(s)."
        )
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
