# Roadmap 2026Q3 — 2026-08-01

Status: adopted · Supersedes the Tier-1 queue in accuracy_improvement_plan.md Addendum 4 · Priority rule: **expected decision value × confidence in evidence × implementation feasibility.** Novelty scores zero.

Companion docs: [PROJECT_STATE_ASSESSMENT.md](PROJECT_STATE_ASSESSMENT.md) (why), [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md) (what), [VALIDATION_STRATEGY.md](VALIDATION_STRATEGY.md) (proof), [gap_research_2026Q3.md](gap_research_2026Q3.md) (evidence).

Item format: problem → solution → evidence → arch layer → complexity (S/M/L) → benefit → deps → validation → kill criterion → done-when.

---

## P0 — Correctness / foundations (anything that can make existing outputs wrong)

Ship order within P0 is the listed order. Nothing in P1+ starts until P0-A through P0-D are done — every report shipped with known-wrong facts spends the project's only real capital (trustworthiness).

**P0-A. TTM basis for all stock-over-flow ratios + annual-basis Beneish/accrual anchors.**
Problem: net_debt/quarterly-EBITDA ~4× overstated (GLW); M-score quarterly components vs annual cutoffs; accrual anchors ~4× understated — one root cause, three faces (P0-1/2/3). Solution: TTM constructor in L2; formulas declare `basis`; basis-mismatch lint test. Evidence: measured GLW error; 668/2000 covenants use 4Q EBITDA, zero single-quarter. Complexity: M. Benefit: kills the top-red-flag artifact class. Validation: GLW fixture reproduces ~1.85×; golden diffs reviewed. Kill: none (correctness). Done: no ratio in any report mixes stock with sub-annual flow.

**P0-B. Seasonal comparators.**
Problem: all pair metrics sequential-quarter (P0-4); high-growth caveat fires at +40% QoQ i.e. never (P0-7). Solution: comparator service (`yoy_lag4` default for seasonal-sensitive metrics; Q4-never-vs-interim per Binz–Kapons); SGI threshold re-based to YoY. Evidence: GLW FCF-trough misread; Foster 1977 lag-4 baseline. Complexity: M. Validation: Corning-shape fixture; retailer 4-4-5 fixture. Done: no trend metric compares across unlike quarters.

**P0-C. Flag hygiene + retirements.**
Problem: proven-noise and wrong-signed metrics still score and flag (P0-8, P0-13; adjustment_recurrence = 35% of Narrative block at 100% live FP; sbc pair wrong-signed at 45% of Capital Integrity; zero-weighted metrics still flag; discredited KPI extractor live). Solution: the retirement list in TARGET_ARCHITECTURE §5; flags only from scored-or-Tier-listed specs; unwire old KPI extractor. Complexity: S. Validation: rerun MXL/GLW archives — flag lists contain no retired signals. Done: config contains no scored signal with a measured-noise or wrong-signed verdict.

**P0-D. Silent-failure elimination + as-of stamps.**
Problem: doc-fetch failures indistinguishable from non-disclosure; offerings appendix vanishes on exception; 24h caches can serve pre-filing data on filing day; EX-99 candidates[0] can pick the tables exhibit (P0-11/12). Solution: `AcquisitionGap` items rendered in-report; `fresh=True` on event-day fetches; data-as-of line on every report; EX-99 selection prefers the release doc by simple rules with diagnostic. Complexity: S–M. Done: a report generated during an EDGAR outage says so, visibly.

**P0-E. 8-K quarter-labeling fix (P0-6).**
Solution: label from the filing index period, not companyfacts-snap. Complexity: S. Done: fresh earnings 8-K labels correctly before the 10-Q exists.

**P0-F. CI.**
Problem: 282 tests + golden gates bite only when run by hand. Solution: GitHub Actions running the offline suite + provenance-completeness + basis-lint on every push. Complexity: S. Done: a red X is impossible to miss.

## P1 — Highest expected incremental value (evidence-backed)

**P1-A. Provenance end-to-end (`SourcedValue`).**
Problem: accession/filed/tag collected then discarded; ledger says "period documents." Solution: per-field provenance through PeriodFinancials → EvidenceItem; unified ledger schema (L9); ledger JSON persisted beside each report. Evidence: the audit's judgment that this is the project's stated core promise, unfulfilled at two joints where data already existed; Hudson Labs' source-linking as the market's credibility differentiator. Complexity: M. Kill: none. Done: provenance-completeness gate green in CI.

