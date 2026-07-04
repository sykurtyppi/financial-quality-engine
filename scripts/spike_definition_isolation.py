#!/usr/bin/env python3
"""SPIKE (throwaway, time-boxed) — definition-isolation on four anchors.

Phase 4 showed the KPI-drift recall was mostly EXTRACTION ARTIFACTS: the
prose-proximity extractor grabbed the first definition-cued sentence near a KPI
name, which caught revenue guidance (MiMedx), boilerplate (Comscore), and
reconciliation tables with period numbers (clean companies) — not the actual
non-GAAP definition.

This spike tests one falsifiable hypothesis:

    A parser that isolates the non-GAAP ADD-BACK COMPONENT SET (the reconciling
    line items that DEFINE a non-GAAP metric), rather than nearby prose, will
    (a) recover the two genuine redefinitions (SunPower, WageWorks) and
    (b) NOT manufacture a definition from guidance/boilerplate (MiMedx, P&G).

The component set is the definition: "Adjusted EBITDA = net income before
interest, taxes, D&A, stock-based comp, restructuring" IS defined by
{interest, taxes, depreciation, amortization, stock_based_comp, restructuring}.
Guidance text and boilerplate contain NO such set -> None -> no pair -> no fire.
That structural property is the whole bet.

    EDGAR_IDENTITY="Name email" .venv/bin/python scripts/spike_definition_isolation.py

Nothing here is wired into scoring, config, or the product. Disposable diagnostic.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.backtesting.clean_narrative_control import ANCHOR
from app.services.backtesting.restatement_control import first_402_date
from app.services.ingestion.edgar_documents import fetch_documents
from app.services.ingestion.sec_client import SecClient
from app.services.narrative.baselines import group_documents
from app.services.narrative.kpi_drift import KPI_DICTIONARY

# --- Canonical reconciliation vocabulary ------------------------------------
# Each concept is a normalized add-back/adjustment component. A non-GAAP metric's
# DEFINITION is the SET of these it reconciles across. This list is deliberately
# concept-level (not company-specific) so the same parser works across filers.
COMPONENTS: dict[str, tuple[str, ...]] = {
    "interest": (r"interest expense", r"interest income", r"\binterest\b"),
    "taxes": (r"income tax(?:es)?", r"provision for income", r"tax (?:expense|benefit)"),
    "depreciation": (r"depreciation",),
    "amortization": (r"amortization",),
    "amort_intangibles": (r"amortization of (?:acquired |purchased )?intangibl",),
    "stock_based_comp": (r"stock[- ]based compensation", r"share[- ]based compensation", r"equity[- ]based compensation"),
    "restructuring": (r"restructuring",),
    "impairment": (r"impairment", r"goodwill impairment"),
    "litigation": (r"litigation", r"legal settlement"),
    "acquisition_costs": (r"acquisition[- ](?:related )?(?:costs|expenses)", r"transaction (?:costs|expenses)", r"deal costs"),
    "integration": (r"integration (?:costs|expenses)",),
    "contingent_consideration": (r"contingent consideration", r"change in fair value of contingent"),
    "severance": (r"severance",),
    "gain_loss_disposal": (r"gain (?:on|from) (?:sale|disposal)", r"loss (?:on|from) (?:sale|disposal)", r"gain \(loss\)"),
    "foreign_exchange": (r"foreign (?:currency|exchange)", r"\bfx\b"),
    "one_time": (r"one[- ]time", r"non[- ]recurring", r"unusual items?"),
    "inventory_stepup": (r"inventory step[- ]?up", r"fair value (?:adjustment )?(?:of|to) inventory"),
    "warrant": (r"warrant", r"change in fair value of warrant"),
    "debt_extinguishment": (r"(?:loss|gain) on (?:early )?extinguishment of debt", r"debt extinguishment"),
    "pension": (r"pension", r"actuarial"),
    "covid": (r"covid",),
}

# The GAAP starting points a non-GAAP reconciliation walks up FROM.
GAAP_ANCHORS = (
    r"net income", r"net loss", r"net income \(loss\)", r"net earnings",
    r"gross profit", r"gross margin", r"operating income", r"loss from operations",
    r"income from operations", r"diluted (?:eps|earnings per share)", r"basic eps",
)

# A genuine non-GAAP definition sentence has a metric name + a defining cue.
_DEF_CUES = (
    "defined as", "we define", "define ", "is calculated as", "calculated as",
    "reconciliation of", "reconciled to", "excludes", "excluding", "adjusted to exclude",
    "adjustments to", "adds back", "add back",
)

# Non-GAAP metrics only (the KPIs whose meaning is a reconciliation set). Volume/
# usage KPIs (DAU, GMV) are out of scope for this spike — those are not the
# reconciliation-definition problem.
NON_GAAP_KPIS = {"Adjusted EBITDA", "Adjusted EPS", "Gross margin", "Free cash flow"}

MIN_COMPONENTS = 2  # a real add-back set has >= 2 components; 0-1 is prose/guidance


def _components_in(text: str) -> set[str]:
    low = text.lower()
    found = set()
    for concept, pats in COMPONENTS.items():
        if any(re.search(p, low) for p in pats):
            found.add(concept)
    # amort_intangibles implies amortization; keep the more specific one only when present
    if "amort_intangibles" in found:
        found.discard("amortization")
    return found


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.;])\s+", text) if s.strip()]


def isolate_definition(text: str, kpi: str) -> set[str] | None:
    """Isolate the non-GAAP add-back COMPONENT SET for `kpi`, or None if the text
    contains no genuine reconciliation definition for it.

    Two passes, both STRUCTURE-targeted (not prose-proximity):
      1. Definitional sentence: a sentence naming the KPI with a defining cue,
         extended across continuation sentences; take the component set within.
      2. Reconciliation window: the span between a GAAP anchor line and the KPI
         total; take the component set of the reconciling line items.
    A result is accepted only if it has >= MIN_COMPONENTS — the structural gate
    that guidance/boilerplate cannot pass.
    """
    kpi_pats = KPI_DICTIONARY[kpi]

    def names_kpi(s: str) -> bool:
        return any(re.search(p, s, re.IGNORECASE) for p in kpi_pats)

    # Pass 1: definitional sentence(s).
    sents = _sentences(text)
    for i, s in enumerate(sents):
        low = s.lower()
        if names_kpi(s) and any(c in low for c in _DEF_CUES):
            span = " ".join(sents[i:i + 4])  # allow the add-back list to spill over
            comps = _components_in(span)
            if len(comps) >= MIN_COMPONENTS:
                return comps

    # Pass 2: reconciliation window (GAAP anchor -> KPI total).
    low_all = text.lower()
    for m in re.finditer("|".join(kpi_pats), text, re.IGNORECASE):
        end = m.start()
        # find the nearest GAAP anchor within 1200 chars before the KPI total
        window_start = max(0, end - 1200)
        window = text[window_start:end]
        if re.search("|".join(GAAP_ANCHORS), window, re.IGNORECASE):
            comps = _components_in(window)
            if len(comps) >= MIN_COMPONENTS:
                return comps
    return None


@dataclass
class Anchor:
    label: str
    kind: str  # "genuine" (must recover) | "artifact" (must reject)
    cik: int | None
    ticker: str | None
    use_402_cutoff: bool
    kpis: tuple[str, ...]
    note: str


ANCHORS = [
    Anchor("SunPower", "genuine", 867773, None, True, ("Adjusted EBITDA", "Gross margin"),
           "non-GAAP adjustment set genuinely changed (Phase-4 251d lead)"),
    Anchor("WageWorks", "genuine", 1158863, None, True, ("Adjusted EBITDA",),
           "add-backs broadened to include SBC + contingent consideration"),
    Anchor("MiMedx", "artifact", 1376339, None, True, ("Adjusted EPS", "Adjusted EBITDA"),
           "Phase-4 'Adjusted EPS' fire was revenue-guidance text, not a definition"),
    Anchor("Procter & Gamble", "artifact", None, "PG", False, ("Gross margin", "Free cash flow"),
           "clean control; boilerplate/tables, no genuine redefinition"),
]


def fetch(client: SecClient, a: Anchor):
    if a.use_402_cutoff:
        cutoff = first_402_date(client, a.cik)
        facts = client.company_facts_by_cik(a.cik)
        cik = a.cik
    else:
        cutoff = ANCHOR
        cik = client.resolve_cik(a.ticker)
        facts = client.company_facts(a.ticker)
    docs = fetch_documents(client, a.ticker or a.label, facts, n_filings=12, cik=cik, before=cutoff)
    return docs.documents, cutoff


def isolate_per_period(documents, kpi):
    """[(period, component_set_or_None)] oldest-first."""
    out = []
    for pd in group_documents(documents):
        out.append((pd.fiscal_label, isolate_definition(pd.all_text, kpi)))
    return out


def main() -> int:
    client = SecClient()
    print("DEFINITION-ISOLATION SPIKE — 4 anchors\n" + "=" * 64)
    verdicts = {}
    for a in ANCHORS:
        try:
            docs, cutoff = fetch(client, a)
        except Exception as e:  # noqa: BLE001
            print(f"\n{a.label}: FETCH FAILED: {e}")
            verdicts[a.label] = ("ERROR", str(e))
            continue
        periods = group_documents(docs)
        print(f"\n{a.label}  [{a.kind}]  cutoff={cutoff}  docs={len(docs)}  periods={len(periods)}")
        print(f"  ({a.note})")
        anchor_changed = False
        anchor_manufactured = False
        for kpi in a.kpis:
            series = isolate_per_period(docs, kpi)
            defined = [(p, c) for p, c in series if c is not None]
            print(f"  · {kpi}: {len(defined)}/{len(series)} periods isolated")
            for p, c in series:
                mark = "  ~none~" if c is None else f"  {{{', '.join(sorted(c))}}}"
                print(f"        {p}: {mark}")
            # change detection on ISOLATED SETS: latest defined vs prior defined
            if len(defined) >= 2:
                (pp, pc), (cp, cc) = defined[-2], defined[-1]
                delta = (cc - pc) | (pc - cc)
                if delta:
                    anchor_changed = True
                    added = sorted(cc - pc)
                    removed = sorted(pc - cc)
                    print(f"        >> CHANGE {pp}->{cp}: +{added} -{removed}")
        # Kill-criterion bookkeeping
        if a.kind == "genuine":
            verdicts[a.label] = ("RECOVERED" if anchor_changed else "MISSED", "")
        else:
            # artifact: recovering a *changed* isolated set would be a false fire.
            any_isolated = any(c is not None for kpi in a.kpis for _, c in isolate_per_period(docs, kpi))
            if anchor_changed:
                verdicts[a.label] = ("FALSE-FIRE", "isolated set changed on an artifact case")
            elif any_isolated:
                verdicts[a.label] = ("STABLE", "isolated a real recon set but it did NOT change (correct)")
            else:
                verdicts[a.label] = ("REJECTED", "no reconciliation definition manufactured (correct)")

    print("\n" + "=" * 64 + "\nKILL CRITERION")
    for a in ANCHORS:
        v, why = verdicts.get(a.label, ("?", ""))
        ok = (a.kind == "genuine" and v == "RECOVERED") or (a.kind == "artifact" and v in ("REJECTED", "STABLE"))
        print(f"  [{'PASS' if ok else 'FAIL'}] {a.label:18s} {v:12s} {why}")

    genuine_ok = all(verdicts.get(a.label, ('',))[0] == "RECOVERED" for a in ANCHORS if a.kind == "genuine")
    artifact_ok = all(verdicts.get(a.label, ('',))[0] in ("REJECTED", "STABLE") for a in ANCHORS if a.kind == "artifact")
    print("\nVERDICT:")
    if genuine_ok and artifact_ok:
        print("  PROCEED — isolation recovers both genuine cases and rejects both artifacts.")
    else:
        print("  STOP — isolation did not cleanly separate genuine from artifact.")
        print("  The extraction problem is harder than a note-parser solves; signal stays shelved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
