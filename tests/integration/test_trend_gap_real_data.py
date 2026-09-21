"""A filer whose newest quarter has not landed must not produce a clean score.

Synthetic series prove the trend functions refuse a stale current period.
This proves what that is WORTH, on real companyfacts pushed through the real
mapper, registry and scoring engine.

Two facts are removed from AAPL's trimmed companyfacts — the newest quarter's
operating-cash-flow and capex — which is what a late or partial 10-Q looks
like. Before the period-continuity fix that produced a complete, clean-looking
report:

    accrual_trend                ok   TTM FY2026Q2   -0.0356
    fcf_margin_trend             ok   TTM FY2026Q2    0.0435
    capex_intensity_regime_shift ok   FY2026Q2        0.0028
    Earnings Quality 15.0 | Capex Discipline 26.9 | overall 21.7

Every one of those numbers came from quarters before the one they were
labelled with. Worse, `accrual_trend` was the ONLY Earnings Quality metric
still scoring — 0.25 of 0.85 block weight, ~29%, just over the 25% floor. The
engine's own "insufficient data, no score asserted" rule was being held open
by the stale value it was supposed to exclude, and the block it let through
read 15.0: near the clean end of the scale, on a quarter with no cash-flow
data at all.

The fix does not add a refusal. It stops a stale value from defeating the
refusal that was already there.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.core.pipeline import analyze
from app.schemas.metrics import MetricStatus
from app.services.formulas.registry import compute_metrics
from app.services.ingestion.companyfacts_mapper import build_dataset

FIXTURES = Path(__file__).parent.parent / "fixtures" / "real"

# The concepts a late 10-Q would leave without a current-quarter value.
CASH_FLOW_CONCEPTS = (
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    "PaymentsToAcquirePropertyPlantAndEquipment",
)


@pytest.fixture(scope="module")
def gapped_aapl():
    raw = json.loads((FIXTURES / "companyfacts_AAPL_trimmed.json").read_text())
    dataset, _ = build_dataset(raw, "AAPL", n_quarters=8)
    latest_end = max(p.period_end for p in dataset.periods).isoformat()

    gapped = copy.deepcopy(raw)
    removed = 0
    for concept in CASH_FLOW_CONCEPTS:
        node = gapped["facts"]["us-gaap"].get(concept)
        if not node:
            continue
        for unit, rows in node["units"].items():
            keep = [r for r in rows if r.get("end") != latest_end]
            removed += len(rows) - len(keep)
            node["units"][unit] = keep
    assert removed, "fixture changed: no current-quarter cash-flow facts to remove"
    return build_dataset(gapped, "AAPL", n_quarters=8)[0]


def test_the_intact_fixture_still_scores(gapped_aapl):
    """Guards the guard: if AAPL stopped scoring for unrelated reasons, the
    assertions below would pass for the wrong reason."""
    raw = json.loads((FIXTURES / "companyfacts_AAPL_trimmed.json").read_text())
    dataset, _ = build_dataset(raw, "AAPL", n_quarters=8)
    assert analyze(dataset).overall.score is not None


@pytest.mark.parametrize("metric", [
    "accrual_trend", "fcf_margin_trend", "capex_intensity_regime_shift",
])
def test_no_trend_is_reported_for_a_quarter_with_no_data(gapped_aapl, metric):
    result = compute_metrics(gapped_aapl).get_latest(metric)
    assert result is not None
    assert result.status is MetricStatus.MISSING_DATA, (
        f"{metric} reported {result.value!r} for {result.fiscal_label}, "
        f"a quarter that supplied no observation"
    )


def test_the_coverage_floor_is_not_held_open_by_a_stale_metric(gapped_aapl):
    """The defect's real payload. `accrual_trend` was the only Earnings
    Quality metric left scoring, and its 0.25 of 0.85 block weight cleared the
    0.25 floor on its own — so a block with no usable current data returned
    15.0 instead of refusing."""
    result = analyze(gapped_aapl)
    earnings_quality = next(b for b in result.block_scores if b.name == "Earnings Quality")
    assert earnings_quality.score is None, (
        f"Earnings Quality scored {earnings_quality.score} with no current-quarter "
        f"cash-flow data — the coverage floor did not fire"
    )
    assert "Insufficient data" in (earnings_quality.rationale or "")


def test_the_report_refuses_an_overall_score(gapped_aapl):
    overall = analyze(gapped_aapl).overall
    assert overall.score is None
