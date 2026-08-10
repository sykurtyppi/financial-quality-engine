import pytest

from app.config import scoring_config as cfg
from app.schemas.metrics import MetricResult, MetricStatus
from app.schemas.scoring import Confidence, Direction
from app.services.scoring.engine import interpolate_concern, score_all, score_block


def ok_metric(name: str, value: float) -> MetricResult:
    return MetricResult(
        name=name, formula="test", fiscal_label="Q4", status=MetricStatus.OK, value=value
    )


def missing_metric(name: str) -> MetricResult:
    return MetricResult(
        name=name, formula="test", fiscal_label="Q4", status=MetricStatus.MISSING_DATA
    )


ANCHORS = [(0.0, 10.0), (1.0, 50.0), (2.0, 90.0)]


class TestInterpolation:
    def test_exact_anchor(self):
        assert interpolate_concern(1.0, ANCHORS) == pytest.approx(50.0)

    def test_midpoint(self):
        assert interpolate_concern(0.5, ANCHORS) == pytest.approx(30.0)

    def test_clamps_below(self):
        assert interpolate_concern(-5.0, ANCHORS) == pytest.approx(10.0)

    def test_clamps_above(self):
        assert interpolate_concern(99.0, ANCHORS) == pytest.approx(90.0)

    def test_descending_concern_anchors(self):
        # e.g. cfo_to_net_income: higher value = lower concern
        anchors = [(0.0, 90.0), (1.0, 20.0)]
        assert interpolate_concern(0.5, anchors) == pytest.approx(55.0)


class TestBlockScoring:
    def _spec(self):
        return cfg.BlockSpec(
            name="Test Block",
            metrics=[
                cfg.MetricSpec("a", 0.5, ANCHORS),
                cfg.MetricSpec("b", 0.5, ANCHORS),
            ],
        )

    def test_weighted_average(self):
        metrics = {"a": ok_metric("a", 0.0), "b": ok_metric("b", 2.0)}
        bs = score_block(self._spec(), metrics)
        assert bs.score == pytest.approx(50.0)
        assert bs.data_coverage == 1.0

    def test_renormalizes_over_available(self):
        metrics = {"a": ok_metric("a", 2.0), "b": missing_metric("b")}
        bs = score_block(self._spec(), metrics)
        assert bs.score == pytest.approx(90.0)
        assert bs.data_coverage == pytest.approx(0.5)
        # missing component still appears in the transparency output
        assert any(c.concern_score is None for c in bs.components)

    def test_no_data_returns_none_not_midpoint(self):
        metrics = {"a": missing_metric("a"), "b": missing_metric("b")}
        bs = score_block(self._spec(), metrics)
        assert bs.score is None
        assert bs.confidence is Confidence.LOW
        assert "Insufficient data" in bs.rationale

    def test_uncalibrated_caveat_always_attached(self):
        metrics = {"a": ok_metric("a", 1.0), "b": ok_metric("b", 1.0)}
        bs = score_block(self._spec(), metrics)
        assert cfg.V0_WEIGHTS_CAVEAT in bs.caveats
        assert "not a calibrated probability" in cfg.V0_WEIGHTS_CAVEAT

    def test_direction_thresholds(self):
        metrics = {"a": ok_metric("a", 0.0), "b": ok_metric("b", 0.0)}
        assert score_block(self._spec(), metrics).direction is Direction.POSITIVE
        metrics = {"a": ok_metric("a", 2.0), "b": ok_metric("b", 2.0)}
        assert score_block(self._spec(), metrics).direction is Direction.NEGATIVE


def distress_metric(name: str) -> MetricResult:
    return MetricResult(
        name=name,
        formula="test",
        fiscal_label="Q4",
        status=MetricStatus.NOT_MEANINGFUL,
        distress_signal=True,
        note="denominator in distress",
    )


class TestDistressSignalScoring:
    """P0-9: a metric that is NOT_MEANINGFUL *because of distress* must be scored
    at its maximum concern and keep its weight — never drop out and lift the
    block by renormalizing over less-alarming survivors."""

    def _spec(self):
        return cfg.BlockSpec(
            name="Test Block",
            metrics=[
                cfg.MetricSpec("a", 0.5, ANCHORS),
                cfg.MetricSpec("b", 0.5, ANCHORS),
            ],
        )

    def test_distress_metric_scored_at_max_concern(self):
        # 'a' low concern (10), 'b' distress -> max anchor 90. (10+90)/2 = 50.
        bs = score_block(self._spec(), {"a": ok_metric("a", 0.0), "b": distress_metric("b")})
        assert bs.score == pytest.approx(50.0)
        assert bs.data_coverage == pytest.approx(1.0)  # 'b' counts as covered
        comp_b = next(c for c in bs.components if c.metric_name == "b")
        assert comp_b.concern_score == pytest.approx(90.0)
        assert "distress" in (comp_b.note or "").lower()

    def test_distress_raises_score_versus_dropping(self):
        # The inversion the fix targets: if 'b' merely dropped, the block would
        # reflect only 'a' (concern 10). Distress must pull it UP instead.
        dropped = score_block(self._spec(), {"a": ok_metric("a", 0.0), "b": missing_metric("b")})
        distress = score_block(self._spec(), {"a": ok_metric("a", 0.0), "b": distress_metric("b")})
        assert dropped.score == pytest.approx(10.0)
        assert distress.score > dropped.score
        assert distress.score == pytest.approx(50.0)

    def test_benign_not_meaningful_still_drops(self):
        # NOT_MEANINGFUL without the distress flag keeps the old drop behavior.
        benign = MetricResult(
            name="b", formula="t", fiscal_label="Q4", status=MetricStatus.NOT_MEANINGFUL
        )
        bs = score_block(self._spec(), {"a": ok_metric("a", 2.0), "b": benign})
        assert bs.score == pytest.approx(90.0)  # only 'a'
        assert bs.data_coverage == pytest.approx(0.5)


class TestScoreAll:
    def test_high_growth_caveat_applied_to_growth_sensitive_blocks(self):
        metrics = [ok_metric("receivables_growth_spread", 0.3)]
        blocks, _ = score_all(metrics, high_growth=True)
        rq = next(b for b in blocks if b.name == "Revenue Quality")
        assert any("High-growth profile" in c for c in rq.caveats)
        eq = next(b for b in blocks if b.name == "Earnings Quality")
        assert not any("High-growth profile" in c for c in eq.caveats)

    def test_overall_none_when_insufficient_blocks(self):
        _, overall = score_all([ok_metric("total_accruals", 0.05)])
        # only one block has data -> < 50% weight
        assert overall.score is None
        assert overall.confidence is Confidence.LOW

    def test_block_weights_exposed(self):
        _, overall = score_all([ok_metric("total_accruals", 0.05)])
        assert overall.block_weights == cfg.BLOCK_WEIGHTS
        assert sum(cfg.BLOCK_WEIGHTS.values()) == pytest.approx(1.0)
