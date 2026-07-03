"""Canonical JSON ingestion.

The canonical input format is simply the JSON serialization of
CompanyDataset (see app/schemas/financials.py and data/example_company.json).
Validation errors are raised loudly with field-level detail — bad input is
never coerced or silently dropped.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from app.schemas.financials import CompanyDataset


class IngestionError(ValueError):
    pass


def load_dataset(path: str | Path) -> CompanyDataset:
    p = Path(path)
    if not p.exists():
        raise IngestionError(f"Input file not found: {p}")
    try:
        raw = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise IngestionError(f"Invalid JSON in {p}: {e}") from e
    return parse_dataset(raw)


def parse_dataset(raw: dict) -> CompanyDataset:
    try:
        return CompanyDataset.model_validate(raw)
    except ValidationError as e:
        raise IngestionError(f"Dataset failed schema validation: {e}") from e
