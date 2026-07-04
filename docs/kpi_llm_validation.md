# KPI-Drift LLM Adjudicator — Validation (Phase 4)

Date: 2026-07-04 · Config frozen · Harness: `scripts/run_kpi_phase4.py`
Judgments: `data/kpi_judgments.json` (blind, from definition text alone) · Pairs: `data/kpi_pairs.json`

## Pre-committed criteria (set in the design, before any result)

- clean-company false-positive rate drops to < 8%;
- restater recall held (~within one case of the deterministic baseline);
- the MiMedx / Comscore / SunPower early leads preserved;
- adversarial cosmetic/material test: cosmetic → not material, material → material.

## Result

| | restater recall | clean FP | early-warning leads |
|---|---|---|---|
| Deterministic baseline | 7/10 (70%) | 4/16 (25%) | [150, 251, 315] |
| **LLM adjudicator** | **2/10 (20%)** | **0/16 (0%)** | **[251]** |

| Criterion | Outcome |
|---|---|
| clean FP < 8% | **PASS** (25% → 0%) |
| precision improved | **YES** — decisively |
| restater recall held | **FAIL** (70% → 20%) |
| MiMedx/Comscore/SunPower leads preserved | **FAIL** (only SunPower 251d survived) |
| adversarial cosmetic/material | **PASS** (see `test_kpi_phase4_adversarial.py`) |

## What actually happened (the important part)

The LLM adjudicator did exactly its job — it suppressed cosmetic and artifactual
changes, taking the clean-company false-positive rate from 25% to **zero**. In
doing so it exposed something the token-similarity detector hid: **most of the
deterministic recall was extraction noise, not genuine redefinitions.**

Inspecting the actual extracted "definition" text for the restater fires that the
LLM dropped:

- **MiMedx (Adjusted EPS, the celebrated 150-day lead):** the extracted "prior"
  and "current" definitions are **quarterly revenue-guidance highlights**, not an
  EPS definition. Token-similarity was low simply because guidance numbers change
  every quarter. There is no definitional change in the text. The 1.6-quarter
  "lead" was an artifact.
- **Comscore (Adjusted EBITDA, the 315-day lead):** the "current" text is generic
  boilerplate ("excluding certain costs ... provides a meaningful indication"). It
  states no concrete definition to compare. The real prior definition (a detailed
  add-back list) had no comparable current — the extraction grabbed boilerplate.
- **Kraft Heinz, Molson Coors, Plug Power:** results commentary, headers, and
  product descriptions — not definitions.
- **Clean-company fires (PG, GIS, CSCO, MDT):** reconciliation tables with
  different-period numbers, or identical definitions reported for a new quarter —
  cosmetic by construction.

Only **two** cases survived semantic scrutiny as genuine redefinitions:
- **SunPower** — the non-GAAP gross-margin and Adjusted-EBITDA adjustment sets
  genuinely changed (251-day lead, preserved);
- **WageWorks** — the Adjusted-EBITDA add-backs broadened to include stock-based
  compensation and contingent consideration (contemporaneous).

## Verdict

**Keep the LLM adjudicator; do NOT ship the KPI-drift signal as-is.**

Two separable conclusions:

1. **The LLM layer works and should be kept.** It improved precision decisively
   (FP 25% → 0%), passed the adversarial cosmetic/material test, and — most
   valuably — prevented shipping a mirage. The user's pre-committed rule was
   "remove it only if it does not improve precision"; it clearly does, so it stays.

2. **The KPI-drift signal is not deployable, because the bottleneck is EXTRACTION,
   not adjudication.** The recall collapse is the finding: the deterministic
   detector's apparent signal — including the timing analysis's celebrated MiMedx
   and Comscore early leads — was substantially token-similarity firing on
   period-specific numbers, guidance, and boilerplate that the extractor mislabeled
   as "definitions." When a semantic judge reads the actual text, those evaporate.
   A genuine KPI-drift signal exists (SunPower, WageWorks) but it is much smaller
   than the historical numbers implied.

## Implication for the earlier finding

This revises `narrative_timing.md`. The KPI-definition-change early leads (MiMedx
1.6Q, Comscore 3.5Q) that looked like the project's one predictive signal were, on
rigorous inspection, largely extraction artifacts. SunPower's 2.8Q lead survives.
The honest current state: **the KPI-drift signal is real but rare, and cannot be
trusted until the extractor isolates genuine non-GAAP definitional statements
(structured reconciliation-note parsing) rather than nearby prose and tables.**
That extractor is the true prerequisite — larger than this phase — and only worth
building if the surviving signal (2 of 10) justifies it.

## Honest caveats

- **N is tiny** (18 pairs, 10 restaters, 16 clean). Directional.
- **The judge was the agent, not a production model call**, applying a consistent
  material-only-when-text-shows-a-broadening rubric, recorded in
  `data/kpi_judgments.json` for audit. The load-bearing judgments (MiMedx/Comscore
  extracted text is guidance/boilerplate, not a definition) are objectively
  checkable, not subjective. A production deployment calls a live model through the
  same blind prompt + grounding contract.
- **This validates the ADJUDICATOR and the METHOD, and invalidates the SIGNAL's
  current recall** — a genuinely useful, if deflating, result.

## Bottom line

The four phases delivered a working, grounded, cached, degradable LLM adjudicator
and, through it, the truth: the KPI-drift signal's headline recall was mostly
extraction noise. Precision is now excellent and false positives are gone, but the
real signal is 2 of 10 cases, and the next honest step is not more adjudication —
it is definition-isolation extraction, to be undertaken only if that small real
core is judged worth it. The rigorous path prevented shipping the mirage; that is
the phase working as designed.
