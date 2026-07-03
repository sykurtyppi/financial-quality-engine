# Narrative-Contradiction Test on Restatement Cases

Date: 2026-07-03 · Config 0.3.0 (frozen) · Harness: `scripts/run_restatement_narrative.py`

## What this tests

The metric controls refuted the forensic claim: the deterministic numbers mostly
missed these accounting problems ([restatement_control.md](restatement_control.md)).
This exercises the one untested capability — the narrative-vs-economics layer — on
the same 10 restatement (4.02) cases, using **point-in-time pre-4.02 documents**
(MD&A, risk factors, earnings releases; the fetcher now supports a `before` cutoff
and CIK access for delisted names).

Two things are measured separately:
- **Metric-gated mismatches** — fire only when a positive narrative meets an
  already-flagged metric, so they structurally cannot fire where the metrics
  missed.
- **Independent narrative detectors** — adjustment-language recurrence, KPI drift,
  disclosure reduction, defensive tone, guidance shift, high-severity term
  emergence. These read the documents directly and are the only part that could
  catch what the metrics missed.

## Result

- **Metric-gated mismatches fired on 1/10 cases** (Comscore). This confirms the
  structural prediction: where the metrics missed, the mismatch layer missed too.
- **Independent narrative findings fired on 10/10 cases (6/6 pure-forensic).** All
  the signal came from the independent detectors — the part that adds beyond the
  metrics.

**Catches on cases the metrics missed entirely:**

| Case | Metrics verdict | Narrative layer caught (independently) |
|---|---|---|
| **Molson Coors** | miss (32, distress-driven) | **"material weakness" + "restatement" terms emerged**; guidance weakened; Free Cash Flow definition changed (0.09 similarity) |
| **Rockwell Medical** | unscorable | **"going concern" + "substantial doubt" emerged**; disclosure fell to 38%; a KPI dropped |
| **MiMedx** (channel-stuffing) | miss (42, not accounting) | Adjusted EBITDA & Gross Margin KPIs **dropped the quarter before restating**; Adjusted EPS definition changed; disclosure fell to 39% |
| **WageWorks** | caught | "delisting" term emerged; disclosure collapsed to 23% |

High-severity term emergence (material weakness / going concern / substantial
doubt / restatement / delisting) fired for **Molson Coors, WageWorks, Rockwell
Medical** — three cases, two of which the metrics missed or couldn't score.

## Honest interpretation — genuinely encouraging, and not yet validated

**This is the first evidence that the narrative layer sees what the numbers do
not.** On Molson Coors and Rockwell — cases the metrics missed or couldn't score —
the narrative layer independently surfaced the single most specific possible
tells: management's own "material weakness," "restatement," and "going concern"
language. That is qualitatively better than anything the metric engine managed on
these names, and it is the strongest signal to date that the surviving capability
is real.

**But the 100% hit rate is not, by itself, discrimination — and I will not present
it as such.** The confound is severe and specific:

- **adjustment_recurrence is almost certainly non-discriminating.** "Restructuring
  / impairment / adjusted EBITDA appears in 7 of 8 periods" describes most large
  companies, and *especially* serial acquirers like Molson Coors and Kraft Heinz,
  for whom that language is normal, not fraudulent. This detector would fire on
  clean companies at a similar rate. It is volume, not signal.
- **disclosure_reduction is partly an artifact.** The word-count ratio swings on
  filing type — a short 10-Q against a trailing mean that includes a long 10-K
  reads as "reduced disclosure" even when nothing was withheld. Some of the
  23–44% collapses are real; some are 10-K-vs-10-Q length, and this test does not
  yet separate them.
- **The genuinely promising, low-false-positive signals** are: **high-severity
  term emergence** (healthy companies do not say "going concern"), **KPI
  removal/definition-change of a previously-touted metric** (dropping the number
  you bragged about, right before admitting it was wrong, is a real behavioral
  tell), and *genuine* disclosure collapse. These fired on several of the exact
  cases the metrics missed. **These are the signals a clean-company control must
  test.**

**Timing caveat (important, unresolved).** The `before` cutoff is the 4.02 date, so
a "material weakness" appearing in the 10-K filed a week before the 4.02 counts as
a catch — but that is barely predictive; it is *contemporaneous* disclosure, not an
early warning. Whether the high-severity terms emerged *quarters ahead* or *in the
announcing filing* is not yet separated and materially changes the value.

## What must happen before any claim

**A clean-company narrative control** — run the identical narrative layer on a
matched set of companies that did NOT restate, and measure whether the *specific*
signals (high-severity emergence, KPI drops, genuine disclosure collapse) fire
much less than on restaters. If clean companies also light up on adjustment
language (they will) but stay dark on high-severity emergence and touted-KPI drops
(the hypothesis), then the narrative layer has a real, narrow, discriminating
core. If they light up equally, the 100% here is just base-rate noise. This is the
exact analog of the distressed-survivor and 4.02 controls that corrected the
earlier results, and it is the required next step.

## Bottom line

The narrative layer did what the metrics could not: it surfaced management's own
distress-and-restatement language on cases the numbers missed. That is the first
positive signal for the one capability the three metric controls left standing —
and it is precisely the capability the strategy reviews argued was the real
product. It is **promising and unvalidated**: the discriminating power lives (if
anywhere) in a few specific low-FP detectors, not in the 100% headline, and it
needs a clean-company control and a timing analysis before it can be believed.
This is the thread worth pulling — and, not coincidentally, the thing the live
decision-impact journal tests directly.
