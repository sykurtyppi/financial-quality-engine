"""Distress thermometer tests (P1-C). Verifies the AOM aggregation
(average-within-cluster, max-across-clusters) and additive regime dummies."""

from datetime import date

from app.schemas.financials import PeriodFinancials, PeriodType
from app.schemas.metrics import MetricResult, MetricStatus
from app.schemas.scoring import BlockScore, ComponentContribution, Confidence, Direction
from app.services.scoring.thermometer import compute_thermometer


def _hist(name: str, values: list[float]) -> list[MetricResult]:
    return [
        MetricResult(name=name, formula="t", fiscal_label=f"Q{i}", status=MetricStatus.OK, value=v)
        for i, v in enumerate(values)
    ]


def _component(name: str, concern: float | None) -> ComponentContribution:
    return ComponentContribution(
        metric_name=name,
        metric_value=1.0 if concern is not None else None,
        concern_score=concern,
        weight=1.0,
        anchors=[(0.0, 0.0), (1.0, 100.0)],
        status="ok" if concern is not None else "missing_data",
    )


def _block(name: str, concerns: dict[str, float | None]) -> BlockScore:
    return BlockScore(
        name=name,
        score=50.0,
        direction=Direction.MIXED,
        confidence=Confidence.MEDIUM,
        rationale="test",
        components=[_component(n, c) for n, c in concerns.items()],
        data_coverage=1.0,
    )


def _period(label: str, **kw) -> PeriodFinancials:
    return PeriodFinancials(
        period_end=kw.pop("period_end", date(2025, 12, 31)),
        period_type=PeriodType.QUARTER,
        fiscal_label=label,
        **kw,
    )


HEALTHY = [_period("FY2025Q4", net_income=100.0, ebit=120.0, depreciation_amortization=20.0)]


class TestAOMAggregation:
    def test_max_across_clusters_not_diluted_by_calm_clusters(self):
        # One screaming cluster (leverage 90), three calm ones (~10). Weighted
        # averaging would bury the 90; AOM must surface it.
        blocks = [
            _block("Balance Sheet Stress", {"net_debt_to_ebitda": 90.0, "interest_coverage": 90.0}),
            _block("Cash Conversion", {"cfo_to_net_income": 10.0, "fcf_margin": 10.0}),
            _block("Capital Integrity", {"issuance_pressure": 10.0}),
        ]
        t = compute_thermometer(blocks, HEALTHY)
        assert t.reading == 90.0  # max across clusters
        assert t.hottest_cluster.name == "Balance Sheet & Leverage"

    def test_average_within_cluster(self):
        blocks = [_block("Cash Conversion", {"cfo_to_net_income": 80.0, "fcf_margin": 40.0})]
        t = compute_thermometer(blocks, HEALTHY)
        # single cluster: mean(80, 40) = 60
        assert t.reading == 60.0
        assert t.clusters[0].concern == 60.0

    def test_none_when_nothing_computable(self):
        blocks = [_block("Narrative Drift", {"adjustment_recurrence_ratio": 50.0})]
        # no distress-cluster metrics, healthy financials -> no reading
        t = compute_thermometer(blocks, HEALTHY)
        assert t.reading is None

    def test_single_member_cluster_is_insufficient(self):
        # Review finding 3: one available metric must NOT become the headline.
        blocks = [_block("Balance Sheet Stress", {"current_ratio": 90.0})]
        t = compute_thermometer(blocks, HEALTHY)  # 1 of 5 cluster members present
        assert t.reading is None  # insufficient, not 90


