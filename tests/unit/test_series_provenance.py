"""A series metric cites exactly the values its formula read — no more, no less.

Hermes audit round 8, finding 1: `incremental_revenue_per_capex` reads
revenue at t and t-4 and capex at t-3..t, but provenance mapped it to five
whole periods of `capex_to_revenue`, so the ledger also cited capex[t-4] and
revenue[t-3..t-1] as evidence (KO FY2026Q1: four values that took no part in
the number). The same inference over a base metric's history mis-cited three
more metrics:

- `dso_trend` / `dio_trend` compare the latest value with the SAME fiscal
  quarter in prior years (t, t-4, t-8), but cited every quarter;
- `capex_intensity_regime_shift` averages capex/revenue from the FIRST
  period, which `capex_to_revenue`'s history (it starts one period later)
  does not hold, so a value it used was not cited at all.

Each test below pins the exact set, then reconciles the cited values to the
inputs the metric recorded, so provenance cannot drift from the formula.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.schemas.metrics import MetricStatus
from app.services.formulas import ttm
from app.services.formulas.registry import compute_metrics
from app.services.ingestion.companyfacts_mapper import build_dataset
from app.services.provenance import sources_for

REAL = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "real"
TICKERS = ("KO", "CRM", "AAPL")


@pytest.fixture(scope="module", params=TICKERS)
def real(request):
    facts = json.loads((REAL / f"companyfacts_{request.param}_trimmed.json").read_text())
    ds, _ = build_dataset(facts, request.param)
    return request.param, ds, compute_metrics(ds)


def _label(key: str) -> str:
    return key.split("[", 1)[1].split("]", 1)[0]


def _ok(metric):
    assert metric.status is MetricStatus.OK, (metric.name, metric.missing_fields)
    return metric


def test_incremental_revenue_per_capex_cites_exactly_what_it_read(real):
    _, ds, bundle = real
    m = _ok(bundle.get_latest("incremental_revenue_per_capex"))
    labels = [p.fiscal_label for p in ds.sorted_periods()]
    t = len(labels) - 1
    capex_keys = {f"capex[{labels[t - k]}]" for k in range(4)}
    found = sources_for(ds, m, bundle=bundle)
    assert set(found) == {f"revenue[{labels[t]}]", f"revenue[{labels[t - 4]}]"} | capex_keys
    assert all(len(v) == 1 for v in found.values())
    assert found[f"revenue[{labels[t]}]"][0].value == pytest.approx(m.inputs["revenue_end"])
    assert found[f"revenue[{labels[t - 4]}]"][0].value == pytest.approx(m.inputs["revenue_start"])
    assert sum(found[k][0].value for k in capex_keys) == pytest.approx(m.inputs["total_capex"])


def test_the_four_values_hermes_found_over_claimed_are_gone():
    facts = json.loads((REAL / "companyfacts_KO_trimmed.json").read_text())
    ds, _ = build_dataset(facts, "KO")
    bundle = compute_metrics(ds)
    m = bundle.get_latest("incremental_revenue_per_capex")
    assert m.fiscal_label == "FY2026Q1"
    found = sources_for(ds, m, bundle=bundle)
    assert set(found) == {
        "revenue[FY2025Q1]", "revenue[FY2026Q1]",
        "capex[FY2025Q2]", "capex[FY2025Q3]", "capex[FY2025Q4]", "capex[FY2026Q1]",
    }


def test_the_regime_shift_cites_every_usable_period_from_the_first(real):
    _, ds, bundle = real
    m = _ok(bundle.get_latest("capex_intensity_regime_shift"))
    periods = ds.sorted_periods()
    usable = [p for p in periods
              if p.capex is not None and p.revenue is not None and p.revenue > 0]
    found = sources_for(ds, m, bundle=bundle)
    assert set(found) == {f"{f}[{p.fiscal_label}]" for p in usable for f in ("capex", "revenue")}
    assert f"capex[{periods[0].fiscal_label}]" in found  # the value that went uncited

    def ratio(label: str) -> float:
        return found[f"capex[{label}]"][0].value / found[f"revenue[{label}]"][0].value

    recent, prior = usable[-4:], usable[:-4]
    assert [p.fiscal_label for p in recent] == [p.fiscal_label for p in periods[-4:]]
    assert len(recent) == m.inputs["n_recent"] and len(prior) == m.inputs["n_prior"]
    assert sum(ratio(p.fiscal_label) for p in recent) / 4 == pytest.approx(m.inputs["recent_mean"])
    assert (sum(ratio(p.fiscal_label) for p in prior) / len(prior)
            == pytest.approx(m.inputs["prior_mean"]))


@pytest.mark.parametrize("name, base", [("dso_trend", "dso"), ("dio_trend", "dio")])
def test_a_seasonal_trend_cites_only_the_same_fiscal_quarter(real, name, base):
    ticker, ds, bundle = real
    m = bundle.get_latest(name)
    if m.status is not MetricStatus.OK:
        assert (ticker, name) == ("CRM", "dio_trend")  # no inventory
        assert sources_for(ds, m, bundle=bundle) == {}
        return
    history = bundle.history[base]
    latest = history[-1]
    priors = [history[i] for i in range(len(history) - 5, -1, -4)]
    priors = [p for p in priors if p.status is MetricStatus.OK]
    found = sources_for(ds, m, bundle=bundle)
    assert {_label(k) for k in found} == {x.fiscal_label for x in [latest, *priors]}
    for x in [latest, *priors]:  # each cited period carries that period's base sources
        for key, values in sources_for(ds, x).items():
            assert found[f"{base}[{x.fiscal_label}].{key}"] == values
    assert latest.value == pytest.approx(m.inputs["latest"])
    assert (sum(p.value for p in priors) / len(priors)
            == pytest.approx(m.inputs["same_quarter_prior_mean"]))
    assert len(priors) == m.inputs["n_prior_years"]


@pytest.mark.parametrize("name, base", [("accrual_trend", "total_accruals"),
                                        ("fcf_margin_trend", "fcf_margin")])
def test_a_trend_over_all_history_cites_every_ok_period(real, name, base):
    _, ds, bundle = real
    m = _ok(bundle.get_latest(name))
    ok = [x for x in bundle.history[base] if x.status is MetricStatus.OK]
    found = sources_for(ds, m, bundle=bundle)
    assert {_label(k) for k in found} == {
        x.fiscal_label.removeprefix(ttm.TTM_LABEL_PREFIX) for x in ok}
    assert ok[-1].value == pytest.approx(m.inputs["latest"])
    assert sum(x.value for x in ok[:-1]) / len(ok[:-1]) == pytest.approx(m.inputs["prior_mean"])


def test_a_series_metric_that_computed_nothing_cites_nothing():
    from app.schemas.metrics import MetricResult

    facts = json.loads((REAL / "companyfacts_KO_trimmed.json").read_text())
    ds, _ = build_dataset(facts, "KO")
    bundle = compute_metrics(ds)
    label = bundle.get_latest("dso_trend").fiscal_label
    for name in ("dso_trend", "accrual_trend", "incremental_revenue_per_capex",
                 "capex_intensity_regime_shift"):
        missing = MetricResult(name=name, formula="x", fiscal_label=label,
                               status=MetricStatus.MISSING_DATA)
        assert sources_for(ds, missing, bundle=bundle) == {}


def _ko():
    facts = json.loads((REAL / "companyfacts_KO_trimmed.json").read_text())
    ds, _ = build_dataset(facts, "KO")
    return ds


def test_a_period_with_zero_revenue_is_not_cited_by_the_regime_shift():
    """`capex_intensity_regime_shift` skips a period whose revenue is not
    positive (no capex/revenue ratio); provenance must skip it too."""
    ds = _ko()
    periods = ds.sorted_periods()
    zero = periods[1]
    ds.periods = [p.model_copy(update={"revenue": 0.0}) if p is zero else p for p in ds.periods]
    bundle = compute_metrics(ds)
    m = _ok(bundle.get_latest("capex_intensity_regime_shift"))
    found = sources_for(ds, m, bundle=bundle)
    assert f"revenue[{zero.fiscal_label}]" not in found
    assert f"capex[{zero.fiscal_label}]" not in found
    assert m.inputs["n_prior"] == 3.0  # the formula skipped it as well
    assert len({_label(k) for k in found}) == 7


def test_a_window_that_starts_at_the_first_period_cites_it():
    """Five periods: t-4 IS the first period, and its revenue is an input."""
    ds = _ko()
    ds.periods = ds.sorted_periods()[-5:]
    bundle = compute_metrics(ds)
    m = _ok(bundle.get_latest("incremental_revenue_per_capex"))
    labels = [p.fiscal_label for p in ds.sorted_periods()]
    found = sources_for(ds, m, bundle=bundle)
    assert found[f"revenue[{labels[0]}]"][0].value == pytest.approx(m.inputs["revenue_start"])
    assert set(found) == {f"revenue[{labels[0]}]", f"revenue[{labels[4]}]"} | {
        f"capex[{labels[k]}]" for k in range(1, 5)}
