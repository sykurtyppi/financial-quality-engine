# Blind Historical Earnings Validation Framework
### A pre-registration-grade methodology for testing whether the engine changes investment decisions
**Status:** methodology only — no implementation · **Date:** 2026-07-03
**Intended standard:** designed to be pre-registered and to survive peer review at a
decision-science / quantitative-finance venue.

> **The claim under test is narrow and specific.** We are *not* testing "does the score
> predict returns" (that is a backtest, and we already have a modest one). We are testing
> whether **an analyst equipped with the engine makes measurably better earnings-event
> decisions than the same analyst without it, and than the same analyst given a generic LLM
> summary of the same filings.** That is a randomized human-subjects experiment run on
> historical events under strict point-in-time and blinding controls. Decision *change* is a
> mechanism; decision *quality* is the outcome. Change for the worse counts against us.

---

## 1. Research Questions and Hypotheses (pre-specified, falsifiable)

**Primary (H1).** Holding the analyst and the available information constant, adding the
engine's output improves earnings-event decision quality relative to a controlled baseline
workflow. *Estimand:* difference in mean **Brier score** of the analyst's stated probability
of a pre-defined quality event, engine arm minus baseline arm. **Directional prediction:**
engine Brier < baseline Brier. **Null:** no difference.

**Secondary (H2).** The engine outperforms a generic LLM earnings summary built from the
identical point-in-time filings (same estimand).

**Secondary (H3, mechanism).** The engine increases the rate at which the analyst *names the
specific issue that subsequently materialized* ("caught the real issue"), without a
proportional increase in false alarms.

**Secondary (H4, attention).** Given a batch of simultaneous events and a fixed
investigation budget, the engine changes *which* names the analyst investigates and raises
the yield of real issues per unit of attention (Module B).

**Secondary (H5, heterogeneity).** Effects vary by sector and market regime; pre-specified
subgroups, treated as hypothesis-generating (not confirmatory).

All confirmatory tests (H1–H4) are ordered for hierarchical multiplicity control (§10). H5
is exploratory and reported as such.

---

## 2. Design Overview

Three complementary modules, triangulating one conclusion:

- **Module A — Per-event decision RCT (primary).** A two-stage, within-analyst delta design
  with a randomized second-stage arm. Isolates the *marginal* contribution of the engine
  output while holding the analyst and baseline information fixed.
- **Module B — Attention-allocation experiment.** Tests the product's actual claimed value:
  does the engine improve *which* names get scrutinized under a scarce-attention budget.
- **Module C — Mechanical signal triangulation (human-free).** A decision-rule backtest that
  removes analyst variance, scales to thousands of events, and cross-checks whether any human
  effect is consistent with an underlying signal. Supporting, not primary.

Modules A and B are the answer to "changes decisions"; Module C guards against a human effect
that is placebo rather than signal.

---

## 3. The Hindsight / Point-in-Time Discipline (the core threat)

Four independent cut-offs must all bind at each event's decision timestamp *t*:

1. **Fundamentals cut-off.** Only XBRL facts with `filed ≤ t` (the engine's PIT path already
   enforces this; the baseline and LLM arms must be restricted identically).
2. **Document cut-off.** Only filings, transcripts, and press releases *publicly available by
   t*. No later 10-K clarifying an earlier 10-Q.
3. **Market/consensus cut-off.** Prices, positioning, and consensus estimates as-of t only
   (§8 on consensus reconstruction). Realized post-event outcomes are physically withheld from
   the evaluator environment.
4. **Evaluator cut-off (the human one).** Analysts and adjudicators are blinded to everything
   after t, to the study hypothesis, and — as far as feasible — to which arm produced an
   output (§9). This is the cut-off most studies neglect and it is where hindsight actually
   leaks in.

**Survivorship (non-negotiable).** The event universe at each t must be the set of companies
*actually filing at t*, reconstructed from point-in-time constituent/delisting records —
**including firms later delisted, acquired, or bankrupted.** A universe drawn from today's
tickers is disqualifying, because the names that most validate a quality screen are precisely
the ones that disappeared. This requires point-in-time universe data (e.g., CRSP or a
delisting-inclusive fundamentals vendor); if unavailable, the study is explicitly labeled
survivor-restricted and its estimates are treated as lower bounds on true-positive capture.

