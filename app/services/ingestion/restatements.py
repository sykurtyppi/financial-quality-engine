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

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta

from app.services.ingestion.companyfacts_mapper import (
    FLOW_FIELDS,
    INSTANT_FIELDS,
    _parse_date,
    _unit_for,
)
from app.services.ingestion.composition import compose_total_debt, resolve_by_strategy
from app.services.ingestion.fields import FIELDS
from app.services.ingestion.payloads import concept_rows
from app.services.ingestion.precedence import Rank, conflicts, earliest, latest, rank
from app.services.ingestion.selection import Composer, SeriesSelection, parse_components

# Relative change below which a same-period revision is treated as rounding or
# an immaterial reclassification rather than a restatement. The XBRL survey
# measured a 3.4-6.7% base rate of benign changes; 1% cleanly separates the
# 2008 AAPL Assets restatement (8.6%) from a $1M/$3.5B (0.03%) reclassification.
DEFAULT_MATERIALITY_PCT = 0.01

# Share counts are routinely restated by stock splits / reverse splits — a
# neutral corporate action, not an accounting restatement. Excluded to avoid a
# flood of split-adjustment false positives (Apple's 2014 7-for-1 split makes
# every prior share count appear "revised" +600%).
# The registry flags them (`FieldSpec.split_adjusted`).
SPLIT_ADJUSTED_FIELDS = frozenset(f.name for f in FIELDS if f.split_adjusted)
_REGISTRY_FIELDS = frozenset(f.name for f in FIELDS)


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
        # An amendment that a later filing reversed leaves the figure where it
        # started: it is reported for the /A event, and moved neither way.
        if self.current_value > self.original_value:
            return "up"
        if self.current_value < self.original_value:
            return "down"
        return "unchanged"

    @property
    def is_amendment(self) -> bool:
        """True iff a material /A amendment touched this period (regardless of
        the form carrying the current value)."""
        return self.amendment_accession is not None


@dataclass(frozen=True)
class SameDayConflict:
    """Facts for one period filed on one day, at one amendment level, that
    disagree. Companyfacts records dates, not times, so which of them counts
    is the `precedence` convention (highest accession) — reported so a
    reader knows the choice was a convention. Never a revision, never
    promoted."""

    field_name: str
    tag: str  # the component concept that carries the conflict
    period_end: date
    period_start: date | None
    filed: date
    amended: bool
    values: tuple[float, ...]
    accessions: tuple[str, ...]


@dataclass(frozen=True)
class DerivedRevision:
    """A derived quarter (a year-to-date difference, the year less three
    quarters, or a sum of components) whose scored value moved materially
    between the filings behind it, reconstructed by re-running the mapper
    as of each filing date. The raw-fact check can miss it: a 0.9% revision
    to H1 is below materiality, but moves a Q2 of 1 to 1.9. Evidence, not a
    verdict: `moved_by` names the filings made on the date it last moved."""

    field_name: str
    period_start: date | None
    period_end: date
    method: str
    original_value: float
    original_filed: date
    current_value: float
    current_filed: date
    moved_by: tuple[tuple[str, str], ...]  # (form, accession) filed that day

    @property
    def pct_change(self) -> float | None:
        if self.original_value == 0:
            return None
        return (self.current_value - self.original_value) / abs(self.original_value)

    @property
    def is_amendment(self) -> bool:
        return any(form.endswith("/A") for form, _accn in self.moved_by)


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
    conflicts: tuple[SameDayConflict, ...] = ()
    derived: tuple[DerivedRevision, ...] = ()

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
    # Shape-checked: a malformed payload raises ExternalPayloadError here
    # instead of an AttributeError somewhere downstream. Includes the
    # shares-filed-under-USD fallback.
    return concept_rows(facts_json, taxonomy, tag, unit)


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


def _compose_debt(present: Mapping[str, float]) -> tuple[float, tuple[str, ...]] | None:
    c = compose_total_debt(present)
    return None if c is None else (c.total, c.used)


def _resolver(name: str) -> Callable[[Mapping[str, float]], tuple[float, tuple[str, ...]] | None]:
    def compose(present: Mapping[str, float]) -> tuple[float, tuple[str, ...]] | None:
        r = resolve_by_strategy(name, present)
        return None if r is None else (r.total, r.used)
    return compose


