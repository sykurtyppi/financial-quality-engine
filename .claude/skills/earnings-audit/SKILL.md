---
name: earnings-audit
description: Audit a company's earnings release and value it from primary sources. Use when asked to review/audit an earnings print, assess a quarter, check whether a thesis still holds after results, or value a company. Enforces EDGAR-first extraction, self-computed multiples, and a correction table for known metric artifacts. Triggers on "audit these earnings", "they just reported", "check this quarter", "what are the multiples", "value this company", "deep dive on <TICKER>".
---

# Earnings Audit & Valuation

A repeatable procedure for auditing an earnings print and valuing the company **from primary
sources**. Built from repeated live runs (MXL, BA, MSFT, META, FPS, AMKR, GLW, 2026-07).

**Governing principle: never report a number you did not either read in a filing or compute
yourself.** Secondary sources were wrong or stale in the majority of checks logged below.

**Hard boundary: this produces analysis, never investment advice.** No buy/sell/hold calls, no
position sizing, no price targets presented as recommendations. Valuation ranges are analytical
exercises with stated assumptions. If asked directly for a recommendation, say plainly once that
you can't make it, then deliver the maximum analytical substance you can — the failure mode is
not "gave advice", it's "hid behind the boundary and delivered nothing."

---

## Phase 0 — Identity and data availability (never skip)

Do this **before** any analysis. It has repeatedly changed the entire framing.

```bash
# Ticker -> CIK
curl -s -H "User-Agent: <name> <email>" "https://www.sec.gov/files/company_tickers.json" \
 | python3 -c "
import json,sys
d=json.load(sys.stdin)
for v in d.values():
  if v['ticker']=='TICKER': print(str(v['cik_str']).zfill(10), v['title'])"

# Filing history: form types, dates, accession numbers
curl -s -H "User-Agent: <name> <email>" "https://data.sec.gov/submissions/CIK##########.json" \
 | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(d.get('name'), d.get('sicDescription'), d.get('exchanges'), 'FYE', d.get('fiscalYearEnd'))
r=d['filings']['recent']
for f,dt,acc,doc in list(zip(r['form'],r['filingDate'],r['accessionNumber'],r['primaryDocument']))[:40]:
    print(f,dt,acc,doc)"
```

Extract from this **before** reading any numbers:

1. **How much history exists.** A high CIK (>2,000,000) means a recent registrant. The scoring
   engine wants 8 quarters; a 2026 IPO has 2. *Say so up front and discount the score to near-zero
   confidence.* (FPS: 63% coverage, revenue missing 2 of 4 quarters, CFO missing 3 of 4 — the
   headline score was meaningless and leading with it would have been misleading.)
2. **Is the 10-Q/10-K filed yet?** This decides whether you're analysing the new quarter or a
   pre-print baseline. **Always state which.**
3. **The offering / financing cadence.** Count S-1, 424B4, S-1MEF, S-3 filings. Four offerings in
   five months is a fact about the company that no financial statement will show you (FPS).
4. **8-K clusters and Form 4s** around offerings and prints.

### Observed 10-Q filing lag (re-verify per company; this is the single best predictor of whether
the engine can see the new quarter)

| Pattern | Companies observed |
|---|---|
| Same night as the earnings 8-K | MXL |
| Same day | BA |
| Next morning | MSFT, META, AAPL, AMZN, AMKR |
| ~3 days | GLW |
| Not for ~1 week+ | GOOGL |

---

## Phase 1 — Read the primary document, not coverage

Get the 8-K exhibit list, then strip and read it:

```bash
curl -s -H "User-Agent: <name> <email>" \
  "https://www.sec.gov/Archives/edgar/data/<cik>/<accession-no-dashes>/" \
 | python3 -c "
import sys,re
t=sys.stdin.read()
for m in re.finditer(r'href=\"(/Archives[^\"]+\.htm)\"',t): print(m.group(1))"

# Strip HTML to readable text
curl -s -H "User-Agent: <name> <email>" "<exhibit-url>" | python3 -c "
import sys,re,html
t=sys.stdin.read(); t=re.sub(r'<[^>]+>',' ',t); t=html.unescape(t)
t=re.sub(r'[ \t]+',' ',t); t=re.sub(r'\n\s*\n+','\n',t)
print(t[:6000])"
```

