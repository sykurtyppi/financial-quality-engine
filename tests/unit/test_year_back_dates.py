"""A comparison with "a year earlier" checks that the other end IS a year earlier.

Hermes finding 5. Three formulas reached back a year by POSITION — four
entries back in the period list — with no date check, so a history missing
a year compared across two:

- Beneish (`registry._ttm_metrics`) paired TTM t with `ttm.annualize(i - 4)`;
  `annualize` checks only the gaps inside its own window, so SGI came back
  OK comparing TTM FY2025Q4 with TTM FY2023Q4;
- `seasonal_trend_change` (DSO/DIO) took every 4th history entry back as the
  same fiscal quarter in prior years, FY2023Q1 included beside FY2026Q1;
- `incremental_revenue_per_capex` took revenue at t and t-4 as a year apart.

Each now uses the rule the Sloan base and the YoY spreads already use
(`registry._year_ago`: 330-400 days), and provenance cites what they read.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.schemas.financials import (
    CompanyDataset,
    CompanyProfile,
    PeriodFinancials,
    PeriodType,
)
from app.schemas.metrics import MetricStatus
from app.services.formulas import capex, registry, ttm
from app.services.formulas import working_capital as wc
from app.services.provenance import sources_for

_ENDS = [(3, 31), (6, 30), (9, 30), (12, 31)]


def _q(year: int, qi: int, **kw) -> PeriodFinancials:
    m, d = _ENDS[qi]
    base = dict(revenue=100.0, cost_of_revenue=60.0, sga_expense=10.0, receivables=50.0,
                inventory=30.0, net_income=10.0, cfo=12.0, capex=5.0,
                depreciation_amortization=4.0, total_assets=400.0, current_assets=150.0,
                ppe_net=100.0, current_liabilities=80.0, total_debt=100.0)
    base.update(kw)
    return PeriodFinancials(period_end=date(year, m, d), period_type=PeriodType.QUARTER,
                            fiscal_label=f"FY{year}Q{qi + 1}", **base)


def _year(year: int, **kw) -> list[PeriodFinancials]:
    return [_q(year, qi, **kw) for qi in range(4)]


def _bundle(periods: list[PeriodFinancials]):
    ds = CompanyDataset(profile=CompanyProfile(ticker="GAP"), periods=periods)
    return ds, registry.compute_metrics(ds)


# --- Beneish -----------------------------------------------------------------------


def test_beneish_sgi_across_a_missing_year_is_not_ok():
    """FY2024 absent: position i-4 from FY2025Q4 is FY2023Q4, two years back.
    Both TTM windows are internally contiguous, so only a date check between
    them refuses the pair."""
    _, bundle = _bundle(_year(2022) + _year(2023) + _year(2025, revenue=150.0))
    sgi = bundle.get_latest("beneish_sgi")
    assert sgi.fiscal_label == "TTM FY2025Q4"
    assert sgi.status is MetricStatus.MISSING_DATA and sgi.value is None
    assert sgi.missing_fields == [ttm.PRIOR_YEAR_MISSING]
    assert sgi.note == f"{registry.YEAR_AGO_NOTE} {registry.TTM_NOTE}"
    assert sgi.note.startswith("No year-ago quarter within 330-400 days")
    for name in registry.TTM_BASIS_METRICS - {"cfo_to_net_income", "fcf_to_net_income",
                                              "fcf_margin", "net_debt_to_ebitda",
                                              "total_accruals"}:
        m = bundle.get_latest(name)
        assert m.status is MetricStatus.MISSING_DATA, name
        assert m.missing_fields == [ttm.PRIOR_YEAR_MISSING], name
        assert m.note.endswith(sgi.note), name


def test_beneish_on_a_contiguous_history_is_unchanged():
    _, bundle = _bundle(_year(2024) + _year(2025, revenue=150.0))
    sgi = bundle.get_latest("beneish_sgi")
    assert sgi.status is MetricStatus.OK and sgi.value == pytest.approx(1.5)
    assert sgi.note == registry.TTM_NOTE
    assert bundle.get_latest("beneish_m_score").status is MetricStatus.OK


def test_beneish_without_an_earlier_window_still_says_the_window_is_missing():
    """Five quarters: a year-ago quarter exists but no TTM window ends there —
    the existing window-missing path, not the new year-ago one."""
    _, bundle = _bundle([_q(2024, 3)] + _year(2025))
    sgi = bundle.get_latest("beneish_sgi")
    assert sgi.status is MetricStatus.MISSING_DATA
    assert sgi.missing_fields == [ttm.TTM_WINDOW_MISSING]
    assert sgi.note == registry.TTM_NOTE


def test_beneish_provenance_cites_no_prior_window_across_the_gap():
    periods = _year(2022) + _year(2023) + _year(2025, revenue=150.0)
    for p in periods:
        p.sources = {"revenue": _sv(p)}
    ds, bundle = _bundle(periods)
    sgi = bundle.get_latest("beneish_sgi")
    assert set(sources_for(ds, sgi, bundle=bundle)) == {"revenue"}  # never revenue_prior
    ok = sgi.model_copy(update={"status": MetricStatus.OK, "value": 1.5})
    assert set(sources_for(ds, ok, bundle=bundle)) == {"revenue"}


def test_beneish_provenance_cites_the_prior_window_a_year_back():
    periods = _year(2024) + _year(2025, revenue=150.0)
    for p in periods:
        p.sources = {"revenue": _sv(p)}
    ds, bundle = _bundle(periods)
    sgi = bundle.get_latest("beneish_sgi")
    found = sources_for(ds, sgi, bundle=bundle)
    assert sum(sv.value for sv in found["revenue"]) == pytest.approx(sgi.inputs["revenue"])
    assert sum(sv.value for sv in found["revenue_prior"]) == pytest.approx(
        sgi.inputs["revenue_prior"])
    assert [sv.inputs[0].end for sv in found["revenue_prior"]] == [
        p.period_end for p in periods[:4]]


def _sv(p: PeriodFinancials):
    from app.schemas.financials import FactRef, SourcedValue

    ref = FactRef(concept="us-gaap:Revenues", start=None, end=p.period_end, value=p.revenue,
                  accession=f"a-{p.fiscal_label}", form="10-Q", filed=p.period_end)
    return SourcedValue(field="revenue", value=p.revenue, strategy="t", method="direct",
                        inputs=(ref,))


# --- same fiscal quarter in prior years --------------------------------------------


def test_same_quarter_priors_walk_back_a_year_at_a_time():
    ends = [date(2023, 3, 31), date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31),
            date(2024, 3, 31), date(2024, 6, 30), date(2024, 9, 30), date(2024, 12, 31),
            date(2025, 3, 31)]
    assert wc.same_quarter_priors(ends) == [4, 0]
    assert wc.same_quarter_priors(ends[:4]) == []
    assert wc.same_quarter_priors([]) == []


@pytest.mark.parametrize("gap, kept", [(329, False), (330, True), (400, True), (401, False)])
def test_same_quarter_priors_accept_330_to_400_days(gap, kept):
    """The registry's year-ago bounds: 52/53-week years pass, a skipped or
    extra year does not."""
    from datetime import timedelta

    last = date(2026, 3, 31)
    ends = [last - timedelta(days=gap), date(2025, 6, 30), date(2025, 9, 30),
            date(2025, 12, 31), last]
    assert wc.same_quarter_priors(ends) == ([0] if kept else [])


def test_same_quarter_priors_stop_at_the_first_break():
    """A later year beyond the break is not rejoined: every prior must chain
    back a year at a time from the latest."""
    ends = [date(2021, 3, 31), date(2021, 6, 30), date(2021, 9, 30), date(2021, 12, 31),
            date(2023, 3, 31), date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31),
            date(2024, 3, 31)]
    assert wc.same_quarter_priors(ends) == [4]


def test_dso_priors_stop_at_the_missing_year():
    """FY2024 absent: FY2026Q1's same-quarter priors are FY2025Q1 only; the
    positional stride also took FY2023Q1, three years back."""
    periods = ([_q(2022, 3)] + _year(2023, receivables=500.0) + _year(2025)
               + [_q(2026, 0, receivables=60.0)])
    _, bundle = _bundle(periods)
    dso = bundle.history["dso"]
    assert [m.fiscal_label for m in dso][::4] == ["FY2023Q1", "FY2025Q1", "FY2026Q1"]
    m = bundle.get_latest("dso_trend")
    assert m.status is MetricStatus.OK
    assert m.inputs["n_prior_years"] == 1.0
    assert m.inputs["same_quarter_prior_mean"] == pytest.approx(50.0 / 100.0 * 91)
    assert m.value == pytest.approx((60.0 - 50.0) / 100.0 * 91)


def test_dio_with_only_a_distant_prior_is_missing():
    periods = [_q(2022, 3)] + _year(2023) + [_q(2026, 0)]
    _, bundle = _bundle(periods)
    for name in ("dso_trend", "dio_trend"):
        m = bundle.get_latest(name)
        assert m.status is MetricStatus.MISSING_DATA, name
        assert m.missing_fields == ["same-quarter history (need >= 1 prior year)"]


def test_the_dso_trend_on_a_contiguous_history_is_unchanged():
    periods = [_q(2022, 3)] + _year(2023, receivables=40.0) + _year(2024) + _year(2025)
    periods += [_q(2026, 0, receivables=70.0)]
    _, bundle = _bundle(periods)
    m = bundle.get_latest("dso_trend")
    assert m.inputs["n_prior_years"] == 3.0
    assert m.value == pytest.approx((70.0 - (40.0 + 50.0 + 50.0) / 3) / 100.0 * 91)


def test_seasonal_trend_change_needs_one_end_per_entry():
    from app.schemas.metrics import MetricResult

    series = [MetricResult(name="dso", formula="f", fiscal_label=f"P{i}",
                           status=MetricStatus.OK, value=1.0) for i in range(5)]
    with pytest.raises(ValueError, match="5 entries but 4 period ends"):
        wc.seasonal_trend_change("dso_trend", series, ends=[date(2025, 1, 1)] * 4)


# --- incremental revenue per capex -------------------------------------------------


def test_incremental_revenue_per_capex_across_a_gap_is_missing():
    series = [_q(2023, 3)] + _year(2025, revenue=150.0)
    m = capex.incremental_revenue_per_capex(series)
    assert m.status is MetricStatus.MISSING_DATA and m.value is None
    assert m.missing_fields == [
        "revenue a year before FY2025Q4 (FY2023Q4 ends 731 days earlier; need 330-400)"]
    assert m.fiscal_label == "FY2025Q4"


def test_incremental_revenue_per_capex_a_year_apart_is_unchanged():
    series = [_q(2024, 3)] + _year(2025, revenue=150.0)
    m = capex.incremental_revenue_per_capex(series)
    assert m.status is MetricStatus.OK
    assert m.value == pytest.approx((150.0 - 100.0) / 20.0)


def test_incremental_revenue_per_capex_refused_cites_nothing():
    periods = [_q(2023, 3)] + _year(2025, revenue=150.0)
    for p in periods:
        p.sources = {"revenue": _sv(p)}
    ds, bundle = _bundle(periods)
    m = bundle.get_latest("incremental_revenue_per_capex")
    assert m.status is MetricStatus.MISSING_DATA
    assert sources_for(ds, m, bundle=bundle) == {}
