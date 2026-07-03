# PORTFOLIO MANAGER EXPERIENCE
### Redesigning how a PM experiences the engine — from a blank page
**Seat:** Head PM, event-driven fund · **Date:** 2026-07-03
**Rule I designed under:** my own capital is on this. Optimize for one thing —
*allocate attention and ask better questions after earnings.* Nothing else.

> The engine is unchanged. Every calculation, flag, and evidence chain stays. I am
> only redesigning what reaches my eyes, in what order, and when. This is an
> experience redesign, and it starts by **throwing out the entire current report.**

---

## 1. New Product Philosophy

**The old philosophy — "score every company on earnings quality and report it
completely" — is wrong for a PM.** It optimizes for coverage and defensibility. I
optimize for *the next 20 minutes of my attention.*

The new philosophy, in one sentence:

> **This is not a scoring product. It is an attention-allocation and question-generation
> product. Its job is to answer: "which 3 of these 18 prints deserve my time, and what is
> the first question I ask?"**

Three principles fall out of that:

1. **Contradiction is the atom, not the score.** The unit of value is a specific place
   where *management's story and the company's cash disagree* — with the evidence attached.
   Everything is built around surfacing those.
2. **Novelty beats level.** A name deteriorating *this quarter* is news. A name that's been
   mediocre for eight quarters is priced in. Attention follows the *derivative*, not the
   *level*. (The engine already computes both; I'm only changing what I lead with.)
3. **Evidence before verdict.** Experienced investors trust an explanation they can check,
   not a number they can't. The number earns nothing; the line-item contradiction earns
   everything.

---

## 2. New Information Hierarchy

Ranked strictly by decision value. Everything below line 3 is *drill-down*, not front-page.

1. **The one contradiction that matters** (narrative vs economics) — *lead*.
2. **What changed vs last quarter** (max 3 deltas that moved) — *context*.
3. **The first question to investigate** — *the deliverable*.
4. — fold —
5. Full narrative-vs-economics panel (all contradictions).
6. Evidence chain per contradiction (excerpt → metric → formula).
7. Why it matters (valuation/earnings/guidance translation).
8. Watch-next-quarter items.
9. Everything else (scores, full metrics, data quality) — *appendix, on demand.*

The **overall score does not appear on the front page at all** (see §9). It survives only
as an internal sort key and a one-word triage tag.

---

## 3. New Report Layout — the 90-second card

One screen. No scroll. If it doesn't earn a line, it's below the fold.

```
──────────────────────────────────────────────────────────────
 NVDA · FY26Q2 · reported 4:05pm · [NEW CONCERN]      cov 94%
──────────────────────────────────────────────────────────────
 STORY vs CASH:  Management says "demand remains strong."
                 The balance sheet disagrees.

   ▸ Inventory +37% vs revenue +22%  ·  DSO 45→58d (8Q high)
     → demand may be softening faster than the narrative admits

 CHANGED THIS Q (that matters):
   • DSO 45 → 58 days        (biggest 1Q jump in 8 quarters)
   • Inventory/rev gap +15pp  (was in-line last Q)
   • Cash still backs it: CFO/NI 1.1x ✓  (beat is real cash)

 ASK FIRST:
   "Is the inventory build allocation-driven (supply) or
    demand-driven (softening)? Payment-term change behind DSO?"

 WHY CARE:
   If demand-driven, this quarter's revenue may be borrowing
   from next quarter → headwind to the guide the market just
   celebrated.

 Clean: leverage · dilution · capex · margins.   [expand ▸]
──────────────────────────────────────────────────────────────
```

That is the whole product for 90% of my mornings. Six answers, no score, one screen:
*clean or not · story-vs-economics · what to doubt · what changed · what to ask · why care.*

---

## 4. New Daily Workflow

**T-minus (weekend before a reporting week):** the engine runs the Research Queue (§6) on my
350-name universe using *last* quarter's data and pre-loads which names to scrutinize hardest.
I read the queue once, ~10 minutes, and mentally tag ~8 names.

**6:35am, 18 names printed overnight:** I open the Research Queue, not 18 reports. It shows
me **three lanes** (§6). I read Lane 1 ("New Concerns") first — usually 2–4 cards. Each is a
90-second card. Total: ~5 minutes to know where the bodies might be.

**Market hours:** I do *not* read reports during the open. If a name I'm in prints and the
queue tagged it, I glance at the one card. Otherwise the engine is closed. **It is a
pre-open and post-close instrument, never an intraday one.**

**Where it does NOT belong:** in my sizing, my entry/exit timing, or any name where I have
no exposure and the queue didn't rank it. It is a router to my attention, full stop.

---

## 5. Research Queue Design — the real innovation

**Do not rank by overall score.** The overall score ranks *level of concern*, and level is
mostly priced in. Rank by **attention value = how much this print should change what I do.**

Three lanes, scanned in order:

**Lane 1 — NEW CONCERNS** *(the highest-value lane)*
Names where a block or contradiction *newly* crossed into concern this quarter — the
*derivative*, not the level. Ranked within the lane by: `Δconcern this quarter ×
contradiction sharpness × my exposure flag`. A name that jumped from 25→48 outranks a name
sitting at 60 for the eighth straight quarter. **News, not history.**

**Lane 2 — LIVE CONTRADICTIONS**
Every name printing this cycle that has an active narrative-vs-economics gap, ranked by gap
magnitude × confidence. Overlaps Lane 1 but also catches chronic-but-loud cases.

**Lane 3 — CLEARED**
Names I was worried about last quarter where the concern *resolved* this print. One line
each. This is quietly valuable: it tells me I can *stop* worrying about something and
reallocate attention away — attention allocation cuts both ways.

Everything else — the ~330 names that are unremarkable — **never generates a card.** Silence
is the default and it is a feature. The queue's job is to be short.

Ranking inputs are all things the engine already computes (per-period history, concern
deltas, exposure is a user tag). No new math — only a new sort.

---

## 6. Narrative vs Economics Design — the core feature

This is the product's reason to exist. Each contradiction is a first-class object:

```
CONTRADICTION · confidence: HIGH
  MANAGEMENT CLAIM
    "Demand remained strong across our end markets."
    — FY26Q2 earnings release   [excerpt ▸]

  ECONOMIC EVIDENCE
    Receivables grew 37% while revenue grew 22% (8Q-wide gap).
    DSO rose 45 → 58 days, an 8-quarter high.
    [metric: receivables_growth_spread = +0.15 · formula ▸]

  POSSIBLE BENIGN EXPLANATION
    Large end-of-quarter shipments to a few big customers;
    a one-off timing effect that reverses next quarter.

  ANALYST FOLLOW-UP
    Confirm whether receivables concentration rose (a few
    customers) vs broadened, and whether payment terms changed.
```

Five fields, always: **claim · evidence · confidence · benign explanation · follow-up.** The
*benign explanation is mandatory and load-bearing* — it's what separates a research tool from
a short-seller's hit piece, and it's what makes an experienced PM trust it. A tool that only
prosecutes gets ignored; a tool that states the exculpatory case first gets read. It also
keeps us on the right side of the legal framing permanently.

Note the ordering: **claim and evidence come before any label.** I read *what was said* and
*what the cash did* and form my own view before the engine tells me its confidence. That is
deliberate — see §9.

---

## 7. Evidence Drill-down Design

One click on any contradiction or delta expands to exactly three layers, no more:

1. **The excerpt** — verbatim management language, dated, sourced to the filing/accession.
   (I need to see the actual words, not a paraphrase — paraphrase is where trust dies.)
2. **The metric** — the computed value, the prior-period value, and the 8-quarter sparkline.
   Trajectory in one glance.
3. **The formula + inputs** — collapsed by default, one more click. For the 1-in-20 times I
   want to argue with the number, the whole chain is there to the line item.

Rule: **each layer is optional and each is one click deeper.** A PM who trusts the flag never
expands. A PM who's about to put on risk expands to the formula. The drill-down's job is to
make the flag *defensible on demand*, not to make me read it every time. Depth available,
never imposed.

---

## 8. Trading Implications Design

Renamed from the accounting frame to the research frame. Never says buy/sell. Five prompts,
each a question or a watch item:

```
INVESTIGATE   The inventory build: allocation or demand?
SKEPTICAL OF  The "strong demand" framing until receivables reconcile.
MISSING INFO  Customer concentration behind the receivables jump
              (not disclosed this quarter).
WOULD CHANGE  A return of DSO toward 45d next quarter would clear
 CONFIDENCE   this entirely; a further rise confirms it.
WATCH NEXT Q  Whether the inventory clears or compounds.
```

Every line is actionable *research direction*, not a trade. "What would change my confidence"
is the most valuable line and the one most reports omit — it tells me the *specific future
observation* that resolves the question, which is how a disciplined PM actually updates.

---

## 9. Should there even be an Overall Score? — the deep question

**No, not on the front page. Hide it.**

The reasoning, from our own evidence:
- The overall score is a *tail alarm, not a ranking* — Q1–Q4 forward returns are
  non-monotone; only the top quintile separates. A number that only means something above
  the 90th percentile should not be presented as a continuous score, because it invites the
  PM to read gradations that aren't there.
- **Experienced investors trust explanations over numbers, and they're right to.** A number
  hides its reasoning; a contradiction shows it. When I see "concern 62/100" I have to
  reverse-engineer why. When I see "management says demand is strong, receivables say
  otherwise," I'm already thinking about the trade. The explanation *is* the product; the
  score is a lossy compression of it.
- A score also invites the one behavior that loses money here: sizing off it. Hiding it
  removes the temptation.

**So:** evidence and contradictions come *first and foremost*; the score degrades to (a) an
internal sort key for the queue and (b) a single triage tag — `[NEW CONCERN]` / `[CLEAN]` /
`[WATCH]` — which is a *state*, not a *number*. If a PM insists on the number it lives one
click deep in the appendix, labeled with its own limitations. **The default experience never
shows a number.** This is the biggest philosophical break from the current product and I'm
confident it's correct.

---

## 10. If Bloomberg copies it tomorrow — the true differentiator

Not UI. Not AI. Not summaries. The true, durable differentiator is:

> **An opinionated, auditable contradiction between what management said and what the cash
> did — rendered as a first-class object, defensible to the line item, and produced
> identically on every name whether or not a human covers it.**

Two things inside that Bloomberg structurally will not copy:

1. **The willingness to be opinionated.** Bloomberg is a neutral data vendor; its business
   model and legal posture forbid it from telling a client "management's story doesn't match
   the cash on ticker X." It ships *tools* and leaves the judgment to you. This product ships
   the *judgment* with the evidence to defend it. A data vendor can't cross that line; a
   research tool can. That's a moat of *posture*, not technology.
2. **The collision itself, across uncovered names.** Bloomberg has the numbers in one
   function, the transcript in another, and *no one collides them* — that's still the
   analyst's manual job, and the analyst only does it for names they cover. This product does
   the collision on all 350 names, consistently, before I've had coffee. Bloomberg gives me a
   library; this gives me a research associate who already read the filing and flagged the one
   weird thing on names I'd never have gotten to.

If Bloomberg builds a prettier version, it's still a library. The differentiator is *doing
the associate's first-pass judgment, auditably, at universe scale.* That's the thing worth
opening every morning.

---

## 11. What makes this worth opening every morning

Three sentences:
- It reads 18 filings before I wake up and hands me the *two* that don't add up.
- It shows me *where management's words and the company's cash disagree* — the one check no
  other tool on my desk performs at earnings speed.
- It extends my judgment into the 300 names I could never manually work up, so I stop being
  blindsided by companies I don't cover.

If it does those three things, it's on my desk permanently. If it makes me read a scorecard,
it's closed by week two.

---

## 12. The one thing users should remember

> **This tool does not tell you what a company is worth or what to do. It tells you where to
> look and what to ask — by catching the places where management's story and the cash flow
> disagree. Trust the contradiction and its evidence; ignore the score.**

---

## Features to Delete (consolidated)

- **The overall score, from the front page.** Demote to internal sort key + one-word tag.
- **The 8-block scorecard as a wall of numbers.** Show only the 1–3 blocks that *moved*.
- **Green flags.** I assume clean by default; reassurance is noise. Collapse to a single
  "Clean: …" line naming what was checked and passed.
- **Full metric-detail tables, evidence ledger, formula spec.** All to on-demand drill-down.
- **The disclaimer.** Footer. Legal, not investment.
- **Any section I'd read "rarely" during market hours** — which is all of the above.

## Features to Promote

- **Narrative-vs-Economics contradictions** → the front page and the core object (§6).
- **What-changed deltas** → second line, filtered to what *moved*.
- **"Ask first" question** → the deliverable, top-of-card.
- **"Why care" valuation translation** → the bridge from accounting to P&L (§8, mandatory).
- **The Research Queue's "New Concerns" lane** → the daily entry point, replacing 18 reports.
- **"What would change my confidence"** → the most under-used, most decision-relevant line.

---

### Closing, from the seat

The current product is a *forensic accounting report*. What a PM needs is a *research
associate's morning note*: short, opinionated, evidenced, and silent on the 330 names that
don't matter today. Same engine, same math, same evidence — radically less shown, radically
better ordered, and no number in sight. Build *that* experience and I'd want it every earnings
morning. Ship me the scorecard and I'd never open it twice.
