# INVESTMENT COMMITTEE REVIEW
### Adoption decision: Earnings Quality & Narrative Drift Engine
**Date:** 2026-07-03 · **Classification:** Internal / IC-confidential
**Question before the committee:** *Would our firm adopt this?*

> Six members reviewed independently before debate. We did not seek consensus and
> did not reach it. The disagreements below are real and are the most useful part of
> this document. Software is treated as finished; this is a judgment, not a code review.

---

## Executive Summary

The committee is **split, with a center of gravity at "Proceed with modifications — as an
internal instrument, scoped tightly to proving decision impact, not as a product."**

The project is better-engineered and far more intellectually honest than the median internal
tool we review. It also solves a **smaller problem than it was built to solve.** It does not
detect fraud, does not produce alpha, and does not rank companies — its own calibration says
so. What it genuinely does today: **collide management's narrative against the company's cash,
auditably, on every name the morning it reports, and surface the handful where the story and
the money disagree.** That is real, it is unserved elsewhere at earnings speed, and it is a
*coverage-and-attention* tool, not a *decision* tool.

Whether it improves returns is **unproven** — the one experiment that would settle it (a
blind, live decision-impact journal) has not run. Every "yes" vote below is conditional on
running it. The Risk Manager's dissent — that a tool this persuasive and this uncalibrated is
dangerous precisely because analysts will over-trust it — is not resolved and should not be
papered over.

**Vote tally:** Strongly Support ×1 · Proceed ×1 · Proceed w/ Modifications ×3 · Reject-as-is
(conditional Proceed) ×1. No unconditional rejections; no unconditional approvals beyond the
technologist's, which is scoped to "internal tool."

---

## Individual Reviews

### Chief Investment Officer — *does this improve returns, or just create interesting analysis?*

Today: **interesting analysis.** I have no evidence it improves returns, and I am paid to be
ruthless about that distinction. The miss test (4 of 5 known blowups elevated pre-event) is
suggestive but it is six hand-picked names with no base rate, and the wide sweep tells me
"elevated" fires on 13.5% of large-caps — so the pre-event flags sit in a large field of
false alarms. The accrual-to-margin signal is real and also thirty years old and public;
McLean-Pontiff says signals like it decay ~35% once published. **Nothing here is edge the
market doesn't have.**

But I don't dismiss it. The plausible return path is not *signal*, it's *attention* — a PM
who gets blindsided less often, and who extends coverage into names they'd never model,
compounds fewer unforced errors. That is a real if unglamorous source of returns. I will not
believe it until the decision-impact journal shows it on live decisions, blind. **Vote:
Proceed with modifications** — fund nothing but the proof.

### Head of Fundamental Research — *would analysts use it, or revert to Bloomberg and Excel?*

On their **covered** names: they revert. My analysts know their fifteen names' cash-conversion
history cold; this tool tells them nothing they don't have, and analysts trust their own models
over any black box. That's not a criticism of the tool, it's how good analysts work.

On the **long tail** — the names that print into our universe that nobody models — they'd use
it, because the alternative is *nothing*, and today "nothing" is what those names get until they
surprise us. The narrative-vs-economics collision specifically is a check my analysts *should*
do and mostly *don't*, because it's tedious. If it arrives as a two-line contradiction with the
excerpt attached, they'll read it. If it arrives as an eleven-section report, they won't — and
the current output is the report. **Partial adoption, contingent on the experience redesign the
PM reviews already specified.** **Vote: Proceed with modifications.**

### Senior Event-Driven PM — *sizing? priority? mistakes? nothing?*

- **Sizing: no, and it must never.** Wrong horizon (quarterly/12-month vs my minutes-to-weeks),
  wrong precision (44.5% false positives). Anyone who sizes off this loses money.
- **Research priority: yes.** This is its real job — of 18 prints, tell me the 2 to scrutinize.
  That's worth a desk slot on its own.
