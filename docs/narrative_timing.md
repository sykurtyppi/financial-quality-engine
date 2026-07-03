# Narrative-Signal Timing — early warning or contemporaneous?

Date: 2026-07-03 · Config 0.3.0 (frozen) · Harness: `scripts/run_narrative_timing.py`

## The question

The clean control validated two discriminating narrative signals. Their *value*
depends entirely on timing: does the signal emerge quarters ahead of the 4.02
(early warning) or only in the filing that accompanies the restatement
(contemporaneous — a faithful never-misser, but not prediction)? Measured as the
lead time, in days, between when each signal emerged (by filing date) and the 4.02.

## Result — the two signals split

**high_severity_disclosure: CONTEMPORANEOUS. 0/5 early warning.**

Every firing (Molson Coors, WageWorks, Rockwell) led the 4.02 by **0 days** — the
"material weakness / going concern / restatement" language emerged in the very
10-K/10-Q filed the same day as the 4.02 non-reliance 8-K. This is partly
structural: the detector fires on a term's *first appearance*, which is exactly
when the company discloses it. So high-severity emergence is a **coincident
indicator** — it catches the disclosure at the moment it is made, never earlier.

Value: a systematic never-misser. Across 300 names a human can't read, it will not
miss a company admitting a material weakness. But it tells you nothing the 4.02
filing itself does not tell you that same day. **It confirms; it does not predict.**

**kpi_definition_change: LEADING. 3/6 early warning, including the hardest case.**

| Case | KPI-definition-change lead |
|---|---|
| Comscore | **315 days (~3.5 quarters)** |
| SunPower | **251 days (~2.8 quarters)** |
| **MiMedx** | **150 days (~1.6 quarters)** |
| Plug Power | 49 days (~0.5Q) |
| Kraft Heinz | 5 days (contemporaneous) |
| Molson Coors | 0 days (contemporaneous) |

Companies redefined the metrics they were touting **one to three-and-a-half
quarters before** admitting the numbers were wrong. Most striking: **MiMedx** — the
channel-stuffing fraud the deterministic metrics missed entirely — changed its
Adjusted EPS definition and dropped its Adjusted EBITDA / Gross Margin KPIs ~1.6
quarters ahead of the restatement. On the exact case the numbers failed, the
KPI-behavior signal led. This is the single most encouraging data point in the
project.

## Interpretation — two signals, two jobs

- **high_severity_disclosure = confirmatory monitor (0-lead).** Systematic, never
  misses a disclosure, but coincident with it. Useful for coverage-at-scale, not
  for getting ahead.
- **kpi_definition_change = genuine early warning (~1.5–3.5Q lead in half of
  cases).** The behavioral tell — you stop bragging about a number a couple
  quarters before you admit it was wrong — and it led on the fraud the metrics
  missed. This is the predictive signal, and it is the more valuable of the two.

## The direct-scan is noisy — do not trust its "early" numbers

The secondary direct-scan (earliest MD&A/release mention of any high-severity
term) reported median lead ~424 days, but this is **unreliable and I am not
counting it**: a "going concern" or "subpoena" mention three years before a
restatement is almost always an incidental or contextual reference, not a signal
of the specific restatement. Isolated term mentions at long lead are noise; the
detector's controlled "first-emergence-vs-trailing" logic (the 0-lead result
above) is the trustworthy measure.

## Honest caveats

- **Small N** (5 high-severity firings, 6 KPI firings). Directional.
- **kpi_definition_change detection is crude** — token-similarity of definitional
  sentences, which can flag innocuous wording changes. It discriminates in
  aggregate (60% vs 18% clean) and led on real cases, but at the individual level
  it is noisy and needs the LLM layer (under the grounding contract) to become
  precise.
- **The high-severity 0-lead is partly structural** (the detector fires on first
  appearance = disclosure moment). Getting earlier warning from high-severity
  *language* would require detecting the deterioration the language describes —
  which is the metrics' job, and the metrics missed it.

## Where this leaves the whole investigation

Six free historical experiments now complete the picture the paid data could not
improve on:

| Claim | Verdict |
|---|---|
| Detects distress | YES |
| Predicts which company fails | NO |
| Detects misstatement via metrics | NO |
| Detects misstatement via narrative | YES, narrow (2 of ~7 detectors) |
| high-severity signal is an early warning | NO — contemporaneous confirmer |
| KPI-definition drift is an early warning | **YES — ~1.5–3.5Q lead, incl. the fraud metrics missed** |

**The engine's defensible, validated core is now fully characterized:** a distress
thermometer; a contemporaneous high-severity disclosure monitor; and — the one
genuinely predictive, differentiated signal — **KPI-definition drift, which leads
restatements by one to three quarters, including on frauds the numbers miss.**
Everything else is scaffolding or noise.

That single leading signal is the thread worth everything now. The last open
question is the one no historical test can answer: does surfacing it change a real
decision? That is the live decision-impact journal — and the KPI-drift alert is
exactly what it should be built to test.
