# Thesis Change Monitor — Architecture & Direction Plan

**Date:** 2026-07-27 · **Status:** proposal, not yet accepted
**Supersedes nothing.** Extends [what_this_engine_can_and_cannot_do.md](what_this_engine_can_and_cannot_do.md)
(model frozen), implements the design in [PORTFOLIO_MANAGER_EXPERIENCE.md](PORTFOLIO_MANAGER_EXPERIENCE.md),
and depends on the PIT storage item at [roadmap.md](roadmap.md) §v0.5.1.

---

## 0. The question this answers

Should the project become (a) a stock screener, (b) an AI thesis generator, (c) a
valuation terminal, or (d) something else? Research below says **(d)**: a
point-in-time *thesis change monitor* whose value proposition is **reducing
disclosure processing cost for a user who already has a thesis** — not predicting
returns.

---

## 1. Competitive research (retrieved 2026-07-27)

### 1.1 Filing diffing is a solved commodity — do not build it as the differentiator

| Product | What it does | Notes |
|---|---|---|
| BamSEC | Full-text search, **side-by-side redline filing comparison**, table extraction | Acquired by Tegus, Oct 2021 |
| Sentieo | Sentence-level filing analysis, research workflow | Acquired by AlphaSense, May 2022 |
| Calcbench | SEC filing + earnings-release financial data, cross-filing comparison | Institutional |
| ZScoreX `sec-diff`, PageCrawl.io, GeminIQ | Standalone filing-diff / risk-factor change monitoring | Cheap/free tier |

Sentieo and AlphaSense are described as the institutional default for
sentence-level filing analysis. **Conclusion: "we diff filings" is not a wedge.**

### 1.2 Earnings-quality scoring is occupied

CFRA, New Constructs, Transparently.AI, and **Hudson Labs** (formerly Bedrock AI,
Y Combinator, finance-specific LLMs; client list cited at >$600B AUM) all sell
red-flag / quality scoring. Hudson Labs is explicitly institutional. Our own
capstone already concluded our score is a weak tail alarm, so competing on score
quality is doubly unattractive.

### 1.3 AI research assistants all generate theses *for* the user

Quartr, Fiscal.ai (formerly FinChat), AlphaSense, Brightwave, Rogo, Finster —
summarization, Q&A over filings/calls/slides, initiation-style reports. This is
the crowded lane, and it is the lane that would destroy our journal's independent
prior.

### 1.4 Ingestion is commoditized too

Open-source: `dgunning/edgartools` (MIT; "full SEC corpus free, open, inspectable,
no keys or bills"), `stefanoamorelli/sec-edgar-toolkit`, `henrysouchien/edgar-parser`
(XBRL namespace resolution, period alignment, sign normalization), `Arelle/EdgarRenderer`,
`sec-api-python`. **Our ingestion layer is not a moat and should not be marketed as one.**

### 1.5 The gap

Across commercial and open-source search I found **no product that lets a user lock
a written thesis with *named assumptions* and then maps newly filed facts to those
specific assumptions, with the decision impact recorded and scored afterward.**

Everything found does one of two things:
- diffs filings **without knowing your thesis** (BamSEC, ZScoreX, PageCrawl), or
- **writes the thesis for you** (Quartr, Fiscal.ai, Hudson Labs, Brightwave, Rogo).

*Caveat: absence in a web search is not proof of absence. Treat as a strong signal,
not a certainty. Re-check before any public positioning claim.*

---

## 2. Academic research

### 2.1 The strongest support for change-oriented design

**Cohen, Malloy & Nguyen, "Lazy Prices"** (NBER w25084; *Journal of Finance* 2020).
Firms mostly repeat prior filing language; when they *actively change* it, that
change carries information. A long-short portfolio on "changers vs non-changers"
earned up to **188bp/month** alpha; filing changes predicted future earnings,
profitability, news announcements, and firm-level bankruptcies. Critically: **no
significant announcement effect** — investors miss the signal at release and price
it in only gradually.

**How much weight this carries for us:** it justifies *presenting changes* as the
primary unit of information. It does **not** license ranking, because the paper's
result *is* a cross-sectional long-short strategy, i.e. exactly the ranking our
capstone forbids — and published-anomaly decay (McLean & Pontiff; Green, Hand &
Zhang) applies to it as it did to our own "predictive" signal. Use it as design
rationale, never as an alpha claim.

### 2.2 The honest value proposition

**Blankespoor, deHaan & Marinovic**, "Disclosure Processing Costs, Investors'
Information Choice, and Equity Market Outcomes: A Review" (*Journal of Accounting
and Economics*, 2020). Monitoring for, acquiring, and analyzing disclosures carries
real cost; **learning from a disclosure is an active economic choice**, and
investors expect compensation for costly processing.