def composer_of(
    selection: SeriesSelection,
) -> Callable[[Mapping[str, float]], tuple[float, tuple[str, ...]] | None] | None:
    """The per-date rule that rebuilds the figure the selection names — the
    mapper's own (composition.py) — or None for a single concept, which is
    compared fact by fact. Taken from the selection, not the field's name:
    what the mapper did is the authority."""
    if selection.composer is Composer.DEBT:
        return _compose_debt
    if selection.composer is Composer.STRATEGY:
        return _resolver(selection.field)
    return None


# What callers pass as the mapper's selection: `IngestionDiagnostics.
# selected_series()` (objects), or the legacy `selected_tags()` strings.
Selected = Mapping[str, "SeriesSelection | str | None"]


def _as_selection(field_name: str, selected: SeriesSelection | str | None) -> SeriesSelection | None:
    if selected is None or isinstance(selected, SeriesSelection):
        return selected
    return SeriesSelection.from_tag_used(field_name, selected)


def _trail(
    facts_json: dict, taxonomy: str, tag: str, unit: str, as_of: date | None
) -> dict[tuple[date | None, date], list[tuple[date, float, str, str]]]:
    """Every eligible filing of each period of one concept, as
    `(filed, value, form, accession)` in companyfacts order."""
    rows: dict[tuple[date | None, date], list[tuple[date, float, str, str]]] = {}
    for e in _eligible_rows(facts_json, taxonomy, tag, unit, as_of):
        try:
            key = (_parse_date(e["start"]) if "start" in e else None, _parse_date(e["end"]))
            rows.setdefault(key, []).append(
                (_parse_date(e["filed"]), float(e["val"]), e.get("form", ""), e.get("accn", ""))
            )
        except (KeyError, ValueError, TypeError):
            continue
    return rows


def _order(f: tuple) -> Rank:
    """Current-fact order (`precedence`) of a trail row `(filed, value, form,
    accession, ...)`. A composite vintage carries its own rank as element 5:
    its form and accession describe what was filed at that level, which can
    be nothing the composer counted."""
    return f[5] if len(f) > 5 else rank(f[0], f[2], f[3])


def _composite_vintages(
    facts_json: dict,
    components: list[tuple[str, str]],
    unit: str,
    as_of: date | None,
    *,
    compose: Callable[[Mapping[str, float]], tuple[float, tuple[str, ...]] | None] | None = None,
) -> dict[tuple[date | None, date], list[tuple[date, float, str, str, frozenset[str], Rank]]]:
    """Rebuild a summed field's value as it stood at each filing vintage.

    For every period, a vintage is any LEVEL — filing date and amendment
    status (`precedence.level`) — at which some component was filed. The
    aggregate at that vintage sums each component's current value at or
    below it — so a component the amendment did not re-report carries
    forward, which is what the mapper does and therefore what was scored.
    An original and its amendment filed on one day are two vintages, not
    one: keyed by date alone, the amendment's state was never built and a
    real 110 -> 160 restatement disappeared (Hermes round 3).

    The contributing component set travels with each vintage. A vintage where
    a component is simply ABSENT (a tag the filer had not started using) is a
    change in how the figure is COMPOSED, not a revision of it, and comparing
    across that boundary would manufacture a restatement out of a taxonomy
    change — the failure this module's header already warns about for single
    tags. The caller drops such pairs.
    """
    per_component: dict[tuple[str, str], dict[tuple[date | None, date], list]] = {
        (taxonomy, tag): _trail(facts_json, taxonomy, tag, unit, as_of)
        for taxonomy, tag in components
    }

    out: dict[tuple[date | None, date], list] = {}
    keys = {k for rows in per_component.values() for k in rows}
    for key in keys:
        levels = sorted({_order(f)[:2] for rows in per_component.values() for f in rows.get(key, [])})
        for lvl in levels:
            vintage = lvl[0]
            # Current value of each component at or below this level, by the
            # mapper's own order (`precedence`). Iterated in a fixed tag order, not the order the
            # selection string happened to list the components: float
            # addition is not associative, and iteration order once moved
            # the aggregate by ~1e-13 — never enough to flip a materiality
            # decision, but enough that the same report did not reproduce
            # byte-identically.
            latest_by: dict[tuple[str, str], tuple] = {}
            for (taxonomy, tag), rows in sorted(per_component.items()):
                eligible = [f for f in rows.get(key, []) if _order(f)[:2] <= lvl]
                if eligible:
                    latest_by[(taxonomy, tag)] = latest(eligible, key=_order)
            if compose is not None:
                # Not a sum of whatever was filed: aggregate current debt
                # excludes the current portion, an aggregate D&A tag
                # excludes its components. The mapper's own per-date rule
                # decides which components count.
                composed = compose({tag: f[1] for (_tax, tag), f in latest_by.items()})
                if composed is None:
                    continue
                total, used = composed
                counted = {k: f for k, f in latest_by.items() if k[1] in used}
            else:
                counted = latest_by
                total = 0.0
                for f in counted.values():
                    total += f[1]
            present = {f"{taxonomy}:{tag}" for taxonomy, tag in counted}
            filed_today = [(f[2], f[3]) for f in counted.values() if _order(f)[:2] == lvl]
            form = accn = ""
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
                    (vintage, total, form, accn, frozenset(present), (lvl[0], lvl[1], accn))
                )
    return out