---

## 4. Event Sampling and Randomization

**Sampling frame.** All quarterly earnings events in the study window (proposed: ~2012–2023,
long enough to span multiple regimes, old enough to have fully realized forward outcomes) for
the PIT-reconstructed universe.

**Rare-outcome enrichment.** Quality events are rare (single-digit % base rate), so a simple
random sample would be nearly all negatives and badly underpowered on H3. Use **case-control /
enriched sampling:** oversample positive cases (events followed by a pre-defined quality
problem) and draw matched negative controls, then recover population-level estimates via
**inverse-probability-of-selection weighting.** Enrichment ratio and matching covariates
(sector, size, pre-event momentum) pre-registered.

**Stratification.** Draw within a pre-specified grid of **GICS sector × market regime ×
outcome class.** Market regime taxonomy is fixed ex-ante and computed only from data available
at t (e.g., VIX terciles × trailing SPX trend sign; or an NBER/credit-spread regime label) —
never from forward data.

**Randomization.**
- *Event → arm* (Module A stage 2): blocked randomization stratified by sector × regime ×
  outcome class, so arms are balanced on the confounders.
- *Event → evaluator*: Latin-square / balanced-incomplete-block assignment so each evaluator
  sees a comparable mix and no evaluator sees the same company twice within a short window.
- *Ordering*: counterbalanced to neutralize fatigue and learning; event presentation order
  randomized per evaluator.

**Concealment.** Allocation sequence generated in advance, held by a party not evaluating, and
revealed per event only after the stage-1 decision is locked.

---

## 5. Module A — The Per-Event Decision RCT (primary)

**Two-stage delta design, per (evaluator, event):**

- **Stage 1 (baseline, common to all).** The evaluator receives the *controlled baseline
  information set* — the PIT filings + a standardized as-of data sheet (financial history,
  price, consensus context) — i.e., a realistic "read the 10-Q + Bloomberg" workflow. They
  submit a **locked, timestamped decision** in the standardized schema (§6). This lock is
  irreversible (enforced structurally, as the live journal already does).

