# Q2 2026 Earnings Season — Journal Prep Sheet

Working document for the decision-impact journal (researched 2026-07-20; sources
in the footer). This sheet holds the *context*; the theses are yours, written
blind, per case, before each report. Nothing here is investment advice.

**The setup:** highest expectations bar in five years (blended S&P Q2 EPS
+24.7% y/y; +63% tech vs +12% ex-tech), most stretched positioning in five years
(BofA Bull & Bear 9.5/10, long-semis most crowded trade, cash 3.6%), VIX ~17 and
HY ~269bp (complacent), ~$16T of Mag-7 market cap reporting inside ten days — and
a hold-vs-HIKE FOMC decision landing mid-week (Warsh Fed; ~87% priced hold after
June CPI −0.4% m/m, September leans hike).

**Reaction regime so far:** misses punished ~−4.2% (vs −2.9% hist.); beats muted
or negative when guidance/capex disappoints (TSMC beat + raised capex → −3.6%;
NFLX beat Q2, guided Q3 light, CUT engagement disclosure → −8% to 52-wk low).
The quarter is the ante; guidance is the product.

## Tristan's season base case — recorded 2026-07-21, pre-prints

> The key watch item is capex spend. TSMC and ASML confirmed companies are still
> raising capex — supportive, the "we have not peaked" narrative holds. But the
> tape is not supportive, so we could see drawdown even after good earnings from
> the tech giants. **Base case: even if the print is good, the market still goes
> lower. An extreme beat is required to go higher.**

Scoring note (pre-committed): with ~8 mega-cap prints this fortnight, the base
case predicts most good-print names close DOWN. Count it at season end. To keep
the "extreme beat" leg falsifiable, define the threshold IN each thesis at open
time (e.g., magnitude vs consensus + guidance raise) — otherwise hindsight will
relabel whatever rallied as "extreme." Per-name theses go in the entries as
always; this is the season-level prior they instantiate.

## Calendar

| Date | Event | Notes |
|---|---|---|
| Tue Jul 21 | GM | |
| **Wed Jul 22** | **GOOGL** (AMC), TSLA (5:30pm, 8-K-confirmed) | first mega-cap |
| Thu Jul 23 | INTC (AMC, confirmed), MA (BMO, *unverified*) | |
| Tue Jul 28 | V (AMC, *unverified*) · **FOMC begins** | |
| **Wed Jul 29** | **MSFT** (AMC, IR-confirmed), **META** (AMC) · **FOMC decision 2pm** | the collision day |
| **Thu Jul 30** | **AAPL** (AMC, IR-confirmed), **AMZN** (AMC) · Q2 GDP advance (am) | |
| Fri Jul 24 or 31 | XOM — *date conflicts across sources* | |
| Mon Aug 3 | CAT (BMO, *unverified*) | |
| Tue Aug 4 | **AMD** (AMC, confirmed), MCD (*unverified*) | |
| Wed Aug 5 | LLY (BMO, *unverified*) | |
| Thu Aug 7 | **July jobs report** | labor cooling: June +57k, participation 61.5% |
| Wed Aug 12 | **July CPI** | re-tests the "hold" pricing |
| Wed Aug 26 | **NVDA** (FQ2-27) | season's verdict arrives without it |

## Per-name: the consensus questions (write YOUR view against these)

- **GOOGL (Jul 22):** Cloud sustains ~+63%? Search vs Gemini cannibalization?
  Do cloud profits justify the raised $180–190B capex? (Cons.: EPS ~$2.88, rev ~$117B)
- **TSLA (Jul 22):** Post-delivery-beat selloff (−7.5%) says autos numbers aren't
  the story — margins and the AI/robotaxi narrative are.
- **MSFT (Jul 29):** Azure holds high-30s/40%? ($80B power-constrained backlog =
  bulls' demand evidence). Copilot seats (20M+, +250% adds) / $37B AI run-rate.
  FY27 capex guide vs ~$190B CY26.
- **META (Jul 29):** Does record ad revenue keep buying tolerance for the RAISED
  $125–145B capex? Margin/FCF trajectory. (Guide: rev $58–61B)
- **AAPL (Jul 30):** Services after record ~$31B; Apple Intelligence China
  (Qwen-powered, CAC-approved mid-July; stock at records ~$334). iPhone 17 cycle.
  Cook-succession chatter is **unverified — treat as rumor**.
- **AMZN (Jul 30):** AWS ≥28% (enterprise-AI barometer). ~$200B capex vs TTM FCF
  collapse to $1.2B (from $25.9B). Project Leo ~$1B as a discipline tell.
  (Guide: $194–199B rev, $20–24B op inc)

**Season-wide frame:** AI-capex debate has moved from "how big" (big-four ~$725B
2026, +77% y/y) to "who can prove monetization" (~10¢ direct AI revenue per capex
dollar in 2025). JPM strategists compare the spender/supplier gap to pre-dot-com.
Mag-7 = 33.7% of index; +18% vs +11% (other 493, drifting down).

## Per-case workflow (the rules that make it evidence)

1. `journal.py open TICKER --thesis "..." --conviction N` (or the web UI) —
   **before the print, before reading anything.** Your real view, 1–2 sentences,
   ideally a *position against a consensus question above*.
2. Generate the report (locks the thesis). Read it.
3. Fill AFTER: impact (changed_thesis / changed_confidence / new_investigation /
   no_value), conviction_after, what it surfaced, what you disagreed with.
4. Weeks later: OUTCOME (what happened; helped / neutral / hurt / too_early).
5. **Log every case, including the boring ones.** Never edit a BEFORE block.

**What the engine can and cannot contribute** (docs/what_this_engine_can_and_
cannot_do.md): it will NOT predict beats/misses. Its two validated lenses fit
this season's texture: the **disclosure monitor** (NFLX cutting engagement
metrics is the exact pattern) and the **distress thermometer** on stressed
balance sheets. Treat other detector firings as unvalidated.

**Macro glance before each thesis** (Market Dashboard domains): VIX ~17,
HY OAS ~269bp, positioning stretched, oil ~$76–78 (Iran ceasefire broken),
10Y ~4.57% / 2Y ~4.21%.

## Sources (retrieved 2026-07-20)

FactSet Earnings Insight Jul 17 · Fed FOMC calendar · BLS June CPI + jobs ·
CME FedWatch · BofA July FMS (via Reuters) · Microsoft/Apple IR · Tesla 8-K ·
FactSet Q2 preview · Futurum AI-capex · TheStreet Mag-7 · Fortune/CNBC NFLX.
Full URLs in the session research notes. Dates marked *unverified* were
aggregator-sourced — confirm on company IR before relying on them.
