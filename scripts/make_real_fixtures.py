#!/usr/bin/env python3
"""Produce committed real-company fixtures for offline tests.

SEC filing data is public domain, so real (trimmed) companyfacts are committed
directly: only the tags the mapper reads, entries ending 2023-06-01 or later.
Keeps fixtures small while exercising every derivation path on real data.

Regenerating is a window-close action (docs/evaluation_protocol.md): the
trimmed tag set now includes the finance-lease liabilities, which moves
total_debt — and so block scores — for any filer that reports them; the
calibration snapshot and the real-fixture tests are computed from these files.

    EDGAR_IDENTITY="Name email" .venv/bin/python scripts/make_real_fixtures.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.ingestion.fields import all_tags
from app.services.ingestion.sec_client import SecClient

FIXTURE_TICKERS = ["AAPL", "KO", "CRM"]  # Sep-FYE + calendar-FYE + Jan-FYE (52/53-week)
MIN_END = "2023-06-01"
OUT_DIR = ROOT / "tests" / "fixtures" / "real"


def wanted_tags() -> set[tuple[str, str]]:
    # The field registry's full tag set — the same one PIT trims to. This used
    # to be a hand-kept copy that never learned the finance-lease tags, so the
    # committed fixtures cannot exercise that total_debt branch.
    return set(all_tags())


def trim(facts_json: dict) -> dict:
    keep = wanted_tags()
    out = {"entityName": facts_json.get("entityName"), "facts": {}}
    for taxonomy, tag in keep:
        concept = facts_json.get("facts", {}).get(taxonomy, {}).get(tag)
        if not concept:
            continue
        units_out = {}
        for unit, entries in concept.get("units", {}).items():
            kept = [e for e in entries if e.get("end", "") >= MIN_END]
            if kept:
                units_out[unit] = kept
        if units_out:
            out["facts"].setdefault(taxonomy, {})[tag] = {"units": units_out}
    return out


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    client = SecClient()
    for ticker in FIXTURE_TICKERS:
        full = client.company_facts(ticker)
        trimmed = trim(full)
        path = OUT_DIR / f"companyfacts_{ticker}_trimmed.json"
        path.write_text(json.dumps(trimmed, separators=(",", ":")))
        print(f"Wrote {path} ({path.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
