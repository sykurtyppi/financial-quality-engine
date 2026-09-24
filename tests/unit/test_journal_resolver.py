"""Assumption-resolver tests (P1-E follow-up). Verifies the engine's proposed
met/violated/unresolvable calls across every input class the CLI can send it."""

from datetime import date
from pathlib import Path

import pytest

from app.schemas.financials import (
    CompanyDataset,
    CompanyProfile,
    PeriodFinancials,
    PeriodType,
)
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
    # Default: source=None → a numeric-only commitment. The
    # TestSourceProvenance suites pass a source explicitly.
    base = dict(metric="revenue", comparator=">", threshold=165_000_000.0,
                window="FY2026Q2", source=None, resolve_by=date(2026, 8, 15))
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

    def test_missing_field_is_pending_not_unresolvable(self):
        # Round-10 finding 4: missing data is RETRYABLE (pending), not
        # terminal. The field exists on PeriodFinancials — the value just
        # hasn't been populated for this period yet.
        r = propose_resolution(_a(), _ds(_p()))  # no revenue
        assert r.state == "pending"
        assert "missing" in (r.note or "").lower()

    def test_unknown_metric_is_unresolvable_not_pending(self):
        # Round-10 finding 4 boundary: an unknown metric name IS structural.
        # No later filing can conjure a `fake_metric_xyz` into existence.
        a = _a(metric="fake_metric_xyz")
        r = propose_resolution(a, _ds(_p(revenue=200_000_000)))
        assert r.state == "unresolvable"
        assert "unknown" in (r.note or "").lower()


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
    def test_no_matching_period_is_pending(self):
        # Round-10 finding 4: the requested fiscal period may not have been
        # filed yet — retryable.
        r = propose_resolution(_a(window="FY2027Q1"), _ds(_p(revenue=200_000_000)))
        assert r.state == "pending"
        assert r.at is None  # no period landed → no observation date
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

    def test_symbolic_mismatched_comparator_refused_defensively(self):
        # Round-11 finding 3: `cfo < positive` used to resolve met when CFO
        # was positive (the resolver ignored the `<`). Now the resolver
        # returns unresolvable even for unlocked/programmatic entries where
        # can_lock never ran.
        a = _a(metric="cfo", comparator="<", threshold="positive")
        r = propose_resolution(a, _ds(_p(cfo=5.0)))
        assert r.state == "unresolvable"

    def test_negative_symbolic_needs_lt_not_gt(self):
        # Same defense in the other direction.
        a = _a(metric="cfo", comparator=">", threshold="negative")
        r = propose_resolution(a, _ds(_p(cfo=-5.0)))
        assert r.state == "unresolvable"


    # Hermes audit round 4: the zero boundary of every symbolic keyword was
    # unpinned — `positive` accepting zero or `non_negative` rejecting it
    # survived the suite. Zero is where the strict and non-strict keywords
    # disagree, so it is checked for each, with a value either side.
    @pytest.mark.parametrize(
        ("threshold", "comparator", "cfo", "state"),
        [
            ("positive", ">", 0.0, "violated"),
            ("positive", ">", 1e-9, "met"),
            ("negative", "<", 0.0, "violated"),
            ("negative", "<", -1e-9, "met"),
            ("non_negative", ">=", 0.0, "met"),
            ("nonnegative", ">=", 0.0, "met"),
            ("non_negative", ">=", -1e-9, "violated"),
            ("nonnegative", ">=", -1e-9, "violated"),
            ("non_positive", "<=", 0.0, "met"),
            ("nonpositive", "<=", 0.0, "met"),
            ("non_positive", "<=", 1e-9, "violated"),
            ("nonpositive", "<=", 1e-9, "violated"),
            ("zero", "==", 0.0, "met"),
            ("zero", "==", 1e-9, "violated"),
            ("zero", "==", -1e-9, "violated"),
        ],
    )
    def test_zero_boundary_of_each_keyword(self, threshold, comparator, cfo, state):
        a = _a(metric="cfo", comparator=comparator, threshold=threshold)
        r = propose_resolution(a, _ds(_p(cfo=cfo)))
        assert r.state == state and r.observed == cfo