Targeted search inside a long filing (prospectus, 10-Q) for specific concepts:

```bash
... | python3 -c "
import sys,re,html
t=sys.stdin.read(); t=re.sub(r'<[^>]+>',' ',t); t=html.unescape(t); t=re.sub(r'\s+',' ',t)
for kw in ['Tax Receivable Agreement','lock-up','selling stockholder','CHIPS','controlled company']:
    ms=list(re.finditer(kw,t,re.I)); print('>>',kw,'hits:',len(ms))
    for m in ms[:2]: print('   ', t[max(0,m.start()-300):m.start()+360].strip()[:640])"
```

**Why this is mandatory, from logged failures:**

- A widely-cited "63x EV/EBITDA" for FPS was computed at a stale price; the real figure at the
  then-current price was ~33x. Recomputing revealed the multiple had *halved*, which was the story.
- Press coverage cited a "25% investment tax credit" for AMKR. The 10-Q said **35%** (raised under
  OBBBA). The secondary sources were a legislative cycle behind.
- WebFetch returns HTTP 403 on Seeking Alpha, TipRanks, CNBC and similar. EDGAR direct-curl always
  works. Don't burn turns retrying paywalled hosts.

---

## Phase 2 — The four comparisons (in priority order)

Most analysis stops at #2. **#1 is the most informative and the most often skipped.**

1. **Actual vs the company's own prior guidance.** Management set that bar. Beating consensus is a
   statement about analysts; beating the top of your own guided range on every line is a statement
   about the business *and* about management's sandbagging tendency — which is what tells you how
   to read the *next* guide.
   *AMKR Q2'26: guided $1.75–1.85B / $0.42–0.52 / 14.5–15.5% / $105–130M; delivered $1.898B /
   $0.70 / 16.8% / $174M — above the high end of all four.*
2. **Actual vs consensus.**
3. **Guidance vs consensus** — and check *both* lines separately. A revenue guide 0.8% light with an
   EPS guide *above* consensus is not "guided below." (GLW fell 14% on exactly that.)
4. **Quality of the change.** Decompose margin moves. If every cost line improved, it's operating
   leverage and the forward guide is credible. If one line moved, it's mix or a one-off.
   *AMKR: materials 53.5→52.6, labor 10.7→10.0, depreciation 9.3→8.6, other mfg 12.3→12.0 — all four.*

**Always check the prior-year comparative for one-offs.** AMKR's Q2'25 base contained a $32M
benefit from an acquisition contingency ($0.07 EPS), so headline y/y growth *understated* the
improvement. Comparatives are inflated or depressed more often than anyone checks.

**Adjusted-vs-GAAP bridge.** Compute EBIT + D&A from reported figures and compare to guided
"Adjusted EBITDA." FPS: reported implied ~13.9% margin against a 23% adjusted guide — a nine-point
bridge that deserves line-by-line reading, especially for recently-IPO'd or sponsor-owned companies.

---

## Phase 3 — Valuation: compute it, never quote it

**Share count** — from the 10-Q, not a website:

- Use **diluted**. Cross-check: `net income ÷ diluted EPS` should reproduce it.
- **Diluted vs basic can diverge violently.** MXL: shares outstanding +1.3% q/q while *diluted*
  shares grew **11.1%** (87.6M → 97.3M) as options came into the money. If a thesis rests on a
  small share count, the diluted line is the one that tests it.
- **Up-C structures**: fully-exchanged count = Class A outstanding + Opco/LLC units held by
  continuing owners (exchangeable 1:1). Using Class A alone understates market cap badly.
  *FPS: 233.7M Class A + 44.5M Class B/Opco ≈ 278M.*

**Enterprise value** = fully-diluted market cap + total debt − (cash + short-term investments).
Don't forget short-term investments; AMKR's $2.5B sat there and made net debt ≈ zero.

**Compute and report all of these — omitting one invites the question:**

| Metric | Formula | Notes |
|---|---|---|
| P/E | price ÷ EPS | State GAAP or core/adjusted. Undefined at a loss. |
| **P/S** | market cap ÷ sales | **Compare to the company's own history**, not just peers. |
| EV/Sales | EV ÷ sales | |
| EV/EBITDA | EV ÷ (EBIT + D&A) | **Annualize.** See artifact #1. |
| **PEG** | P/E ÷ growth % | Show a *table* across growth bases. See below. |
| FCF yield | FCF ÷ market cap | |

