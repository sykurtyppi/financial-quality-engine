# What This Engine Can and Cannot Do — Capstone

Date: 2026-07-04 · Status: research phase concluded · Config frozen at 0.3.0 · 237 tests

This document is the honest summary of the whole project. It states what the
engine was built to do, what six independent free experiments actually
established, and — crucially — how each of those findings matches the published
academic literature. The short version: **the engine works as a distress-triage
and disclosure-monitoring tool, and does not work as a single-company fraud or
failure predictor — and that is exactly what 25 years of accounting and finance
research predicts.** We reproduced the consensus on a fresh sample.

---

## 1. The original ambition, and the honest verdict

The engine set out to be a **forensic accounting / earnings-quality detector**:
score a company from its financials and disclosures and flag the ones with
aggressive accounting before trouble surfaces. That framing is **refuted on its
own core cases.** What survives is narrower and real:

| Capability | Verdict | Evidence (ours) |
|---|---|---|
| **Distress detection** (is this firm under financial stress now?) | **YES** | 75–83% of firms that later died flagged ≥p90 vs a **13.5%** base rate |
| **Failure / death prediction** (which distressed firms actually die?) | **NO** | distressed-survivors flag **70% ≥p90** — statistically the same as decedents' **75%** |
| **Single-firm accounting-misstatement detection** | **NO** | pure-forensic restaters: only 2/5 elevated, 1/5 accounting-driven; **MiMedx channel-stuffing missed entirely** |
| **8-block score as a ranking / predictor** | **WEAK** | tail screen hit 55.5% vs 45.7% base — a small aggregate edge, not a per-name signal |
| **Narrative: high-severity-disclosure monitor** | **YES, but contemporaneous** | 30% restaters vs **0%** clean — specific and low-FP, but 0/5 early; fires *with* the disclosure, not ahead |
| **Narrative: KPI-definition drift** | **SHELVED** | looked predictive (3/6 early) but Phase 4 + the isolation spike showed it was mostly extraction artifacts; ~1/10 genuine |

**The defensible core is two things:** a distress thermometer, and a
contemporaneous high-severity-disclosure alert. Everything else — the forensic
promise, the score as a ranking, most narrative detectors, KPI-drift — is
scaffolding or noise. See the per-experiment docs:
[survivorship_pilot](survivorship_pilot.md), [distressed_control](distressed_control.md),
[restatement_control](restatement_control.md), [restatement_narrative](restatement_narrative.md),
[clean_narrative_control](clean_narrative_control.md), [narrative_timing](narrative_timing.md),
[kpi_llm_validation](kpi_llm_validation.md), [kpi_definition_isolation_spike](kpi_definition_isolation_spike.md).

---

## 2. Why this is a success, not a failure: we reproduced the literature

Every one of our findings has a matching, respected result. We did not discover a
flaw in the field; we independently re-derived what the field already knows, on a
fresh sample, with an open and grounded pipeline.

| Our finding | The published result it reproduces |
|---|---|
| Distress is detectable from ratios | **Altman Z-score** (1968, *J. Finance*): ~95% accurate 1 year pre-bankruptcy in-sample; still the reference distress screen |
| But distressed-survivors ≈ distressed-decedents | **Campbell, Hilscher & Szilagyi** (2008, *J. Finance*), "In Search of Distress Risk"; the sub-2% failure base rate forces **precision collapse** (Shumway 2001; Chava-Jarrow 2004) — distress is common, failure conditional on distress is not |
| Single-firm fraud detection fails on precision | **Beneish M-score** (1999, *FAJ*): own base rate 0.69%, tolerates ~15% false positives only by assuming a miss is "20–40× as costly"; did **not** cleanly flag Enron on contemporaneous filings. **Dechow F-score** (2011, *CAR*): catching 2/3 of frauds needs a 36% false-positive rate; the high-confidence cutoff catches <1 in 5. **Bao RUSBoost** (2020, *JAR*): AUC 0.725 but single-firm precision ~4.5% — and **Walker (2021)** showed even that collapses to 2.5%, *below a decade-old logit*, once a data-leakage recoding is undone. Fraud models are triage screens "not adopted by auditors." |
| The 8-block score is a weak aggregate, not a per-name signal | Firm-level return R² ≈ **2%** even when the portfolio-sorted premium is highly significant — the explicit *average-vs-single-name* gap |
| The one "predictive" signal (KPI-drift) decayed / was thin | **McLean & Pontiff** (2016, *J. Finance*): predictor returns decay 26% out-of-sample, 58% post-publication. **Green, Hand & Soliman** (2011, *Mgmt. Sci.*): the Sloan accruals anomaly was **competed to zero** after ~2003. **Hou, Xue & Zhang** (2020, *RFS*): 65–82% of anomalies fail to replicate |

---

## 3. So why do economists respect these formulas at all?

Because they earned their reputation answering a **different question** than a
single-company screen asks. Three facts resolve the apparent paradox:

1. **Unit of analysis.** These models were validated for **cross-sectional
   average** power — explaining behavior across thousands of firms, where a tiny
   per-firm edge becomes overwhelmingly significant when averaged. A predictor can
   have a t-stat > 4 across the market *and* a firm-level R² of ~2%
   simultaneously. That is not a contradiction; it is the difference between
   "reliable on average" and "reliable for this one name." **This engine operated
   at the single-name level — the hardest regime, and the one the formulas were
   never sold for.**