**P1-B. Offerings → pipeline + events promotion (4.02/NT/4.01/2.06, filing-lag).**
Problem: the season's best new evidence lives in a script append; Capital Integrity scored FPS 10/100 with the sponsor's sell-downs in an unconnected appendix; 4.02 detection exists only in the backtest. Solution: L6 as specified; capital-integrity↔offerings consistency check; `as_of` not `date.today()`. Evidence: FPS miss (worst of season); NT/4.02 rules survey-verified with published directions. Complexity: M. Validation: FPS archive rerun — the card must surface the sell-downs adjacent to any dilution claim. Done: an equity takedown in-window forces the caveat.

**P1-C. Triage layer replaces composite (flag tiers + regime dummies + AOM thermometer).**
Problem: composite measured non-discriminating; guards amputate distress top (P0-9). Solution: TARGET_ARCHITECTURE §7. Evidence: gap research Gap 1+2 (Piotroski/Ohlson/AOM precedents; negative-EBITDA prevalence 28%). Complexity: M–L. Validation: season-archive ablation (would the card have read differently on the 11 names — specifically: does FPS/GLW/AMKR triage change); distressed-control rerun — decedents' tier counts must exceed clean controls' at least directionally. Kill: if tiered counts discriminate no better than the composite on the archived season + controls, revert to evidence-only reporting with no aggregate at all. Done: no 0–100 number on any surface.

**P1-D. Decision card (90-second surface) + single report builder.**
Problem: surface inverts the evidence (score first, validated signals buried); two divergent report paths. Solution: L11 card ordering; one builder for CLI+web. Evidence: season review ("the audit loop is the product"); PM-review card spec of 2026-07-03. Complexity: M. Validation: golden card fixtures; next live earnings day used via card only. Done: card renders NEW/WORSENED → thermometer → events → checked-and-clean → data-quality, in that order, both surfaces.

**P1-E. Journal schema v2 + hash lock.**
Problem: free-text schema can't resolve assumptions, no falsifiers/probabilities/contamination field; n=1 after a full season. Solution: VALIDATION_STRATEGY §5 (one-assumption-minimum lock keeps friction low). Evidence: Brodeur 2024 specificity result; Arkes falsifier result; the season's lived non-compliance under even the light schema. Complexity: S–M. Kill: if entry rate stays ~0 for another season, the product thesis itself is falsified — stop building and reassess. Done: an entry can lock with thesis+conviction+1 assumption row in <3 minutes.

**P1-F. Restatement-footprint detector (vintage store + diff).**
Problem: latest-filed-wins silently accepts restated history (P0-5) — blindness where the survey says the leading indicator lives (little-r revisions). Solution: L1 vintage store (start capturing NOW — value compounds with time) + L4 vintage diff. Evidence: survey 5 (Choudhary et al.; measured base rates 3.4–6.7% Big-R). Complexity: M. Validation: Kraft Heinz fixture; base-rate sanity check. Kill: if 2 quarters of live coverage produce only noise diffs (immaterial reclassifications), demote to appendix. Done: any silent prior-period change ≥ threshold surfaces as a provenance-linked item.

## P2 — Validation and calibration

**P2-A. Section-extraction benchmark run** (3,737-filing gold set) → publish our extractor's precision/recall; fix to ≥0.90/0.90 or report honestly. Complexity: M. This gates every narrative measure's denominator.
**P2-B. Placebo harness**: clean-corpus + shuffled-period placebo runners for every live textual detector; FP bounds enforced in config (VALIDATION_STRATEGY L3). Complexity: S–M.
**P2-C. PIT replay runner** + quarterly replay-identity check. Complexity: M. Deps: P1-F vintage store.
**P2-D. Adjudication debt**: the stalled 0/29 wide-sweep labels — either adjudicate (one sitting) or formally close Track 3 with a dated note. Complexity: S. An open-but-dead validation track is worse than a closed one.
**P2-E. Reference-class store v1** (XBRL frames, ~15 core ratios, quarterly): enables percentile language and later conformal p-values. Complexity: M–L. Kill: if frames coverage proves too spotty for mid/small-caps, fall back to own-history percentiles only.

## P3 — Decision experience

