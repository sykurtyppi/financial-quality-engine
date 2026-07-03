"""Golden-file test: the rendered report for the STRETCHCO fixture must be
byte-identical to the reviewed golden copy.

To regenerate after an intentional reporting change:
    .venv/bin/python scripts/generate_golden.py
then review the diff before committing.
"""

from pathlib import Path

from app.core.pipeline import analyze
from app.services.reporting.markdown_report import render
from tests.fixtures.companies import stretch_dataset

GOLDEN = Path(__file__).parent.parent / "golden_reports" / "stretchco_report.md"
FIXED_DATE = "2026-01-01"


def test_stretchco_report_matches_golden():
    result = analyze(stretch_dataset())
    report = render(result, generated_on=FIXED_DATE)
    assert GOLDEN.exists(), "Golden report missing — run scripts/generate_golden.py"
    assert report == GOLDEN.read_text()


def test_report_is_deterministic():
    r1 = render(analyze(stretch_dataset()), generated_on=FIXED_DATE)
    r2 = render(analyze(stretch_dataset()), generated_on=FIXED_DATE)
    assert r1 == r2