def _composer_for(
    field_name: str, selected_tags: Selected | None
) -> Callable[[Mapping[str, float]], tuple[float, tuple[str, ...]] | None] | None:
    """The rule the mapper composed `field_name` with — from its selection
    when supplied, else from the registry (the approximation path)."""
    if field_name not in _REGISTRY_FIELDS:
        return None
    selection = _as_selection(field_name, (selected_tags or {}).get(field_name))
    return composer_of(selection or SeriesSelection.of(field_name, ()))


def _resolve_tags(
    facts_json: dict,
    field_name: str,
    candidates: tuple[tuple[str, str], ...],
    unit: str,
    as_of: date | None,
    selected_tags: Selected | None,
) -> list[tuple[str, str]]:
    """The series to inspect for `field_name`: the mapper's own selection when
    the caller supplied one (an object, or a legacy string — its unqualified
    pieces are us-gaap), else the coverage approximation.

    A supplied selection is authoritative even when it is None — the mapper
    found no usable series for that field, so there is nothing the engine
    scored and nothing to report a revision against. Falling back to the
    approximation there would resurrect precisely the mismatch this argument
    exists to prevent.
    """
    if selected_tags is not None and field_name in selected_tags:
        selected = selected_tags[field_name]
        if isinstance(selected, SeriesSelection):
            return selected.concepts
        return [(t, c) for t, _, c in (x.partition(":") for x in parse_components(selected))]
    active = _active_tag(facts_json, candidates, unit, as_of)
    return [active] if active is not None else []


