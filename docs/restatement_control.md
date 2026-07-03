# Restatement (4.02) Forensic Control — the acid test of the original claim

Date: 2026-07-03 · Config 0.3.0 (frozen) · Harness: `scripts/run_restatement_control.py`

## What this tests

The project was originally a **forensic accounting** engine: detect accounting
problems before the company admits them. The distress controls
([distressed_control.md](distressed_control.md)) showed the score is a distress
thermometer. This is the direct test of the *forensic* claim: score companies
that filed an 8-K Item 4.02 (non-reliance on previously issued financials — an
unambiguous "our numbers were wrong" admission) in the quarters BEFORE the 4.02,
when the misstated numbers were still being reported.

The sharp question is not just "did it elevate" but **"was the elevation driven by
the ACCOUNTING blocks (Earnings Quality = accruals; Revenue Quality = receivables
vs revenue, the channel-stuffing tell) or merely by DISTRESS blocks?"** Only
accounting-block elevation is genuine forensic detection. The purest cases are
companies that were financially *healthy* when they misstated — no distress
signal to rescue the flag.

## Result

| Case | Type | Best overall (band) | Driver | Verdict |
|---|---|---|---|---|
| **WageWorks** | pure-forensic | 58.0 (≥p90) | Revenue Q 86, Earnings Q 85 | **caught, accounting-driven** ✓ |
| MiMedx | pure-forensic | 42.2 (≥p80) | Working Capital; Revenue Q = n/a | **missed** (the textbook channel-stuffing case) |
| Comscore | pure-forensic | 32.4 (≥p50) | Capital Integrity | **missed** |
| Molson Coors | pure-forensic | 32.1 (≥p50) | Balance Sheet | **missed** |
| Kandi | pure-forensic | 58.3 (≥p90) | Cash Conversion | flagged, wrong reason (distress) |
| Rockwell Medical | pure-forensic | — | — | **unscorable** (data coverage) |
| Kraft Heinz | distressed-too | 62.1 (≥p90) | Cash Conversion | flagged via distress |
| SunPower | distressed-too | 53.6 (≥p90) | Earnings Q 73 | flagged, accounting-driven ✓ |
| Plug Power | distressed-too | 53.3 (≥p90) | Working Capital | flagged via distress |
| Nikola | distressed-too | 33.4 (≥p50) | Revenue Q 64 | weak; accounting-driven but low overall |

**Summary:**
- Restaters elevated ≥p90: **5/9 (56%)** — *lower* than distressed-survivors (70%)
  and the dead set (75%). Restatement, as a category, flags **less** than distress.
- Accounting-block-driven: **3/9 (33%)**.
- **Pure-forensic subset (healthy-at-time, n=5): 2/5 elevated ≥p90; only 1/5
  accounting-driven.**

## Interpretation (blunt)

**The forensic-accounting claim is not supported by this evidence.** On the pure
cases — companies whose numbers were wrong but which were not financially
distressed — the engine caught 1 (WageWorks), left 1 unscorable (Rockwell), and
**missed 3, including MiMedx**, the classic channel-stuffing fraud whose receivables
ballooned against revenue: the exact pattern Revenue Quality exists to detect. On
MiMedx, Revenue Quality could not even compute (data gap), and Comscore's
barter-inflated revenue produced a Revenue Quality of 27. The block built for this
job did not do this job.

When restaters *did* flag, it was usually the **distress** blocks firing (Kraft
Heinz, Kandi, Plug Power via Cash Conversion / Working Capital) — i.e. the same
distress detection as everywhere else, not accounting detection. Restaters that
were also distressed got flagged for being distressed; restaters that were healthy
mostly slipped through.

**WageWorks proves the mechanism can work.** Its revenue restatement showed up as
Revenue Quality 86 and Earnings Quality 85 a full six months before the 4.02. So
the accrual/receivables machinery is not broken in principle — it fires when the
misstatement manifests cleanly in the specific ratios and the data is complete. It
just does not fire reliably, and data gaps on smaller/messier filers (MiMedx,
Rockwell) compound the failure.

## Where this leaves the project (three controls, one conclusion)

| Claim | Test | Verdict |
|---|---|---|
| Detects financial distress | distress vs base rate | **YES** (70–83% vs 13.5%) |
| Predicts which company fails | distressed-survivor control | **NO** (survivors flag ~ as much as decedents) |
| Detects accounting misstatement | this 4.02 control | **NO** (pure cases: 1 catch, 3 misses, 1 unscorable) |

The engine is a **distress / financial-stress thermometer** — consistent and
useful for triage, and nothing more that these historical tests can support. Its
original identity (forensic misstatement detection) is refuted on the very cases
that identity was built for. The one capability that survives — occasionally,
when data is clean — is the accrual/receivables catch (WageWorks), which is
exactly the deterministic core; it is real but unreliable.

## Honest caveats

- Small N (5 pure cases); directional, not significant.
- Some misses are *data* failures (MiMedx Revenue Quality n/a; Rockwell
  unscorable), not pure *signal* failures — better ingestion for small filers
  might recover a few. But "we can't compute the number on the exact companies
  that commit revenue fraud" is itself a damning operational limitation.
- These historical scores use fundamentals only — **no documents.** The
  narrative-vs-economics contradiction layer (the actual differentiated product)
  is not exercised here and is not refuted by this test. It remains the one
  untested-in-decision-terms capability, and the only place left to look for an
  edge. That is what the live decision-impact journal is for.

## Bottom line

The cheap, free control refuted the project's founding claim. That is a success of
the method, not a failure of the effort: better to learn from public delisted data
that the forensic thesis does not hold than to learn it from a paid product with
users. The engine is a distress thermometer plus an occasionally-working accrual
catch. Whether the *narrative contradiction* layer adds decision value is now the
only open question worth spending on — and it costs nothing but a season of
honest journaling to answer.