- **Stage 2 (randomized treatment).** The evaluator is randomized to one of **three arms** and
  may revise their decision:
  - **ENGINE:** the engine's PIT output (card + evidence).
  - **LLM:** a generic LLM summary of the *same* PIT filings (§7), matched in length/format.
  - **CONTROL (active placebo):** additional time plus a neutral, non-diagnostic document
    (e.g., the company's boilerplate business description). This separates "the engine helped"
    from "any second look / more time helped" — without it, a naive engine-vs-stage-1
    comparison would be confounded by effort.

**Why this design.** The delta (stage 2 − stage 1) holds the analyst, the baseline
information, and the event fixed, so it isolates the *marginal* value of the treatment. The
three-way randomized second stage gives both the engine-vs-LLM contrast (H2) and the
engine-vs-effort contrast (via CONTROL), while the stage-1 lock prevents the analyst from
rationalizing backward.

**Information-equivalence control.** All three arms operate on the *same underlying PIT
documents*. The arms differ only in *processing* (engine's structured collision vs LLM's
summary vs nothing). This is essential: any measured difference is attributable to the
tool's synthesis, not to differential information access.

---

## 6. The Standardized Decision Schema (what makes it objective)

Every decision, at both stages, is captured in a fixed machine-scoreable schema so arms are
directly comparable and outcomes are computable without subjective coding:

| Field | Type | Scored against |
|---|---|---|
| `action` | {avoid, underweight, neutral, overweight} | forward relative return (§ outcomes) |
| `conviction` | 1–5 | sizing-weighted P&L proxy |
| `p_quality_event` | probability [0,1] that a pre-defined quality event occurs within K quarters | **Brier / log score** (primary) |
| `fade_or_chase` | {fade, chase, none} on the post-print move | realized N-day post-earnings drift |
| `first_issue` | free text: the one thing to investigate first | blind adjudication vs realized mechanism (H3) |
| `flagged_problem` | boolean: "I expect a quality problem here" | specificity / false-alarm rate |

Free-text `first_issue` is the only non-numeric field and is scored by blinded adjudicators
against a pre-defined codebook of realized mechanisms (§ ground truth), with inter-rater
reliability reported (Cohen's κ).

---

## 7. The LLM Comparator — and the Memorization Threat

The LLM arm must be a *fair, strong* generic baseline: a capable model given the same PIT
filings and prompted to produce an analyst-oriented earnings summary — **no engine scaffolding,
no forensic checklist injected** (that would smuggle the engine's method into the "generic"
arm and rig the comparison).

**The dominant threat: training-data leakage.** A model may already "know" what happened to a
2018 earnings event. This would make the LLM arm illegitimately strong *and* contaminate its
outputs with future knowledge. Layered mitigations, all pre-registered:

1. **Closed-book-on-the-future protocol.** The model receives only the PIT documents and is
   instructed to reason solely from provided text; system constraints forbid outside knowledge.
2. **Leakage probes.** For each event, inject counterfactual/future-tense probe questions; if
   the model volunteers post-event facts, the event is flagged. Report the leakage rate.
3. **Sensitivity analysis** excluding high-leakage events; the primary LLM contrast is reported
   both full-sample and leakage-excluded.
4. **Cut-off partitioning.** Where the model's training cut-off is known, report results
   separately for events *after* the cut-off (leakage-free by construction) even though these
   have shorter realized-outcome windows. Tension between leakage and outcome maturity is
   acknowledged, not hidden.
5. **Symmetry note.** The *engine* arm is immune to this specific leakage (it is deterministic
   over PIT inputs), which is itself a finding worth stating — but we must not let the LLM's
   leakage flatter the engine by comparison; hence the exclusions above.

The same leakage discipline applies to any LLM used to *adjudicate* free-text (prefer human
adjudication for the primary; if LLM-assisted, blinded and leakage-probed).

---

## 8. Consensus / Expectations Reconstruction

Event-driven decisions turn on *surprise vs expectations*, which the engine does not provide.
Two design commitments:

1. **Hold it constant across arms.** Every arm receives the *same* as-of consensus context in
   the baseline sheet, so imperfect consensus data cannot differentially advantage an arm — it
   is a shared constant, removed as a between-arm confounder.
2. **Source honestly.** Prefer point-in-time consensus (I/B/E/S or equivalent). If unavailable,
   use a pre-registered naive expectation model (e.g., seasonal-random-walk or trailing
   analyst-free estimate) and label consensus a study limitation. The absence of premium
   consensus data weakens external validity for pure event-trading claims but does **not**
   bias the *between-arm* comparison, which is the primary estimand.

---

## 9. Blinding

| Party | Blinded to |
|---|---|
| Evaluators | the future (PIT cut-off); the study hypothesis; arm identity *where feasible* |
| Adjudicators (of `first_issue` and quality-event labels) | arm; evaluator; the study hypothesis |
| Analyst producing outputs | realized outcomes |
| Statistician | arm labels during code development (analyze on permuted labels first) |

**Arm-identity blinding of evaluators** is the hardest and is partially achievable: render all
three arms' outputs through a **common neutral template** (same typography, same section
skeleton) so an evaluator cannot trivially tell "engine" from "LLM." Perfect blinding is
impossible (a structured contradiction table looks different from prose), so the *load-bearing*
blind is on the **adjudicators and the outcome data**, not on the evaluator's tool-recognition.
Demand characteristics are further limited by the active-placebo CONTROL arm and by not telling
evaluators which tool the study "hopes" wins.

---

## 10. Statistical Analysis Plan (pre-registered)

**Primary model.** Mixed-effects regression of the decision-quality outcome (Brier) on arm
(fixed), with **crossed random effects for evaluator and event**, and pre-specified covariates
(sector, regime, a difficulty proxy, stage-1 Brier as baseline). Selection weights (§4) applied
for population inference.

**Primary contrast.** ENGINE vs BASELINE (H1), as the stage-2 − stage-1 delta difference, with a
pre-specified **superiority** test; a **non-inferiority** margin is *also* pre-registered so a
null is interpretable ("no worse, no better" vs "underpowered").

**Multiplicity.** Confirmatory hypotheses H1→H2→H3→H4 tested in fixed hierarchical order
(gatekeeping); within secondary outcomes, Holm correction. H5 exploratory, FDR-controlled,
labeled non-confirmatory.

**Secondary outcomes.** Directional accuracy (post-earnings drift), "caught the real issue"
rate and false-alarm rate (H3, with a specificity floor so we cannot win H3 by flagging
everything), decision-change magnitude, sizing-weighted forward relative return at 1-week /
1-month / 1-quarter horizons (multiple horizons because the engine's signal is quarterly but
the trade is event-driven — the mismatch is measured, not assumed away), and process metrics
(time-to-decision, questions generated).

**Power / sample size.** Pre-registered assumptions: target effect size (e.g., a Brier
reduction judged decision-relevant), evaluator/event ICCs, α = 0.05, power = 0.80 → derive
required (events × evaluators). **Honest constraint:** a solo builder plus a handful of
analysts will likely be *underpowered* for a definitive confirmatory result. Therefore this is
pre-registered as a **pilot** whose primary purpose is an unbiased effect-size estimate with a
confidence interval, explicitly powering a subsequent larger study — not a p-value hunt. A wide
Cit that includes zero is a legitimate, publishable outcome.

**Pre-registration & reproducibility.** Protocol, sampling frame, decision schema, outcome
definitions, exclusion rules, and analysis code are frozen and time-stamped *before* the first
event is scored; the engine's config is version-pinned (0.3.0, already frozen); all PIT
snapshots are archived so any result is exactly reproducible.

---

## 11. Ground Truth — Quality-Event Labels (objective, PIT-forward)

The `p_quality_event` outcome requires a pre-specified, objectively verifiable label set,
observed in the forward window the evaluator could not see. Pre-registered label = occurrence
within K quarters (K pre-set, e.g., 4–6) of **any** of:

- an 8-K Item 4.02 (non-reliance) or a formal restatement;
- a downward guidance revision beyond a pre-set threshold;
- realized operating-margin deterioration beyond a pre-set threshold (the engine's validated
  signal — included but *pre-committed* so it cannot be reverse-fit);
- a large adverse relative drawdown beyond a pre-set threshold;
- a going-concern or material-weakness disclosure.

Each is machine-detectable from filings/prices (the backtesting layer already detects 4.02 and
computes forward margins/returns). The label set is fixed before sampling; composite and
component labels both reported. `first_issue` adjudication maps the analyst's stated concern to
*which* label actually fired (H3 is about catching the *mechanism*, not just the direction).

---

## 12. Module B — Attention-Allocation Experiment

Directly tests the product's real claim ("routes scarce attention better").

**Design.** Present the evaluator a *batch* of M simultaneous PIT earnings events (a simulated
earnings-morning cluster) and a fixed budget: investigate only K of M. Randomize whether the
batch is accompanied by the engine's ranked queue or not (between-subjects, or within-subject
crossover on disjoint batches).

