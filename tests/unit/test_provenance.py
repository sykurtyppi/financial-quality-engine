"""Every mapped value, and every metric, names the filings it came from.

`PeriodFinancials.sources` records, for each field of each period, the
signed XBRL facts it was computed from (value == Σ sign·fact). The
invariants below hold on every payload the selection snapshot is built from.
`provenance.sources_for` maps a metric's inputs to those values through the
formula registry's own pairing rules, and must reproduce each input exactly.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from app.services.formulas import ttm
from app.services.formulas.registry import compute_metrics
from app.services.ingestion.companyfacts_mapper import build_dataset
from app.services.ingestion.fields import FIELDS
from app.services.metrics_registry import BASIS, FINANCIAL_METRICS, Basis
from app.services.provenance import accessions_for, sources_for
from tests.fixtures import selection_cases
from tests.unit.test_series_selection import INPUTS

REAL = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "real"
FIELD_NAMES = [f.name for f in FIELDS]


# --- per-value provenance ------------------------------------------------------


@pytest.mark.parametrize(("case", "facts"), INPUTS, ids=[c for c, _ in INPUTS])
def test_every_value_is_sourced_and_its_facts_reproduce_it(case, facts):
    ds, diag = build_dataset(facts, "X")
    for p in ds.periods:
        present = {f for f in FIELD_NAMES if getattr(p, f) is not None}
        assert set(p.sources) == present, (case, p.fiscal_label)
        for name, sv in p.sources.items():
            assert sv.field == name and sv.value == getattr(p, name)
            assert sv.inputs, (case, name, p.fiscal_label)
            for ref in sv.inputs:
                assert ":" in ref.concept and ref.sign in (1, -1)
                if sv.method == "nearest":  # a cover-page count, dated after the quarter end
                    assert 0 < (ref.end - p.period_end).days <= 60
                else:
                    assert ref.end <= p.period_end
            rebuilt = 0.0
            for ref in sv.inputs:
                rebuilt += ref.sign * ref.value
            if sv.method in ("direct", "nearest", "composite") or sv.strategy != "single":
                assert rebuilt == sv.value, (case, name, p.fiscal_label)
            else:  # derived by differences: equal to rounding
                assert math.isclose(rebuilt, sv.value, rel_tol=1e-12, abs_tol=1e-6)
            src = diag.field_by_name(name).period_sources[p.period_end.isoformat()]
            assert (sv.strategy, sv.method, sv.partial) == (src.strategy, src.method, src.partial)


def test_real_facts_carry_their_accession():
    facts = json.loads((REAL / "companyfacts_AAPL_trimmed.json").read_text())
    ds, _ = build_dataset(facts, "AAPL")
    refs = [r for p in ds.periods for sv in p.sources.values() for r in sv.inputs]
    assert refs and all(r.accession and r.form for r in refs)


def test_a_year_to_date_difference_names_both_facts_signed():
    ds, _ = build_dataset(selection_cases.flows(), "X")
    [q2] = [p for p in ds.periods if p.period_end.isoformat() == "2024-06-30"]
    cfo = q2.sources["cfo"]
    assert cfo.method == "ytd_diff"
    assert [(r.sign, r.start.isoformat(), r.end.isoformat()) for r in cfo.inputs] == [
        (1, "2024-01-01", "2024-06-30"), (-1, "2024-01-01", "2024-03-31"),
    ]


def test_a_derived_quarter_names_the_facts_it_actually_subtracted():
    """Hermes audit round 4: Q2 = H1 − Q1 subtracts Q1 AS IT STOOD WHEN H1
    WAS FILED. The later 10-Q/A restating Q1 (300 → 340) did not change H1,
    so Q2 is 600 − 300 and the refs name the original Q1 — not the
    restatement, which the value does not contain."""
    ds, diag = build_dataset(selection_cases.ytd_vintage_mix(), "X")
    [q2] = [p for p in ds.periods if p.period_end.isoformat() == "2024-06-30"]
    sv = q2.sources["cfo"]
    assert sv.value == 600.0 - 300.0 and sv.note is None
    assert [(r.sign, r.form, r.value) for r in sv.inputs] == [(1, "10-Q", 600.0), (-1, "10-Q", 300.0)]
    assert sum(r.sign * r.value for r in sv.inputs) == sv.value
    assert not any("different dates" in n for n in diag.field_by_name("cfo").notes)


def test_a_fiscal_year_difference_names_the_quarters_as_the_10k_embedded_them():
    ds, _ = build_dataset(selection_cases.mixed_vintages(), "X")
    [q4] = [p for p in ds.periods if p.period_end.isoformat() == "2024-12-31"]
    sv = q4.sources["operating_income"]
    assert sv.method == "fy_minus_3q" and sv.value == 100.0 and sv.note is None
    assert [(r.sign, r.value) for r in sv.inputs] == [(1, 400.0), (-1, 100.0), (-1, 100.0), (-1, 100.0)]
    assert "10-Q/A" not in {r.form for r in sv.inputs}


def test_a_quarter_that_could_not_be_rebuilt_as_of_one_filing_is_named():
    """Q1 was first filed after the H1 figure it is subtracted from, so no
    single filing date has both: the latest values are used, and the value
    and the field say so."""
    ds, diag = build_dataset(selection_cases.mixed_vintages(), "X")
    [q2] = [p for p in ds.periods if p.period_end.isoformat() == "2024-06-30"]
    sv = q2.sources["cfo"]
    assert sv.value == 130.0 - 60.0 and "different dates" in (sv.note or "")
    assert [(r.sign, r.value) for r in sv.inputs] == [(1, 130.0), (-1, 60.0)]
    assert any("different dates at FY2024Q2" in n for n in diag.field_by_name("cfo").notes)


def test_sources_never_leave_the_period_they_describe():
    ds, _ = build_dataset(selection_cases.flows(), "X")
    periods = ds.sorted_periods()
    assert "sources" not in periods[-1].model_dump()
    annual = ttm.annualize(periods, len(periods) - 1)
    assert annual is not None and annual.sources == {}


# --- metric provenance -----------------------------------------------------------


def test_every_financial_metric_declares_its_basis():
    assert set(BASIS) == FINANCIAL_METRICS


@pytest.mark.parametrize("ticker", ["AAPL", "KO", "CRM"])
def test_a_metrics_sources_reproduce_its_inputs(ticker):
    facts = json.loads((REAL / f"companyfacts_{ticker}_trimmed.json").read_text())
    ds, _ = build_dataset(facts, ticker)
    bundle = compute_metrics(ds)
    checked = 0
    for name, history in bundle.history.items():
        basis = BASIS[name]
        for m in history:
            found = sources_for(ds, m, bundle=bundle)
            if basis in (Basis.SERIES, Basis.COMPOSITE):
                continue
            for key, value in m.inputs.items():
                field = key.removesuffix("_prior")
                if value is None or field not in FIELD_NAMES:
                    continue
                # Every field input the metric read is resolved…
                assert key in found, (name, m.fiscal_label, key)
                values = [sv.value for sv in found[key]]
                # …and the sourced values ARE the input (a TTM flow sums its
                # four quarters exactly as the TTM window does).
                assert (sum(values) if len(values) > 1 else values[0]) == value, (
                    name, m.fiscal_label, key,
                )
                checked += 1
    assert checked > 300


def test_the_m_score_resolves_through_its_indices_and_series_metrics_do_not():
    facts = json.loads((REAL / "companyfacts_KO_trimmed.json").read_text())
    ds, _ = build_dataset(facts, "KO")
    bundle = compute_metrics(ds)
    m_score = bundle.get_latest("beneish_m_score")
    found = sources_for(ds, m_score, bundle=bundle)
    assert "lvgi.total_debt" in found and "aqi.total_assets_prior" in found
    assert sources_for(ds, m_score) == {}  # needs the bundle its indices came from
    assert sources_for(ds, bundle.get_latest("accrual_trend")) == {}


def test_accessions_for_lists_each_filing_once_in_order():
    facts = json.loads((REAL / "companyfacts_KO_trimmed.json").read_text())
    ds, _ = build_dataset(facts, "KO")
    bundle = compute_metrics(ds)
    metric = bundle.get_latest("net_debt_to_ebitda")
    expected = list(dict.fromkeys(
        accession
        for values in sources_for(ds, metric, bundle=bundle).values()
        for sv in values for accession in sv.accessions()
    ))
    assert accessions_for(ds, metric, bundle=bundle) == expected
    assert expected and len(expected) == len(set(expected))


@pytest.mark.parametrize("sign", [0, 2, -2])
def test_a_fact_ref_is_only_ever_added_or_subtracted(sign):
    # Hermes audit round 6: `sign` was a plain int, so a schema-valid
    # FactRef(sign=0) broke `value == Σ sign·input` and the resolver's
    # "reporting fact" test (`sign > 0`). Only +1 and -1 validate.
    from datetime import date

    from pydantic import ValidationError

    from app.schemas.financials import FactRef

    kw = dict(concept="us-gaap:Revenues", accession="a", filed=date(2024, 5, 1), form="10-Q",
              start=date(2024, 1, 1), end=date(2024, 3, 31), value=1.0)
    assert FactRef(**kw).sign == 1 and FactRef(**kw, sign=-1).sign == -1
    with pytest.raises(ValidationError):
        FactRef(**kw, sign=sign)
