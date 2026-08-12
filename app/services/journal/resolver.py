"""Assumption resolver (P1-E follow-up).

Given a preregistered `Assumption` and the company's current data, the engine
proposes `pending / met / violated / unresolvable` — closing the loop the
"machine-checkable" name implies. Per VALIDATION_STRATEGY §5 the engine
PROPOSES and the user CONFIRMS: this module produces `Resolution` objects;
commit is a separate step (the CLI's `resolve --commit`).

Round-10 & round-11 fixes:
 - **round-10 finding 4 (`pending` vs `unresolvable`)**: `pending` = data not
   yet available (retry later). `unresolvable` = structural spec problem
   (unknown metric, unsupported comparator, unknown symbolic threshold).
 - **round-11 finding 2 (source provenance)**: whitelisting {10-K, 10-Q} was
   NOT provenance — the same quarterly value resolved met under either form
   with `source_accession=None`. Now: ANY assumption that specifies `source`
   returns pending until per-value provenance (P1-A) can attest form and
   accession. Assumptions without `source` set are numeric-only commitments
   and DO auto-terminate.
 - **round-11 finding 3 (symbolic comparator)**: `_resolve_symbolic` used to
   ignore the comparator, letting `cfo < positive` resolve `met` when
   CFO=+5. Fixed at can_lock (mismatched pairs refused) and here (defensive:
   returns unresolvable for a mismatched pair on any unlocked entry that
   sneaks past can_lock).

Lookup rule: engine spec_id in `bundle.history` first (e.g. `cfo_to_net_income`),
then fall back to raw XBRL fields on `PeriodFinancials` (`revenue`, `cfo`, …).
"""

from __future__ import annotations

from app.schemas.financials import CompanyDataset, PeriodFinancials
from app.schemas.metrics import MetricResult, MetricStatus
from app.services.formulas.registry import MetricsBundle
from app.services.journal.schema_v2 import (
    _SYMBOLIC_THRESHOLDS,
    Assumption,
    Comparator,
    Resolution,
)


def _find_period(dataset: CompanyDataset, window: str) -> PeriodFinancials | None:
    """Match `window` against `fiscal_label`. Case-insensitive exact match; the
    fiscal labels the mapper produces are structural (e.g. FY2026Q2)."""
    target = window.strip().upper()
    for p in dataset.periods:
        if p.fiscal_label.upper() == target:
            return p
    return None


def _lookup_metric_value(
    metric_name: str,
    period: PeriodFinancials,
    bundle: MetricsBundle | None,
) -> tuple[float | None, str, bool]:
    """Returns (value, note, structural).
    Value is None with an explanatory note when the metric is missing, non-OK,
    or non-finite for that period. `structural` is True iff the metric name is
    unknown (unresolvable), False iff the metric is known but not yet populated
    (pending)."""
    # 1. Engine spec_id: consult the bundle's latest+history (latest ≡ this period
    #    when the assumption's window matches the bundle's latest period). We
    #    look up by period label to be safe if the bundle's latest is elsewhere.
    if bundle is not None:
        for m in bundle.history.get(metric_name, []):
            if m.fiscal_label == period.fiscal_label:
                if m.status is not MetricStatus.OK or m.value is None:
                    # NOT_MEANINGFUL is a structural output (denominator=0 etc.)
                    # — it is deterministic for the current inputs. Anything
                    # else (missing, error) may resolve when later data arrives.
                    structural = m.status is MetricStatus.NOT_MEANINGFUL
                    return None, (
                        f"metric '{metric_name}' is {m.status.value} in "
                        f"{period.fiscal_label}"
                    ), structural
                return float(m.value), f"engine metric '{metric_name}' ({period.fiscal_label})", False

    # 2. Raw XBRL-mapped field on PeriodFinancials.
    if hasattr(period, metric_name):
        raw = getattr(period, metric_name)
        if raw is None:
            # Field exists but not populated for this period — comparative
            # revisions do arrive, so retryable (pending).
            return None, f"field '{metric_name}' missing in {period.fiscal_label}", False
        return float(raw), f"XBRL field '{metric_name}' ({period.fiscal_label})", False

    # Unknown metric name is a spec problem — never resolvable given this schema.
    return None, f"unknown metric or field '{metric_name}'", True


def _apply_comparator(value: float, cmp: Comparator, threshold: float) -> bool:
    if cmp == ">":
        return value > threshold
    if cmp == "<":
        return value < threshold
    if cmp == ">=":
        return value >= threshold
    if cmp == "<=":
        return value <= threshold
    if cmp == "==":
        return value == threshold
    # `within` needs a range; return False so the caller marks unresolvable.
    return False