**P3-A. Checked-and-clean section** (assumption-not-violated rendering; Barber–Odean framing). Dep: P1-D/E.
**P3-B. Funding-context pass** (GrantsReceivable, supplier-finance A–D, ITC/prepayment lexicon) attached to cash-conversion evidence as benign candidates — the AMKR class. Evidence: survey 5, runs-today verified. Complexity: S–M.
**P3-C. Insider evidence stream** (cluster purchases, derived lateness, plan terminations, Form 144 adoption dates). Evidence: survey 6 with measured base rates and trap list. Complexity: M.
**P3-D. Lazy-Prices cosine per section + Risk-Factor set-diff + specificity + numbers-to-words density.** Evidence: the only outcome-validated change detectors (gap research Gap 3). Complexity: M. Placebo-gated (P2-B) before appearing above Tier-3.
**P3-E. Guidance-error history + ER↔10-Q numeric diff.** Evidence: gap research Gap 4 (persistence peer-reviewed; channel divergence documented). Complexity: M–L (guidance parsing is the hard part — start with numeric-range regex on EX-99.1, measure parse rate, no promises).

## P4 — Experimental (explicitly not evidence-backed yet; time-boxed spikes only)

- Non-GAAP adjustment ledger + n-gram novelty gate (design exists; needs a corpus study before scoring anything).
- Comment-letter (UPLOAD/CORRESP) stream.
- Sentence-embedding drift as a *second* similarity detector (working-paper support only).
- Earnings-call ingestion (EX-99.2 route; no clean free source — prevalence study first).
- Conformal min-p headline (after P2-E matures).
- ATM utilization tracking (survey says hand-collected only — do not automate on promises).

**Standing do-not-build list (unchanged):** stock-return prediction; portfolio deployment; LZ deception cues; PIPE investor classification; death-spiral return claims; 10b5-1 abuse scores; LLM-generated financial facts; any effect size on the do-not-ship list.

---

## Dependency sequence

```
P0-A → P0-B → P0-C → P0-D/E → P0-F (CI)          [strictly serial start; ~none parallelizable safely]
P1-A ← requires P0 stable golden baseline
P1-B, P1-E, P1-F(vintage capture)  ← parallel after P0
P1-C ← requires P0-A/B/C (clean signals) ; P1-D ← requires P1-C shape decided
P2-A/B ← anytime after P0-F ; P2-C ← P1-F ; P2-E ← independent, long-lead
P3-* ← after their P1/P2 gates ; P4 ← never before its spike gate
```

## Target states (calibrated to actual repo + single-operator pace)

**After 2 weeks** (≈ P0 complete + P1-E + vintage capture on):
- No known-wrong number ships. TTM/seasonal bases everywhere; retired signals gone from flags; as-of stamps on every report; CI green publicly.
- Journal v2 lockable in <3 min; vintage snapshots accumulating from every live run.
- **Proven by then:** GLW/MXL archive reruns show artifact-free flag lists (the correction table from the season becomes empty on rerun).

**After 1 month** (+ P1-A/B/C/D):
- Provenance-complete ledger JSON beside every report; offerings + 8-K events in-pipeline with the FPS-class caveat working; composite gone; decision card is the default surface on both paths.
- **Proven by then:** season-archive ablation documents whether tiered triage discriminates where the composite didn't (this is P1-C's kill gate — a negative result reverts to no-aggregate).

**After 3 months** (+ P2, P3-A/B/C, opportunistic P3-D/E; one full earnings season used through the card):
- Extraction benchmark number published; placebo bounds enforced; PIT replay passing; reference-class percentiles v1 in card language; funding-context and insider streams live.
- **Proven by then — the ones that matter:** 8–10 locked journal cases with resolutions (the Q3 season is the supply; KTOS/AMPX-class same-day filers prioritized), the four-boolean engine↔belief↔outcome tally computed, and a written answer to "would you keep using it voluntarily?" If the tally is negative or the journal is still empty, that IS the result — the roadmap's remaining items stop pending reassessment.

## Kill criteria (program level)

1. Journal empty after another full season → product thesis falsified as a decision tool; the repo remains a validated research artifact; stop feature work.
2. Tiered triage no better than composite on archives + controls → no aggregate at all; evidence-only surface.
3. Any new detector failing its placebo bound ships evidence-plane-only or not at all — no exceptions for interestingness.