class TestNumericBoundaries:
    # Found by the in-repo mutation harness (`>` -> `>=` survived): a value
    # exactly AT the threshold is where the strict and non-strict
    # comparators disagree, and no test put one there.
    @pytest.mark.parametrize(
        ("comparator", "revenue", "state"),
        [
            (">", 100.0, "violated"), (">", 100.5, "met"),
            ("<", 100.0, "violated"), ("<", 99.5, "met"),
            (">=", 100.0, "met"), (">=", 99.5, "violated"),
            ("<=", 100.0, "met"), ("<=", 100.5, "violated"),
            ("==", 100.0, "met"), ("==", 100.5, "violated"),
        ],
    )
    def test_a_value_at_the_threshold(self, comparator, revenue, state):
        a = _a(comparator=comparator, threshold=100.0)
        assert propose_resolution(a, _ds(_p(revenue=revenue))).state == state


class TestUnsupportedComparator:
    def test_within_returns_unresolvable_with_note(self):
        # `can_lock` refuses `within` (round-10 finding 6) so it should not
        # reach the resolver in normal use. Kept as a defensive check for
        # programmatically-constructed (unlocked) entries.
        a = _a(comparator="within", threshold=1.0)
        r = propose_resolution(a, _ds(_p(revenue=1.0)))
        assert r.state == "unresolvable"
        assert "within" in (r.note or "")


class TestFabricationSafety:
    def test_never_marks_met_without_a_value(self):
        # No matching period + no metric — nothing to compare against. Must
        # never produce met/violated. (`pending` for a missing window is the
        # correct behavior now — the filing may still land.)
        r = propose_resolution(_a(window="FY2029Q1"), _ds(_p(revenue=999_999_999)))
        assert r.state != "met" and r.state != "violated"

    def test_index_is_carried_through(self):
        r = propose_resolution(_a(), _ds(_p(revenue=200_000_000)), assumption_index=3)
        assert r.assumption_index == 3


