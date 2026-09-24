"""The rules for composing a field from several concepts at one date,
shared by the mapper and the restatement detector (and, through the mapper,
the vintage comparison): `compose_total_debt` for total debt, and
`resolve_by_strategy` for the fields whose registry entry lists alternative
strategies (SG&A, D&A).

Total debt is assembled from several balance-sheet concepts, and the rule
for which of them may be added together is accounting, not coverage:

- `DebtCurrent` is AGGREGATE current debt: it already contains the current
  portion of long-term debt and short-term borrowings. It is added to the
  noncurrent figure on its own, never together with `LongTermDebtCurrent` or
  short-term borrowings (800 + 100 + 150 double counts; the total is 950).
- Otherwise the current side is the current portion of long-term debt plus
  separately reported short-term borrowings.
- `LongTermDebt` is a TOTAL (current + noncurrent): used only when no
  noncurrent figure is reported, and never with `DebtCurrent`.
- Finance-lease liabilities are added unless the debt figure beside them
  already embeds them (the field's `lease_inclusive_tags`).

The rule is applied to the concepts a filer reported for ONE balance-sheet
date. It used to be applied to whole series, each role taking the first
tag with any value anywhere in the buffered window, so a tag used only in
old quarters (Intel's `CommercialPaper`, KO's pre-migration debt tags) hid
the tag in use today, and its missing quarters silently counted as zero.

Imports only the field registry.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.services.ingestion.fields import Composition, field, role_tags

NONCURRENT = role_tags("total_debt", "noncurrent")
CURRENT_AGGREGATE = role_tags("total_debt", "current_aggregate")
CURRENT = role_tags("total_debt", "current")
SHORT = role_tags("total_debt", "short")
TOTAL = role_tags("total_debt", "total")
FINANCE_LEASE_NONCURRENT = role_tags("total_debt", "finance_lease_nc")
FINANCE_LEASE_CURRENT = role_tags("total_debt", "finance_lease_c")
LEASE_INCLUSIVE = field("total_debt").lease_inclusive_tags
# Every debt concept, in the order a selection string names them.
DEBT_TAGS: tuple[str, ...] = (
    NONCURRENT + TOTAL + CURRENT_AGGREGATE + CURRENT + SHORT
    + FINANCE_LEASE_NONCURRENT + FINANCE_LEASE_CURRENT
)

SPLIT = "split"  # noncurrent + current portion + short-term borrowings
SPLIT_AGGREGATE_CURRENT = "split_aggregate_current"  # noncurrent + DebtCurrent
TOTAL_FALLBACK = "total"  # LongTermDebt + short-term borrowings


@dataclass(frozen=True)
class DebtComposition:
    total: float
    strategy: str
    # Concepts summed, in summation order (fixed role order, so the float
    # result is reproducible whatever order the caller's mapping has).
    used: tuple[str, ...]
    # Optional roles nothing reported at this date: "current", "short".
    # Counted as zero — the figure may understate, and callers say so.
    missing: tuple[str, ...]
    finance_lease_added: bool


def _first(candidates: tuple[str, ...], present: Mapping[str, float]) -> str | None:
    return next((c for c in candidates if c in present), None)


def compose_total_debt(present: Mapping[str, float]) -> DebtComposition | None:
    """Total debt from the concepts reported at one balance-sheet date
    (`present`: bare concept name -> value), or None when neither a
    noncurrent figure nor a `LongTermDebt` total is reported."""
    used: list[str] = []
    missing: list[str] = []
    total = 0.0

    def add(concept: str | None) -> None:
        nonlocal total
        if concept is not None:
            total += present[concept]
            used.append(concept)

    noncurrent = _first(NONCURRENT, present)
    if noncurrent is not None:
        add(noncurrent)
        current_side: str | None
        aggregate = _first(CURRENT_AGGREGATE, present)
        if aggregate is not None:
            strategy, current_side = SPLIT_AGGREGATE_CURRENT, aggregate
            add(aggregate)
        else:
            strategy = SPLIT
            current_side = _first(CURRENT, present)
            if current_side is None:
                missing.append("current")
            add(current_side)
            short = _first(SHORT, present)
            if short is None:
                missing.append("short")
            add(short)
        before = len(used)
        if noncurrent not in LEASE_INCLUSIVE:
            add(_first(FINANCE_LEASE_NONCURRENT, present))
        if current_side not in LEASE_INCLUSIVE:
            add(_first(FINANCE_LEASE_CURRENT, present))
        return DebtComposition(total, strategy, tuple(used), tuple(missing), len(used) > before)

    total_tag = _first(TOTAL, present)
    if total_tag is None:
        return None
    add(total_tag)
    short = _first(SHORT, present)
    if short is None:
        missing.append("short")
    add(short)
    before = len(used)
    # LongTermDebt is not lease-inclusive by definition; finance leases are
    # added, with the caveat that some filers' totals already embed them.
    add(_first(FINANCE_LEASE_NONCURRENT, present))
    add(_first(FINANCE_LEASE_CURRENT, present))
    return DebtComposition(total, TOTAL_FALLBACK, tuple(used), tuple(missing), len(used) > before)


# --- fields with alternative strategies (SG&A, D&A) -------------------------

SINGLE = "single"  # the field's own concept (an aggregate, for D&A)
COMPOSITE = "composite"  # every component of the summed strategy
PARTIAL = "partial"  # a partial fallback (depreciation alone, for D&A)


@dataclass(frozen=True)
class Resolved:
    total: float
    strategy: str  # SINGLE | COMPOSITE | PARTIAL
    used: tuple[str, ...]  # bare concept names, in summation order

    @property
    def partial(self) -> bool:
        return self.strategy == PARTIAL


def resolve_by_strategy(name: str, present: Mapping[str, float]) -> Resolved | None:
    """The field's value at ONE date from the concepts reported at it
    (`present`: bare concept name -> value), taking the field's strategies in
    registry order: its own concept, then every component summed, then a
    partial fallback. For D&A: the aggregate tag, else depreciation +
    amortization, else depreciation alone (partial).

    The choice used to be made once for the whole window, so a filer that
    reported amortization for only part of it (Alphabet) got depreciation
    alone in EVERY quarter, including those where the full figure was
    there. `present` should hold at most one of the first strategy's
    candidates — the one the mapper selected; the first found is used."""
    spec = field(name)
    for i, strategy in enumerate(spec.strategies):
        tags = [concept for _taxonomy, concept in strategy.tags]
        if strategy.composition is Composition.SINGLE:
            tag = next((t for t in tags if t in present), None)
            if tag is not None:
                return Resolved(present[tag], SINGLE if i == 0 else PARTIAL, (tag,))
        elif strategy.composition is Composition.SUM_ALL_REQUIRED:
            if all(t in present for t in tags):
                total = 0.0
                for t in tags:
                    total += present[t]
                return Resolved(total, COMPOSITE, tuple(tags))
    return None
