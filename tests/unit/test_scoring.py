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


_NON_FINITE = pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"])


class TestNonFiniteMetricValues:
    """Hermes audit of 424b0b4, finding 7, at the metric contract: an OK
    result carrying NaN or +/-inf is not a measurement. NaN reached
    `interpolate_concern`, matched no anchor segment and raised
    AssertionError("unreachable"); +/-inf was clamped to an extreme concern
    as if it were an extreme ratio. It is NOT_MEANINGFUL, with no value."""

    @_NON_FINITE
    def test_ok_with_a_non_finite_value_is_not_meaningful(self, value):
        m = ok_metric("a", value)
        assert m.status is MetricStatus.NOT_MEANINGFUL
        assert m.value is None
        assert m.note == f"value is not a finite number ({value!r})"
        assert m.distress_signal is False
        assert not m.is_ok

    def test_an_existing_note_is_kept(self):
        m = MetricResult(name="a", formula="test", fiscal_label="Q4", status=MetricStatus.OK,
                         value=float("nan"), note="TTM basis")
        assert m.note == "TTM basis; value is not a finite number (nan)"

    def test_distress_signal_is_left_as_given(self):
        m = MetricResult(name="a", formula="test", fiscal_label="Q4", status=MetricStatus.OK,
                         value=float("inf"), distress_signal=True)
        assert m.distress_signal is True

    @pytest.mark.parametrize("value", [1.0, 0.0, -2.5, 1e308])
    def test_a_finite_value_is_untouched(self, value):
        m = ok_metric("a", value)
        assert (m.status, m.value, m.note) == (MetricStatus.OK, value, None)

    @_NON_FINITE
    @pytest.mark.parametrize("status", [MetricStatus.MISSING_DATA, MetricStatus.NOT_MEANINGFUL])
    def test_a_non_ok_result_drops_the_value_and_keeps_its_status(self, value, status):
        m = MetricResult(name="a", formula="test", fiscal_label="Q4", status=status,
                         value=value, note="denominator is zero")
        assert (m.status, m.value, m.note) == (status, None, "denominator is zero")

    def test_the_contract_holds_when_parsed_from_json(self):
        m = MetricResult.model_validate_json(
            '{"name": "a", "formula": "t", "fiscal_label": "Q4", "status": "ok", "value": NaN}')
        assert (m.status, m.value) == (MetricStatus.NOT_MEANINGFUL, None)

    @_NON_FINITE
    def test_the_engine_scores_it_as_absent_not_as_nan(self, value):
        """Every block's metrics OK at a mid-anchor value, then the first
        one's value made non-finite: the scores are those of the same bundle
        without it, never NaN and never an exception."""
        spec = cfg.BLOCKS[0]
        base = [ok_metric(ms.metric_name, (ms.anchors[0][0] + ms.anchors[-1][0]) / 2)
                for s in cfg.BLOCKS for ms in s.metrics]
        target = spec.metrics[0].metric_name
        with_bad = [ok_metric(m.name, value) if m.name == target else m for m in base]
        without = [m for m in base if m.name != target]
        blocks, overall = score_all(with_bad)
        ref_blocks, ref_overall = score_all(without)
        assert overall.score is not None and overall.score == overall.score  # not NaN
        assert overall.score == ref_overall.score
        assert [b.score for b in blocks] == [b.score for b in ref_blocks]
        (comp,) = [c for c in blocks[0].components if c.metric_name == target]
        assert comp.concern_score is None and comp.status == "not_meaningful"
