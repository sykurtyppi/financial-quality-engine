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
   with `source_accession=None`. The interim rule parked every source-set
   assumption at pending. Now the mapped values carry the filed facts they
   were computed from (`PeriodFinancials.sources`), so a preregistered form
   is ATTESTED: the value resolves only when the filings that reported it
   are of that form, and `source_accession` names the filing.
 - **round-11 finding 3 (symbolic comparator)**: `_resolve_symbolic` used to
   ignore the comparator, letting `cfo < positive` resolve `met` when
   CFO=+5. Fixed at can_lock (mismatched pairs refused) and here (defensive:
   returns unresolvable for a mismatched pair on any unlocked entry that
   sneaks past can_lock).

Lookup rule: engine spec_id in `bundle.history` first (e.g. `cfo_to_net_income`),
then fall back to raw XBRL fields on `PeriodFinancials` (`revenue`, `cfo`, …).
"""

from __future__ import annotations

from app.schemas.financials import (
    CompanyDataset,
    FactRef,
    PeriodFinancials,
    SourcedValue,
)
from app.schemas.metrics import MetricResult, MetricStatus
from app.services.formulas.registry import MetricsBundle
from app.services.formulas.ttm import TTM_LABEL_PREFIX
from app.services.ingestion.precedence import latest as latest_fact
from app.services.ingestion.precedence import rank
from app.services.journal.schema_v2 import (
    _SYMBOLIC_THRESHOLDS,
    Assumption,
    Comparator,
    Resolution,
)
from app.services.provenance import sources_for


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
    #
    #    A metric computed on a trailing-twelve-month basis labels its result
    #    `TTM FY2025Q4` — the TTM window ENDING at that quarter. Matching the
    #    quarterly label alone reached none of them: 16 of the 43 registry
    #    metrics, including `total_accruals`, every Beneish component and
    #    `cfo_to_net_income`, could be preregistered and then never resolved,
    #    because no dataset period is labelled `TTM FY2025Q4` either. Both
    #    spellings name the same commitment — "this metric, as of this
    #    quarter" — so both are accepted, and the note says which basis
    #    answered so the reader knows a twelve-month window was measured
    #    rather than the quarter alone.
    if bundle is not None:
        ttm_label = f"{TTM_LABEL_PREFIX}{period.fiscal_label}"
        for m in bundle.history.get(metric_name, []):
            if m.fiscal_label in (period.fiscal_label, ttm_label):
                basis = "TTM ending " if m.fiscal_label == ttm_label else ""
                if m.status is not MetricStatus.OK or m.value is None:
                    # NOT_MEANINGFUL is a structural output (denominator=0 etc.)
                    # — it is deterministic for the current inputs. Anything
                    # else (missing, error) may resolve when later data arrives.
                    structural = m.status is MetricStatus.NOT_MEANINGFUL
                    return None, (
                        f"metric '{metric_name}' is {m.status.value} in "
                        f"{basis}{period.fiscal_label}"
                    ), structural
                return (
                    float(m.value),
                    f"engine metric '{metric_name}' ({basis}{period.fiscal_label})",
                    False,
                )

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


def _engine_metric(
    metric_name: str, period: PeriodFinancials, bundle: MetricsBundle | None
) -> MetricResult | None:
    """The bundle's result for this metric in this period (either label
    spelling, as `_lookup_metric_value` accepts), or None."""
    if bundle is None:
        return None
    labels = (period.fiscal_label, f"{TTM_LABEL_PREFIX}{period.fiscal_label}")
    return next((m for m in bundle.history.get(metric_name, []) if m.fiscal_label in labels), None)


def _sourced_values(
    metric_name: str,
    period: PeriodFinancials,
    dataset: CompanyDataset,
    bundle: MetricsBundle | None,
) -> list[SourcedValue]:
    """The mapped values the resolved number was computed from: an engine
    metric's inputs (`provenance.sources_for`, the registry's own pairing
    rules), else the raw field's own value."""
    metric = _engine_metric(metric_name, period, bundle)
    if metric is not None:
        return [sv for values in sources_for(dataset, metric, bundle=bundle).values()
                for sv in values]
    sv = period.sources.get(metric_name)
    return [sv] if sv is not None else []


