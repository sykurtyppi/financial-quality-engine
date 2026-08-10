"""Assumption-resolver tests (P1-E follow-up). Verifies the engine's proposed
met/violated/unresolvable calls across every input class the CLI can send it."""

from datetime import date

import pytest

from app.schemas.financials import CompanyDataset, CompanyProfile, PeriodFinancials, PeriodType
from app.schemas.metrics import MetricResult, MetricStatus
from app.services.formulas.registry import MetricsBundle
from app.services.journal.resolver import propose_resolution
from app.services.journal.schema_v2 import Assumption


def _p(**kw) -> PeriodFinancials:
    base = dict(period_end=date(2026, 6, 30), period_type=PeriodType.QUARTER,
                fiscal_label="FY2026Q2")
    base.update(kw)
    return PeriodFinancials(**base)


def _ds(*periods: PeriodFinancials) -> CompanyDataset:
    return CompanyDataset(profile=CompanyProfile(ticker="MXL"), periods=list(periods))


def _a(**overrides) -> Assumption:
    base = dict(metric="revenue", comparator=">", threshold=165_000_000.0,
                window="FY2026Q2", source="10-Q", resolve_by=date(2026, 8, 15))
    base.update(overrides)
    return Assumption(**base)


def _bundle(metric_name: str, value: float | None, status: MetricStatus = MetricStatus.OK,
            label: str = "FY2026Q2") -> MetricsBundle:
    m = MetricResult(name=metric_name, formula="test", fiscal_label=label,
                     status=status, value=value)
    return MetricsBundle(latest=[m], history={metric_name: [m]})


class TestXbrlFieldPath:
    def test_met_when_revenue_above_threshold(self):
        r = propose_resolution(_a(), _ds(_p(revenue=172_000_000)))
        assert r.state == "met"
        assert r.observed == 172_000_000
        assert r.at == date(2026, 6, 30)

    def test_violated_when_revenue_below_threshold(self):
        r = propose_resolution(_a(), _ds(_p(revenue=150_000_000)))
        assert r.state == "violated"

    def test_missing_field_is_unresolvable(self):
        r = propose_resolution(_a(), _ds(_p()))  # no revenue
        assert r.state == "unresolvable"
        assert "missing" in (r.note or "").lower()


class TestEngineMetricPath:
    def test_engine_metric_beats_xbrl_field(self):
        # cfo_to_net_income is a bundle metric — not a PeriodFinancials field.
        a = _a(metric="cfo_to_net_income", comparator=">", threshold=0.8)
        r = propose_resolution(a, _ds(_p(cfo=100.0, net_income=100.0)),
                               bundle=_bundle("cfo_to_net_income", 1.05))
        assert r.state == "met"
        assert r.observed == 1.05

    def test_engine_metric_not_meaningful_is_unresolvable(self):
        a = _a(metric="cfo_to_net_income", comparator=">", threshold=0.8)
        r = propose_resolution(
            a, _ds(_p(cfo=100.0, net_income=-10.0)),
            bundle=_bundle("cfo_to_net_income", None, status=MetricStatus.NOT_MEANINGFUL),
        )
        assert r.state == "unresolvable"
        assert "not_meaningful" in (r.note or "")


class TestWindow:
    def test_no_matching_period_is_unresolvable(self):
        r = propose_resolution(_a(window="FY2027Q1"), _ds(_p(revenue=200_000_000)))
        assert r.state == "unresolvable"
        assert r.at is None  # never happened → no period date
        assert "FY2027Q1" in (r.note or "")

    def test_window_match_is_case_insensitive(self):
        r = propose_resolution(_a(window="fy2026q2"), _ds(_p(revenue=200_000_000)))
        assert r.state == "met"


class TestSymbolicThresholds:
    def test_positive_met(self):
        a = _a(metric="cfo", comparator=">", threshold="positive")
        r = propose_resolution(a, _ds(_p(cfo=5.0)))
        assert r.state == "met" and r.observed == 5.0

    def test_positive_violated_on_negative_cfo(self):
        a = _a(metric="cfo", comparator=">", threshold="positive")
        r = propose_resolution(a, _ds(_p(cfo=-3.0)))
        assert r.state == "violated"

    def test_unknown_symbolic_threshold_is_unresolvable_never_fabricated(self):
        a = _a(metric="cfo", comparator=">", threshold="strong")
        r = propose_resolution(a, _ds(_p(cfo=5.0)))
        assert r.state == "unresolvable"
        assert "strong" in (r.note or "")


class TestUnsupportedComparator:
    def test_within_returns_unresolvable_with_note(self):
        a = _a(comparator="within", threshold=1.0)
        r = propose_resolution(a, _ds(_p(revenue=1.0)))
        assert r.state == "unresolvable"
        assert "within" in (r.note or "")


class TestFabricationSafety:
    def test_never_marks_met_without_a_value(self):
        # No matching period + no metric — nothing to compare against. Must
        # produce unresolvable, never met/violated.
        r = propose_resolution(_a(window="FY2029Q1"), _ds(_p(revenue=999_999_999)))
        assert r.state == "unresolvable"

    def test_index_is_carried_through(self):
        r = propose_resolution(_a(), _ds(_p(revenue=200_000_000)), assumption_index=3)
        assert r.assumption_index == 3