class TestSourceProvenance:
    """Round-11 finding 2: the {10-K, 10-Q} whitelist was NOT provenance — the
    same value resolved met under either form with no accession, so every
    source-set assumption was parked at pending. Mapped values now carry the
    filed facts they were computed from (`PeriodFinancials.sources`): a
    preregistered form is attested against the filings that REPORTED the
    period, and the accession is recorded."""

    @staticmethod
    def _sourced(value, *refs):
        from app.schemas.financials import SourcedValue

        method = "direct" if len(refs) == 1 else "ytd_diff"
        return {"revenue": SourcedValue(field="revenue", value=value, strategy="single",
                                        method=method, partial=False, inputs=refs)}

    @staticmethod
    def _ref(form, accession, filed, *, start=date(2026, 4, 1), end=date(2026, 6, 30),
             value=200_000_000.0, sign=1):
        from app.schemas.financials import FactRef

        return FactRef(concept="us-gaap:Revenues", accession=accession, filed=filed,
                       form=form, start=start, end=end, value=value, sign=sign)

    def _period(self, *refs, value=200_000_000.0):
        return _p(revenue=value, sources=self._sourced(value, *refs))

    def test_no_source_resolves_numerically_and_still_names_the_filing(self):
        ref = self._ref("10-Q", "0001-26-000010", date(2026, 8, 1))
        r = propose_resolution(_a(source=None), _ds(self._period(ref)))
        assert r.state == "met" and r.source_accession == "0001-26-000010"
        assert "reported in 10-Q 0001-26-000010 filed 2026-08-01" in r.note

    def test_a_same_day_amendment_is_the_filing_cited(self):
        """Two reporting facts filed on one day: the 10-Q/A is current, by the
        shared `precedence` order, whichever is listed first."""
        orig = self._ref("10-Q", "0001-26-000020", date(2026, 8, 1), value=100_000_000.0)
        amended = self._ref("10-Q/A", "0001-26-000010", date(2026, 8, 1), value=100_000_000.0)
        for refs in ((orig, amended), (amended, orig)):
            r = propose_resolution(_a(source=None), _ds(self._period(*refs)))
            assert r.source_accession == "0001-26-000010"
            assert "reported in 10-Q/A 0001-26-000010 filed 2026-08-01" in r.note

    def test_a_matching_form_resolves_with_its_accession(self):
        ref = self._ref("10-Q", "0001-26-000010", date(2026, 8, 1))
        r = propose_resolution(_a(source="10-Q"), _ds(self._period(ref)))
        assert r.state == "met" and r.source_accession == "0001-26-000010"
        r = propose_resolution(_a(source="10-Q", threshold=300_000_000.0),
                               _ds(self._period(ref)))
        assert r.state == "violated" and r.source_accession == "0001-26-000010"

    def test_another_form_stays_pending_and_names_what_reported_it(self):
        ref = self._ref("10-K", "0001-26-000099", date(2026, 9, 1))
        r = propose_resolution(_a(source="10-Q"), _ds(self._period(ref)))
        assert r.state == "pending" and r.source_accession is None
        assert "10-K 0001-26-000099 filed 2026-09-01" in r.note
        assert r.observed == 200_000_000.0  # shown, never committed

    def test_the_family_takes_an_amendment_but_an_amendment_source_takes_only_one(self):
        amended = self._ref("10-Q/A", "0001-26-000011", date(2026, 9, 10))
        original = self._ref("10-Q", "0001-26-000010", date(2026, 8, 1))
        assert propose_resolution(_a(source="10-Q"), _ds(self._period(amended))).state == "met"
        assert propose_resolution(_a(source="10-Q/A"), _ds(self._period(amended))).state == "met"
        assert propose_resolution(_a(source="10-Q/A"), _ds(self._period(original))).state == "pending"
        assert propose_resolution(_a(source="10-K"), _ds(self._period(amended))).state == "pending"

    def test_proxy_and_other_forms(self):
        def state(source, form):
            ref = self._ref(form, "0001-26-000012", date(2026, 8, 1))
            return propose_resolution(_a(source=source), _ds(self._period(ref))).state

        assert state("proxy", "DEF 14A") == "met" and state("proxy", "10-Q") == "pending"
        assert state("other", "20-F") == "met" and state("other", "10-K") == "pending"
        assert state("8-K", "8-K") == "met" and state("8-K", "10-Q") == "pending"

    def test_the_subtracted_prior_quarter_is_not_what_reported_the_period(self):
        """A year-to-date difference: +H1 (this 10-Q) −Q1 (an earlier 10-K/A,
        say). The earlier filing reported Q1, not this quarter."""
        ytd = self._ref("10-Q", "0001-26-000010", date(2026, 8, 1), start=date(2026, 1, 1),
                        value=300_000_000.0)
        q1 = self._ref("10-K/A", "0001-26-000005", date(2026, 5, 20), start=date(2026, 1, 1),
                       end=date(2026, 3, 31), value=100_000_000.0, sign=-1)
        r = propose_resolution(_a(source="10-Q"), _ds(self._period(ytd, q1)))
        assert r.state == "met" and r.source_accession == "0001-26-000010"

    def test_every_filing_that_reported_the_period_must_match(self):
        a = self._ref("10-Q", "0001-26-000010", date(2026, 8, 1), value=120_000_000.0)
        b = self._ref("8-K", "0001-26-000020", date(2026, 8, 2), value=80_000_000.0)
        r = propose_resolution(_a(source="10-Q"), _ds(self._period(a, b)))
        assert r.state == "pending" and "8-K 0001-26-000020" in r.note

    def test_the_accession_is_the_filing_that_completed_the_value(self):
        """Two reporting facts, both 10-Q family — a component re-filed on a
        10-Q/A: the value as it stands was completed by the later filing."""
        a = self._ref("10-Q", "0001-26-000010", date(2026, 8, 1), value=120_000_000.0)
        b = self._ref("10-Q/A", "0001-26-000011", date(2026, 9, 10), value=80_000_000.0)
        r = propose_resolution(_a(source="10-Q"), _ds(self._period(a, b)))
        assert r.state == "met" and r.source_accession == "0001-26-000011"
        r = propose_resolution(_a(source="10-Q"), _ds(self._period(b, a)))
        assert r.source_accession == "0001-26-000011"

    def test_without_per_value_provenance_it_stays_pending(self):
        r = propose_resolution(_a(source="10-Q"), _ds(_p(revenue=200_000_000)))
        assert r.state == "pending" and "no per-value provenance" in r.note
        assert propose_resolution(_a(source=None), _ds(_p(revenue=200_000_000))).state == "met"

    def test_a_missing_period_or_value_is_pending_before_any_attestation(self):
        r = propose_resolution(_a(source="8-K", window="FY2029Q1"), _ds(_p(revenue=1)))
        assert r.state == "pending" and "no period matching" in r.note
        r = propose_resolution(_a(source="10-Q", metric="nonsense"), _ds(_p(revenue=1)))
        assert r.state == "unresolvable"

    def test_an_attested_symbolic_mismatch_is_unresolvable(self):
        from app.schemas.financials import SourcedValue

        ref = self._ref("10-Q", "0001-26-000010", date(2026, 8, 1), value=5.0)
        cfo = SourcedValue(field="cfo", value=5.0, strategy="single", method="direct",
                           partial=False, inputs=(ref,))
        r = propose_resolution(
            _a(source="10-Q", metric="cfo", comparator="<", threshold="positive"),
            _ds(_p(cfo=5.0, sources={"cfo": cfo})),
        )
        assert r.state == "unresolvable" and r.source_accession == "0001-26-000010"