**PEG must be shown as a range, not a number.** Compute it against every defensible growth basis —
current y/y, guided next-quarter, management's multi-year CAGR, and sales CAGR alone. The spread is
usually 50–60%, and *that spread is the actual debate*. PEG looks precise and is not.
*GLW: 1.20 / 1.29 / 1.50 / 1.90 depending on input.*

Consensus-based PEG needs licensed forward estimates (out of scope). **Guidance-based PEG is
computable and should be shown** — don't refuse it as a data-availability problem.

**P/S against the company's own history is often the most revealing single number.** GLW at 5.4x
forward sales was down 56% from its high and still ~2x its own historical 2–3x range. Peer
comparison alone would have missed that.

**Where EBITDA multiples break — say so rather than printing a big number:**
- Near an earnings trough the denominator collapses: MXL EBITDA ≈ $1.4M, BA ≈ $1.0B against $47B
  debt. The ratio is arithmetic, not information.
- For heavy-capex businesses EV/EBITDA *flatters*, because EBITDA excludes the thing that defines
  the case. AMKR screened at ~10x while guiding capex at 1.6–1.9x EBITDA. Always pair the multiple
  with capex/EBITDA.

**Valuation output format** — bear/base/bull table with the assumption stated per row, cross-checked
by sum-of-the-parts where segments differ in growth. Give an explicit range and a centre. Label
every input you estimated rather than read.

---

## Phase 4 — Correction table for known metric artifacts

**Apply these before reporting any engine output.** Each was hand-corrected in 5 of 5 live runs;
they are measurements, not opinions.

| # | Artifact | Symptom | Correction |
|---|---|---|---|
| 1 | **Quarterly-EBITDA leverage** | `net_debt_to_ebitda` divides net debt by **one quarter's** EBITDA → overstates leverage ~4x. GLW showed 7.39x (top red flag, 90/100); actual ~1.85x. | Annualize the denominator. Cross-check against interest coverage. |
| 2 | **Boilerplate adjustment language** | `adjustment_recurrence_ratio` = 1.0, concern 88/100 — matching "integration" inside Office 365 risk text, "one-time" inside a TCJA note copy-pasted since 2018. | Discount on mega-caps and any filer with stable boilerplate. Read the cited excerpt before reporting. |
| 3 | **Seasonal FCF trough read as trend** | GLW Q1 FCF margin 0.7% → flagged Cash Conversion 62. Q2 adjusted FCF was $1.42B, a 30% margin. | Check the same quarter a year prior before calling FCF deterioration. |
| 4 | **FCF blind to non-operating funding** | CFO − capex ignores CHIPS grants, investment tax credits, and customer prepayments. AMKR flagged FCF/NI = −0.954 while funded by a $407M grant, a 35% ITC, and a $1.5B NVIDIA prepayment. | Search the filing for grants/incentives/prepayments before interpreting negative FCF. |
| 5 | **Dilution blind to sponsor sell-downs** | FPS scored Capital Integrity **10/100 (lowest concern)** while the sponsor sold four times in five months. Float expands; the company issues nothing; share-count metrics see nothing. | Read the 424B4 use-of-proceeds. "We will not receive any of the proceeds" is the tell. |
| 6 | **Thin-history scores** | <8 quarters → renormalized weights, missing blocks, meaningless composite. | Lead with the coverage caveat; do not headline the score. |

When a metric fires, always produce **the strongest benign explanation** alongside it. A finding
without its best counter-argument is an accusation, not analysis.

---

## Phase 5 — Structure and who is selling

Ownership structure explains more post-IPO price action than the income statement does.

- **Read the latest 424B4 use-of-proceeds.** Distinguish primary (company keeps cash) from secondary
  (selling stockholders). Watch for pass-throughs: FPS's "by us" tranche funded an Opco redemption
  from the sponsor, so **none** of the money stayed in the business.
- **Up-C + Tax Receivable Agreement.** TRAs typically route **85% of tax savings** to pre-IPO owners
  and *grow as exchanges occur* — the exchanges these offerings execute. 232 mentions in the FPS
  prospectus. Public holders own a claim on the business through a structure built to benefit
  someone else.