2. **Base rates make precision brutal.** Serious misstatement is ~0.5–1% of
   firm-years; bankruptcy ~1–2%. Even a genuinely good classifier (70% recall,
   10% false-positive rate) yields **~6% precision** at those base rates — ~14
   false alarms per true case. Beneish, Dechow, and Bao all say this openly; it is
   arithmetic, not a modeling defect.

3. **Live edges get arbitraged away.** The formulas that once had tradeable power
   (Sloan accruals) were competed to zero once published. A 1996 backtest winner
   can be a 2025 coinflip with no error by anyone — the market absorbed it.

The respect is earned — for explaining averages and mechanisms. The category
error is expecting a cross-sectional-average tool to classify one company.
Essentially nobody credible claims it can.

---

## 4. Honest caveats about *our* evidence

- **Small n.** Our controls use n = 10–16 per group, so our specific percentages
  are noisy. What is robust is the **direction**, and the direction matches the
  large-sample literature in every case.
- **Discovered-fraud bias.** Like the academic models, our restatement cases are
  *caught* misstatements (8-K Item 4.02). True misstatement is under-observed, so
  detection difficulty is, if anything, understated.
- **Survivorship correction was applied** (delisted-company facts persist on
  EDGAR by CIK), which is what let the distress result avoid the usual upward bias.
- **No look-ahead.** All scoring is point-in-time (companyfacts filed-date
  filtering; document `before=` cutoffs), so the failures are not artifacts of
  hindsight.
- **What we did not test:** portfolio-scale, diversified, cross-sectional
  deployment — the one regime where a small average edge is legitimately
  exploitable. We did not test it because the project's premise was single-name
  forensics, and because that edge is already arbitraged and non-novel (Green et
  al.). There is no attractive modeling pivot hiding in our negative results.

---

## 5. What to do with this

- **Treat the modeling phase as concluded and correctly answered.** Do not build
  the HTML-table definition extractor, add detectors, change score weights, or
  position the score as a ranking. The evidence says that road ends at the
  base-rate/decay wall.
- **The two validated signals are usable as a triage/attention tool** — a distress
  thermometer plus a contemporaneous disclosure-severity alert — provided they are
  presented as *screens that route attention*, never as predictions or
  accusations. See [legal_framing.md](legal_framing.md).
- **The only open question no historical test can answer** is whether surfacing
  those two signals changes a real decision. That is a human-workflow question for
  the decision-impact journal ([evaluation_protocol.md](evaluation_protocol.md)),
  not a modeling question.

---

## 6. Bottom line

We built a tool that correctly tells us these tools do not do what people wish
they did. It detects distress (as Altman showed), cannot separate the distressed
who survive from those who fail (as Campbell et al. showed), cannot catch a
single company's accounting misstatement at usable precision (as Beneish, Dechow,
and Bao showed), and found its one "predictive" signal to be mostly decayed
artifact (as McLean-Pontiff and Green et al. would predict). Reproducing that
consensus independently, with rigor and without fooling ourselves, is the
project's real deliverable.

---

## References

- Altman, E. (1968). Financial Ratios, Discriminant Analysis and the Prediction of Corporate Bankruptcy. *Journal of Finance* 23(4). https://onlinelibrary.wiley.com/doi/10.1111/j.1540-6261.1968.tb00843.x
- Beneish, M. (1999). The Detection of Earnings Manipulation. *Financial Analysts Journal* 55(5). https://www.calctopia.com/papers/beneish1999.pdf
- Campbell, J., Hilscher, J. & Szilagyi, J. (2008). In Search of Distress Risk. *Journal of Finance* 63(6). https://scholar.harvard.edu/files/campbell/files/campbellhilscherszilagyi_jf2008.pdf
- Chava, S. & Jarrow, R. (2004). Bankruptcy Prediction with Industry Effects. *Review of Finance* 8. https://papers.ssrn.com/sol3/papers.cfm?abstract_id=287474
- Dechow, P., Ge, W., Larson, C. & Sloan, R. (2011). Predicting Material Accounting Misstatements. *Contemporary Accounting Research* 28(1). https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1911-3846.2010.01041.x
- Bao, Y., Ke, B., Li, B., Yu, Y. & Zhang, J. (2020). Detecting Accounting Fraud in Publicly Traded U.S. Firms Using a Machine Learning Approach. *Journal of Accounting Research* 58(1). https://onlinelibrary.wiley.com/doi/abs/10.1111/1475-679X.12292
- Walker, S. (2021). Critique of "Detecting Accounting Fraud … Machine Learning." *Econ Journal Watch* 18(1). https://econjwatch.org/File+download/1185/WalkerMar2021.pdf
- McLean, R. & Pontiff, J. (2016). Does Academic Research Destroy Stock Return Predictability? *Journal of Finance* 71(1). https://onlinelibrary.wiley.com/doi/abs/10.1111/jofi.12365
- Green, J., Hand, J. & Soliman, M. (2011). Going, Going, Gone? The Apparent Demise of the Accruals Anomaly. *Management Science* 57(5). https://pubsonline.informs.org/doi/abs/10.1287/mnsc.1110.1320
- Hou, K., Xue, C. & Zhang, L. (2020). Replicating Anomalies. *Review of Financial Studies* 33(5). https://academic.oup.com/rfs/article-abstract/33/5/2019/5236964
- Harvey, C., Liu, Y. & Zhu, H. (2016). … and the Cross-Section of Expected Returns. *Review of Financial Studies* 29(1). https://academic.oup.com/rfs/article-abstract/29/1/5/1843824
- Shumway, T. (2001). Forecasting Bankruptcy More Accurately: A Simple Hazard Model. *Journal of Business* 74. https://papers.ssrn.com/sol3/papers.cfm?abstract_id=171436
