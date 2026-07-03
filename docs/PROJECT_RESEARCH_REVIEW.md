# PROJECT RESEARCH REVIEW
### Internal strategy memo — Earnings Quality & Narrative Drift Engine
**Prepared by:** Research Director (acting) · **Date:** 2026-07-03 · **Classification:** Internal / candid

> This memo is written to reject the project first and defend it second. It uses
> only evidence the project has already produced. Confidence levels are attached
> to every major claim (§15). Where the honest answer is "we don't know," it says so.

---

## 1. Executive Summary

We have built a genuinely well-engineered system that answers a question the market
has largely already answered, using signals that are largely commodity, calibrated on
a sample too small and too survivorship-biased to support the claims a buyer would need.

That is the bad news, and it is most of the news.

The good news is narrower but real: the **methodological discipline** (point-in-time
backtesting done correctly, an enforceable no-hallucination contract, honest caveats
everywhere) is rare, and the one empirical result that matters most — the false-negative
miss test — is quietly encouraging (4 of 5 testable known blowups elevated *before* the
event). But that is a coverage-and-rigor edge, not an insight edge. On any single name,
this engine tells a competent analyst almost nothing they could not find themselves.

**The central strategic finding:** the engine's value is **coverage × consistency ×
auditability**, not alpha. It is a *triage instrument*, not a *decision instrument*.
That reframing changes everything downstream — the user, the product, the roadmap, and
whether commercialization is even the right goal (it probably is not).

**Headline recommendations (detail in §7, §12–14):**
1. **Do not commercialize this as a forensic-accounting product.** The category is
   occupied (Transparently.AI, StarMine, Hudson Labs), the legal exposure is real, and
   we cannot earn institutional trust as a solo effort on this calibration. *(High confidence)*
2. **Reframe identity from "forensic platform" to "earnings-quality triage copilot."**
   Defensive, honest, analyst-facing — and legally survivable. *(Medium-high confidence)*
3. **Spend the next six months on evidence, not features** — chiefly the delisting-inclusive
   backtest and the decision-impact journal. Stop adding detectors and formulas. *(High confidence)*
4. **Treat the highest-value outcome as an internal research tool + a top-decile portfolio
   artifact**, and be at peace with that. It is worth more there than as a failed SaaS. *(Medium-high confidence)*

---

## 2. What We Have Built

A deterministic-first earnings-quality engine with, in dependency order:

- **Ingestion** — dependency-free SEC companyfacts client + XBRL mapper, validated *to the
  dollar* against filed annuals (AAPL/MSFT/KO). Real, hard, correct work.
- **Formula engine** — ~40 metrics: accruals, Beneish family, working-capital quality,
  balance-sheet stress, capital structure/dilution, capex regime.
- **Scoring** — 8 blocks + overall, 0–100 concern convention, exposed weights/anchors/
  coverage/confidence, false-positive controls (high-growth caveat, financial-institution
  exclusion).
- **Narrative layer (v0.4)** — document ingestion (MD&A, risk factors, earnings releases),
  detectors for adjustment recurrence, KPI drift/definition change, guidance shift,
  defensive tone, risk-factor expansion, high-severity term emergence.
- **Evidence ledger + grounding validator** — every claim traces to an excerpt; a validator
  that *rejects* LLM output citing unknown evidence, banned vocabulary, or ungrounded numbers.
- **Calibration + backtesting** — point-in-time walk-forward over 75 companies / 1,275
  company-quarters, plus a false-negative miss test and a 350-company wide sweep.
- **Reporting** — analyst-grade markdown with a strict legal-framing posture.

This is roughly 18 months of a competent quant-dev's output compressed. The engineering
quality is not in question. Its *research value* is.

---

## 3. What Actually Works

Ranked by strength of evidence.

1. **Ingestion correctness. (High confidence.)** Reconciles to filed totals; handles the
   real XBRL traps (YTD differencing, Q4 derivation, tag switches, fiscal-label bugs). This
   is the least glamorous and most trustworthy part of the system.
