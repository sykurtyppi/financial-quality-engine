# Validation Strategy — 2026-08-01

Status: adopted design (execution scheduled in [ROADMAP_2026Q3.md](ROADMAP_2026Q3.md)) · Governing question: **does the engine actually improve investor decisions?** Every validation activity below is assigned to one of five levels, because conflating them is how projects fool themselves. Hard rule inherited from the whole project history: **no claim ships above its evidence level.**

Leakage discipline applies to every historical test: the only admissible inputs at timestamp T are facts filed ≤ T (vintage store), documents available ≤ T, and reference distributions built from data ≤ T. Any test that cannot state its as-of is invalid by definition.

---

## Level 1 — Component validation ("does each detector detect what it claims?")

| Component | Method | Gate |
|---|---|---|
| XBRL mapper | to-the-dollar reconciliation vs filed statements (exists: AAPL/MSFT/KO); add one 52/53-week filer and one FYE-change filer | exact match or documented exception |
| TTM constructor / comparator service | fixtures incl. Apple FY2024 YTD shape, 4-4-5 calendars, Q4-derivation gaps | unit tests, no gap-spanning TTM |
| Section extraction (MD&A/Item 1A) | **public 3,737-filing gold benchmark (κ=0.92)** — measure precision/recall of our extractor against it; publish the number | ≥0.90/0.90 on parseable filings; unparseable rate reported, never hidden |
| Offerings parser | real-prospectus fixtures (exists: KTOS/FPS/AMZN-debt) + quarterly spot-check of 10 random 424Bs | share counts and classification exact |
| 8-K item / NT rules | fixtures from real filings incl. edge cases (4.01 resign-vs-dismiss, NT extension arithmetic) | rule-exact |
| Form 4 derived lateness / clusters | fixtures; cross-check derived lateness base rate ≈ 8.7% on a random sample (survey-6 measured value) | within 2pp |
| Narrative measures | per-measure fixtures; **placebo test**: run each detector on the clean-control corpus — any detector firing >20% on clean filers cannot ship as Tier-1/2 | placebo-bounded FP |
| Report/card rendering | golden files (exists); provenance-completeness gate: zero evidence items without accession+filed | byte-deterministic |

**CI requirement**: all of Level 1 runs on every commit (GitHub Actions, offline fixtures only). Today's 282 tests bite only when run by hand — that ends.

## Level 2 — Historical/PIT validation ("would the information have existed at time T?")

- **PIT replay runner**: regenerate any past report at `as_of = T` from the vintage store and diff against the report actually shipped that day. Gate: zero facts in the replay that were filed after T.
- **Vintage-store honesty test**: for each live report, store the snapshot; a quarterly job replays 3 random past reports and asserts identity.
- **Reference-class leakage test**: distributions dated Q3 must be rejected by a Q2 as_of run.
- **Filing-timing audit** (exists informally from the season): per covered filer, record 8-K time, 10-Q lag, and when each fact became available — this is the empirical basis for any "same-day evidence" claim.

## Level 3 — Signal validation ("does the signal correspond to meaningful outcomes?")

Already done and **not to be re-litigated**: the eight completed experiments (distress ✓, death ✗, misstatement ✗, high-severity 30/0, adjustment-keywords noise, KPI-drift ~1/10). New signals enter through the same door:

- **Design pattern** (proven in-house): case set + matched control + pre-committed criteria + published nulls. n=10–16 per side is acceptable for direction, never for magnitude; every writeup states this.
- **Queue** (per roadmap): restatement-footprint detector vs the survey-5 base rates (3.4–6.7% Big-R mostly benign — corroboration required); adjustment-ledger novelty on the restater/clean corpora already assembled; risk-factor set-diff on the same corpora (Lazy-Prices sign discipline: 86% of changes negative — sign every diff); NT/4.02 rules against their published effect directions; cluster purchases against the survey-6 direction.
- **Placebo signals**: every new textual detector runs against (a) the clean-control corpus and (b) a shuffled-period placebo (current docs vs wrong-quarter metrics). A detector that can't beat its placebo is evidence-plane-only, labeled unvalidated.
- **Ablation**: when the tiered-triage layer exists, ablate each tier on the season-2026Q2 archive (11 reports + outcomes now known) — which tier changed what the card would have said? This is retrospective and small-n; it informs presentation, it does not validate prediction.