- **Lock-ups.** Get the exact term and date. Check whether underwriters have **waived** prior
  lock-ups — FPS's did, repeatedly, which made each "expiry" a non-event.
- **Remaining sponsor stake** is the real overhang measure, not the lock-up date. FPS: still 51.54%
  after four offerings. Reframe correctly: if the sponsor prices deals near a given level and holds
  half the company, that is a **ceiling dynamic on rallies**, not a floor risk at the lows.
- **Insider transactions.** Absence of buying is often more informative than presence of selling.
  GLW: ~$30.7M sold over 90 days including the CEO, zero purchases, with the stock down 56%.
  Always caveat 10b5-1 plans.
- **13F sweeps are usually noise.** Small RIA moves are not signal. **Say the search was empty
  rather than dressing up noise as positioning.**

---

## Phase 6 — Positioning, anchoring, and the sell side

**Separate the day's move from the drawdown. They are different questions with different answers.**
GLW: a 0.8% guidance shortfall producing −14% is disproportionate to the information (overreaction);
a 56% retracement of a 400% run is not obviously an overreaction (unwind of an extreme).

**Always state the drawdown *and* the rally from the low.** GLW at $119 was simultaneously **−56%
from the high and +119% from the low.** "Down 56%" invites anchoring on a peak that was never
established as correct.

**Sell-side targets: check direction, date, and conflict.**
- *Targets below price* → the stock may converge down to them. MXL traded $91 against $42–$60
  targets; it closed −18.65% on a beat, converging toward where analysts already were.
- *Targets above price* → different setup, but check **when** they were set. GLW's $230–$243 cluster
  was issued within one week near the highs — momentum-chasing, and stale by the print.
- **Conflict check:** are the banks publishing research also running the company's offerings?
  (FPS: Goldman, Jefferies, Morgan Stanley did both.) A hold from a lead underwriter is the most
  informative rating in the table.
- **Calibrate your own range against consensus and say which is more optimistic**, explicitly.

**Beat ≠ rally.** In a stretched-positioning regime, genuinely good prints get sold. Logged in
2026 Q2: TSMC −3.6% (beat + raised capex), NFLX −8%, GOOGL −6.17%, MXL −18.65%, GLW −14%. When the
business is fine and the stock falls, the usual explanation is that the entry multiple already
contained the thesis — check what was priced in *before* concluding the market is wrong.

---

## Phase 7 — Cross-portfolio checks

Run these whenever the holder owns more than one name:

- **Factor correlation.** Distinct tickers in distinct industries can be one bet. Optical DSPs,
  advanced packaging, data-center electrical equipment and optical fiber are four expressions of a
  single AI-capex thesis. Diversification by position count is not diversification.
- **Thesis contradictions between holdings.** A MaxLinear bull case resting on *CPO adoption being
  delayed* is in direct tension with Corning's GLASSBRIDGE removing CPO's serviceability blocker.
  Name it — it may be an unintentional exposure or a deliberate hedge, but it should be deliberate.
- **Cycle position.** Sizing up after a print with margins at multi-year highs in a cyclical
  business means part of the "execution" is cycle. Say which part you can and cannot separate.

---

## Output contract

1. **Lead with what the tooling could not do.** Coverage gaps, missing 10-Q, thin history — before
   any score or conclusion.
2. **Credit the user's own work specifically** where it holds up, and say where it's understated.
3. **Separate:** what the filing says · what you computed · what you assumed · what you could not verify.
4. **Label every estimate.** "Q4 is my assumption, not guided."
5. **Report empty searches as empty.**
6. **State corrections plainly and move on.** If a prior figure was stale or wrong, fix it in one
   sentence without ceremony.
7. **Close with the resolving observation** — the specific filing, date, or disclosure that would
   settle the open question (first 10-K, next 10-Q segment detail, lock-up schedule).

## Hard rules

- No buy/sell/hold recommendations, no position sizing, no target prices offered as advice.
- Never state a multiple you did not compute from filed share counts and filed financials.
- Never present an engine score without its coverage percentage and the Phase 4 corrections.
- Never let a detector fire in the output without the strongest benign explanation beside it.
- Distinguish a *plan* or *report* from an implemented fact, and date every external claim — a
  policy headline from ten months ago is not news and is probably priced.
