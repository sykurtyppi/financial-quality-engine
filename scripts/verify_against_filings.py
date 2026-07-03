#!/usr/bin/env python3
"""Manual-verification harness: mapped quarterly values vs source filings.

Two checks per company (AAPL, MSFT, KO):

1. SPOT VALUES — mapped quarterly values must equal values filed in specific
   accessions (hardcoded from the filings themselves).
2. ANNUAL RECONCILIATION — for every flow field and every complete fiscal year
   in the window, the four mapped quarterly values must sum to the
   independently filed annual total (the 10-K fact). This exercises the
   ytd_diff and fy_minus_3q derivation paths end-to-end against the filer's
   own arithmetic.

Results are recorded in docs/real_data_validation.md.

    EDGAR_IDENTITY="Name email" .venv/bin/python scripts/verify_against_filings.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.ingestion.companyfacts_mapper import (
    ANNUAL_DAYS,
    FLOW_FIELDS,
    RawFact,
    _collect,
    _dedupe_latest_filed,
    build_dataset,
)
from app.services.ingestion.sec_client import SecClient

# Spot values transcribed from filings (accession, concept, value).
SPOT_CHECKS = {
    "AAPL": [
        # 10-Q accession 0000320193-26-000013 (filed 2026-05-01), quarter ended
        # 2026-03-28: RevenueFromContractWithCustomerExcludingAssessedTax (QTD).
        ("revenue", date(2026, 3, 28), 111_184_000_000.0),
    ],
}

RECONCILE_FIELDS = ["revenue", "net_income", "cfo", "capex", "stock_based_compensation"]
TICKERS = ["AAPL", "MSFT", "KO"]
TOLERANCE = 2.0  # dollars; filer rounding is exact in XBRL, allow float noise


def annual_facts(facts_json: dict, field_name: str) -> dict[tuple[date, date], RawFact]:
    for taxonomy, tag in FLOW_FIELDS[field_name]:
        facts = [
            f
            for f in _collect(facts_json, taxonomy, tag, "USD")
            if f.days is not None and ANNUAL_DAYS[0] <= f.days <= ANNUAL_DAYS[1]
        ]
        if facts:
            return _dedupe_latest_filed(facts)
    return {}


def main() -> int:
    client = SecClient()
    failures = 0
    for ticker in TICKERS:
        facts_json = client.company_facts(ticker)
        dataset, diag = build_dataset(facts_json, ticker, n_quarters=8)
        by_end = {p.period_end: p for p in dataset.periods}
        print(f"\n{ticker} — {diag.entity_name}")

        for field_name, qend, expected in SPOT_CHECKS.get(ticker, []):
            got = getattr(by_end.get(qend), field_name, None) if qend in by_end else None
            ok = got is not None and abs(got - expected) <= TOLERANCE
            failures += 0 if ok else 1
            print(f"  SPOT  {field_name} @ {qend}: mapped={got:,.0f} filed={expected:,.0f} "
                  f"{'OK' if ok else 'MISMATCH'}")

        for field_name in RECONCILE_FIELDS:
            annuals = annual_facts(facts_json, field_name)
            checked = 0
            for (fy_start, fy_end), fact in sorted(annuals.items()):
                quarters = [
                    p for p in dataset.periods if fy_start <= p.period_end <= fy_end  # type: ignore[operator]
                ]
                if len(quarters) != 4:
                    continue  # fiscal year not fully inside the window
                vals = [getattr(p, field_name) for p in quarters]
                if any(v is None for v in vals):
                    print(f"  RECON {field_name} FY→{fy_end}: incomplete quarters (reported as missing)")
                    continue
                total = sum(vals)  # type: ignore[arg-type]
                ok = abs(total - fact.val) <= TOLERANCE
                failures += 0 if ok else 1
                checked += 1
                print(
                    f"  RECON {field_name} FY→{fy_end}: ΣQ={total:,.0f} "
                    f"filed FY={fact.val:,.0f} {'OK' if ok else 'MISMATCH'}"
                )
            if checked == 0:
                print(f"  RECON {field_name}: no complete fiscal year in window")

    print(f"\n{'PASS' if failures == 0 else f'FAIL ({failures} mismatches)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