**Outcome.** *Yield* = number of true quality events among the K chosen, population-weighted;
plus rank-quality metrics (e.g., precision@K, and whether the chosen-K would have caught the
event that actually mattered). Engine-assisted vs unassisted yield is the estimand.

**Why separate from Module A.** A tool can improve per-name decisions yet not improve *which
names you open*, or vice-versa. The product's thesis is specifically about the second; it earns
its own test.

---

## 13. Module C — Mechanical Signal Triangulation (human-free)

Removes analyst variance and scales. Three pre-registered mechanical decision rules over the
enriched PIT event sample, evaluated on the §11 labels:

- **Baseline rule:** a fixed function of standard ratios (the textbook screen).
- **LLM rule:** signals extracted by the closed-book LLM from the PIT filings, mapped to the
  same decision schema by a fixed rubric.
- **Engine rule:** the engine's flags/contradictions mapped to the same schema by a fixed rubric.

Compare AUC / calibration / precision-recall across rules, with the same selection weights and
leakage controls. **Interpretation guardrail:** Module C tests *signal*, not *decisions*. A
human effect in Module A that is *not* accompanied by any Module C signal advantage is a red
flag for placebo/demand effects and must be reported as such; conversely, Module C signal with
no Module A human effect means the information exists but the presentation fails to move
decisions (a product problem, not a signal problem). The two together localize where value does
or does not live.

