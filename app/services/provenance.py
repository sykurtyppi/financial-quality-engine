"""The filed facts behind a metric.

The mapper records, for every period's value of every field, the signed XBRL
facts it was computed from (`PeriodFinancials.sources`). A metric reads those
values through the formula registry's pairing rules — a TTM window, the same
quarter a year earlier, the previous quarter — so naming the filings behind a
metric means applying the same rules to its inputs. `sources_for` does that,
reusing the registry's own year-ago rule and the TTM window check rather than
restating them; `metrics_registry.BASIS` says which rule each metric uses.

A TTM period rebuilt by `ttm.annualize` carries no sources of its own (they
are excluded from serialization): a four-quarter sum is resolved here, by
label, to its four quarters.
"""

from __future__ import annotations

from app.schemas.financials import CompanyDataset, PeriodFinancials, SourcedValue
from app.schemas.metrics import MetricResult
from app.services.formulas import ttm
from app.services.formulas.registry import MetricsBundle, _year_ago
from app.services.ingestion.fields import FIELDS
from app.services.metrics_registry import BASIS, Basis

_PRIOR = "_prior"
_FIELDS = frozenset(spec.name for spec in FIELDS)
# The M-score's inputs are its indices, named without the family prefix.
_COMPONENT_PREFIX = "beneish_"


def _index(periods: list[PeriodFinancials], label: str) -> int | None:
    end = label.removeprefix(ttm.TTM_LABEL_PREFIX)
    return next((i for i, p in enumerate(periods) if p.fiscal_label == end), None)


def _annual(periods: list[PeriodFinancials], i: int, field: str) -> list[PeriodFinancials]:
    """The periods an annual-basis (TTM) value of `field` ending at `i` was
    read from: the four quarters for a flow, the end quarter for an
    instant — or none when `ttm.annualize` would not build that window."""
    if i < 0 or ttm.annualize(periods, i) is None:
        return []
    return periods[i - 3 : i + 1] if field in ttm.FLOW_FIELDS else [periods[i]]


def _periods_for(
    periods: list[PeriodFinancials], i: int, basis: Basis, field: str, prior: bool
) -> list[PeriodFinancials]:
    if basis is Basis.PAIR:
        if not prior:
            return [periods[i]]
        return [periods[i - 1]] if i > 0 else []
    if basis is Basis.YOY:
        if not prior:
            return [periods[i]]
        year_ago = _year_ago(periods, i)
        return [year_ago] if year_ago is not None else []
    if basis is Basis.ACCRUALS and prior:
        year_ago = _year_ago(periods, i)
        return [year_ago] if year_ago is not None else []
    if basis is Basis.TTM_YOY and prior:
        return _annual(periods, i - 4, field)
    if basis in (Basis.TTM, Basis.ACCRUALS, Basis.TTM_YOY):
        return _annual(periods, i, field)
    return []


def sources_for(
    dataset: CompanyDataset, metric: MetricResult, *, bundle: MetricsBundle | None = None
) -> dict[str, list[SourcedValue]]:
    """Input name -> the sourced values it was computed from, in period
    order (a TTM flow input maps to its four quarters). Inputs that are not
    fields (a trend's statistics) and periods built without sources map to
    nothing. The M-score resolves through its component indices, which needs
    the `bundle` they were computed in."""
    basis = BASIS.get(metric.name)
    if basis is None or basis is Basis.SERIES:
        return {}
    periods = dataset.sorted_periods()
    i = _index(periods, metric.fiscal_label)
    if i is None:
        return {}
    if basis is Basis.COMPOSITE:
        out: dict[str, list[SourcedValue]] = {}
        for key in metric.inputs:
            component = _component(bundle, _COMPONENT_PREFIX + key, metric.fiscal_label)
            if component is not None:
                for sub_key, values in sources_for(dataset, component).items():
                    out.setdefault(f"{key}.{sub_key}", []).extend(values)
        return out
    out = {}
    for key in metric.inputs:
        prior = key.endswith(_PRIOR)
        field = key.removesuffix(_PRIOR) if prior else key
        if field not in _FIELDS:
            continue
        found = [
            p.sources[field] for p in _periods_for(periods, i, basis, field, prior)
            if field in p.sources
        ]
        if found:
            out[key] = found
    return out


def accessions_for(
    dataset: CompanyDataset, metric: MetricResult, *, bundle: MetricsBundle | None = None
) -> list[str]:
    """Every filing behind a metric, in first-seen order."""
    seen: dict[str, None] = {}
    for values in sources_for(dataset, metric, bundle=bundle).values():
        for sv in values:
            for accession in sv.accessions():
                seen.setdefault(accession, None)
    return list(seen)


def _component(bundle: MetricsBundle | None, name: str, label: str) -> MetricResult | None:
    if bundle is None:
        return None
    return next((m for m in bundle.history.get(name, []) if m.fiscal_label == label), None)
