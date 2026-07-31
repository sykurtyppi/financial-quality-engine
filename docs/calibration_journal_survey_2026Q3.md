# Literature survey: thesis pre-registration, decision journals, forecast calibration
*(agent-researched 2026-07-31; companion to [arxiv_survey_2026Q3.md](arxiv_survey_2026Q3.md).
PDF-verified items were read from primary sources in-session; snippet-only and
unverified items are marked. Full sources list at the agent transcript.)*

## The null result, stated plainly

**There is no rigorous empirical literature on investor decision journals or
pre-registered investment theses.** No RCT, no field study, nothing measuring
whether keeping one changes decision quality. The concept is practitioner
folklore. **Consequence:** the product may never claim "journals improve
outcomes." It may claim that the journal's *components* — pre-specification,
forced counter-explanation, belief updating — each have separate support in
adjacent literatures. Keep that distinction in all user-facing copy.

## The convergent design finding: specificity is the active ingredient

Three independent literatures agree that vague commitment does nothing and
specific procedure does the work:

- **Brodeur, Cook, Hartley & Heyes (JPE Micro 2024; 15,992 test statistics):**
  bare pre-registration shows NO reduction in p-hacking; only detailed
  pre-analysis plans do. A free-text "I think margins hold" note is, on this
  evidence, worthless as a commitment device.
- **Lord, Lepper & Preston (JPSP 1984):** "be fair and unbiased" instructions
  fail; the specific "consider the opposite" procedure works. (Direction
  verified; magnitudes not.)
- **Arkes, Faust, Guilmette & Hart (JAP 1988, PDF-verified):** telling people
  about hindsight bias fails ("such warnings proved ineffective"); requiring one
  concrete piece of supporting evidence per rival hypothesis cut biased judgments
  from 58% to 41% (χ²=4.12, p<.05). Reduced, not eliminated; single study;
  the Guilbault et al. 2004 meta-analysis (Md=.39) finds debiasing manipulations
  generally fail — so claim mechanism ("removes the memory-reconstruction
  channel"), never "debiasing."

**Journal schema consequences (BEFORE-block upgrade):**
1. Assumptions become machine-checkable five-field records: metric (bound to an
   XBRL concept/derived ratio) · comparator+threshold · measurement window ·
   source · resolution date. Refuse to arm monitoring below a specificity floor.
2. Two required fields per assumption: one filed fact that would FALSIFY it, one
   that would CONFIRM it (the Arkes port). Replay both at resolution.
3. Immutability already implemented (never edit a BEFORE) — supported by
   Fischhoff 1975: outcome knowledge distorts invisibly; verbatim originals
   displayed before outcomes.
4. Always resolve every armed assumption at its date, including abandoned ones;
   track an "abandoned before resolution" count (Olken 2015: the mechanism is
   mandatory reporting of everything pre-specified).

## Calibration: what actually has effect sizes

- **Chang, Chen, Mellers & Tetlock (JDM 2016, PDF-verified).** One-hour
  debiasing training improved Brier 6–11% across four randomized tournaments.
  Table 5 (observational, self-tagged): **comparison-class reasoning had the
  best Brier of any principle (0.17 vs 0.49 control)**. Mere repeated
  participation without structure: 0% and −9% improvement (Table 9) — tracking
  alone does nothing. Targeted practice (re-forecasts per question) mediated
  the training effect in 3 of 4 years.
  → **Feature: reference-class base rates from XBRL.** For "gross margin stays
  >40%": of peer-set firms (same SIC, size band) above 40% in year t, what
  fraction remained above in t+1..t+3. The single best-evidenced feature in
  either survey. Medium effort.
- **Mellers et al. (Psych Science 2014, PDF-verified).** Superforecasters:
  7.8 forecasts per question vs 1.4; early 100% forecasts resolved correct only
  ~70% of the time. → filing-triggered re-affirmation of every open assumption;
  update-count display; flag extreme early confidence. Small effort.
  (Correlational — do not claim updating causes accuracy.)
- **Mellers et al. (PPS 2015, PDF-verified).** Supers used ~57 distinct
  probability values vs ~30. → do not snap conviction to coarse buckets; the
  current 1–5 scale is the coarsest possible instrument. Correlate, not cause.
- **Murphy (1973) Brier decomposition** (calibration/resolution/uncertainty):
  ~20 lines of arithmetic over the journal's own resolved entries — but
  **gate below ~50 resolved items**; at journal scale (~2 dozen/yr) a
  calibration curve is sampling noise dressed as feedback.
- **Reference-class forecasting (Flyvbjerg):** no quantified effect size
  retrievable; 2025 review frames open problems. Justify the reference-class
  feature on Chang et al., not Flyvbjerg.

## The strongest economic argument for the product (reframing)

**Barber & Odean (JF 2000, PDF-verified):** 66,465 households; frequent traders
earned 11.4% net vs 18.5% for infrequent — **~7.1pp/yr lost to costs — while
gross returns barely differed**. High-turnover investors weren't worse pickers;
they were beaten by their own activity. → The thesis monitor's defensible value
claim is **the trades it prevents, not the insights it prompts**. Product rule:
alerts carry an explicit computed state "assumption NOT violated" as prominently
as violations. (Caveat: 1991–97 commissions; effect smaller in the zero-
commission era. Corroborated by Barber & Odean 2001: men traded 45% more,
earned 1.4pp less.)

## Lazy Prices corrections (C1, PDF-verified — refines survey #7)

- Headline VW alpha is **34–58bp/mo**; the famous 188bp/mo is the **Risk-Factors-
  section-only** result. Litigation-language changes: 71bp/mo. Zero announcement
  effect anywhere; EDGAR-log evidence shows the mechanism is investors not
  performing the diff.
- **86% of changes are negative-sentiment**; sign every diff with the
  Loughran–McDonald dictionary (deterministic) — unsigned "this changed" is
  near-uninformative.
- Preprocessing recipe: drop tables >15% numeric characters; strip exhibits/
  XBRL/binaries; SRAF stage-one 10-X parse is the reference implementation.
- Section priority by effect size: **Risk Factors > litigation > exec-team
  references > MD&A** (changes concentrate in MD&A; information concentrates
  in Risk Factors). Cue-detection: changes WITHOUT comparative phrasing
  ("compared to prior year") are the least-processed, highest-value alerts.
- Caveats: sample ends 2014, decay likely; return numbers stay out of the
  product per the frozen framing — signal-detection use only.
- Corroboration: Campbell et al. (RAS 2014) — Item 1A is firm-specific, not
  boilerplate (early-mandate sample; boilerplate has grown since).
- Under-updating prior: Abarbanell & Bernard 1992 — even analysts underreact
  (explains ≤half of PEAD); when a filed fact moves against an assumption,
  do not damp the alert as "one quarter is noise" — systematic error runs
  toward under-reaction. SUE (Bernard & Thomas) usable ONLY as a
  within-company salience ranker, never surfaced as a signal.

## Unverified/open (flagged by the agent, preserved here)

Mitchell/Russo/Pennington "premortem +30%" — unverifiable, do not cite.
Guilbault 2004 moderator stats and Lord 1984 magnitudes — snippet-only.
Tooling/overtrading field experiments and Metaculus/GJO evaluations — not
searched (budget). Decision-journal literature — confirmed absent.
