"""PR 0.6 — boundary tests for the correctness constants nothing pinned.

`MIN_COVERAGE_FOR_SCORE = 0.25` and the overall gate `weight_avail < 0.5`
were tested at 50%/0% and 20% respectively: flipping `<` to `<=`, or moving
either constant, passed the suite. These tests sit exactly on the boundary
on both sides. They pin BEHAVIOUR under the frozen 0.4.0 config — changing
a constant is a flag-only, protocol-logged decision, and these tests are
what would make such a change visible.
"""

from __future__ import annotations

import pytest

from app.config import scoring_config as cfg
from app.schemas.metrics import MetricResult, MetricStatus
from app.schemas.scoring import BlockScore, Confidence, Direction
from app.services.scoring import engine
from app.services.scoring.engine import (
    MIN_COVERAGE_FOR_SCORE,
    score_block,
    score_overall,
)

ANCHORS = [(0.0, 10.0), (1.0, 50.0), (2.0, 90.0)]


def _ok(name, value=1.0):
    return MetricResult(name=name, formula="t", fiscal_label="Q", value=value, status=MetricStatus.OK)


def _missing(name):
    return MetricResult(name=name, formula="t", fiscal_label="Q", status=MetricStatus.MISSING_DATA)


def _block(weights):
    return cfg.BlockSpec("B", [cfg.MetricSpec(f"m{i}", w, ANCHORS) for i, w in enumerate(weights)])


class TestBlockCoverageGate:
    def test_the_constant_is_what_the_calibration_was_run_under(self):
        assert MIN_COVERAGE_FOR_SCORE == 0.25

    def test_exactly_at_the_floor_a_score_is_asserted(self):
        # 4 x 0.25: one OK metric is exactly 25% of block weight.
        spec = _block([0.25, 0.25, 0.25, 0.25])
        bs = score_block(spec, {"m0": _ok("m0"), "m1": _missing("m1"), "m2": _missing("m2"), "m3": _missing("m3")})
        assert bs.data_coverage == 0.25
        assert bs.score == pytest.approx(50.0)

    def test_one_ulp_below_the_floor_refuses(self):
        # weight_total 1.0, weight_ok 0.2499 -> coverage 0.2499 < 0.25.
        spec = _block([0.2499, 0.2501, 0.25, 0.25])
        bs = score_block(spec, {"m0": _ok("m0"), "m1": _missing("m1"), "m2": _missing("m2"), "m3": _missing("m3")})
        # `data_coverage` is rounded to 3 dp for display (reads 0.25); the
        # gate itself compares the unrounded ratio, which is what we pin.
        assert 0.2499 / 1.0 < MIN_COVERAGE_FOR_SCORE
        assert bs.score is None
        assert bs.confidence is Confidence.LOW and bs.direction is Direction.MIXED
        assert "Insufficient data" in bs.rationale

    def test_refusal_keeps_every_component_for_transparency(self):
        spec = _block([0.2499, 0.2501, 0.25, 0.25])
        bs = score_block(spec, {"m0": _ok("m0")})
        assert [c.metric_name for c in bs.components] == ["m0", "m1", "m2", "m3"]


def _bs(name, score):
    return BlockScore(name=name, score=score, direction=Direction.MIXED, confidence=Confidence.LOW,
                      rationale="t", components=[], data_coverage=1.0)


class TestOverallWeightGate:
    """No subset of the real BLOCK_WEIGHTS sums to exactly 0.5 in float
    accumulation order, so the boundary is pinned on a controlled table;
    the real-weights case checks the same gate one block either side."""

    def test_exactly_half_the_design_weight_is_enough(self, monkeypatch):
        monkeypatch.setattr(cfg, "BLOCK_WEIGHTS", {"A": 0.5, "B": 0.5})
        overall = score_overall([_bs("A", 40.0), _bs("B", None)])
        assert overall.score == 40.0
        assert "renormalized over 50%" in " ".join(overall.caveats)

    def test_just_under_half_refuses(self, monkeypatch):
        monkeypatch.setattr(cfg, "BLOCK_WEIGHTS", {"A": 0.4999, "B": 0.5001})
        overall = score_overall([_bs("A", 40.0), _bs("B", None)])
        assert overall.score is None
        assert overall.direction is Direction.MIXED and overall.confidence is Confidence.LOW
        assert "not asserted" in overall.rationale

    def test_real_weights_either_side_of_the_gate(self):
        names = list(cfg.BLOCK_WEIGHTS)
        # Earnings 0.20 + Cash Conversion 0.17 + Capex 0.15 = 0.52 >= 0.5
        scored = {"Earnings Quality", "Cash Conversion", "Capex Discipline"}
        blocks = [_bs(n, 30.0 if n in scored else None) for n in names]
        assert score_overall(blocks).score == 30.0
        # Earnings 0.20 + Cash Conversion 0.17 + Balance Sheet 0.14 = 0.51 -> still scored;
        # drop Balance Sheet for Working Capital 0.07: 0.44 < 0.5 -> refused.
        scored = {"Earnings Quality", "Cash Conversion", "Working Capital Stress"}
        blocks = [_bs(n, 30.0 if n in scored else None) for n in names]
        assert score_overall(blocks).score is None

    def test_the_gate_reads_the_live_weight_table(self, monkeypatch):
        """score_overall must consult cfg.BLOCK_WEIGHTS at call time (the
        snapshot test regenerates against it), not a copy taken at import."""
        monkeypatch.setattr(cfg, "BLOCK_WEIGHTS", {"A": 1.0})
        assert score_overall([_bs("A", 12.0)]).score == 12.0
        assert engine.cfg is cfg
