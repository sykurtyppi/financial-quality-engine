# KPI-Drift Adjudication Layer — Design (LLM materiality judge)

Status: **DESIGN ONLY — not implemented.** · Date: 2026-07-04
Precedes any code. Reviewed against: the grounding contract
(`app/services/narrative/grounding.py`), the blind validation framework, and the
narrative-timing finding.

## 1. Why this exists, and the exact success criterion

The historical investigation established that **KPI-definition drift is the one
validated, differentiated, *predictive* signal** in the engine — it led
restatements by ~1.5–3.5 quarters (MiMedx 1.6Q, Comscore 3.5Q, SunPower 2.8Q),
including on the channel-stuffing fraud the deterministic metrics missed entirely.

But the current detector is crude: Jaccard token-similarity of definitional
sentences (`kpi_drift.detect_definition_changes`, threshold 0.55). It discriminates
in aggregate (fired on ~60% of restaters vs 18% of clean companies) but at the
individual level it flags innocuous rewording as readily as real redefinition.

**The LLM layer's single job is to raise precision** — to distinguish a *material*
redefinition (the metric now measures something different, usually more flattering)
from *cosmetic* wording changes, so the false positives that token-similarity
produces are suppressed while the real leads are kept.

**Success criterion (pre-committed, measurable):** re-run the clean-company control
and the timing analysis with the LLM adjudicator in place, and require:
- clean-company false-positive rate **drops materially** (target: 18% → < 8%);
- restatement recall **held** (still fires on the same restaters, especially the
  MiMedx early lead);
- the ~1.5–3.5Q lead **preserved** (materiality is judged on the same early filing).

If precision does not improve on the clean control, the LLM layer is not worth its
cost or its dependency, and we keep the deterministic detector. This is the same
control-first discipline that governed every prior step.

## 2. The governing principle (non-negotiable)

**The LLM adjudicates; it never searches, and it never invents.**

- The **deterministic layer** finds candidate KPI-definition pairs and supplies the
  *verbatim* prior and current definitional sentences. The LLM receives only those.
- The **LLM** classifies the *materiality and direction* of the change and explains
  it, citing the specific words that changed — grounded entirely in the provided
  text.
- The **grounding validator** rejects any output that cites unknown evidence, uses
  banned/accusatory vocabulary, uses an off-vocabulary label, or contains a number
  not present in the provided definitions.
- If the LLM is unavailable, errors, or its output fails validation, the system
  **falls back to the deterministic token-similarity finding** (flagged
  low-confidence). The signal is never dropped and the engine stays
  deterministic-first.

This keeps the LLM strictly a *consumer of pre-extracted evidence*, which is the
only role the whole project's architecture permits it.

## 3. Pipeline (five stages, LLM only in stage 3)

```
1. EXTRACT (deterministic, exists)
     KPI dictionary match -> definitional sentence per KPI per period
     -> candidate pairs: (kpi, prior_def, prior_period, current_def, current_period)
     Also: KPI-drop candidates (touted in prior periods, absent now).

2. PRE-FILTER (deterministic, cheap)
     Drop identical / near-identical pairs (token-similarity >= 0.90) — no need to
     spend an LLM call on unchanged text. Everything below goes to adjudication.
     (Token-similarity is demoted from FINAL JUDGE to CHEAP PRE-FILTER — its exact
     failure mode, calling wording changes "material", is what the LLM now fixes.)

3. ADJUDICATE (LLM, under grounding contract)  <-- the new layer
     For each surviving pair: classify materiality + direction, explain with citation.

4. GROUND (deterministic validator, exists)
     validate_annotations(): reject unknown-id / banned-vocab / off-vocab / ungrounded-number.
     Failures -> fall back to the deterministic finding, low-confidence.

5. GATE + SCORE (deterministic)
     Only material changes emit a KPI-drift finding. Direction sets concern.
     Verbatim before/after retained in the evidence ledger for the analyst.
```

## 4. Data contracts (design specs — not yet code)