---

## 14. Threats to Validity and Mitigations

| Threat | Mechanism | Mitigation |
|---|---|---|
| Hindsight leakage | Evaluator infers the future | Four-cut-off PIT (§3); outcomes physically withheld; blinded adjudication |
| Survivorship | Delisted names absent | PIT constituent reconstruction incl. delisted; else label lower-bound |
| LLM memorization | Model knows the outcome | Closed-book protocol; leakage probes; post-cut-off partition; sensitivity exclusions (§7) |
| Effort confound | "Second look" helps, not the engine | Active-placebo CONTROL arm (§5) |
| Rare-outcome underpower | Base rate too low | Case-control enrichment + IP weighting (§4) |
| Gaming H3 by over-flagging | Win "caught it" by flagging all | Pre-set specificity floor; joint hit/false-alarm reporting |
| Evaluator learning/fatigue | Order effects | Counterbalancing, Latin-square, no-repeat windows |
| Demand characteristics | Analyst guesses intent | Hypothesis-blind; neutral common template; placebo arm |
| Consensus data gaps | Weak expectations input | Held constant across arms (§8); limitation stated |
| Researcher DoF | p-hacking | Pre-registration; frozen config/snapshots; hierarchical multiplicity |
| Horizon mismatch | Quarterly signal vs event trade | Multi-horizon outcomes (1w/1m/1q), reported separately |
| Generalization | Few evaluators, one engine version | Framed as pilot; effect-size CI powers a larger study |

---

## 15. What Would Count as a Positive, Negative, or Null Result

- **Positive:** ENGINE Brier significantly below BASELINE (H1) *and* consistent direction in
  Module C signal, with H3 "caught the real issue" up without a specificity collapse. This
  would justify building the copilot wedge.
- **Negative:** ENGINE no better than CONTROL (effort placebo) or worse than LLM, especially
  with a Module C signal advantage present — meaning the presentation fails to move decisions.
  Directs effort to the experience redesign, or to stopping.
- **Null / underpowered:** CI includes zero but excludes the pre-registered decision-relevant
  effect in neither direction — the expected pilot outcome; publish the effect-size estimate and
  power the confirmatory study. A null is a legitimate, reportable result, not a failure to hide.

---

## 16. Sequencing (methodology → execution, when authorized)

1. Pre-register the protocol; freeze config, sampling frame, schema, labels, analysis code.
2. Acquire PIT universe + (ideally) consensus + delisting-inclusive fundamentals.
3. Build the enriched, stratified event sample; generate all three arms' PIT outputs offline.
4. Run evaluators under the blinding protocol; lock stage-1 before stage-2 (structural).
5. Adjudicate free-text blind; compute labels from forward data.
6. Analyze per the frozen plan; report full-sample and leakage-excluded; publish the CI.

The live decision-impact journal already running is the *prospective, single-analyst* cousin of
Module A; this framework is its blinded, randomized, multi-evaluator, historical generalization.
The two are designed to converge or, more usefully, to disagree in a way that localizes the
truth.

---

### One-paragraph statement of what this framework is for

It exists to prevent the most likely way this project fools itself: concluding from a handful of
memorable saves that the engine "works," when the honest question is whether an analyst *using*
it makes better decisions than the same analyst without it, or with a free LLM, on a fair,
survivorship-complete, hindsight-proof sample of ordinary earnings events. If the answer is yes,
we will have evidence no strategy memo can produce. If it is no, we will know that too — and
knowing is the entire point.