def _resolve_symbolic(value: float, cmp: Comparator, threshold: str) -> str | None:
    """Evaluate a symbolic threshold. Returns 'met' / 'violated' / None
    (unresolvable — unknown keyword or mismatched comparator).

    Round-11 finding 3: the comparator IS load-bearing. The old code ignored
    it, so `cfo < positive` with CFO=+5 resolved `met` (because "positive" was
    True regardless of the `<`). Now: each symbolic keyword has exactly one
    canonical comparator; any other pairing returns None (unresolvable). This
    is a defensive re-check — `can_lock` refuses the same pairs so an entry
    that reached the resolver only hits this branch if constructed unlocked.
    """
    key = threshold.strip().lower()
    expected = _SYMBOLIC_THRESHOLDS.get(key)
    if expected is None or cmp != expected:
        return None
    checks: dict[str, bool] = {
        "positive": value > 0,
        "negative": value < 0,
        "non_negative": value >= 0,
        "nonnegative": value >= 0,
        "non_positive": value <= 0,
        "nonpositive": value <= 0,
        "zero": value == 0,
    }
    return "met" if checks[key] else "violated"


def propose_resolution(
    assumption: Assumption,
    dataset: CompanyDataset,
    bundle: MetricsBundle | None = None,
    assumption_index: int = 0,
) -> Resolution:
    """Engine proposal for one assumption. The user confirms separately (the
    `resolve --commit` step); this function never mutates state.

    States (round-10 findings 4 & 5):
      pending       — data not yet available OR source not auto-verifiable;
                      RETRY LATER; not committed by `resolve --commit`.
      met/violated  — terminal; committed.
      unresolvable  — the assumption spec is structurally undecidable given
                      this resolver (unknown metric name, unsupported
                      comparator, unknown symbolic threshold). Terminal.
    """
    # Round-11 finding 2. The old whitelist ({10-K, 10-Q}) was NOT provenance:
    # the mapper's canonical dataset merges companyfacts values and cannot say
    # which form supplied any given number, so the same quarter resolved `met`
    # under either source with `source_accession=None`. Honest interim rule:
    # if the user preregistered a specific source they COMMITTED to that
    # form/accession attribution — refuse to auto-terminate until per-value
    # provenance (P1-A) lands. Numeric-only assumptions (source=None) are
    # legitimate: the user is not claiming a form, only a value.
    if assumption.source is not None:
        return Resolution(
            assumption_index=assumption_index,
            state="pending",
            note=(
                f"source '{assumption.source}' preregistered but the mapper "
                f"cannot yet attest per-value form/accession (P1-A). Resolve "
                f"manually with a source_accession, or leave source unset for "
                f"a numeric-only commitment."
            ),
        )

    period = _find_period(dataset, assumption.window)
    if period is None:
        # Window not present — filing may simply not have arrived; retryable.
        return Resolution(
            assumption_index=assumption_index,
            state="pending",
            note=f"no period matching window '{assumption.window}' in current data",
        )

    value, note, structural = _lookup_metric_value(assumption.metric, period, bundle)
    if value is None:
        return Resolution(
            assumption_index=assumption_index,
            state="unresolvable" if structural else "pending",
            at=period.period_end,
            note=note,
        )

    # Symbolic threshold path.
    if isinstance(assumption.threshold, str):
        outcome = _resolve_symbolic(value, assumption.comparator, assumption.threshold)
        if outcome is None:
            return Resolution(
                assumption_index=assumption_index,
                state="unresolvable",
                observed=value,
                at=period.period_end,
                note=(
                    f"symbolic threshold '{assumption.threshold}' with comparator "
                    f"'{assumption.comparator}' not recognized"
                ),
            )
        return Resolution(
            assumption_index=assumption_index,
            state=outcome,  # type: ignore[arg-type]  # Literal validated by pydantic
            observed=value,
            at=period.period_end,
            note=note,
        )

    if assumption.comparator == "within":
        # Structural: the resolver cannot evaluate `within` under any data;
        # normally `can_lock` rejects this comparator (round-10 finding 6),
        # but defend against programmatically-constructed unlocked entries.
        return Resolution(
            assumption_index=assumption_index,
            state="unresolvable",
            observed=value,
            at=period.period_end,
            note="comparator 'within' requires a range syntax not yet supported",
        )

    met = _apply_comparator(value, assumption.comparator, float(assumption.threshold))
    return Resolution(
        assumption_index=assumption_index,
        state="met" if met else "violated",
        observed=value,
        at=period.period_end,
        note=note,
    )