```python
class KpiChangeMateriality(str, Enum):
    NO_MATERIAL_CHANGE   = "no_material_change"    # cosmetic / reordering / synonyms
    BROADENED_EXCLUSIONS = "broadened_exclusions"  # now excludes MORE (flatters metric)
    NARROWED_SCOPE       = "narrowed_scope"        # now covers less
    CHANGED_BASIS        = "changed_basis"         # fundamentally different calculation
    AMBIGUOUS            = "ambiguous_requires_review"

class KpiChangeDirection(str, Enum):
    MORE_FLATTERING = "more_flattering"  # change tends to improve the reported figure
    LESS_FLATTERING = "less_flattering"
    NEUTRAL         = "neutral"
    UNCLEAR         = "unclear"

class KpiDefinitionPair(BaseModel):        # produced by stage 1, entered in the ledger
    evidence_id: str                        # NE-nnn, so the LLM output can reference it
    kpi: str
    prior_period: str
    prior_definition: str                   # verbatim
    current_period: str
    current_definition: str                 # verbatim

class KpiDriftAdjudication(BaseModel):      # the LLM's structured output
    evidence_id: str                        # MUST equal the pair's id (grounding check)
    materiality: KpiChangeMateriality
    direction: KpiChangeDirection
    changed_clause: str                     # the specific span that changed, quoted from input
    explanation: str                        # <= 2 sentences, grounded, non-accusatory
    confidence: str                         # high | medium | low
```

Notes:
- `changed_clause` must be a substring (normalized) of the supplied definitions —
  a second, cheap, deterministic anti-hallucination check *in addition to* the
  grounding validator's number check.
- The KPI-drop case reuses the same schema with `current_definition = ""` and a
  materiality of `NARROWED_SCOPE` if the LLM judges the dropped metric was a headline
  figure (vs an incidental one-time mention).

## 5. The adjudicator interface (provider-agnostic, testable, degradable)

```python
class KpiDriftAdjudicator(Protocol):
    def adjudicate(self, pair: KpiDefinitionPair) -> KpiDriftAdjudication | None: ...
```

Two implementations:
- `DeterministicAdjudicator` — the current token-similarity logic, wrapped to emit a
  `KpiDriftAdjudication` (materiality inferred coarsely, confidence="low"). This is
  the **fallback and the offline-test default**, so the whole engine remains runnable
  and reproducible with no network and no model.
- `LlmAdjudicator` — calls the model (temperature 0), parses structured output,
  runs stage-4 grounding. On any failure returns `None`, and the caller falls back
  to `DeterministicAdjudicator`.

The engine depends on the *protocol*, never on a vendor — identical to how the
grounding contract is already vendor-agnostic.

## 6. LLM prompt specification

**System (constraints, fixed):**
- You are given the prior and current definitions of ONE disclosed metric for ONE
  company. Judge only whether, and how, the definition materially changed.
- Use ONLY the text provided. Do not use any outside knowledge of the company.
- You do not know, and must not guess, anything about the company's future,
  performance, or any restatement. (Blinding — see §8.)
- Do not assert intent, wrongdoing, fraud, or manipulation. Describe the change
  mechanically. (Legal framing — banned vocabulary is rejected downstream.)
- Quote the specific clause that changed. Do not introduce numbers not present in
  the definitions.
- Output the structured schema exactly.

**User (template, per pair):**
```
Metric: {kpi}
Prior definition ({prior_period}): "{prior_definition}"
Current definition ({current_period}): "{current_definition}"
```

**Output:** forced structured `KpiDriftAdjudication` (StructuredOutput / tool-call
style, so parsing cannot fail silently).

The prompt is deliberately tiny and self-contained: one metric, two sentences, no
context. That is what makes it (a) cheap, (b) cacheable, (c) blind, and (d) hard to
hallucinate around.

## 7. Grounding integration

The `KpiDefinitionPair` is written to the narrative evidence ledger first, yielding
its `NE-nnn` id. The LLM's `KpiDriftAdjudication.explanation` + `evidence_id` are
wrapped into the existing `GroundedAnnotation` and run through
`validate_annotations()` with:
- `evidence_ids = [pair.evidence_id]` — must exist (it does; we just made it);
- `classification` mapped deterministically from materiality
  (`broadened_exclusions`/`changed_basis`/`narrowed_scope` → `narrative_drift` or
  `presentation_risk`; `no_material_change` → suppressed, no finding emitted);
- `explanation` — checked for banned vocabulary and for ungrounded numbers against
  the pair's definitions (the number pool = the two definition strings).

No change to `ALLOWED_CLASSIFICATIONS` is required (materiality is a separate
structured axis). One small addition: register the materiality/direction enums as
allowed structured values, and add `changed_clause ⊆ input` as an extra check.

## 8. Blinding — why it is load-bearing

The adjudicator must never see the outcome (the 4.02, the fraud, the stock move).
Two reasons:
1. **Validation integrity.** If the LLM is told "this company later restated," its
   materiality judgments are contaminated and the clean-control comparison is
   meaningless. The blind validation framework requires this.
