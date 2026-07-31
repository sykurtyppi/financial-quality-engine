# Engine Performance Review — Q2 2026 Earnings Season (live use)

**Written 2026-07-31**, after 11 live company runs during earnings week
(MXL, BA, MSFT×2, META, FPS, AMKR, GLW, AAPL, AMZN, AMPX, KTOS).
This is the honest operational record behind any product decision.
Journal state at writing: **1 locked case (MXL), AFTER block empty, 0 outcomes.**
The pre-committed decision gate has NOT closed; this document does not replace it.

---

## 1. What the engine got right

| Run | Finding | Outcome |
|---|---|---|
| **META (pre-print, 7/27)** | `growth_capex_narrative_vs_fcf` mismatch (high conf.); buybacks = 0 vs $6B/q SBC; capex regime shift | **Confirmed 7/29**: FCF $784M (−91% y/y), buybacks still zero, capex floor raised. The baseline read like a prediction of the print. Best result of the season. |
| **AMZN (pre-print, 7/29)** | 47/100, worst hyperscaler Cash Conversion; FCF/NI −0.60 | Confirmed next day: TTM FCF −$7.6B, first negative print. (Stock +10% anyway — the engine measures quality, not returns.) |
| **MXL (7/27)** | Diluted shares +11.1%/q; SBC 5.7x CFO; "free cash flow" dropped as a disclosed KPI | Three findings the holder did not have; the KPI removal is the validated disclosure-monitor lens firing as designed. |
| **AAPL (pre-print, 7/29)** | Inventory +37.5pp over revenue; risk factors +78% | Inventory: tariff pre-build hypothesis confirmed via Q3 tariff refunds. Risk-factor read pending 10-Q. |
| **BA (7/27)** | Disclosure volume 72% of trailing avg | The only finding not present in any sell-side preview. |
| **KTOS (7/30)** | Shares +10.1% in one quarter | Led directly to discovering the $1.2B raise at $84.00 — the decisive fact of that audit. |
| **FPS / SKHY / NOK** | Refused or heavily caveated scoring on thin/absent data | Coverage discipline worked; "cannot score this" was the correct output three times. |

Structural edge confirmed: **same-day 10-Q filers (MXL, BA, AMKR, KTOS, AMPX) give the
engine print-day data** while the street works from the press release. This is a real,
repeatable speed advantage for exactly the small/mid caps where disclosure attention
is thinnest (Blankespoor processing-cost logic).

## 2. What the engine got wrong (measured, not opinion)

| # | Artifact | Season evidence |
|---|---|---|
| 1 | `adjustment_recurrence` fires on boilerplate | Flagged at 79–88/100 on **11 of 11** companies; hand-discounted **11 of 11** times. A detector with a 100% observed false-positive rate this season. |
| 2 | `net_debt_to_ebitda` uses one quarter's EBITDA | GLW flagged 7.39x as TOP red flag (concern 90); true leverage ~1.85x. Overstates ~4x for any normal earner. |
| 3 | Seasonal FCF trough read as trend | GLW Q1 0.7% FCF margin → Cash Conversion 62; Q2 printed $1.42B adjusted FCF (~30%). |
| 4 | FCF blind to non-operating funding | AMKR flagged FCF/NI −0.954 while funded by $407M CHIPS grant + 35% ITC + $1.5B NVIDIA prepayment. |
| 5 | **Capital Integrity blind to sponsor sell-downs** | FPS scored 10/100 — *lowest concern* — while the sponsor sold four times in five months. Worst miss of the season. |
| 6 | Composite score discriminates poorly | 11 runs clustered 31–58, all "Mixed." AAPL (31, cleanest) fell 6%; AMZN (47, worst hyperscaler) rallied 10%. The composite carried near-zero decision value; **all value was in specific evidence-linked findings and §5 deltas.** |

## 3. The operational finding that matters most for the product question

**The engine alone was never the unit of value this season. The audit loop was:**
engine output → artifact corrections (§2) → primary-source verification on EDGAR →
strongest-benign-explanation per flag → valuation computed from filed numbers →
positioning/structure context → mapped against the holder's own thesis.

That loop is now encoded in `.claude/skills/earnings-audit/SKILL.md`. Every decision-
relevant discovery of the week (the $84 KTOS raise, FPS's pass-through offering, the
AMKR 35% ITC, the Anthropic-marks pattern across GOOGL/MSFT/AMZN) came from the loop,
not the score. A product that ships the score ships the weakest layer.

## 4. Consequences

1. The §2 table is the pre-launch engineering backlog. Items 1–3 are code fixes;
   4–5 need new data (grants/prepayments; 424B4 use-of-proceeds); 6 means the
   composite is demoted to an internal sort key (already the PM_EXPERIENCE design).
2. The product thesis from `thesis_monitor_architecture.md` stands, sharpened:
   the sellable object is the **audit loop + thesis-change monitoring + decision
   journal**, with the deterministic engine as its evidence generator.
3. The decision gate still governs phases 6–8 (frontend polish, LLM adversary,
   release). Nothing this week closed it: n = 1 case, 0 outcomes. Next week's
   KTOS (8/4) and AMPX (8/5) prints are the best remaining chances to run the
   full protocol properly.