2. **The false-negative miss test. (Medium confidence — small sample.)** 4 of 5 testable
   known blowups scored ≥p80/p90 *before* the event (KHC 52, UAA 56 with Revenue Quality 75,
   PLUG 47, SMCI 44 + the stale-filing detector firing on the real delinquency). Crucially,
   the hits were driven by the exact blocks the independent calibration flagged as trustworthy
   (cash conversion, accrual/revenue quality). That internal consistency is more persuasive
   than the raw hit count.
3. **Out-of-sample score stability. (Medium-high confidence.)** The 350-name sweep's
   distribution matched the calibration distribution almost exactly (p50 33.8 vs 31.7, p90
   45.8 vs 45.1). The scoring is not overfit to the calibration set.
4. **The accrual → forward-margin-deterioration signal. (Medium confidence.)** IC −0.20 to
   −0.25, point-in-time. This is the classic Sloan result reproduced honestly. It is real,
   it is also 30 years old and public.
5. **The evidence/grounding discipline. (High confidence it works; low confidence it's an
   edge.)** The no-hallucination validator does what it claims. Whether anyone pays for that
   is a different question (§4).

---

## 4. What Probably Doesn't

1. **The overall score as a *ranking*.** The quintile table is damning if read honestly:
   Q1–Q4 forward returns are non-monotone (+4.6%, +4.6%, +9.5%, +10.2% mean); only Q5 breaks
   negative (−5.3% mean, −14.5% median). **The score is a tail alarm, not a ranking.** Below
   the top quintile it carries little information. *(High confidence.)*
2. **The 44.5% false-positive rate.** At the flag threshold, nearly half of flags precede
   benign outcomes. This is *fine* for a triage tool and *fatal* for anything presented as a
   verdict. *(High confidence.)*
3. **Novelty of the signals.** Every formula is textbook; the deep-research phase confirmed
   ML-on-raw-fundamentals now beats ratio screens (Bao et al. 2020) and that classic screens
   carry brutal base-rate FP problems (F-score ~153:1). We are not at the research frontier;
   we are at the 1999–2011 frontier, well implemented. *(High confidence.)*
4. **The Narrative Drift block's scoring.** It is 100% uncalibrated (no historical document
   corpus existed for the backtest) yet ships inside the overall score at 10% weight. We are
   scoring on faith there. The *detectors* may be useful; the *weight* is unearned. *(High confidence.)*
5. **Lexicon-based narrative detection as a durable moat.** LLMs already do fuzzy paraphrase-
   robust versions of tone/guidance/definition-drift. Our deterministic versions are more
   auditable but less capable, and the capability gap widens every quarter. *(Medium confidence.)*

---

## 5. Where Our Edge Exists

Be conservative here; most claimed edges evaporate on inspection.

**Subsystem moat assessment:**

| Subsystem | Verdict | Reasoning |
|---|---|---|
| Formula engine | **Commodity** | Textbook; EdgarTools + a weekend reproduces it. |
| Ingestion/mapper | **Useful, not a moat** | Correct and hard, but SEC data is free to everyone; not proprietary. |
| Narrative detectors | **Useful, decaying** | Moderately differentiated at prosumer tier; LLMs erode it. |
| Scoring/weights | **Commodity + liability** | Weights are thin-sample; the *transparency* of them is the only distinctive part. |
| **Evidence ledger + grounding validator** | **Differentiated (trust, not insight)** | Genuinely uncommon discipline. It is a *compliance/auditability* edge, valuable to regulated buyers — but it enables trust, it does not produce insight. |
| **Calibration + PIT backtesting framework** | **Potential moat (methodological)** | The rigor is the most credible asset in the repo. But it is a *process* edge; the *findings* are thin. Framework > findings. |
| Report generation | **Commodity** | Templated markdown. |
| Sector normalization | **Does not exist yet; the sweep proved it's needed** | The hook is empty. |