**This is the value claim the project should make.** It requires no predictive edge
whatsoever: the tool is worth using if it lowers the cost of noticing what changed
in a filing relative to what you believed. That is a defensible, literature-backed
claim, and it is fully consistent with our negative modeling results.

### 2.3 Supporting / constraining

- **Loughran & McDonald** — generic sentiment dictionaries misfire on financial
  text. Supports our deterministic, evidence-cited detectors over LLM sentiment,
  and supports demoting boilerplate-matching detectors (see §5.4).
- **Beneish, Dechow et al., Altman, Campbell–Hilscher–Szilagyi** — already
  reproduced in our capstone; no change.
- **XBRL custom-tag comparability** — SEC staff observations warn custom tags harm
  cross-company comparability. Reinforces *compare a company to its own history*,
  with tag + accession provenance retained.
- **Debiasing / calibration literature** — decision-support systems have measurable
  debiasing effects on investors; calibration research finds systematic
  overconfidence for stated probabilities above ~0.3 (Tetlock's accountability
  work: who answers to whom, for what, under what ground rules).

### 2.4 An under-recognized implication

The **journal is not merely validation scaffolding — it is plausibly the product.**
Locked thesis + conviction 1–5 + recorded outcome is a personal **calibration
instrument**, and the debiasing/accountability literature supports it directly.
Nothing found in §1 ships this for individual investors.

---

## 3. Positioning

> **Not** a stock picker. **Not** a thesis generator. **Not** a valuation terminal.
> A *thesis debugger*: it remembers what you believed, shows exactly which filed
> facts moved against which assumption, and refuses to rewrite your history.

Empirical support from this project's own use, 2026-07-27: four reports generated
the same day. **BA (no thesis)** → score 42, ten flags, nothing actionable, because
no assumption existed for the findings to attach to. **MXL (detailed thesis)** →
same engine, same score band, three findings landing on specific claims about share
count and cash generation. Identical tool, opposite usefulness; the only variable
was whether a thesis existed to check against.

---

## 4. Architecture — three planes

The existing two-plane split (deterministic facts vs. everything else) becomes
three. **Plane boundaries are the core invariant: nothing from plane 2 or 3 may
ever influence a concern score.**

### Plane 1 — Facts (deterministic, existing, frozen model)

```
EDGAR XBRL companyfacts + document sections
  → PIT vintage store  keyed (ticker, period, filing_date, accession)
  → metrics + detectors (unchanged, weights frozen at 0.3.0)
  → FactSnapshot
```

**Change from today:** ingestion currently lets the latest-filed value silently
overwrite an earlier vintage in live mode (`roadmap.md` §v0.5.1 — the backtester
already PIT-filters). Every report generated to date came through that path. This
is a correctness bug, not a feature, and it must be fixed before any cross-run
diffing, or the diff manufactures look-ahead bias.

### Plane 2 — Belief (user-authored, immutable once locked)

```
Thesis (verbatim)
  ├── assumptions[]      — named, each with a falsifier
  ├── conviction 1–5
  ├── intended_action
  └── entry_price + entry_date        ← MISSING TODAY
```

**Gap found:** the journal schema records `conviction`, `intended_action`, `impact`,
`conviction_after`, and `verdict: helped/neutral/hurt` — but **no price at which
the decision was made.** The OUTCOME block is therefore unfalsifiable: "did it
help?" gets scored from memory. This is a small fix and it blocks the only
measurement the project exists to produce.

### Plane 3 — Market (separate, non-scoring)

`app/services/backtesting/prices.py` already fetches daily adjusted closes from the
Yahoo chart API — no key, cached, with `price_on_or_after`. It is wired to the
backtester only. Wire it into the live path **for instrumentation and outcome
measurement only.**

Permitted uses: entry/outcome price capture; benchmark-relative return in OUTCOME;
one derived line — *the sales multiple implicitly accepted at open vs. now.*

**Not permitted:** feeding any concern score; P/E and EV/EBITDA as headline
metrics (EBITDA is ~$1.4M at MXL and ~$1.0B at BA against $47B debt — earnings
multiples are undefined precisely where the distress lens is validated; only P/S
and EV/Sales survive on these names); **PEG at any point** — it needs licensed
forward consensus estimates, which is a data purchase and out of scope.

---

## 5. The change model

For each `(assumption × fact)` pair, emit exactly one state:

| State | Meaning |
|---|---|
| `new` | concern crossed into elevated this period |
| `worsened` | already elevated, moved materially against the assumption |
| `improving` | moved materially in favour |
| `cleared` | previously elevated, no longer |
| `unresolved` | the filing does not speak to this assumption |
| `data_artifact` | coverage gap or boilerplate match — demoted by default |

`unresolved` is load-bearing: "the filings said nothing about your Samsung 3nm
capacity assumption" is honest and useful, and no competitor surfaces it.

