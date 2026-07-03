#!/usr/bin/env python3
"""Track 3: wide funnel sweep — fundamentals-only scoring across the largest
N SEC filers, producing results CSV + a flag adjudication worksheet.

The SEC ticker registry is ordered by market cap; the first N entries are
taken, banks/insurers auto-excluded via SIC. Documents are NOT fetched here
(the funnel's expensive narrative stage runs only on adjudicated flags).

    EDGAR_IDENTITY="Name email" .venv/bin/python scripts/wide_sweep.py [N]
"""

from __future__ import annotations

import csv
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import scoring_config as cfg
from app.core.pipeline import analyze
from app.services.backtesting.events import fetch_entity_events
from app.services.ingestion.companyfacts_mapper import build_dataset
from app.services.ingestion.sec_client import SecClient

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("wide_sweep")

FLAG_THRESHOLD = cfg.DIRECTION_NEGATIVE_ABOVE  # 45 = empirical p90
LOW_COVERAGE = 0.60
OUT_DIR = ROOT / "data" / "sweep"


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 350
    client = SecClient()
    registry = client._cached_json("company_tickers.json",  # noqa: SLF001
                                   "https://www.sec.gov/files/company_tickers.json")
    seen_ciks: set[int] = set()
    universe: list[str] = []
    for entry in registry.values():
        cik = int(entry["cik_str"])
        if cik in seen_ciks:
            continue  # multiple share classes
        seen_ciks.add(cik)
        universe.append(entry["ticker"])
        if len(universe) >= n:
            break

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results_path = OUT_DIR / "sweep_results.csv"
    adjud_path = OUT_DIR / "adjudication.csv"

    res_fields = ["ticker", "name", "sic", "status", "coverage", "overall", "direction",
                  "n_red_flags", "top_block", "top_block_score", "red_flag_metrics"]
    adj_fields = ["ticker", "name", "overall", "coverage", "red_flag_metrics",
                  "auto_label", "adjudication", "notes"]

    with results_path.open("w", newline="") as rf, adjud_path.open("w", newline="") as af:
        res = csv.DictWriter(rf, fieldnames=res_fields)
        adj = csv.DictWriter(af, fieldnames=adj_fields)
        res.writeheader()
        adj.writeheader()

        for i, ticker in enumerate(universe):
            row: dict[str, object] = {"ticker": ticker}
            try:
                facts = client.company_facts(ticker)
                events = fetch_entity_events(client, ticker)
                ds, diag = build_dataset(facts, ticker, n_quarters=8)
                ds.profile.is_financial_institution = events.is_financial_institution
                row["name"] = diag.entity_name
                row["sic"] = events.sic
                row["coverage"] = round(diag.coverage(), 3)
                result = analyze(ds)
            except Exception as e:  # noqa: BLE001 - a bad filer must not kill the sweep
                row["status"] = f"error: {str(e)[:80]}"
                res.writerow(row)
                continue

            if result.excluded:
                row["status"] = "excluded_financial"
                res.writerow(row)
                continue
            overall = result.overall.score if result.overall else None
            scored_blocks = [b for b in result.block_scores if b.score is not None]
            top = max(scored_blocks, key=lambda b: b.score) if scored_blocks else None  # type: ignore[arg-type]
            flag_metrics = ";".join(f.evidence_metrics[0] for f in result.red_flags)
            row.update({
                "status": "ok",
                "overall": overall,
                "direction": result.overall.direction.value if result.overall else "",
                "n_red_flags": len(result.red_flags),
                "top_block": top.name if top else "",
                "top_block_score": top.score if top else "",
                "red_flag_metrics": flag_metrics,
            })
            res.writerow(row)

            if overall is not None and overall > FLAG_THRESHOLD:
                auto = "data_artifact?" if diag.coverage() < LOW_COVERAGE else ""
                adj.writerow({
                    "ticker": ticker, "name": diag.entity_name, "overall": overall,
                    "coverage": round(diag.coverage(), 3),
                    "red_flag_metrics": flag_metrics,
                    "auto_label": auto, "adjudication": "", "notes": "",
                })
            if (i + 1) % 25 == 0:
                logger.warning("progress: %d/%d", i + 1, len(universe))
                rf.flush()
                af.flush()

    print(f"Wrote {results_path} and {adjud_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