class TestSourceProvenanceOnRealFilings:
    """On a real companyfacts payload: a quarter a 10-Q reported attests as
    10-Q; a fourth quarter the mapper derives from the 10-K's year was
    reported by the 10-K — the 10-Q whose nine months it subtracts did not
    report it — and says so; an engine metric attests through the facts
    behind its inputs."""

    @staticmethod
    def _ko():
        import json
        from pathlib import Path

        from app.services.formulas.registry import compute_metrics
        from app.services.ingestion.companyfacts_mapper import build_dataset

        path = Path(__file__).resolve().parents[1] / "fixtures" / "real" / "companyfacts_KO_trimmed.json"
        ds, _ = build_dataset(json.loads(path.read_text()), "KO")
        return ds, compute_metrics(ds)

    @staticmethod
    def _assume(metric, period, source):
        return _a(metric=metric, comparator=">", threshold=0.0, window=period.fiscal_label,
                  source=source)

    def test_a_10q_quarter_attests_as_10q_with_the_filing_used(self):
        ds, bundle = self._ko()
        period = next(p for p in ds.periods if p.sources["revenue"].method == "direct"
                      and p.sources["revenue"].inputs[0].form.startswith("10-Q"))
        r = propose_resolution(self._assume("revenue", period, "10-Q"), ds, bundle)
        assert r.state == "met"
        assert r.source_accession == period.sources["revenue"].inputs[0].accession

    def test_a_q4_derived_from_the_10k_is_the_10ks(self):
        ds, bundle = self._ko()
        # KO's Q4: the 10-K's year less the nine months its Q3 10-Q reported.
        period = next(p for p in ds.periods if p.fiscal_label.endswith("Q4"))
        annual, nine_months = period.sources["revenue"].inputs
        assert annual.form == "10-K" and annual.sign == 1
        assert nine_months.form == "10-Q" and nine_months.sign == -1
        r = propose_resolution(self._assume("revenue", period, "10-Q"), ds, bundle)
        assert r.state == "pending" and annual.accession in r.note
        r = propose_resolution(self._assume("revenue", period, "10-K"), ds, bundle)
        assert r.state == "met" and r.source_accession == annual.accession

    def test_an_engine_metric_attests_through_its_inputs(self):
        ds, bundle = self._ko()
        metric = bundle.get_latest("cfo_to_net_income")
        period = next(p for p in ds.periods if metric.fiscal_label.endswith(p.fiscal_label))
        expect = {r.form.removesuffix("/A") for sv in (period.sources["cfo"],
                                                       period.sources["net_income"])
                  for r in sv.inputs if r.sign > 0 and r.end >= period.period_end}
        [form] = expect  # one filing reported the metric's own quarter
        r = propose_resolution(self._assume("cfo_to_net_income", period, form), ds, bundle)
        assert r.state in ("met", "violated") and r.source_accession
        assert "TTM ending" in r.note


def test_the_resolve_command_prints_the_filing_behind_each_proposal(monkeypatch, capsys):
    import argparse
    from types import SimpleNamespace

    from app.services.ingestion import edgar_adapter
    from scripts import journal

    ds, _bundle = TestSourceProvenanceOnRealFilings._ko()
    period = ds.sorted_periods()[-1]
    assumption = _a(metric="revenue", comparator=">", threshold=0.0,
                    window=period.fiscal_label, source="10-Q")
    entry = SimpleNamespace(ticker="KO", before=SimpleNamespace(assumptions=[assumption]))
    monkeypatch.setattr(journal.store, "find_entry", lambda t, d: Path("KO_x.md"))
    monkeypatch.setattr(journal.store, "is_v2", lambda p: True)
    monkeypatch.setattr(journal.store, "load_v2", lambda p: entry)
    monkeypatch.setattr(journal, "verify_lock", lambda e: True)
    monkeypatch.setattr(journal, "open_assumption_indices", lambda e: [0])
    monkeypatch.setattr(edgar_adapter, "fetch_dataset", lambda t: (ds, None))

    assert journal.cmd_resolve(argparse.Namespace(ticker="KO", date=None, commit=False)) == 0
    out = capsys.readouterr().out
    accession = period.sources["revenue"].inputs[0].accession
    assert "MET" in out and f"source: {accession}" in out
