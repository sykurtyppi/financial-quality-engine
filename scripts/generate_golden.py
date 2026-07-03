#!/usr/bin/env python3
"""Regenerate the golden report and the example dataset JSON.

Run after intentional changes to formulas/scoring/reporting, then REVIEW THE
DIFF before committing — the golden file is the reviewed spec of the output.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.pipeline import analyze
from app.services.reporting.markdown_report import render
from tests.fixtures.companies import stretch_dataset

FIXED_DATE = "2026-01-01"


def main() -> None:
    ds = stretch_dataset()
    report = render(analyze(ds), generated_on=FIXED_DATE)
    golden = ROOT / "tests" / "golden_reports" / "stretchco_report.md"
    golden.write_text(report)
    print(f"Wrote {golden}")

    example = ROOT / "data" / "example_company.json"
    example.write_text(json.dumps(ds.model_dump(mode="json"), indent=2))
    print(f"Wrote {example}")


if __name__ == "__main__":
    main()
