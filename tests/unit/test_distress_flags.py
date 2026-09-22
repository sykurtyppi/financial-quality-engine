"""PR 0.3 — a distress-scored component must reach the flag list.

P0-9 made the scorer keep a NOT_MEANINGFUL-because-of-distress metric at its
maximum concern with full weight (`score_block`). But `_generate_flags`
dropped every component whose metric had no value — and a distress-scored
component is exactly one of those. "Non-positive EBITDA with net debt" scored
90 and was absent from the card's attention flags: the most damning
components were the only ones that could never be shown.

Invariant pinned here: a weight>0 component at or above the red threshold
produces exactly one red flag, whether or not the metric has a value.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.pipeline import RED_FLAG_CONCERN, _generate_flags, analyze
from app.schemas.metrics import MetricResult, MetricStatus
from app.schemas.report import AnalysisResult
from app.services.ingestion.companyfacts_mapper import build_dataset
from app.services.scoring.engine import ComponentContribution
from tests.fixtures.companies import clean_dataset, stretch_dataset

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real"
DISTRESS_METRICS = ("cfo_to_net_income", "net_debt_to_ebitda")


def distressed_dataset():
    """StretchCo with its trailing four quarters in unambiguous distress: net
    loss with cash burn, negative EBITDA carrying net debt. Four quarters
    because the two guarded metrics are computed on a TTM basis."""
    ds = stretch_dataset()
    for p in ds.periods[-4:]:
        p.net_income = -50.0
        p.cfo = -20.0
        p.ebit = -60.0
        p.depreciation_amortization = 10.0
        p.total_debt = 500.0
        p.cash_and_equivalents = 100.0
    return ds


def _qualifying(result: AnalysisResult) -> list[str]:
    """Metric names that the invariant says must carry exactly one red flag."""
    by_name = {m.name: m for m in result.metrics}
    out: list[str] = []
    for block in result.block_scores:
        for c in block.components:
            if c.weight == 0 or c.concern_score is None or c.concern_score < RED_FLAG_CONCERN:
                continue
            if c.metric_name in by_name and c.metric_name not in out:
                out.append(c.metric_name)
    return out


@pytest.mark.parametrize("make", [stretch_dataset, clean_dataset, distressed_dataset])
def test_every_red_threshold_component_carries_exactly_one_red_flag(make):
    result = analyze(make())
    qualifying = _qualifying(result)
    flagged = [f.evidence_metrics[0] for f in result.red_flags]
    if len(qualifying) <= 10:
        assert sorted(flagged) == sorted(qualifying)
    else:  # the list is capped at ten; every shown flag must still qualify
        assert len(flagged) == 10 and set(flagged) <= set(qualifying)
    assert len(flagged) == len(set(flagged)), "a metric flagged twice"


def test_distress_scored_components_now_reach_the_card():
    result = analyze(distressed_dataset())
    distress = {m.name: m for m in result.metrics if m.distress_signal}
    assert set(distress) == set(DISTRESS_METRICS), "fixture no longer triggers both guards"
    for name in DISTRESS_METRICS:
        assert distress[name].status is MetricStatus.NOT_MEANINGFUL
        assert distress[name].value is None
    flags = {f.evidence_metrics[0]: f for f in result.red_flags}
    assert flags["net_debt_to_ebitda"].title == "Elevated leverage — non-positive EBITDA with net debt"
    assert flags["cfo_to_net_income"].title == (
        "Operating cash flow lagging reported earnings — net loss with negative operating cash flow"
    )
    for name in DISTRESS_METRICS:
        detail = flags[name].detail
        assert detail.startswith(f"{name} undefined — ")
        assert "scored at this metric's maximum because the denominator itself signals distress" in detail
        assert "None" not in detail and "nan" not in detail.lower()
        assert flags[name].severity == "red"
        assert flags[name].fiscal_label == distress[name].fiscal_label
    # Never green: a distress state is the metric's maximum concern.
    assert not any(f.evidence_metrics[0] in DISTRESS_METRICS for f in result.green_flags)


def test_the_card_shows_the_distress_flags():
    from app.services.reporting.decision_card import render_decision_card
    from app.services.scoring.thermometer import compute_thermometer

    ds = distressed_dataset()
    result = analyze(ds)
    card = render_decision_card(
        result, compute_thermometer(result.block_scores, ds.periods), generated_on="2026-09-22"
    )
    assert "- Elevated leverage — non-positive EBITDA with net debt (" in card
    assert "- Operating cash flow lagging reported earnings — net loss with negative operating cash flow (" in card


def test_a_benign_not_meaningful_metric_still_does_not_flag():
    """The guard is distress-specific. A loss WITH positive operating cash
    flow (benign guard) and negative EBITDA at NET CASH (benign guard) are
    dropped from scoring and must stay out of the flag list."""
    ds = stretch_dataset()
    for p in ds.periods[-4:]:
        p.net_income = -50.0
        p.cfo = 40.0  # loss, but cash-generative -> benign, not distress
        p.ebit = -60.0
        p.depreciation_amortization = 10.0
        p.total_debt = 50.0
        p.cash_and_equivalents = 500.0  # net cash -> benign, not distress
    result = analyze(ds)
    by_name = {m.name: m for m in result.metrics}
    for name in DISTRESS_METRICS:
        assert by_name[name].status is MetricStatus.NOT_MEANINGFUL
        assert by_name[name].value is None
        assert by_name[name].distress_signal is False
    flagged = {f.evidence_metrics[0] for f in result.red_flags + result.green_flags}
    assert not flagged & set(DISTRESS_METRICS)


def _component(name, *, weight, concern, value=None):
    return ComponentContribution(
        metric_name=name, metric_value=value, concern_score=concern, weight=weight,
        anchors=[(0.0, 10.0), (6.0, 90.0)], status="not_meaningful", note="x",
    )


def _distress_metric(name):
    return MetricResult(
        name=name, formula="a / b", fiscal_label="FY2025Q4", status=MetricStatus.NOT_MEANINGFUL,
        note="Denominator crossed zero: maximum concern", distress_signal=True,
    )


def test_zero_weight_distress_component_still_never_flags():
    """P0-13 is preserved: exclusion from the score is exclusion from the
    flag list, distress or not."""
    blocks = [SimpleNamespace(components=[_component("m", weight=0.0, concern=90.0)])]
    red, green = _generate_flags(blocks, {"m": _distress_metric("m")})
    assert red == [] and green == []
    blocks = [SimpleNamespace(components=[_component("m", weight=0.2, concern=90.0)])]
    red, _ = _generate_flags(blocks, {"m": _distress_metric("m")})
    assert [f.evidence_metrics for f in red] == [["m"]]
    assert red[0].title == "Elevated concern: m — denominator crossed zero"


def test_a_valueless_metric_without_the_distress_flag_is_still_dropped():
    blocks = [SimpleNamespace(components=[_component("m", weight=0.2, concern=90.0)])]
    plain = _distress_metric("m").model_copy(update={"distress_signal": False})
    assert _generate_flags(blocks, {"m": plain}) == ([], [])
    assert _generate_flags(blocks, {}) == ([], [])


@pytest.mark.parametrize("ticker", ["AAPL", "KO", "CRM"])
def test_real_fixtures_carry_no_distress_component_so_their_flags_are_unchanged(ticker):
    """The change can only add flags where a distress-scored component
    exists. None of the calibration fixtures has one, so by construction
    their flag lists — and the golden StretchCo report — are what they were."""
    facts = json.loads((REAL / f"companyfacts_{ticker}_trimmed.json").read_text())
    ds, _ = build_dataset(facts, ticker, n_quarters=8)
    result = analyze(ds)
    assert not any(m.distress_signal for m in result.metrics)
    assert all(f.detail.split(" = ")[0] == f.evidence_metrics[0] for f in result.red_flags + result.green_flags)


# --- audit regressions (2026-09-22): the card's questions -------------------------

def _red(title, metric):
    from app.schemas.report import Flag

    return Flag(severity="red", title=title, detail="d", evidence_metrics=[metric],
                fiscal_label="FY2025Q4")


def test_a_distress_flag_keeps_its_analyst_question():
    """The distress title carries a suffix; an exact-title lookup lost the
    question, and with only such flags the card said nothing was flagged."""
    from app.core.pipeline import _analyst_questions

    flag = _red("Operating cash flow lagging reported earnings — net loss with cash burn",
                "cfo_to_net_income")
    assert _analyst_questions([flag], []) == [
        "Which accrual items explain the gap between net income and operating cash flow, "
        "and are they expected to reverse?"
    ]


def test_red_flags_without_a_template_never_read_as_nothing_flagged():
    from app.core.pipeline import _analyst_questions

    flags = [_red("Elevated concern: fcf_margin_trend", "fcf_margin_trend"),
             _red("Elevated leverage — non-positive EBITDA with net debt", "net_debt_to_ebitda")]
    questions = _analyst_questions(flags, [])
    assert questions and not any("No elevated-concern items were flagged" in q for q in questions)
    assert _analyst_questions([], []) == [
        "No elevated-concern items were flagged; confirm data completeness before "
        "concluding the period is clean."
    ]
