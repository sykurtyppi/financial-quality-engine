#!/usr/bin/env python3
"""Run the v0.3 walk-forward backtest over the stratified universe.

    EDGAR_IDENTITY="Name email" .venv/bin/python scripts/run_backtest.py [out.csv]
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.backtesting.runner import run_backtest

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "data" / "backtest" / "backtest_results.csv")
    path = run_backtest(out)
    print(f"Backtest written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
