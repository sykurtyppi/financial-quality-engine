"""A trend may not be labelled with a period that contributed nothing to it.

Every trend metric here computes over the OK observations but labelled its
result with `series[-1].fiscal_label` — the period the caller ASKED about.
When the newest observation was not OK the two diverged: a value derived
entirely from older quarters came back `OK`, stamped with the current
quarter, and the scoring layer consumed it as a current-period signal.

Three consequences, all silent:
  * missingness became an apparently valid measurement;
  * the scorecard showed freshness it did not have;
  * a backtest row's labelled period did not match the information that
    produced it, so replayed results were not reproducible from the data
    available at that date.

`capex_intensity_regime_shift` was worse than mislabelled: it compares a
recent window against everything prior, so compacting unusable periods out of
the list slid the window backwards onto older quarters. Both halves of the
comparison then described the wrong span.

1054 tests passed with all of this present. The suite measured that the
functions returned a number, never which period the number was about.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.config.scoring_config import BLOCKS
from app.schemas.financials import PeriodFinancials, PeriodType
from app.schemas.metrics import MetricResult, MetricStatus
from app.services.formulas.accruals import accrual_trend
from app.services.formulas.capex import capex_intensity_regime_shift
from app.services.formulas.working_capital import seasonal_trend_change, trend_change
from app.services.scoring.engine import score_block


def _ok(label: str, value: float, name: str = "m") -> MetricResult:
    return MetricResult(name=name, formula="f", fiscal_label=label,
                        status=MetricStatus.OK, value=value)


def _missing(label: str, name: str = "m") -> MetricResult:
    return MetricResult(name=name, formula="f", fiscal_label=label,
                        status=MetricStatus.MISSING_DATA, missing_fields=["input"])


def _not_meaningful(label: str, name: str = "m") -> MetricResult:
    return MetricResult(name=name, formula="f", fiscal_label=label,
                        status=MetricStatus.NOT_MEANINGFUL, note="zero denominator")


def _period(label: str, capex: float | None, revenue: float | None = 1000.0) -> PeriodFinancials:
    return PeriodFinancials(fiscal_label=label, period_end=date(2026, 1, 1),
                            period_type=PeriodType.QUARTER, capex=capex, revenue=revenue)


_HISTORY = [_ok("FY2026Q1", 0.01), _ok("FY2026Q2", 0.02), _ok("FY2026Q3", 0.03)]

# Every trend that takes a MetricResult series, with enough history that the
# OLD code had a usable window after discarding the newest observation.
_SERIES_TRENDS = [
    pytest.param(lambda s: accrual_trend(s), id="accrual_trend"),
    pytest.param(lambda s: trend_change("fcf_margin_trend", s), id="fcf_margin_trend"),
    pytest.param(lambda s: seasonal_trend_change("dso_trend", s), id="dso_trend"),
]


@pytest.mark.parametrize("fn", _SERIES_TRENDS)
@pytest.mark.parametrize("absent", [_missing, _not_meaningful])
def test_an_unusable_latest_period_is_not_reported_as_a_current_trend(fn, absent):
    long_history = [_ok(f"FY202{4 + i // 4}Q{i % 4 + 1}", 0.01 * i) for i in range(8)]
    result = fn(long_history + [absent("FY2026Q4")])
    assert result.status is MetricStatus.MISSING_DATA
    assert result.value is None
    # The label is kept: the caller did ask about this period, and the honest
    # answer is that this period has no value — not that an older one does.
    assert result.fiscal_label == "FY2026Q4"


@pytest.mark.parametrize("fn", _SERIES_TRENDS)
def test_a_usable_latest_period_still_computes(fn):
    long_history = [_ok(f"FY202{4 + i // 4}Q{i % 4 + 1}", 0.01 * i) for i in range(8)]
    result = fn(long_history + [_ok("FY2026Q4", 0.5)])
    assert result.status is MetricStatus.OK
    assert result.value is not None
    assert result.fiscal_label == "FY2026Q4"


@pytest.mark.parametrize("fn", [
    pytest.param(lambda s: accrual_trend(s), id="accrual_trend"),
    pytest.param(lambda s: trend_change("fcf_margin_trend", s), id="fcf_margin_trend"),
])
def test_contributing_periods_are_disclosed(fn):
    result = fn(_HISTORY + [_ok("FY2026Q4", 0.09)])
    assert result.note and "4 period(s)" in result.note and "FY2026Q1" in result.note


@pytest.mark.parametrize("fn", [
    pytest.param(lambda s: accrual_trend(s), id="accrual_trend"),
    pytest.param(lambda s: trend_change("fcf_margin_trend", s), id="fcf_margin_trend"),
])
def test_a_hole_in_the_middle_is_visible_in_the_provenance(fn):
    """A mean over priors tolerates a gap, but the reader must be able to see
    one: 3 contributors spanning Q1-Q4 is not the same evidence as 4."""
    gapped = [_ok("FY2026Q1", 0.01), _missing("FY2026Q2"), _ok("FY2026Q3", 0.03),
              _ok("FY2026Q4", 0.09)]
    result = fn(gapped)
    assert result.status is MetricStatus.OK
    assert result.note and "3 period(s)" in result.note


# --- capex: the window itself moved, not only the label --------------------

def test_capex_window_does_not_slide_backwards_over_a_gap():
    full = [_period(f"Q{i}", 100.0 + 10 * i) for i in range(1, 8)]
    assert capex_intensity_regime_shift(full).status is MetricStatus.OK

    gapped = full[:4] + [_period("Q5", None)] + full[5:]
    result = capex_intensity_regime_shift(gapped)
    assert result.status is MetricStatus.MISSING_DATA
    assert "gaps at Q5" in result.missing_fields[0]


def test_capex_reports_the_span_it_actually_used():
    full = [_period(f"Q{i}", 100.0 + 10 * i) for i in range(1, 8)]
    result = capex_intensity_regime_shift(full)
    assert result.note == "recent window Q4–Q7; baseline Q1–Q3"
    assert result.inputs["n_recent"] == 4.0 and result.inputs["n_prior"] == 3.0


def test_capex_names_the_latest_period_when_that_is_what_is_missing():
    """Two guards can both refuse this input; they are not interchangeable.
    The operator reading `gaps at Q7` looks for a hole in the middle of the
    window, while `the most recent period supplied no observation` points at
    a late filing. Mutation testing found the specific guard removable with
    the suite still green, because only the status was pinned."""
    series = [_period(f"Q{i}", 100.0 + 10 * i) for i in range(1, 7)] + [_period("Q7", None)]
    result = capex_intensity_regime_shift(series)
    assert result.status is MetricStatus.MISSING_DATA
    why = result.missing_fields[0]
    assert "the most recent period supplied no observation" in why
    assert "Q7" in why


def test_capex_needs_a_baseline_not_just_a_window():
    just_the_window = [_period(f"Q{i}", 100.0) for i in range(1, 6)]
    result = capex_intensity_regime_shift(just_the_window)
    assert result.status is MetricStatus.MISSING_DATA


# --- the reason it matters -------------------------------------------------

def test_a_stale_trend_could_suppress_a_genuinely_elevated_current_reading():
    """The scoring consequence, and the direction that matters: a stale trend
    carrying three benign prior quarters was scored as a current observation
    and DILUTED a real current-period accrual spike. Refusing it hands the
    weight back to the metric that was actually measured this period."""
    benign_history = [_ok("FY2026Q1", -0.01), _ok("FY2026Q2", -0.01), _ok("FY2026Q3", -0.01)]
    elevated_now = MetricResult(name="total_accruals", formula="f",
                                fiscal_label="FY2026Q4", status=MetricStatus.OK, value=0.12)
    earnings_quality = next(b for b in BLOCKS if b.name == "Earnings Quality")

    stale = accrual_trend(benign_history + [_missing("FY2026Q4")])
    assert stale.status is MetricStatus.MISSING_DATA

    scored = score_block(earnings_quality,
                         {"total_accruals": elevated_now, "accrual_trend": stale})
    component = next(c for c in scored.components if c.metric_name == "accrual_trend")
    assert component.metric_value is None
    assert component.concern_score is None
    # The block now reflects the one thing this quarter actually measured.
    assert scored.score is not None and scored.score > 80


# --- the non-finite contract ----------------------------------------------
# `build_metric` refuses a non-finite value for every metric built through it,
# and this package's contract says so at the top of `base.py`. The trend
# functions construct their MetricResult directly and had opted out. Found by
# adversarial probing rather than mutation: no mutant expresses "forgot to
# apply a rule the rest of the module applies".

_NON_FINITE = [float("nan"), float("inf"), float("-inf")]


@pytest.mark.parametrize("bad_value", _NON_FINITE)
@pytest.mark.parametrize("fn", [
    pytest.param(lambda s: accrual_trend(s), id="accrual_trend"),
    pytest.param(lambda s: trend_change("fcf_margin_trend", s), id="fcf_margin_trend"),
])
def test_a_non_finite_result_is_not_meaningful(fn, bad_value):
    result = fn([_ok("FY2026Q1", 1.0), _ok("FY2026Q2", 2.0),
                 _ok("FY2026Q3", 3.0), _ok("FY2026Q4", bad_value)])
    assert result.status is MetricStatus.NOT_MEANINGFUL
    assert result.note == "Non-finite result"


def test_a_nan_would_otherwise_crash_the_scorer():
    """Severity, stated concretely. `interpolate_concern` has no branch for a
    NaN and raises AssertionError('unreachable'), which is not caught anywhere
    on the scoring path — one NaN takes the whole report down rather than
    degrading one metric."""
    from app.services.scoring.engine import interpolate_concern

    anchors = [(-0.02, 15), (0.0, 30), (0.03, 60), (0.08, 85)]
    with pytest.raises(AssertionError):
        interpolate_concern(float("nan"), anchors)
    # An infinity does not crash — it silently clamps to the outermost anchor,
    # scoring maximum concern on a number that means nothing. Also refused.
    assert interpolate_concern(float("inf"), anchors) == 85


def test_the_guard_is_unreachable_from_the_real_pipeline_today():
    """Honest scope: every trend's inputs come from `build_metric`, which
    already filters non-finite values, so this closes a contract hole rather
    than a live failure. If a future metric stops going through
    `build_metric`, this stops being true — hence the guard."""
    import inspect

    from app.services.formulas import accruals, working_capital

    for module, fname in [(accruals, "total_accruals"), (accruals, "fcf_margin"),
                          (working_capital, "dso"), (working_capital, "dio")]:
        src = inspect.getsource(getattr(module, fname))
        assert "build_metric(" in src, (
            f"{fname} no longer goes through build_metric; a non-finite value "
            f"can now reach a trend, and the guard is load-bearing"
        )


@pytest.mark.parametrize("bad_capex", [float("inf"), float("nan")])
def test_capex_refuses_a_non_finite_regime_shift(bad_capex):
    """capex takes PeriodFinancials rather than MetricResult, so it needs its
    own case — the parametrized pair above cannot reach it. Mutation testing
    caught the omission: its guard was removable with the suite green."""
    series = [_period(f"Q{i}", 100.0 + 10 * i) for i in range(1, 7)]
    series.append(_period("Q7", bad_capex))
    result = capex_intensity_regime_shift(series)
    assert result.status is MetricStatus.NOT_MEANINGFUL
    assert result.note == "Non-finite result"
