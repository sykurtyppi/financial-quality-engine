#!/usr/bin/env python3
"""Regenerate the calibrated-scoring reproducibility snapshot.

Scores the three committed real fixtures with the current scoring config and
stores overall/block scores + config version. The paired test asserts that
re-scoring reproduces this snapshot exactly — any change to weights, anchors,
or direction bands must come through this file in a reviewed diff.

    .venv/bin/python scripts/generate_calibration_snapshot.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import scoring_config as cfg
from app.core.pipeline import analyze
from app.services.ingestion.companyfacts_mapper import build_dataset

FIXTURES = ROOT / "tests" / "fixtures" / "real"
OUT = ROOT / "tests" / "golden_reports" / "calibration_snapshot.json"


def main() -> None:
    snapshot: dict = {
        "config_version": cfg.CONFIG_VERSION,
        "block_weights": cfg.BLOCK_WEIGHTS,
        "direction_bands": [cfg.DIRECTION_POSITIVE_BELOW, cfg.DIRECTION_NEGATIVE_ABOVE],
        "companies": {},
    }
    for ticker in ("AAPL", "KO", "CRM"):
        facts = json.loads((FIXTURES / f"companyfacts_{ticker}_trimmed.json").read_text())
        ds, _ = build_dataset(facts, ticker, n_quarters=8)
        result = analyze(ds)
        snapshot["companies"][ticker] = {
            "overall": result.overall.score if result.overall else None,
            "direction": result.overall.direction.value if result.overall else None,
            "blocks": {
                b.name: b.score for b in result.block_scores
            },
        }
    OUT.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
