# Survivorship-Corrected Miss Test (Pilot)

Date: 2026-07-03 · Config 0.3.0 (frozen) · Harness: `scripts/run_survivorship_pilot.py`
Data: **free** — CIK-direct SEC companyfacts, which persist on EDGAR after delisting.

## Why this exists

The v0.3 miss test could only score companies still in the current SEC ticker
registry — i.e. **survivors**. Its own write-up flagged this as the dominant
limitation: the one blowup that actually delisted (Tupperware) was *untestable*.
This pilot removes that limitation for free, by fetching delisted companies'
companyfacts directly by CIK. It answers the question the survivor-only test
structurally could not: **on companies that actually failed, did the engine
elevate before the event?**

## Method

12 post-XBRL companies that died (bankruptcy or accounting non-reliance),
hand-assigned canonical event dates from public record (auto-detecting the
*terminal* event from 8-K item codes is noisy — item 3.01 fires on benign,
later-cured notices). Each scored point-in-time (facts filed ≤ as-of only) at
T−18, T−12, T−6 months. Financials excluded by the engine's SIC rule (none in
this set). Reference bands from the v0.3 distribution: p50 31.7 / p80 40.3 /
p90 45.1. **The base rate to beat: 13.5% of general large-caps flag ≥ p90.**

## Result

| Company | Event | Best pre-event score | Band |
|---|---|---|---|
| Sears Holdings | bankruptcy 2018 | 68.0 (T−18) | ≥p90 |
| Mallinckrodt | bankruptcy 2020 | 57.9 | ≥p90 |
| J.C. Penney | bankruptcy 2020 | 55.9 | ≥p90 |
| Party City | bankruptcy 2023 | 53.8 | ≥p90 |
| Bed Bath & Beyond | bankruptcy 2023 | 53.1 | ≥p90 |
| Revlon | bankruptcy 2022 | 51.3 | ≥p90 |
| Tupperware | **non-reliance 2023** | 50.8 | ≥p90 |
| Enviva | bankruptcy 2024 | 50.0 | ≥p90 |
| Chesapeake Energy | bankruptcy 2020 | 46.1 | ≥p90 |
| Whiting Petroleum | bankruptcy 2020 | 41.0 | ≥p80 |
| Diebold Nixdorf | **non-reliance 2022** | 37.3 | ≥p50 (miss) |
| Rite Aid | bankruptcy 2023 | 30.2 | <p50 (miss) |

**Headline: 10/12 (83%) elevated ≥p80 pre-event; 9/12 (75%) ≥p90 — roughly 5–6×
the 13.5% base rate.** All 12 were fully scorable (companyfacts served every
delisted name; no data gaps, no exclusions). Scores generally *rose* toward the
event (e.g. J.C. Penney 39.8 → 47.3 → 55.9 across T−18/−12/−6), i.e. the
deterioration was detectable and increasing.

## What this does and does NOT prove (the honest part)

**The biggest fear did not materialize.** The signal did not collapse when we
added the companies that died — it is *strong* on them. That is the single most
important finding: the engine's flags are not survivor-flattered artifacts.

**But much of this is distress-detection, not fraud-detection.** Nine of the
twelve are bankruptcies, and by T−6 a bankrupt-to-be company has visibly weak
cash conversion and heavy leverage — which the Cash Conversion and Balance Sheet
blocks measure almost by definition. Flagging a visibly-sick company 6 months
before Chapter 11 is useful (systematic, consistent, coverage-extending) but it
is *not* the same as revealing a hidden problem the market missed. A distressed
company is distressed; the engine confirms it rather than uncovering it.

**The purest test — accounting non-reliance — is the weaker result.** The two
non-reliance/restatement cases (the closest thing to "hidden accounting
problem") are Tupperware (50.8, but it *rose* into the band late) and Diebold
Nixdorf (37.3, a miss). Restatement-driven cases, where the numbers are wrong
rather than merely weak, are harder for the engine than cash-flow-collapse
bankruptcies. This tempers any claim that the engine "detects manipulation."

**The two misses locate the blind spots precisely:**
- **Rite Aid (never elevated, 30.2):** its collapse was opioid-litigation
  liability + refinancing failure — a legal/off-balance-sheet story invisible to
  accrual and cash-conversion metrics. This is the *same blind spot* as GE
  (insurance reserves) in the survivor test. Litigation- and liability-driven
  distress is a documented, repeatable gap.
- **Diebold Nixdorf (37.3):** a specific revenue-recognition restatement that
  left the headline cash metrics near-normal.

**The missing control group.** The correct comparison is not the 13.5% general
base rate — it is *distressed companies that did NOT die.* If highly-leveraged
survivors also score ≥p90, then part of this signal is "leverage/distress," not
"impending failure." That matched-control study is the necessary next step
before any stronger claim. Until then, treat the 5–6× lift as an upper bound on
the true discriminating power.

**Selection and scale.** Twelve hand-picked famous failures, not a random
sample; N=12; curated event dates. A pilot, not a study.

## Verdict

The free survivorship correction worked, and it delivered the evidence the
calibration report and the investment committee said did not exist: **on
companies that actually failed, the engine elevated before the event far above
base rate, and the result did not depend on excluding the dead.** That is a real
strengthening of the project's evidence base — the cheap experiment that could
have killed it instead supported it.

The honest qualifier stands: this is largely *distress* detection, weakest
exactly where the "forensic accounting" framing is strongest (restatements), and
it lacks a distressed-survivor control. The next move is that control group and,
if warranted, the paid delisting-inclusive *price* data for the returns-based
version — but only now, with a free positive result already in hand, rather than
on spec.