**The honest synthesis of the moat question:** there is no *insight* moat. The candidate
moats are both about *trust and process* — the grounding/evidence discipline and the
calibration rigor. Those are real and rare, but they are the kind of edge that wins a
procurement review or a job interview, not the kind that finds alpha a good analyst missed.

---

## 6. Where We Are Fooling Ourselves

1. **The miss test is a hand-picked sample of famous names with no base rate.** 4/5 feels
   great; it is 6 cases the author already knew were blowups. We have not run the
   complementary test: how often do *clean* companies also score ≥p80? (The sweep hints:
   13.5% of a large-cap universe flags — so "elevated" is not rare, which deflates the
   miss-test hit rate.) *(High confidence this is a real bias.)*
2. **Survivorship cuts against us in the direction that flatters us.** The one blowup that
   actually delisted (Tupperware) was *untestable*. Our "wins" are survivors that stumbled
   but lived. The frauds that vaporized — the cases that would most validate the engine — are
   invisible. We are grading ourselves on the easy half of the exam. *(High confidence.)*
3. **We conflate "the engine flagged it" with "the engine would have changed the decision."**
   UAA's receivables and channel-stuffing chatter were public in 2016; KHC's stretched brands
   and 3G cost-cutting were consensus. The engine *confirmed and systematized* known concerns.
   That is useful. It is not the same as revealing something the market didn't have. *(Medium-high confidence — see §… decision impact below.)*
4. **The grounding validator guards a model that doesn't exist.** It is elegant, tested, and
   currently protecting against hallucinations from an LLM annotator we have not built. It is
   a promise we've engineered, not a capability we've shipped. *(High confidence.)*
5. **"Deterministic-first" is a virtue we may be over-charging for.** It buys auditability
   and legal safety. It also caps us permanently below what an LLM-native competitor can do on
   the fuzzy tasks (paraphrase, context, sector nuance). We have chosen the defensible-but-
   ceilinged path and should be honest that it is a ceiling. *(Medium confidence.)*

---

## 7. Recommended Product Direction

**Reject the "forensic accounting platform" identity.** It is (a) occupied by
Transparently.AI at the institutional tier with 85,000 companies and a GenAI assistant, (b)
accusatory by nature and therefore legally exposed, and (c) exactly the identity our own
calibration cannot support (we cannot predict fraud/restatements; we tested and got zero
events).

**Adopt: "earnings-quality triage copilot for the fundamental analyst."**

Why this identity:
- It matches what the engine *actually does well*: systematize the boring checks across many
  names, produce an evidence-backed first-pass brief, and hand a triaged watchlist to a human.
- It is **defensive, not accusatory** — "these five items require review," never "this
  company is manipulating." This is legally survivable (the framing is already built).
- It is honest about the 44.5% FP rate: a copilot that surfaces candidates for a human to
  reject is *supposed* to over-surface. The FP rate that kills a "verdict" product is
  acceptable, even expected, in a triage product.
- It positions against the real gap the deep research found: nobody serves the
  *auditable, evidence-first, analyst-tier* rung well.

The rejected alternatives, briefly: *swing-trading platform* (the signal is quarterly and
fundamental, wrong time horizon — reject); *event-driven engine* (we can't predict events —
reject); *institutional due-diligence assistant* (right shape, wrong seller — no solo dev
earns that trust yet); *pure retail product* (retail wants ideas, not risk flags, and churns
— reject). The copilot framing is the only one the evidence supports. *(Medium-high confidence.)*

---

## 8. Recommended Research Direction

The binding constraint is **evidence about decision impact and generalization**, not more
signals. In priority order:

1. **Kill survivorship bias.** Acquire delisting-inclusive fundamentals (Sharadar or similar,
   indie-affordable) and re-run the *unchanged* v0.3 harness. This single step converts
   "directional evidence on survivors" into "real evidence including failures." It is the
   highest-value research action available and the one thing that could move the project from
   "interesting" to "credible." *(High confidence this is #1.)*
2. **Run the complementary base-rate test.** For the miss-test to mean anything, quantify the
   false-alarm rate on a matched clean sample. Without it, 4/5 is a story, not a result.
3. **Calibrate the narrative block or stop scoring it.** Build the historical document corpus
   (EDGAR is retroactively fetchable) and either earn the 10% weight or set it to zero and
   keep narrative as *evidence-only* until proven.
4. **Answer the decision-impact question empirically** via the journal (§ below). This is
   research, not engineering, and it is free.

Explicitly *not* a research priority: more detector families, more ratios, sector-specific
formula variants. We have more machinery than validated signal already.

---

## 9. Recommended Engineering Direction

Six months, ruthlessly ranked. Most of the list is "don't."

- **HIGH ROI:** delisting-inclusive backtest wiring (data + one harness rerun); sector
  normalization *driven by the adjudication labels* (kills the measured utility/defense FPs);
  the report/journal tooling (already done — good).
- **MEDIUM ROI:** the LLM annotator (the grounding contract is the hard part and it's built —
  this is now mostly wiring); IFRS/foreign-issuer mapping (63 of the top 350 don't parse —
  material coverage gap, but only if breadth becomes the goal).
- **LOW ROI:** more narrative detectors; more formulas; web UI; API hardening; real-time.
- **NEGATIVE ROI:** SaaS infrastructure, auth, billing, multi-tenant, alerting, dashboards.
  Every hour here is an hour betting the answer to "does this change decisions" is already
  yes. It isn't yet. *(High confidence on the full ranking.)*

The engineering recommendation in one sentence: **freeze feature development, spend the
budget on the two evidence items, and let the journal decide the rest.**

---

## 10. Risks

1. **Legal (structural).** Per-company quality scores are the defamation/trade-libel fact
   pattern; a solo operator has no legal budget. Mitigated *only* by the defensive framing,
   which must never slip. *Any* commercial publishing raises this from theoretical to live. *(High confidence, high severity.)*
2. **The category is occupied and better-funded.** Transparently.AI, StarMine, Hudson Labs.
   We enter as the smallest, least-trusted, least-distributed participant. *(High confidence.)*
3. **Calibration fragility.** n≈70, one rate cycle (2021–2025), survivorship-biased, zero
   restatement events. Any claim beyond "top-quintile tail screen on survivors" is unsupported
   and, if made publicly, is a reputational and legal liability. *(High confidence.)*
4. **Deterministic ceiling.** LLM-native competitors will out-nuance the narrative layer; our
   auditability advantage may not outweigh their capability advantage for most buyers. *(Medium confidence.)*
5. **Founder-signal trap.** The temptation to build product surface (it *feels* like progress)
   before the research question is answered. This is the failure mode that kills projects at
   exactly this stage. *(High confidence it's the primary internal risk.)*

---

## 11. Opportunities

1. **Highest-realized-value, near-zero-risk: the internal tool.** You get an evidence-backed
   pre-read on any name in one command. That value is *already realized* and compounds with use.
2. **The credibility artifact.** The PIT backtesting, the grounding validator, and the
   relentless honesty of the docs demonstrate senior quant-research judgment better than most
   shipped production systems. As a portfolio/hiring asset this is top-decile *because of* the
   candor, not despite it.
3. **The narrow commercial wedge (if pursued at all):** auditable, evidence-first triage for
   independent analysts and small research shops — the one rung the incumbents underserve. Small
   TAM, but real, and legally survivable in copilot framing.
4. **A research-publication path.** "Point-in-time replication of accrual-quality signals with
   an enforceable LLM-grounding protocol" is a credible write-up. Publishing the *method and the
   honest negative results* is itself reputationally valuable and costs nothing to monetize.

---

## 12. What We Should Stop Building

- **Stop adding detectors, formulas, and narrative families.** *(High confidence.)*
- **Stop treating the overall score as if it ranks.** Present it as a tail alarm or not at all.
- **Stop weighting the uncalibrated narrative block in the overall score** until it is
  calibrated (set to evidence-only).
- **Do not build any SaaS surface** — auth, billing, dashboards, alerts, multi-user.
- **Do not pursue the institutional/hedge-fund buyer.** They have Bloomberg + analysts, won't
  buy unaudited scores from a solo dev, and procurement + trust kill it before calibration does.

## 13. What We Should Double Down On

- **The delisting-inclusive backtest.** The one thing that could change the project's status.
- **The decision-impact journal.** The only test of the only question that matters.
- **Sector normalization from real adjudication labels.** Turns the measured FPs into fixes.
- **The evidence/grounding discipline** — it is the genuine differentiator; make it the
  headline of any external framing (portfolio, publication, or product).
- **Radical honesty in the docs.** It is, counterintuitively, the most valuable asset. Protect it.

---

## 14. If You Were CEO, What Would You Do Next?

I would stop pretending the near-term goal is a company, and run the project as a **six-month
research verdict** with a pre-committed decision rule.

Concretely:
1. **Keep the config frozen.** Run the two evidence items: delisting-inclusive rerun + the
   base-rate complement to the miss test.
2. **Dogfood ruthlessly.** 15–20 names you actually follow, prior-view-first, over the window.
   The journal is the referee.
3. **At window close, apply a pre-committed rule:**
   - *If* the delisting-inclusive backtest holds up *and* the journal shows the engine changed
     or materially sharpened ≥3 decisions → invest in the copilot wedge (narrow, analyst-tier,
     copilot-framed) and the LLM annotator.
   - *If not* → declare it a successful internal tool + portfolio/publication artifact, freeze
     feature work, and move on. This is not failure; it is the honest, high-value outcome, and
     it is the *base case*.
4. **Either way, write up the method and the negative results.** The rigor is the product even
   if the product isn't.

The one thing I would *not* do is keep building. The engine is past the point where more
features raise its expected value; from here, only evidence does. *(Medium-high confidence in
the decision rule; high confidence in "stop building.")*

---

## 15. Confidence Levels — Consolidated

| Claim | Confidence |
|---|---|
| The overall score is a tail alarm, not a ranking | **High** |
| The signals are commodity / not at the research frontier | **High** |
| The category is occupied; institutional commercialization is unrealistic solo | **High** |
| Legal exposure is real and gates any per-company publishing | **High** |
| Survivorship bias flatters our results; miss test lacks a base rate | **High** |
| Stop building features; spend the budget on evidence | **High** |
| Ingestion is correct and trustworthy | **High** |
| The grounding/evidence discipline is the genuine differentiator (trust, not insight) | **High** |
| The miss-test signal is real but small | **Medium** |
| Accrual → margin-deterioration signal is real | **Medium** |
| Reframe to "triage copilot" is the right product identity | **Medium-high** |
| Highest-value outcome is internal tool + portfolio/publication artifact | **Medium-high** |
| The narrow analyst-tier copilot wedge is commercially viable *if* evidence holds | **Low-medium** |
| Delisting-inclusive rerun will materially improve the evidence | **Unknown — that is the point of running it** |

---

### One-paragraph verdict

This project does **not** currently deserve to exist as a serious *commercial* research
product: the edge is coverage-and-audit, not insight; the category is taken; the calibration
is too thin and too survivorship-biased to earn the trust the pitch would require; and the
legal surface is unforgiving for a solo operator. It **does** deserve to exist as a serious
*internal research instrument and a top-decile credibility artifact* — and, contingent on one
honest experiment (the delisting-inclusive backtest) and one honest diary (the decision-impact
journal), it *may* deserve a narrow, defensively-framed analyst-copilot wedge. The correct next
move is not to build more. It is to run those two tests and let them decide.
