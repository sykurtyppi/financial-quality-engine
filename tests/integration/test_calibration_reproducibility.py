"""Calibrated scoring must be reproducible and version-stamped.

The snapshot pins overall/block scores for the three committed real fixtures.
If scoring weights, anchors, or direction bands change, this test fails until
the snapshot is regenerated (scripts/generate_calibration_snapshot.py) and the
diff is reviewed — score changes can never land silently.
"""

import json
from pathlib import Path

import pytest

from app.config import scoring_config as cfg
from app.core.pipeline import analyze
from app.services.ingestion.companyfacts_mapper import build_dataset

ROOT = Path(__file__).parent.parent
SNAPSHOT = ROOT / "golden_reports" / "calibration_snapshot.json"
FIXTURES = ROOT / "fixtures" / "real"


@pytest.fixture(scope="module")
def snapshot():
    assert SNAPSHOT.exists(), "run scripts/generate_calibration_snapshot.py"
    return json.loads(SNAPSHOT.read_text())


class TestConfigIntegrity:
    def test_version_stamped(self, snapshot):
        assert cfg.CONFIG_VERSION == snapshot["config_version"]

    def test_block_weights_sum_to_one(self):
        assert sum(cfg.BLOCK_WEIGHTS.values()) == pytest.approx(1.0)

    def test_direction_bands_match_snapshot(self, snapshot):
        assert [cfg.DIRECTION_POSITIVE_BELOW, cfg.DIRECTION_NEGATIVE_ABOVE] == snapshot[
            "direction_bands"
        ]

    def test_excluded_components_have_zero_weight_but_stay_visible(self):
        by_name = {
            ms.metric_name: ms.weight for b in cfg.BLOCKS for ms in b.metrics
        }
        # v0.3 exclusions: computed and reported, but not scored.
        assert by_name["buyback_offset_ratio"] == 0.0
        assert by_name["working_capital_swing_to_income"] == 0.0

    def test_block_weights_match_snapshot(self, snapshot):
        assert cfg.BLOCK_WEIGHTS == snapshot["block_weights"]


class TestScoreReproducibility:
    @pytest.mark.parametrize("ticker", ["AAPL", "KO", "CRM"])
    def test_fixture_scores_match_snapshot(self, snapshot, ticker):
        facts = json.loads((FIXTURES / f"companyfacts_{ticker}_trimmed.json").read_text())
        ds, _ = build_dataset(facts, ticker, n_quarters=8)
        result = analyze(ds)
        expected = snapshot["companies"][ticker]
        assert result.overall is not None
        assert result.overall.score == expected["overall"]
        assert result.overall.direction.value == expected["direction"]
        got_blocks = {b.name: b.score for b in result.block_scores}
        assert got_blocks == expected["blocks"]