Each surfaced change carries: evidence (excerpt → metric → formula), **the
strongest benign explanation**, one follow-up question, and the observation that
would resolve it.

---

## 6. Frontend

**Yes, build it — and it can proceed in parallel, which earlier sequencing got
wrong.** What exists: `app/web.py` (151 lines), five Jinja templates
(`base/dashboard/open/report/impact`), routes for dashboard, open, report view,
impact capture. The report view currently renders the locked thesis plus a
generation status line.

The redesign is already fully specified in `PORTFOLIO_MANAGER_EXPERIENCE.md`: the
90-second card, information hierarchy (contradiction → deltas → first question),
three lanes, and **the overall score removed from the front page entirely**,
surviving only as an internal sort key.

The real blocker was never design — it is that the card needs two things the data
model doesn't yet provide: assumption-level thesis structure (plane 2) and prior-run
state (plane 1 PIT). **So: freeze the JSON contract first, then frontend and data
model proceed concurrently against it.**

---

## 7. Phased plan

Gate applies **only** to phases whose value depends on whether the engine changes
decisions. Correctness fixes are never gated.

| # | Phase | Gated? | Why |
|---|---|---|---|
| 1 | **Journal price fields** (`entry_price`, `entry_date`, outcome price) | No | Fixes measurement. Hours of work. Do before the next entry. |
| 2 | **PIT vintage store** — DuckDB or SQLite keyed (ticker, period, filing_date, accession) | No | Documented look-ahead bug. Prerequisite for all diffing. |
| 3 | **Restate the decision gate** from ~20–30 cases to 8–10, in writing, now | No | See §8. Must happen before results are known. |
| 4 | **Demote noisy detectors** in report + frontend | No | Measured, not opinion: `adjustment_recurrence` was hand-discounted in 4 of 4 reports on 2026-07-27, always for boilerplate matches ("integration" in Office 365 risk text, TCJA "one-time" tax language). A filter applied 4/4 times manually belongs in code. |
| 5 | **Freeze the change-model JSON contract** (§5) | No | Unblocks parallel work. |
| 6 | **Assumption-level thesis capture** in plane 2 + change-card frontend | Partly | Contract is safe; UX polish should follow first real usage. |
| 7 | **Grounded LLM adversary** — counter-thesis, assumption mapping, benign explanations, missing questions. Refuses claims without source evidence. Never emits buy/sell, price targets, or conviction. | **Yes** | Reuse the existing grounding-contract validator from the shelved KPI adjudicator. |
| 8 | Watchlist alerts, filing-triggered monitoring, open-source release | **Yes** | Only after repeated demonstrated decision value. |

---

## 8. The gate problem

`JOURNAL.md` requires ~20–30 cases **with outcomes**, and outcomes are recorded
weeks later, blind. Current state: **1 case** (MXL, opened 2026-07-27), AFTER block
empty, 0 outcomes.

Realistic throughput: the operator logs only names he holds or knows well; BA was
correctly declined on competence grounds; MSFT and META were spent as research
runs. Expect **2–5 cases this season**, with no outcomes before September. **The
gate as written cannot close this season, and plausibly never closes at
single-operator cadence** — which converts "finish the experiment first" into an
indefinite block on phases 6–8.

Resolution: restate to **8–10 cases with outcomes**, in writing, *now*, while the
answer is still unknown. Lowering it after seeing favourable results would be
worthless.

---

## 9. What not to build

- An "improving companies" ranking or screen — `what_this_engine_can_and_cannot_do.md`
  forbids positioning the score as a ranking; the validation is one-directional
  (high score → trouble, weakly) and inverting a weak negative signal does not
  yield a positive one.
- Engine- or LLM-originated investment theses — crowded lane, and it destroys the
  journal's independent prior.
- A valuation multiples dashboard — commodity (Koyfin, stockanalysis.com, Fiscal.ai
  do it free and better), and undefined on the distressed names where the engine works.
- PEG, ever — licensed forward estimates.
- New detectors, changed score weights, KPI-drift revival, or the HTML-table
  definition extractor. Modeling phase remains concluded.

---

## 10. Open risks

1. **The §1.5 gap may be wrong.** Web search is not a market survey. Verify before
   any public claim of novelty.
2. **Lazy Prices cuts both ways** — its mechanism is the ranking we forbid, and its
   edge is a published anomaly subject to decay. Design rationale only.
3. **The processing-cost thesis is unfalsified here.** Blankespoor et al. establish
   processing costs are real; they do not establish that *this* tool lowers them
   enough to matter. That is precisely what the journal measures.
4. **Single-operator sample.** Even 8–10 cases from one user with concentrated
   holdings cannot generalize. Report it as one operator's experience, not evidence
   about investors.
5. **Yahoo chart API is undocumented and unstable** — acceptable for a personal
   tool, must be isolated behind an interface and never load-bearing for facts.
