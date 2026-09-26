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

A statistic over history cites exactly the values its formula read — the
entries of its base metric's history it used (`metrics_registry.SERIES_OF`),
or the period fields at the offsets it used (`FIELD_WINDOWS`) — never a
window around them (Hermes audit round 8). A series metric that computed no
value read nothing conclusive and cites nothing.
"""

from __future__ import annotations

from app.schemas.financials import CompanyDataset, PeriodFinancials, SourcedValue
from app.schemas.metrics import MetricResult, MetricStatus
from app.services.formulas import ttm
from app.services.formulas.registry import MetricsBundle, _year_ago
from app.services.ingestion.fields import FIELDS
from app.services.metrics_registry import (
    BASIS,
    FIELD_WINDOWS,
    SERIES_OF,
    USABLE_CAPEX_INTENSITY,
    Basis,
    Select,
)

_PRIOR = "_prior"
_FIELDS = frozenset(spec.name for spec in FIELDS)
# The M-score's inputs are its indices, named without the family prefix.
_COMPONENT_PREFIX = "beneish_"


def _index(periods: list[PeriodFinancials], label: str, *, last: bool = False) -> int | None:
    """The period a metric's label names. Quarter ends are every distinct
    balance-sheet date, so a fiscal-calendar change can put two periods under
    one label (round-9 review R5): then the label is ambiguous and names no
    period — unless `last`, for a series metric, which is always computed at
    the final period."""
    end = label.removeprefix(ttm.TTM_LABEL_PREFIX)
    hits = [i for i, p in enumerate(periods) if p.fiscal_label == end]
    if not hits:
        return None
    if last:
        return hits[-1]
    return hits[0] if len(hits) == 1 else None


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
    fields and periods built without sources map to nothing. The M-score
    resolves through its component indices, and a trend through its base
    metric's history (`metrics_registry.SERIES_OF`); both need the `bundle`
    they were computed in."""
    basis = BASIS.get(metric.name)
    if basis is None:
        return {}
    periods = dataset.sorted_periods()
    i = _index(periods, metric.fiscal_label, last=basis in (Basis.SERIES, Basis.FIELDS))
    if i is None:
        return {}
    if basis in (Basis.SERIES, Basis.FIELDS) and metric.status is not MetricStatus.OK:
        return {}
    if basis is Basis.SERIES:
        return _series_sources(dataset, metric, bundle)
    if basis is Basis.FIELDS:
        return _field_sources(periods, i, metric.name)
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


def _series_sources(
    dataset: CompanyDataset, metric: MetricResult, bundle: MetricsBundle | None
) -> dict[str, list[SourcedValue]]:
    """A statistic over a base metric's history: the base metric's sources in
    exactly the entries the statistic read, keyed `<base>[<label>].<input>`.
    The history is the bundle's, so this needs the bundle, as the M-score
    does. Entries after the metric's own period are never read."""
    if bundle is None:
        return {}
    base, select = SERIES_OF[metric.name]
    own = metric.fiscal_label.removeprefix(ttm.TTM_LABEL_PREFIX)
    full = bundle.history.get(base, [])
    ends = [k for k, m in enumerate(full)
            if m.fiscal_label.removeprefix(ttm.TTM_LABEL_PREFIX) == own]
    if not ends:
        return {}
    history = full[: ends[-1] + 1]  # the last entry under the label: the final period
    if select is Select.SAME_QUARTER:
        # `seasonal_trend_change`: the latest entry, then every 4th one back.
        read = [history[-1]] + [history[k] for k in range(len(history) - 5, -1, -4)]
    else:
        read = history
    out: dict[str, list[SourcedValue]] = {}
    # The formulas' own predicate: an entry is read when it is OK AND carries
    # a value (`MetricResult` allows OK with none).
    for m in sorted((m for m in read if m.status is MetricStatus.OK and m.value is not None),
                    key=lambda m: history.index(m)):
        label = m.fiscal_label.removeprefix(ttm.TTM_LABEL_PREFIX)
        for key, values in sources_for(dataset, m).items():
            out[f"{base}[{label}].{key}"] = values
    return out


def _field_sources(
    periods: list[PeriodFinancials], i: int, name: str
) -> dict[str, list[SourcedValue]]:
    """Period fields read directly: `<field>[<label>]` -> that period's
    sourced value, at exactly the offsets `FIELD_WINDOWS` names."""
    spec = FIELD_WINDOWS[name]
    wanted: list[tuple[str, PeriodFinancials]] = []
    if spec == USABLE_CAPEX_INTENSITY:
        for p in periods[: i + 1]:
            if p.capex is not None and p.revenue is not None and p.revenue > 0:
                wanted += [("capex", p), ("revenue", p)]
    else:
        assert isinstance(spec, dict)
        for field, offsets in spec.items():
            wanted += [(field, periods[i + off]) for off in offsets if i + off >= 0]
    out: dict[str, list[SourcedValue]] = {}
    for field, p in sorted(wanted, key=lambda fp: (fp[1].period_end, fp[0])):
        if field in p.sources:
            out[f"{field}[{p.fiscal_label}]"] = [p.sources[field]]
    return out


def _component(bundle: MetricsBundle | None, name: str, label: str) -> MetricResult | None:
    if bundle is None:
        return None
    return next((m for m in bundle.history.get(name, []) if m.fiscal_label == label), None)