class TestRegimeDummies:
    def test_negative_ebitda_adds_concern(self):
        # Two cluster members so it qualifies under MIN_CLUSTER_MEMBERS.
        blocks = [_block("Cash Conversion", {"cfo_to_net_income": 40.0, "fcf_margin": 40.0})]
        distressed = [_period("FY2025Q4", net_income=50.0, ebit=-30.0, depreciation_amortization=10.0)]
        t = compute_thermometer(blocks, distressed)
        assert any(f.code == "EBITDA_NEGATIVE" for f in t.regime_flags)
        assert t.reading == 65.0  # cluster mean 40 + 25

    def test_two_quarter_loss_supersedes_single(self):
        two_q_loss = [
            _period("FY2025Q3", period_end=date(2025, 9, 30), net_income=-10.0),
            _period("FY2025Q4", net_income=-20.0),
        ]
        t = compute_thermometer([], two_q_loss)
        codes = {f.code for f in t.regime_flags}
        assert "NI_NEGATIVE_2Q" in codes
        assert "NI_NEGATIVE" not in codes

    def test_regime_dummy_alone_still_reads(self):
        # Negative EBITDA with no scored distress clusters must still read hot,
        # not vanish (the P0-9 inversion, at thermometer level).
        distressed = [_period("FY2025Q4", net_income=-5.0, ebit=-30.0, depreciation_amortization=10.0)]
        t = compute_thermometer([], distressed)
        assert t.reading is not None
        assert t.reading >= 25.0

class TestOwnHistoryPercentile:
    def test_deterioration_to_new_extreme_reads_high(self):
        # net_debt_to_ebitda: higher = worse; current 8.0 is the worst ever.
        # Second cluster member (interest_coverage, anchor) satisfies membership.
        blocks = [_block("Balance Sheet Stress", {"net_debt_to_ebitda": 50.0, "interest_coverage": 85.0})]
        history = {"net_debt_to_ebitda": _hist("net_debt_to_ebitda", [1.0, 1.2, 1.1, 1.3, 1.0, 8.0])}
        t = compute_thermometer(blocks, HEALTHY, history)
        cluster = next(c for c in t.clusters if c.name == "Balance Sheet & Leverage")
        assert cluster.concern > 80  # net_debt near the top of its own history

    def test_stable_level_reads_near_median_not_false_high(self):
        # current_ratio: lower = worse. Absolute anchor says 85 (false high) for a
        # structurally-low-but-stable ~0.5; own history neutralizes that to ~median.
        # net_debt is also stable (median), so the cluster mean stays moderate.
        blocks = [_block("Balance Sheet Stress", {"current_ratio": 85.0, "net_debt_to_ebitda": 50.0})]
        history = {
            "current_ratio": _hist("current_ratio", [0.52, 0.50, 0.51, 0.49, 0.50, 0.50]),
            "net_debt_to_ebitda": _hist("net_debt_to_ebitda", [2.0, 2.1, 1.9, 2.0, 2.05, 2.0]),
        }
        t = compute_thermometer(blocks, HEALTHY, history)
        cluster = next(c for c in t.clusters if c.name == "Balance Sheet & Leverage")
        assert 40 <= cluster.concern <= 60

    def test_short_history_falls_back_to_anchor(self):
        # Below MIN_HISTORY_FOR_PERCENTILE the anchor concern is used.
        blocks = [_block("Balance Sheet Stress", {"current_ratio": 85.0, "net_debt_to_ebitda": 85.0})]
        history = {"current_ratio": _hist("current_ratio", [0.5, 0.5])}  # only 2 obs
        t = compute_thermometer(blocks, HEALTHY, history)
        cluster = next(c for c in t.clusters if c.name == "Balance Sheet & Leverage")
        assert cluster.concern == 85.0  # both members fall back to anchor 85

    def test_reading_capped_at_100(self):
        blocks = [_block("Balance Sheet Stress", {"net_debt_to_ebitda": 95.0, "interest_coverage": 95.0})]
        distressed = [
            _period("FY2025Q3", period_end=date(2025, 9, 30), net_income=-10.0),
            _period("FY2025Q4", net_income=-20.0, ebit=-30.0, depreciation_amortization=10.0),
        ]
        t = compute_thermometer(blocks, distressed)
        assert t.reading == 100.0  # cluster 95 + 25 + 20 capped