- **Mistakes: probably reduces them,** narrowly — the quality-of-beat gut check ("did the beat
  come with cash?") in the window where I'm deciding fade-or-chase. That's the best moment the
  tool has.
- **The disqualifying gap:** it knows the *quality* of the number but not whether it *beat* — no
  consensus, no guide, no implied move. Event-driven P&L is surprise-vs-expectations; this gives
  me half the picture. I'd take it anyway, as a router, never as a trigger. **Vote: Proceed** (with
  the card-not-report redesign as a hard condition).

### Risk Manager — *how does this increase risk?* **(the dissent — read carefully)**

This tool is dangerous in exactly the way persuasive-but-uncalibrated tools always are: **it
will be trusted more than it has earned.**

1. **The "clean" verdict is the real liability, not the flags.** A flag that's wrong wastes an
   analyst's hour. A *green light* on a name that then blows up is a career event — and we know
   the blind spots: GE-style insurance-reserve risk, financial arms, and low-coverage sectors
   (63 of the top 350 don't even parse). Absence of a flag is being read as safety, and it is
   not. The current product's "Clean:" line is the most dangerous sentence in it.
2. **Survivorship bias flatters everything.** The one blowup that actually delisted was
   untestable. We are grading on the easy half of the exam and the analysts won't remember that
   when the score reassures them.
3. **The uncalibrated narrative block ships at 10% of the score with the same visual authority
   as the calibrated blocks.** That is a misrepresentation of confidence.
4. **Legal:** any external publishing of per-company scores is a defamation fact pattern with no
   legal budget behind it.

I will not approve this as a scored product. I will approve a *contradiction-surfacing research
aid* that shows no score, never says "clean," and never leaves the building. **Vote: Reject as-is;
conditional Proceed only with those guardrails.**

### Director of Research Technology — *engineering quality only; would it survive production?*

This is the easy review and the most positive. Ingestion reconciles to filed totals **to the
dollar**; point-in-time backtesting is done *correctly* (filed-date filtering — most shops get
this wrong); the grounding validator is a genuinely uncommon discipline; test coverage and
documentation exceed most of what we run in production. It would pass our internal review as an
internal tool without hesitation.

Two caveats: it depends on free public feeds (EDGAR, Yahoo) that can break and have no SLA, and
it has no proprietary data — anything here, a competitor can rebuild. So it survives production
*technically* but has no *technical moat.* **Vote: Strongly support — as an internal tool.** I
note that the best-engineered part of this project is also the least strategically decisive, which
should tell the committee something.

### Partner (research budget) — *fund another six months? stop? pivot? integrate?*

The base-case outcome — an excellent internal tool and a top-decile credibility artifact — is
**already achieved.** The question is whether more money buys more than that. My answer: fund
**six months, narrowly, tied to one gate** — does it change live decisions? If yes, we have
something; if no, we harvest what we have and stop. I will not fund open-ended feature work; the
project already has more machinery than validated value. I would also explore **integrating the
contradiction feed into our existing research stack** rather than running it standalone — the
value is one feature, not a platform. **Vote: Proceed with modifications** — scoped, gated, and
biased toward integration over standalone product.

---

## Committee Debate (disagreements documented, not resolved)

**On whether it improves returns.** The PM and the Partner see a real if modest path (fewer
blindsides, more coverage). The CIO won't grant it without the journal. The Risk Manager argues
the *downside* (overconfidence on green lights) could *outweigh* the upside of the flags, and
nobody fully rebutted him — this is the sharpest unresolved split. **Recorded disagreement: is
the expected decision-impact positive or negative? We genuinely do not know, and that is the
whole problem.**

**On the score.** Five of six want the overall score demoted or hidden (echoing the prior
strategic reviews). The Head of Research alone wants to keep a coarse tag for sorting. Consensus
that a *continuous* score is a misrepresentation; disagreement on whether *any* number survives.

**On the "clean" verdict.** The Risk Manager wants it deleted outright. The PM wants it kept
because "what's clean" is useful triage information (it tells him where *not* to spend time).
Unresolved. Compromise floated but not adopted: keep "clean," change the language from a verdict
to a scope statement ("checks that passed: …") so it never reads as an all-clear.

**On identity.** Unanimous that this is *not* a commercial SaaS and *not* a competitor to
Transparently.AI. Split on internal-tool vs open-source-research-platform as the primary
identity (see ranking below).

---

## Strengths

- The narrative-vs-economics collision — a check nobody does systematically at earnings speed.
- Methodological honesty: PIT backtesting, adversarial calibration, caveats everywhere.
- The evidence/grounding discipline: every claim defensible to the line item.
- Coverage extension into names no analyst would manually work up.
- Ingestion correctness and documentation quality.

## Weaknesses

- No proven decision impact. No return evidence.
- Commodity signals; occupied category; no data or technical moat.
- Overall score barely ranks below the top quintile; 44.5% false-positive rate.
- Narrative block uncalibrated but presented with full authority.
- Missing the one input event-driven money needs: expectations/surprise context.

## Unknowns

- Does using it beat not using it on live decisions? *(The central unknown.)*
- Would the results survive a delisting-inclusive, survivorship-corrected backtest?
- What is the false-alarm base rate that the miss test lacks?
- Does the narrative layer add signal, or noise dressed as signal?

## Risks

- **Overconfidence on green lights** (Risk Manager's primary concern).
- Legal exposure on any external per-company publishing.
- Dependence on unSLA'd free data feeds.
- Founder/sunk-cost bias toward building rather than proving.

## Opportunities

- A narrow, defensible internal edge in coverage and attention allocation.
- A publishable methodology (honest negative results included) and a top-decile hiring artifact.
- Integration as a feature into a larger research workflow rather than a standalone product.

---

## Hard Questions — answered directly

**What problem does it actually solve today (not what we hoped)?**
*"I cannot manually read the cash-flow-versus-narrative quality of hundreds of filings each
quarter, and I get blindsided by names I don't cover."* It solves the tedious, universal
cross-check of *management's story against the cash*, consistently, at scale. Nothing more, and
that is enough to be real.

**Who benefits most?** The breadth-constrained: generalist/multi-sector analysts and small teams
covering more names than they can model, and the independent PM. Coverage they can't otherwise
afford.

**Who benefits least?** The single-name specialist who knows fifteen names cold (near-zero
marginal information), and the quant fund (wants raw signals, builds its own). Large,
Bloomberg-and-analyst-rich shops benefit least at the margin.

**What is genuinely excellent?** The narrative-vs-economics collision *as a concept*, the
methodological honesty, and the evidence discipline. And, unusually, the candor of the
documentation itself.

**What is merely impressive engineering?** The 40-metric formula engine, the Beneish
implementation, the 8-block scoring machine. Beautiful craft, commodity value.

**Delete 80% — what survives?** The contradiction detector, its evidence chain, and the
ingestion that feeds it. The scoring, most formulas, the report generator, and the API could all
go and the core value would remain intact. *This tells us where the project actually lives.*

**If Bloomberg launched an identical feature tomorrow, would anyone use ours?** At scale, inside
a large institution — mostly no; they'd take Bloomberg's for integration and trust. Ours survives
only where Bloomberg won't go: independent and small shops, open-source users, and the personal
tool. The honest version: our differentiator (opinionated posture + universe-scale collision) is
*real but not distribution-proof.* We win niches, not the market.

**What research would convince us it deserves to be a real product?** Three things, together:
(1) a delisting-inclusive backtest that holds up; (2) a base-rate-corrected miss test; (3) a
blind, live decision-impact journal showing the engine-influenced decisions outperform the
ignored ones. Absent all three, it stays an internal tool.

**Identity ranking (committee):**
1. **Internal / personal research tool** — highest realized value, lowest risk, already achieved.
2. **Open-source research platform** — high option value, zero commercial risk, best credibility
   and publication path; the strongest *long-term* value creator.
3. **Personal investing platform** — a subset of #1.
4. **Institutional product** — only realistic with years of calibration and a firm behind it.
5. **Commercial SaaS** — occupied, legally exposed, no distribution. Reject.

---

## Final Vote

| Member | Vote | One-line reason |
|---|---|---|
| CIO | **Proceed with modifications** | No return evidence yet; fund only the proof. |
| Head of Fundamental Research | **Proceed with modifications** | Adoption is real but partial and needs the redesign. |
| Senior Event-Driven PM | **Proceed** | Earns a triage slot; never a trigger; redesign required. |
| Risk Manager | **Reject as-is → conditional Proceed** | Dangerous as a scored product; approve only guardrailed. |
| Director of Research Technology | **Strongly support (internal tool)** | Best-engineered internal tool we've reviewed this year. |
| Partner | **Proceed with modifications** | Six gated months, integration over standalone, then decide. |

**Committee resolution:** Proceed with modifications, as an internal instrument, on a six-month
gate, with the score hidden and no external publishing — the Risk Manager's guardrails adopted as
conditions, not suggestions.

---

## Recommended Direction

Reframe from *scored forensic report* to *contradiction feed for attention allocation.* Hide the
score. Delete or relanguage the "clean" verdict. Run the three convincing experiments. Treat the
primary identity as **internal tool now, open-source research platform as the long-term play.**
Do not commercialize.

## One-Year Roadmap — one engineer, month by month

- **M1–2:** Delisting-inclusive backtest (kill survivorship) + base-rate-corrected miss test.
- **M3:** Instrument the blind decision-impact journal; hide the score behind the contradiction feed.
- **M4:** Sector normalization from the adjudication labels (kills the utility/defense false positives).
- **M5:** Ship the experience redesign (90-second card + research queue) — *because unused tools
  generate no evidence.*
- **M6:** Calibrate the narrative block against a historical document corpus, or set its weight to zero.
- **M7–8:** Run a full live earnings season on the card; journal every decision, blind.
- **M9:** **Go/no-go gate.** Did it change decisions, and did those decisions outperform?
- **M10–12:** *If go:* open-source release with the evidence as the headline, or the narrow
  copilot wedge. *If no-go:* publish the methodology and honest negative results; harvest the
  portfolio artifact; stop feature work.

Note: every month before M9 serves the gate. No month is spent on features that don't feed the
proof. If the Partner will only fund six, stop after M6 and run the gate on what exists.

---

## The Single Biggest Mistake We Must Avoid

**Building or commercializing before the decision-impact evidence exists** — mistaking
"impressive and interesting" for "changes decisions." Every prior review flagged this pull; it is
the failure mode that kills projects at exactly this maturity. The engine is past the point where
features raise its value. Only evidence does now.

## The One Thing We Should Never Remove

**The narrative-vs-economics contradiction with its intact evidence chain** — management's own
words set against its own cash, defensible to the line item. Delete everything else before you
touch this. It is the soul of the project and the only thing that would survive deleting 80%.

## The One Thing We Should Stop Believing

**That the overall score means something as a number, and that this is a forensic/fraud product.**
It is a tail alarm, not a ranking, and we are not competing with Transparently.AI. Stop selling
ourselves the story that the number ranks companies or that we detect manipulation. We surface
contradictions and route attention. Believing otherwise is how we'd waste the next year.

---

> **If we had to defend this project in front of the world's best fundamental investors tomorrow,
> what would our opening argument be?**

*"Every earnings morning, we read each company's cash flow against its own management's words —
on every name, not just the ones we cover — and we hand you the two where the story and the money
disagree, with the receipts attached. We will not tell you what a company is worth, and we will
not tell you what to trade. We will tell you where to look first, and we will prove why. That is
the whole product, and on the mornings it matters, it is the difference between reading the filing
you should have read and finding out from the tape."*

That argument — **the contradiction engine, not the scoring engine** — is what this project
should become.
