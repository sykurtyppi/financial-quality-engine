#!/usr/bin/env python3
"""Phase 4: run the KPI-drift validation with the LLM adjudicator and compare to
the deterministic baseline.

Judgments come from data/kpi_judgments.json — one blind materiality judgment per
pair (produced from the two definitions ALONE, no company identity, no outcome).
The completion function looks each up by (kpi, current_definition); pairs with no
recorded judgment return None -> deterministic fallback.

    EDGAR_IDENTITY="Name email" .venv/bin/python scripts/run_kpi_phase4.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.backtesting.kpi_validation import run_validation, summarize
from app.services.narrative.kpi_adjudicator import DeterministicAdjudicator, LlmAdjudicator

MODEL_ID = "agent-blind-rubric-v1"
JUDGMENTS = ROOT / "data" / "kpi_judgments.json"
_CUR_RE = re.compile(r'Current definition \([^)]*\): "(.*)"\s*$', re.DOTALL)
_KPI_RE = re.compile(r"^Metric: (.+)$", re.MULTILINE)


def load_completion():
    records = json.loads(JUDGMENTS.read_text())
    table = {(r["kpi"], _norm(r["current_definition"])): r["judgment"] for r in records}

    def complete(system: str, user: str) -> dict | None:
        km = _KPI_RE.search(user)
        cm = _CUR_RE.search(user)
        if not km or not cm:
            return None
        return table.get((km.group(1).strip(), _norm(cm.group(1))))

    return complete


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def main() -> int:
    complete = load_completion()
    det = summarize(run_validation(adjudicator=DeterministicAdjudicator()))
    llm_adj = LlmAdjudicator(complete=complete, model_id=MODEL_ID)
    llm = summarize(run_validation(adjudicator=llm_adj))

    def line(label, s):
        print(f"  {label:14s} recall {s['restater_fired']}/{s['restater_n']} "
              f"({s['restater_recall']:.0%}) · clean FP {s['clean_fired']}/{s['clean_n']} "
              f"({s['clean_fp_rate']:.0%}) · early leads {s['early_warning_leads']}")

    print("KPI-DRIFT PHASE 4 — deterministic baseline vs LLM adjudicator\n")
    line("deterministic", det)
    line("LLM", llm)

    print("\nPre-committed criteria:")
    fp_ok = llm["clean_fp_rate"] is not None and llm["clean_fp_rate"] < 0.08
    recall_ok = llm["restater_recall"] is not None and llm["restater_recall"] >= det["restater_recall"] - 0.11
    leads_ok = all(x in llm["early_warning_leads"] for x in (150, 251, 315) if x in det["early_warning_leads"])
    precision_improved = (llm["clean_fp_rate"] is not None and det["clean_fp_rate"] is not None
                          and llm["clean_fp_rate"] < det["clean_fp_rate"])
    print(f"  clean FP < 8%:              {'PASS' if fp_ok else 'FAIL'} ({llm['clean_fp_rate']:.0%})")
    print(f"  precision improved:         {'YES' if precision_improved else 'NO'} "
          f"({det['clean_fp_rate']:.0%} -> {llm['clean_fp_rate']:.0%} FP)")
    print(f"  restater recall held:       {'PASS' if recall_ok else 'FAIL'} "
          f"({llm['restater_recall']:.0%} vs {det['restater_recall']:.0%})")
    print(f"  MiMedx/SunPower/Comscore leads preserved: {'PASS' if leads_ok else 'FAIL'} "
          f"({llm['early_warning_leads']})")

    if precision_improved and recall_ok and leads_ok:
        verdict = "KEEP + SHIP: precision up, recall/leads held."
    elif precision_improved and not recall_ok:
        verdict = ("KEEP the LLM (it improves precision and correctly gates noise), but the "
                   "KPI-drift SIGNAL is NOT deployable: the recall collapse shows the deterministic "
                   "detector's recall was largely EXTRACTION ARTIFACTS (tables, guidance, boilerplate), "
                   "not genuine redefinitions. Bottleneck is extraction, not adjudication.")
    else:
        verdict = "REMOVE the LLM layer: no precision gain."
    print(f"\n  VERDICT: {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