def _fields_to_inspect(
    selected_tags: Selected | None,
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
    selected_tags: Selected | None = None,
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
    selected_tags: Selected | None = None,
    n_quarters: int = 8,
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

    `selected_tags` maps a canonical field name to what the mapper actually
    scored: `IngestionDiagnostics.selected_series()` (objects, preferred), or
    the legacy `selected_tags()` strings. Supply it whenever the caller
    has run the mapper: the evidence then names the same series as the score,
    which is the contract this module claims. Without it, `_active_tag`
    approximates the choice and can diverge — a legacy tag with a long history
    of periods outside the report window beats the tag actually scored.
    Split-adjusted share fields are excluded (see SPLIT_ADJUSTED_FIELDS).

    `n_quarters` is the window the report scores (the mapper's own default
    is 8). Derived quarters are rebuilt over exactly that window: the mapper
    ranks candidate tags on the window it is given, so a wider rebuild can
    follow a series the report does not score.
    """
    footprints: list[RestatementFootprint] = []
    seen: set[tuple[str, date | None, date]] = set()  # (tag, start, end) dedupe
    inspected: list[str] = []
    uninspected: dict[str, str] = {}
    excluded: dict[str, str] = {}
    same_day: list[SameDayConflict] = []

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
            groups.append((qualified, _composite_vintages(
                facts_json, series, unit, as_of, compose=_composer_for(field_name, selected_tags)
            )))
        else:
            for taxonomy, tag in series:
                groups.append((f"{taxonomy}:{tag}", _trail(facts_json, taxonomy, tag, unit, as_of)))

        if not any(by_key for _tag, by_key in groups):
            # A resolved series with no eligible fact at all (unit mismatch,
            # or every fact filed after `as_of`) compared nothing.
            uninspected[field_name] = (
                "selected series has no eligible facts"
                + (f" filed by {as_of}" if as_of is not None else "")
            )
            continue
        if period_since is not None and not any(
            end >= period_since for _tag, by_key in groups for (_start, end) in by_key
        ):
            # Facts exist, but none for a period the scan covers: nothing in
            # the window was compared, so the field was not inspected (Hermes
            # audit round 4, finding 3).
            uninspected[field_name] = f"selected series has no period on or after {period_since}"
            continue
        inspected.append(field_name)

        # Same-day disagreements, per component concept: in a summed field
        # the component is where the choice was made.
        for taxonomy, tag in series:
            for (start, end), trail in _trail(facts_json, taxonomy, tag, unit, as_of).items():
                if period_since is not None and end < period_since:
                    continue
                for group in conflicts(trail, key=_order, value=lambda f: f[1]):
                    same_day.append(SameDayConflict(
                        field_name=field_name, tag=f"{taxonomy}:{tag}",
                        period_end=end, period_start=start,
                        filed=group[0][0], amended=group[0][2].endswith("/A"),
                        values=tuple(f[1] for f in group),
                        accessions=tuple(f[3] for f in group),
                    ))

        for qualified_tag, by_key in groups:
            for (start, end), filings in by_key.items():
                if len(filings) < 2 or (qualified_tag, start, end) in seen:
                    continue
                if period_since is not None and end < period_since:
                    continue
                # `filings` is in companyfacts order (same source the mapper
                # reads). `current` MUST resolve same-day ties the way the
                # mapper's _dedupe_latest_filed does — the shared `precedence`
                # order, keeping the FIRST fact on a full tie (round-8 finding:
                # sort()+filings[-1] picked the LAST same-day fact and diverged
                # from scoring).
                current = latest(filings, key=_order)  # the mapper's value

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

                orig = earliest(filings, key=_order)  # as first reported
                # A revision crosses filing levels. Facts sharing the original's
                # date and amendment status that disagree with it are a
                # same-day conflict (listed in `scan.conflicts`), not a later
                # filing changing an earlier one — without this, the accession
                # tie-break would turn one day's ambiguity into a "revision".
                first_level = _order(orig)[:2]
                filings = [f for f in filings if f is orig or _order(f)[:2] != first_level]
                if len(filings) < 2:
                    continue

                # Keep the CURRENT value and the amendment EVENT separate (round-7).
                # `material` = every filing that materially deviates from the
                # originally reported value; the amendment event is the latest
                # material /A filing in the trail, independent of which form
                # carries the current value.
                def _pct(v: float, base: float = orig[1]) -> float | None:
                    return None if base == 0 else abs(v - base) / abs(base)

                material = [
                    f
                    for f in filings
                    if f[1] != orig[1] and ((p := _pct(f[1])) is None or p >= materiality_pct)
                ]
                if not material:
                    continue
                material_amendments = [f for f in material if f[2].endswith("/A")]
                amendment = latest(material_amendments, key=_order) if material_amendments else None
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
    # A quarter already reported from its own filed figure is not repeated.
    reported = {(f.field_name, f.period_end) for f in footprints if f.period_start is not None}
    derived = [
        d for d in derived_revisions(
            facts_json, as_of=as_of, period_since=period_since,
            materiality_pct=materiality_pct, n_quarters=n_quarters,
        )
        if (d.field_name, d.period_end) not in reported
    ]
    return RestatementScan(
        footprints=footprints,
        inspected=tuple(inspected),
        uninspected=uninspected,
        excluded=excluded,
        as_of=as_of,
        period_since=period_since,
        materiality_pct=materiality_pct,
        conflicts=tuple(sorted(
            same_day, key=lambda c: (c.period_end, c.field_name, c.tag, c.filed), reverse=True
        )),
        derived=tuple(derived),
    )


def _dated_copy(facts_json: dict, cutoff: date | None) -> dict:
    """The fact rows filed on or before `cutoff` (all dated rows when None),
    keeping only well-formed levels and rows with a real `filed` date. The
    raw scan above is what reports a malformed payload; this re-reading
    only has to never trip over one."""
    out: dict = {"entityName": facts_json.get("entityName"), "facts": {}}
    taxonomies = facts_json.get("facts")
    if not isinstance(taxonomies, dict):
        return out
    for taxonomy, tags in taxonomies.items():
        if not isinstance(tags, dict):
            continue
        for tag, concept in tags.items():
            units = concept.get("units") if isinstance(concept, dict) else None
            if not isinstance(units, dict):
                continue
            kept_units: dict = {}
            for unit, rows in units.items():
                if not isinstance(rows, list):
                    continue
                kept = []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    try:
                        filed = _parse_date(row["filed"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if cutoff is None or filed <= cutoff:
                        kept.append(row)
                if kept:
                    kept_units[unit] = kept
            if kept_units:
                out["facts"].setdefault(taxonomy, {})[tag] = {"units": kept_units}
    return out


# How the mapper builds a quarter it did not find reported as a quarter.
_DERIVED_METHODS = frozenset({"ytd_diff", "fy_minus_3q", "composite"})


def derived_revisions(
    facts_json: dict,
    *,
    as_of: date | None,
    period_since: date | None,
    materiality_pct: float = DEFAULT_MATERIALITY_PCT,
    n_quarters: int = 8,
) -> list[DerivedRevision]:
    """Derived quarters whose SCORED value moved materially across the
    filings behind them (Hermes audit round 4, finding 2).

    The raw-fact check compares each filed figure with its own earlier
    filings. A quarter the engine derives is never filed as such, so a
    revision below materiality on the figure it is derived from can move it
    by far more, unseen. Here the mapper itself is re-run as of every date
    a fact behind such a quarter was filed; each quarter's trail of scored
    values is compared like a filed figure's. Only the trailing run built
    from the same components is compared: a component appearing is a change
    in how the figure is composed, not a revision of it."""
    from app.services.ingestion.companyfacts_mapper import build_dataset

    facts = _dated_copy(facts_json, as_of)
    try:
        current, diag = build_dataset(facts, "scan", n_quarters=n_quarters)
    except ValueError:
        return []
    targets: dict[tuple[str, date], tuple[str, tuple[str, ...], float, date | None]] = {}
    for i, period in enumerate(current.periods):
        if period_since is not None and period.period_end < period_since:
            continue
        # A derived quarter has no filed start date: it runs from the day
        # after the previous quarter end.
        start = current.periods[i - 1].period_end + timedelta(days=1) if i else None
        for fd in diag.fields:
            src = fd.period_sources.get(period.period_end.isoformat())
            value = getattr(period, fd.field_name, None)
            if src is None or value is None or src.method not in _DERIVED_METHODS:
                continue
            targets[(fd.field_name, period.period_end)] = (
                src.method, tuple(src.components), value, start,
            )
    if not targets:
        return []

    # Every date a fact behind a target quarter was filed: the vintages at
    # which its scored value could have changed.
    by_concept: dict[str, list[dict]] = {}
    days: set[date] = set()
    for (field_name, end), (_m, components, _v, _s) in targets.items():
        for concept in components:
            if concept not in by_concept:
                taxonomy, _, tag = concept.partition(":")
                by_concept[concept] = concept_rows(facts, taxonomy, tag, _unit_for(field_name))
            for row in by_concept[concept]:
                try:
                    row_end, filed = _parse_date(row["end"]), _parse_date(row["filed"])
                except (KeyError, TypeError, ValueError):
                    continue
                if (end - row_end).days <= 400 and row_end <= end:
                    days.add(filed)

    trails: dict[tuple[str, date], list[tuple[date, float, tuple[str, ...]]]] = {}
    for day in sorted(days):
        try:
            ds, d_diag = build_dataset(_dated_copy(facts, day), "scan", n_quarters=n_quarters)
        except ValueError:
            continue  # not enough history yet to establish quarter ends
        by_end = {p.period_end: p for p in ds.periods}
        for (field_name, end) in targets:
            then = by_end.get(end)
            value = getattr(then, field_name, None) if then is not None else None
            if value is None:
                continue
            src = d_diag.field_by_name(field_name).period_sources.get(end.isoformat())
            components = tuple(src.components) if src is not None else ()
            trail = trails.setdefault((field_name, end), [])
            if not trail or trail[-1][1] != value or trail[-1][2] != components:
                trail.append((day, value, components))

    found: list[DerivedRevision] = []
    seen: set[tuple[tuple[str, ...], date]] = set()  # one figure behind two fields: once
    for key, trail in trails.items():
        method, components, _value, start = targets[key]
        stable: list[tuple[date, float, tuple[str, ...]]] = []
        for entry in reversed(trail):  # the trailing run with today's components
            if entry[2] != components:
                break
            stable.insert(0, entry)
        if len(stable) < 2:
            continue
        orig, cur = stable[0], stable[-1]
        base = abs(orig[1])
        moved = abs(cur[1] - orig[1])
        if base == 0 or moved / base < materiality_pct:
            continue
        field_name, end = key
        if (components, end) in seen:
            continue
        seen.add((components, end))
        moved_by = tuple(dict.fromkeys(
            (str(row.get("form", "")), str(row.get("accn", "")))
            for concept in components for row in by_concept.get(concept, [])
            if str(row.get("filed", "")) == cur[0].isoformat()
        ))
        found.append(DerivedRevision(
            field_name=field_name, period_start=start, period_end=end, method=method,
            original_value=orig[1], original_filed=orig[0],
            current_value=cur[1], current_filed=cur[0], moved_by=moved_by,
        ))
    found.sort(key=lambda d: (d.period_end, d.field_name), reverse=True)
    return found


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
    if scan.conflicts:
        lines.append(
            f"- ⚠ {len(scan.conflicts)} same-day conflict(s): one day's filings report "
            "different values for one period, so which counts is a convention, not "
            "something the filings establish. Not revisions; listed at the end."
        )
    lines.append("")
    if not footprints:
        if scan.derived:
            lines.append(
                f"- No filed figure was revised above the {scan.materiality_pct:.0%} "
                f"materiality threshold, but {len(scan.derived)} derived quarter(s) moved "
                "(below)."
            )
        else:
            lines.append(
                f"- No revisions detected above the {scan.materiality_pct:.0%} materiality "
                f"threshold among the {len(scan.inspected)} inspected field(s)."
            )
        lines.extend(_derived_lines(scan.derived))
        lines.extend(_conflict_lines(scan.conflicts))
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
    lines.extend(_derived_lines(scan.derived))
    lines.extend(_conflict_lines(scan.conflicts))
    return "\n".join(lines)


_METHOD_LABELS = {
    "ytd_diff": "year-to-date less earlier quarters",
    "fy_minus_3q": "fiscal year less three quarters",
    "composite": "sum of components",
}


def _derived_lines(found: tuple[DerivedRevision, ...]) -> list[str]:
    """Quarters the engine derives whose scored value moved: invisible to
    the filed-figure comparison above when the figure it is derived from
    moved by less than materiality (Hermes audit round 4, finding 2)."""
    if not found:
        return []
    lines = [
        "",
        "### Derived quarters that moved (rebuilt from the filings behind them)",
        "",
        "_Quarters the engine derives rather than reads, rebuilt as of each date a "
        "filing behind them was made. A small revision to a year-to-date or annual "
        "figure can move a derived quarter by far more. An amended filing (/A) behind "
        "the move promotes it to the card; otherwise it is context._",
        "",
        "| Period | Field | Derived as | Original (as of) | Current (as of) | Change | Moved by |",
        "|---|---|---|---|---|---|---|",
    ]
    for d in found[:_MAX_ROWS]:
        pct = f"{d.pct_change * 100:+.1f}%" if d.pct_change is not None else "n/a"
        moved = ", ".join(f"{form} {accn}" for form, accn in d.moved_by) or "—"
        lines.append(
            f"| {d.period_end} | {d.field_name} | {_METHOD_LABELS.get(d.method, d.method)} "
            f"| {d.original_value:,.0f} ({d.original_filed}) | {d.current_value:,.0f} "
            f"({d.current_filed}) | {pct} | {moved} |"
        )
    if len(found) > _MAX_ROWS:
        lines.append(f"| … | {len(found) - _MAX_ROWS} more | | | | | |")
    return lines


def _conflict_lines(found: tuple[SameDayConflict, ...]) -> list[str]:
    """Same-day disagreements: evidence of an ambiguity, never a revision.
    Companyfacts dates filings but does not order them within a day; the
    value used is the `precedence` convention (higher accession, else the
    first listed)."""
    if not found:
        return []
    lines = [
        "",
        "### Same-day conflicting facts (not revisions)",
        "",
        "_Several values filed on one day, at one amendment level, for one period. "
        "Companyfacts does not record which came last, so the value used is a "
        "convention (the higher accession number, else the first listed). Read the "
        "filings before relying on either._",
        "",
        "| Period | Field | Component | Filed | Values | Accessions |",
        "|---|---|---|---|---|---|",
    ]
    for c in found[:_MAX_ROWS]:
        period = f"{c.period_start} → {c.period_end}" if c.period_start else f"{c.period_end}"
        values = ", ".join(f"{v:,.0f}" for v in c.values)
        form = " (amendment)" if c.amended else ""
        lines.append(
            f"| {period} | {c.field_name} | {c.tag} | {c.filed}{form} | {values} | "
            f"{', '.join(dict.fromkeys(c.accessions))} |"
        )
    if len(found) > _MAX_ROWS:
        lines.append(f"| … | {len(found) - _MAX_ROWS} more | | | | |")
    return lines
