# Gap-Driven Research Pass — 2026-08 (post-audit)

Status: complete · Scope: five gaps exposed by the repository audit, NOT covered by the six 2026Q3 surveys. Evidence strength tagged per finding. This memo governs the triage-layer design in [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md) §7 and several roadmap items.

---

## Gap 1 — How should a multi-signal triage score aggregate?

**Governing negative result:** no validated forensic/distress score in the literature aggregates by weighted-averaging dozens of bounded subscores. The survivors do one of three things: count binary extremes, fit a sparse model on ≤9 non-redundant variables, or use tail-sensitive aggregation. Our measured 31–58 compression is the expected output of the averaging design, not a tuning failure.

- **ESG composite natural experiment** (peer-reviewed): Berg–Kölbel–Rigobon, "Aggregate Confusion," *Review of Finance* 2022 — weight-averaged composites of many correlated soft indicators diverge wildly across vendors (pairwise r 0.38–0.71 vs 0.99 for credit ratings); 56% of divergence is measurement, only 6% weights. The weighting layer is nearly irrelevant; the composite destroys discrimination. https://academic.oup.com/rof/article/26/6/1315/6590670
- **Dechow F-score** (peer-reviewed, replicated): 7-variable logit → probability ÷ unconditional base rate (0.0037) → relative risk ("2.45× background"). The base-rate division IS the calibration step. https://www.researchgate.net/publication/228238563_Predicting_Material_Accounting_Misstatements
- **Piotroski F-score** (peer-reviewed; internationally replicated — Hanauer et al., *J. Asset Mgmt* 2020, ~10%/yr spread ex-US 2000–2018 https://link.springer.com/article/10.1057/s41260-020-00157-2): count of 9 binary passes. Negative result flagged: the US *return* anomaly decayed post-publication (practitioner backtest: https://blog.portfolio123.com/why-piotroskis-f-score-no-longer-works/); the fundamental discrimination did not.
- **Outlier ensembles** (peer-reviewed, replicated): plain averaging buries the one screaming detector; plain max is unstable; **average within correlated clusters, max across clusters (AOM)** beats both. Aggarwal, SIGKDD Expl. 2013 https://dl.acm.org/doi/pdf/10.1145/2481244.2481252 ; Zhao 2019 https://arxiv.org/pdf/1911.10418
- **Conformal anomaly detection** (2024–2026, peer-reviewed line): converts any raw score into a distribution-free cross-sectional p-value via a calibration set; multiplicity-corrected min-p handles "35 looks." https://arxiv.org/abs/2606.13780 ; https://arxiv.org/pdf/2605.13642 ; package: https://github.com/OliverHennhoefer/nonconform
- **Isotonic vs Platt**: isotonic overfits small samples; Platt safer at tens-to-hundreds of labels. https://www.sciencedirect.com/science/article/abs/pii/S0306437920301083

**Adopted design (architecture §7):** tiered red-flag counts (Piotroski form) + Ohlson-style regime dummies now; within-cluster-average/across-cluster-max structure for the thermometer; conformal cross-sectional p-values once the reference-class store exists; Dechow-style relative-risk language only where a survey-verified base rate exists. Weighted averaging survives only as a tie-breaker within a flag-count band.

## Gap 2 — Missingness-as-signal (the guard-renormalization inversion)

- **Ohlson O-score (1980, 45 years of use)** encodes it directly: OENEG (liabilities>assets) and INTWO (NI<0 both of last two years) are dummy *predictors with their own coefficients* — "the ratio regime has broken" as first-class signal. https://en.wikipedia.org/wiki/Ohlson_O-score
- **Classic distress models never divide by earnings.** Altman: all denominators are total assets/liabilities; negative EBIT/RE/WC numerators ARE the signal. CHS 2008: NIMTA (NI over market value of total assets), winsorized. https://scholar.harvard.edu/files/campbell/files/campbellhilscherszilagyi_jf2008.pdf
- **Missing-indicator method** (peer-reviewed, replicated in clinical + credit): a binary "non-computable" predictor improves AUROC when missingness is informative, harmless when not. https://www.ncbi.nlm.nih.gov/pmc/articles/PMC11924964/ ; https://www.nature.com/articles/s41598-025-99997-4
- **Prevalence:** negative-EBITDA firms rose 10.8% → 28.0% (1980→2019) — guards blind us to a quarter of the universe, concentrated in the distress tail. https://arxiv.org/pdf/2605.02680

**Adopted:** `RATIO_REGIME_BROKEN` dummies (NI<0, NI<0 ×2 consecutive, EBITDA<0, equity<0) that ADD concern; re-denominate refusal-prone ratios to assets-scaled forms; never renormalize refused weight away.

## Gap 3 — Semantic filing-change detection, state of the art

- **Outcome-validated (peer-reviewed):** Lazy Prices (JF 2020) — *simple cosine/Jaccard similarity* on YoY filings; changes predict earnings, news, bankruptcies; concentrated in MD&A. https://onlinelibrary.wiley.com/doi/abs/10.1111/jofi.12885 · Doc2Vec 10-K similarity → crash risk https://afajof.org/management/viewp.php?n=56560 · Kim–Muhn–Nikolaev "Bloated Disclosures" (validated bloat measure) https://arxiv.org/abs/2306.10224 and GPT call-risk measures https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4593660
- **Honest read:** no modern embedding approach has demonstrably beaten plain cosine similarity on outcome-validated filing-change detection in a replicated study. LLM value is validated for summarizing/measuring, not for change detection.
- **Hallucination:** <1% on clean summarization but financial benchmarks (PHANTOM, FAITH 2025; FinGround 2026) document fabricated metrics and miscalculated derived quantities; citation generation hallucinates 14–95% across models. https://arxiv.org/html/2604.23588 ; https://arxiv.org/pdf/2604.03159
- Working papers (single, unreplicated — promising only): FinBERT MD&A embeddings https://arxiv.org/abs/2606.29290 ; 10-K novelty https://arxiv.org/pdf/2309.05560

**Adopted:** deterministic per-section YoY cosine similarity (pure Lazy Prices) as the primary change detector; set-diff + specificity for Risk Factors per prior survey; LLM diff-summarization only as presentation with enforced verbatim citations (existing grounding.py contract).

## Gap 4 — Guidance reliability and channel inconsistency

- **Guidance-accuracy persistence is real and market-validated** (peer-reviewed, replicated): Hutton–Stocken https://papers.ssrn.com/sol3/papers.cfm?abstract_id=817108 ; Ng et al. RAST 2013 https://link.springer.com/article/10.1007/s11142-012-9217-4 ; EAR 2024 (accuracy dominates consistency) https://www.tandfonline.com/doi/full/10.1080/09638180.2024.2413001 ; review: Preussner–Aerts 2022 https://onlinelibrary.wiley.com/doi/full/10.1111/1911-3838.12294
- **Larcker–Zakolyukina deception cues: DO NOT BUILD.** Out-of-sample only 6–16% better than random, ≈ accounting-variable models, no successful independent replication found. https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1475-679X.2012.00450.x
- **Tone:** *change* in MD&A tone has incremental information (Feldman RAST 2010 https://link.springer.com/article/10.1007/s11142-009-9111-x); level does not justify a detector.
- **Channel inconsistency is deterministically measurable:** Calcbench documents earnings-release vs 10-K/10-Q numeric divergences (e.g., ABC NI −12% between ER and 10-K) and ER→10-Q lag tracking. https://www.calcbench.com/blog/post/182192835328/differences-in-earnings-releases-and-10-ks · Feasibility precedent: Marvin Labs guidance tracking https://www.marvin-labs.com/blog/new-feature-guidance-tracking/

**Adopted:** per-firm guidance-error history (8-K EX-99 guidance vs realized XBRL actuals); ER-vs-10-Q shared-line-item numeric diff; ER→10-Q lag trend. Skip deception cues entirely.

## Gap 5 — Competitive landscape 2025–2026 (facts)

- **Hudson Labs = Bedrock AI** (rebranded 2023): 1–100 forensic score from narrative red flags only; >70 ≈ 1-in-3 SEC-enforcement-in-3y (self-reported); 2025 adds co-analyst agents; every claim source-linked. https://www.hudson-labs.com/blog/updated-hudson-labs-forensic-risk-score
- **Transparently.AI**: 0–100 manipulation risk, 85k companies; ">90% of collapses 3y ahead" is a marketing claim with no independent validation found. https://www.transparently.ai/what-we-do
- **Calcbench**: as-filed traceability, redlines, ER-vs-filing diffs, API — data, no signal layer. **AlphaSense**: search/summarization at $12k–100k+; Gartner 2025 review flags stale/erroneous financials.
- **The seam:** every scored competitor is black-box ML at institutional pricing; the transparent player has no signals. A legible flag-count engine with complete provenance on free EDGAR data occupies space none of them serve. Source-linking (Hudson Labs' pattern) is the credibility differentiator — not score sophistication.
