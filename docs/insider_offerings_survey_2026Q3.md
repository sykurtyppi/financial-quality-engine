# Survey 6: insider trading (Form 4/144/10b5-1) and equity offerings
*(agent-researched 2026-07-31; sixth and final companion survey. [verified] =
primary PDF or live SEC data read in-session; base rates below were measured on
the full 2026Q2 DERA ownership dataset, 78,328 transactions.)*

## Insider classification — B2 revised

- **Cohen–Malloy–Pomorski exact rule [verified from w16454]:** classifiable =
  ≥1 trade in each of 3 preceding years; routine = same calendar month 3
  consecutive years; opportunistic = rest. Original: opp long-short 82bp/mo VW.
  **Downgrades:** (1) post-2008 replication (unverified thesis) suggests
  **60–70% decay and a dead sell side**; (2) Ali & Hirshleifer (JFE 2017,
  PDF-read) *dominates* CMP — after controlling for their pre-earnings-window
  profitability measure, nonroutineness predicts nothing; (3) modern "routine"
  substantially overlaps 10b5-1 plan sales — dedupe against the checkbox.
- **Ali–Hirshleifer's non-return findings fit our constraint natively:**
  +1 SD firm-level opportunism → +9.9% SEC-investigation probability,
  +7.5% accounting-suit, predicts restatements. Full measure needs daily
  prices (out of scope); **degenerate free version:** flag P/S trades in the
  21 trading days before the next 8-K Item 2.02 (excl. final 2 days).
- **Cluster purchases — cheapest strong signal [WP, read]:** ≥2 distinct
  insiders buying same/consecutive days; ~40% of trades cluster; 3.8% vs 2.0%
  drift over 21 days; **orthogonal to and stronger than CMP under firm FE**.
  Exclude codes A/M/F (mechanical vesting clusters). Small build.
- **Late Form 4 [verified + measured]:** derive lateness from
  TRANS_DATE→FILING_DATE (>2 business days). **NEVER use the
  `transactionTimeliness` element — blank in 99.5% of 2026Q2 rows.**
  Measured base rate 8.71%; RDD at the deadline: +103bp jump from lag-2 to
  lag-3 (2004–2020 sample; post-2023 SEC sweeps likely shrink it). Item 405
  "Delinquent Section 16(a) Reports" caption corroborates at issuer level.
- **Pre-8-K window sign flip [verified]:** trades in [−10,−1] before an 8-K,
  partitioned by Form 4 disclosed BEFORE vs AFTER the 8-K — the interaction
  carries the information (disclosed-after net buys +1.08% vs disclosed-before
  −1.73%). Both legs are free structured EDGAR.
- **J-code trades:** institutional fact verified (residual bucket, mandatory
  free-text description, ignored by every P/S-based factor); the $1.5T HBLR
  effect sizes could NOT be verified — surface J-coded dispositions with
  footnote text as evidence, cite no magnitudes.

## 10b5-1 mechanics [verified live on EDGAR]

- `<aff10b5One>` is **document-level**, boolean, and appears in all four
  lexical forms — **testing == "1" silently drops ~25% of flagged filings**
  (measured: 0/false/1/true = 34,677/9,882/3,960/1,313). True rate 10.6%.
- Plan **adoption date is not structured on Form 4** (footnote free text;
  74.2% parseable). **Form 144 has it structured** (`planAdoptionDate`,
  MM/DD/YYYY, 64% coverage) and fires at ORDER PLACEMENT — days before the
  Form 4. **Item 408 iXBRL** (`ecd:TrdArrAdoptionDate`, `...TerminationDate`,
  `...SecuritiesAggAvailAmt`) cross-validates quarterly, D&O only.
- **Post-April-2023 framing:** cooling-off (90–120d D&O) and adopt-then-trade
  are now *compliance* requirements — Larcker's red flags 1 and 3 become
  **compliance-anomaly findings** (rare, high-value), not abuse scores.
  **Plan TERMINATIONS are the one signal that survived the amendments**
  (Kim–Kim–Rajgopal, JAE 2026: within-90-day sales fell 31.1%→1.7%;
  terminations still precede positive abnormal returns). Never present
  "sold under a plan" as inherently suspicious (Sen's limit-order mechanical
  explanation; Fich et al.: plan sales LESS opportunistic).