## Level 4 — Decision validation ("would it have changed a decision, beneficially?")

The journal is the only instrument here, and the *only* level that can prove the product claim. Protocol (upgraded, §5): prospective, preregistered, resolution-scored. Explicitly: Level-3 success does not imply Level-4 success (a valid signal the user already knew is worth zero), and Level-4 value includes **trades prevented and false alarms correctly dismissed** — the checked-and-clean section is scored too.

Gate restated (unchanged from the season review): 8–10 closed cases with outcomes → answer "would you keep using it voluntarily?"; 20+ cases before any stronger claim.

## Level 5 — Calibration validation ("are confidence levels empirically justified?")

- Confidence enums (high/medium/low) on evidence items get empirical FP rates as cases accumulate: a "high-confidence" class that false-positives >10% gets demoted in config — mechanically, reviewed quarterly.
- Probability calibration (Brier, curves) activates **only** past ~50 resolved journal probabilities (Murphy decomposition floor from the calibration survey). Below that: report raw tallies only, no calibration claims.
- The Chang-2016 result (reference-class base rates: Brier 0.17 vs 0.49) is the reason L5 percentiles ship before any probability language does.

---

## 5. The decision journal — preregistration schema v2

Design constraints from the calibration survey: **specificity is the active ingredient** (Brodeur 2024: bare preregistration does nothing; detailed plans do); one concrete falsifier per rival hypothesis measurably cuts bias (Arkes 1988: 58%→41%); don't force-snap conviction to coarse buckets (Mellers); and there is **no empirical literature showing journals improve outcomes** — so the journal is itself the experiment, never marketed otherwise.

**Anti-annoyance rule**: only `thesis`, `conviction`, and ONE assumption row are required to lock. Everything else is optional-but-nagged. A journal too heavy to use produces n=0, which is worse than imperfect entries (the season proved this: repeated requests for convictions went unanswered under the current, *lighter* schema).

### Entry schema (v2; v1 fields retained)

```
BEFORE (locked; sha256 of this block stored in sidecar at lock time)
  thesis:            free text (unchanged)
  conviction:        1–5 (+ optional 0–100 fine-grained)
  intended_action:   hold | trim | add | avoid | no_position
  catalyst:          expected event + date
  assumptions[]:     machine-checkable rows —
                     {metric: XBRL concept or engine spec_id,
                      comparator: > < >= <= within,
                      threshold: value,
                      window: period,
                      source: which filing will resolve it,
                      resolve_by: date}
  falsifiers[]:      "I am wrong if <concrete observable>" (≥1 encouraged)
  p_outcome:         probability of the named outcome (optional; enables Brier)
  reference_class:   which base rate applies (free text + optional engine ref)
  contamination:     what analysis/discussion preceded this entry (first-class field now)
AFTER (unchanged) + resolution[]: per-assumption row → met | violated | unresolvable, auto-checkable
       where the metric is an engine spec_id (engine proposes, user confirms)
OUTCOME (unchanged verdict) + score: Brier component when p_outcome present
```

### Engine↔journal comparison loop (the product's measurement core)

For each closed case, record four columns: **pre-event belief** (locked entry) · **engine evidence** (ledger items shipped, with ids) · **actual outcome** (resolution rows + market/fundamental result) · **post-event interpretation** (AFTER block). The season sheet then tallies, per case: did engine evidence contradict or support the belief before the event; was the contradiction right; did the user act on it; would acting on it have helped. Four booleans per case — that is the entire product metric, and 8–10 cases suffice for a directional read.

### Tamper evidence
BEFORE-block hash written at lock; verified at every subsequent read; mismatch renders a visible "entry modified after lock" banner. (Cheap, honest, no cryptographic theater.)

---

## 6. What we deliberately do NOT validate

- Market-outcome prediction (refuted; out of scope).
- Portfolio/cross-sectional deployment (non-novel, arbitraged, wrong unit of analysis for this product).
- Any effect size the do-not-ship list bans (Bernard–Thomas magnitudes, deferred-revenue effect sizes, "2%" 53rd-week, death-spiral return claims).
- LLM-generated summaries beyond grounding compliance (they are presentation, not signal).