def _reporting_facts(values: list[SourcedValue], period: PeriodFinancials) -> list[FactRef]:
    """The filed facts that REPORT the period itself: added, not subtracted
    (a year-to-date difference subtracts the prior quarter's figure, which an
    earlier filing reported), and ending at or after the period end (a
    trailing window's earlier quarters were reported by earlier filings; a
    cover-page share count is dated after the quarter end)."""
    out: list[FactRef] = []
    for sv in values:
        for ref in sv.inputs:
            if ref.sign > 0 and ref.end >= period.period_end and ref not in out:
                out.append(ref)
    return out


_FORM_FAMILIES = ("10-K", "10-Q", "8-K")


def _is_proxy(form: str) -> bool:
    return "14A" in form or "14C" in form


def _form_matches(source: str, form: str) -> bool:
    """Whether a fact filed on `form` honours a preregistered `source`. An
    amendment source (`10-Q/A`) is a commitment to the amendment itself;
    `10-Q` is the quarterly report, original or amended; `other` is any
    form outside the named families."""
    if source.endswith("/A"):
        return form == source
    family = form.removesuffix("/A")
    if source in _FORM_FAMILIES:
        return family == source
    if source == "proxy":
        return _is_proxy(family)
    return family not in _FORM_FAMILIES and not _is_proxy(family)


def _filing(ref: FactRef) -> str:
    return f"{ref.form} {ref.accession} filed {ref.filed.isoformat()}"


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

    # Round-11 finding 2, closed: which filings reported this number. A
    # preregistered `source` is a commitment to a form; it is honoured only
    # when every filing that reported the period was of that form. Anything
    # less stays pending (never committed), naming what did report it, so
    # the user can attest by hand.
    reporting = _reporting_facts(
        _sourced_values(assumption.metric, period, dataset, bundle), period
    )
    if assumption.source is not None:
        why = None
        if not reporting:
            why = (
                f"no per-value provenance for '{assumption.metric}' in "
                f"{period.fiscal_label} (the dataset was not mapped from filed facts)"
            )
        else:
            other = [r for r in reporting if not _form_matches(assumption.source, r.form)]
            if other:
                why = (
                    f"preregistered source '{assumption.source}', but "
                    f"{period.fiscal_label} was reported by "
                    + "; ".join(dict.fromkeys(_filing(r) for r in other))
                )
        if why is not None:
            return Resolution(
                assumption_index=assumption_index,
                state="pending",
                observed=value,
                at=period.period_end,
                note=why + ". Resolve manually with a source_accession if that is the commitment.",
            )
    # The filing that completed the value: the current reporting fact, by the
    # order every reader of filed facts shares (`precedence`: filed date, then
    # an amendment over an original, then accession) — so a same-day 10-Q/A
    # is the filing cited, not whichever fact was listed first.
    latest = (
        latest_fact(reporting, key=lambda r: rank(r.filed, r.form, r.accession))
        if reporting else None
    )
    accession = latest.accession if latest is not None else None
    if latest is not None:
        note = f"{note}; reported in {_filing(latest)}"

    # Symbolic threshold path.
    if isinstance(assumption.threshold, str):
        outcome = _resolve_symbolic(value, assumption.comparator, assumption.threshold)
        if outcome is None:
            return Resolution(
                assumption_index=assumption_index,
                state="unresolvable",
                observed=value,
                at=period.period_end,
                source_accession=accession,
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
            source_accession=accession,
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
            source_accession=accession,
            note="comparator 'within' requires a range syntax not yet supported",
        )

    met = _apply_comparator(value, assumption.comparator, float(assumption.threshold))
    return Resolution(
        assumption_index=assumption_index,
        state="met" if met else "violated",
        observed=value,
        at=period.period_end,
        source_accession=accession,
        note=note,
    )
