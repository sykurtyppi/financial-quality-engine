#!/usr/bin/env python3
"""CLI: analyze a canonical JSON dataset and write/print the markdown report.

Usage:
    .venv/bin/python scripts/run_analysis.py data/example_company.json [-o report.md]
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.pipeline import analyze
from app.services.ingestion.json_loader import IngestionError, load_dataset
from app.services.reporting.markdown_report import render


def main() -> int:
    parser = argparse.ArgumentParser(description="Earnings Quality & Narrative Drift analysis")
    parser.add_argument("input", help="Path to canonical CompanyDataset JSON")
    parser.add_argument("-o", "--output", help="Write markdown report to this path")
    args = parser.parse_args()

    try:
        dataset = load_dataset(args.input)
    except IngestionError as e:
        print(f"Ingestion error: {e}", file=sys.stderr)
        return 1

    try:
        result = analyze(dataset)
    except ValueError as e:
        print(f"Analysis error: {e}", file=sys.stderr)
        return 1

    report = render(result, generated_on=date.today().isoformat())
    if args.output:
        Path(args.output).write_text(report)
        print(f"Report written to {args.output}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
