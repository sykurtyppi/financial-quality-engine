#!/usr/bin/env python3
"""Dump the KPI redefinition pairs (restatement + clean sets) as JSON, for blind
adjudication in Phase 4.

    EDGAR_IDENTITY="Name email" .venv/bin/python scripts/dump_kpi_pairs.py > data/kpi_pairs.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.backtesting.clean_narrative_control import ANCHOR, CLEAN
from app.services.backtesting.restatement_control import CASES, first_402_date
from app.services.ingestion.edgar_documents import fetch_documents
from app.services.ingestion.sec_client import SecClient
from app.services.narrative.kpi_extraction import pair_definition_changes


def main() -> int:
    client = SecClient()
    out = []

    def collect(name: str, cik: int, group: str, cutoff, use_cik: bool):
        try:
            facts = client.company_facts_by_cik(cik) if use_cik else client.company_facts(name)
            docs = fetch_documents(client, name, facts, n_filings=12, cik=cik, before=cutoff)
            if len(docs.documents) < 2:
                return
            for p in pair_definition_changes(docs.documents):
                if p.change_type != "redefinition":
                    continue
                out.append({"company": name, "group": group, "kpi": p.kpi,
                            "prior_period": p.prior_period, "current_period": p.current_period,
                            "prior_definition": p.prior_definition,
                            "current_definition": p.current_definition,
                            "prefilter_similarity": p.prefilter_similarity})
        except Exception as e:  # noqa: BLE001
            print(f"# {name}: {e}", file=sys.stderr)

    for rc in CASES:
        ev = first_402_date(client, rc.cik)
        if ev:
            collect(rc.name, rc.cik, "restater", ev, use_cik=True)
    for cc in CLEAN:
        try:
            cik = client.resolve_cik(cc.ticker)
        except Exception:  # noqa: BLE001
            continue
        collect(cc.ticker, cik, "clean", ANCHOR, use_cik=False)

    print(json.dumps(out, indent=2))
    print(f"# {len(out)} pairs", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