2. **Production identity.** In live use there IS no outcome — the filing just
   dropped. Designing the adjudicator to work from the two definitions alone means
   the validation setup and the production setup are identical, so validation
   results transfer.

The harness that runs the validation must strip company identity where feasible and
must never pass outcome data into the prompt. Leakage probes (per the framework)
should confirm the model volunteers no outcome knowledge.

## 9. Reproducibility & determinism

The project prizes reproducible outputs (golden reports, snapshot tests). LLMs are
non-deterministic, so:
- **Temperature 0** for all adjudication calls.
- **Response cache** keyed by `(model_id, prompt_hash)`; a re-run reads the cache,
  so reports are byte-reproducible and cheap. The cache is committed for the
  validation fixtures.
- **Fixture-based tests**: unit tests use recorded adjudications (a captured
  response per known pair), never a live call. CI never depends on a model.
- **Deterministic fallback** guarantees the engine produces *some* answer with no
  model at all, so the golden-report pipeline stays green offline.

## 10. Failure handling & degradation

| Failure | Behavior |
|---|---|
| Model unavailable / timeout | fall back to `DeterministicAdjudicator`, confidence="low" |
| Output fails structured parse | fall back, log |
| Output fails grounding (bad number / banned word / bad id) | reject, fall back, log the violation |
| `changed_clause` not a substring of input | reject (hallucinated span), fall back |
| Pre-filter finds no changed pairs | no finding (correct — nothing changed) |

The signal is never silently dropped; degradation is always to the deterministic
finding with an explicit confidence downgrade.

## 11. Validation plan (before this is trusted)

1. Freeze the deterministic baseline numbers (already recorded): clean FP 18%,
   restater recall ~60%, MiMedx 1.6Q lead.
2. Run the LLM adjudicator over the **same** restatement set and clean-company
   control (blind), producing material-only findings.
3. Compare: clean FP (target < 8%), restater recall (hold), lead (hold ~1.5–3.5Q).
4. Adversarial check: hand a set of *known-cosmetic* changes (reordered clauses,
   synonym swaps) and require `no_material_change`; hand *known-material* changes
   (added exclusions) and require the material labels. This is a direct precision/
   recall test of the judge itself, independent of the restatement outcome.
5. Report all of it — including if it fails — in a `kpi_drift_llm_validation.md`,
   same as every prior control.

## 12. Risks & limitations (honest)

- **The LLM can be wrong on subtle accounting.** Mitigation: it is a *filter*, not a
  verdict; the verbatim before/after stays in the ledger for a human, and the
  materiality label is presented as an opinion, not a fact.
- **Non-determinism / drift across model versions.** Mitigation: temp 0, cache,
  `model_id` in the cache key, re-validate on model change.
- **Small validation N** (single-digit restatement cases). This design cannot fix
  that; it states the limitation and treats the validation as a pilot, powering a
  larger run — consistent with the blind framework.
- **Definitional-sentence extraction is itself imperfect** (stage 1 misses KPIs whose
  definitions span multiple sentences or tables). The LLM cannot adjudicate a pair
  the deterministic layer never extracted. Extraction hardening is a separate,
  prerequisite task — and now a justified one, because there is a validated signal
  to feed.
- **Cost/latency** are minimal by construction (one tiny prompt per changed pair,
  cached), so scale is not a risk.

## 13. What this design deliberately does NOT do

- It does **not** let the LLM read whole filings or "find" KPI changes — extraction
  stays deterministic; the LLM only judges pre-extracted pairs.
- It does **not** let the LLM assert intent, fraud, or a prediction — only the
  mechanical materiality of a definitional change.
- It does **not** make the LLM a hard dependency — the deterministic detector
  remains the floor.
- It does **not** touch the scoring weights or any other detector — scope is
  strictly the precision of the one validated signal.

## Bottom line

The LLM here is a **materiality judge over deterministic candidates**, blind to
outcomes, bounded by the grounding contract, cached for reproducibility, and
degradable to the deterministic detector. Its only mandate is to convert the crude
token-similarity signal into a precise one, measured by a pre-committed drop in the
clean-company false-positive rate with the restatement leads preserved. Nothing
about it violates the deterministic-first, no-invention, evidence-grounded
architecture the whole project is built on — it sharpens the one signal worth
sharpening, and it is testable, blind, and honest by construction. Build it only
after the validation plan in §11 is wired to prove it earns its place.