- **Date guards to encode:** Aug 2002 (2-day deadline), **April 1 2023**
  (checkbox mandatory — structural break in any pre/post comparison),
  ~April 13 2023 (Form 144 electronic, derived date), Sept 2023/Oct 2024
  (late-filing sweeps).

## Offerings — validates and extends the shipped B1 reader

- **>50%-secondary offerings are academically "unusual"** — Billett et al.'s
  SEO screens require ≥50% primary; our reader's "supply without balance-sheet
  benefit" flag now has a literature-anchored benchmark. Primary/secondary
  split confirmed as the highest-signal extractable field.
- **Modern SEO announcement effect is ~−0.98%** (Veld et al. meta-analysis,
  199 studies), not the −3% folklore; more negative when proceeds fund **debt
  reduction** (keyword-extractable moderator). Shelf/accelerated deals price
  ~5 days after announcement → **the S-3 filing is the early warning; the
  424B5 is a fait accompli.**
- **ATM programs [Billett et al., JFQA 2019, PDF-read]:** detection via 8-K
  Item 1.01 keywords ("at-the-market", "sales agency agreement", "controlled
  equity offering", vendor templates Jefferies/B.Riley/Cantor as Ex-1.1);
  capacity from 424B5 cover; **utilization benchmark mean 0.43 / median 0.27**;
  takedown is hand-collected prose — do not promise automated extraction.
  No pre-2008 ATM baseline exists (the market was created by the 2005/2008
  reforms).
- **Lockups:** stated 424B4 date is a **ceiling, not a commitment**
  (near-universal underwriter waiver; tranched/price-triggered releases) —
  render as "stated as X; may release early," never a countdown. Direct
  listings: negative rule, no lockup, overhang from day one. Field & Hanka's
  +40% permanent volume effect is the robust number; the −1.5% return is not
  modernly replicated (genuine gap).
- **Death-spiral/PIPE terms:** extract floating-conversion features from 8-K
  Item 3.02 + exhibits ("lowest VWAP", "Floor Price", "4.99% blocker");
  present **terms and dilution arithmetic only** — d/(1−d) risk-free
  conversion return, shares issuable at current price. The negative-return
  claim is CONTESTED (Benson et al. 2020, matched benchmarks) and the
  Hillion–Vermaelen regime was legislated away in 1998. Chaplinsky–Haushalter
  licenses an ordinal severity ladder (discount-only → warrants → ratchet →
  floating → control transfer) as a distress read, no return claim.
- **Share-count growth** (Pontiff–Woodgate / Daniel–Titman composite
  issuance): one of the few anomalies surviving the microcap critique
  (Fama–French 2008) — as descriptive dilution evidence. Traps verified live:
  cover-page vs balance-sheet dates differ by construction (NVDA example),
  rounding, per-class axes, splits (as-reported counts).
  **Share-count reconciliation has NO literature — ship as data-quality
  check, label as original heuristic.**
  `StockIssuedDuringPeriodSharesNewIssues` is sparsely tagged (NVDA: last
  fact 2013) — do not rely on it.

## Do-not-build (Tier 3)

PIPE investor-type classification (driving variable not in free EDGAR) ·
post-SEO long-run underperformance (contested) · death-spiral return claims ·
earnings-string-break prediction (Ke et al. — requires forecasting) ·
10b5-1 red flags as abuse scores (reframe as compliance anomalies).

## Editorial (agent's, endorsed)

The highest-value findings link filing patterns to **governance/accounting
risk, not returns** (Ali–Hirshleifer's investigation/restatement associations;
contract-severity-as-distress; Billett's own no-timing-ability disclaimer).
Nearly every return effect has decayed, been legislated away, or been
challenged on benchmarks — surface the underlying fact (float expansion,
dilution arithmetic, filing lag, cluster count) and let the user infer.
