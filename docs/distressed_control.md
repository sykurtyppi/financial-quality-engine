# Distressed-Survivor Control (the correction to the survivorship pilot)

Date: 2026-07-03 · Config 0.3.0 (frozen) · Harness: `scripts/run_distressed_controls.py`

## The question this settles

The [survivorship pilot](survivorship_pilot.md) found the engine elevated on 75%
of companies that DIED (≥p90) vs a 13.5% general base rate — a ~5–6× lift that
looked like real predictive power. But that comparison was against *all*
large-caps. The correct control is companies that were **just as distressed and
survived.** If those flag at the same rate, the engine detects distress, not
death, and the lift is against the wrong baseline.

## Method

16 famous near-death SURVIVORS — companies a competent observer genuinely feared
might not make it, which pulled through — sector-matched to the dead set
(retail-, energy-, travel-heavy). Each scored point-in-time at its documented
peak-distress anchor (A−12/−6/0 months). Same engine, same PIT discipline, same
bands.

## Result

**Distressed-survivors at peak distress: 70% ≥p80, 70% ≥p90 (n=10 scorable).**
**Dead set (reference): 83% ≥p80, 75% ≥p90 (n=12).**

Peak scores, survivors: Carnival 67.2 · GameStop 61.1 · American Airlines 52.7 ·
Macy's 50.7 · Occidental 50.1 · Dillard's 48.9 · Kohl's 45.6 · Bausch 39.6 ·
Ford 34.3 · Devon 31.2. Median ≈ 49.
Dead-set peak scores: median ≈ 51 (68.0 down to 30.2).

**The distributions overlap almost entirely.** GameStop (survived) scored 61.1 —
higher than 10 of the 12 companies that died. Carnival (survived) scored 67.2 —
higher than all but one decedent.

## Interpretation (blunt)

**The engine detects distress, not death.** At the individual-company level you
cannot tell a distressed-survivor from a distressed-decedent by the score. The
score measures the *magnitude of financial stress*, and stress magnitude does not
cleanly separate the companies that failed from the ones that merely suffered and
recovered.

This is the honest correction to the pilot: the 13.5%→75% "lift" was almost
entirely **the engine flagging financially distressed companies**, and distressed
companies — dead and alive — flag at ~70–83%. The discrimination between doom and
mere distress is, at this sample size, not there.

**This confirms rather than contradicts the v0.3 calibration**, which already
found the overall score is a tail alarm driven by cash-conversion and
balance-sheet blocks — i.e. distress metrics. A distress thermometer reads high on
anything hot, whether or not it is about to explode.

## What this does and does not mean for the project

**It kills, definitively, any "failure/fraud prediction" positioning.** The
engine is not an oracle for which stressed company blows up. The investment
committee already said to stop believing this; now there is direct evidence.

**It supports the "distress/quality triage" positioning.** The engine
*consistently and systematically flags financially stressed companies* across a
universe no human can manually monitor — 70–83% of genuinely stressed names,
against 13.5% of the general population. As an **attention router** ("these names
are under real stress; a human should look") that is genuinely useful. The human
still judges whether the stress is terminal — which is exactly the division of
labor every PM review landed on.

**It raises the importance of the narrative-vs-economics layer.** If the score is
just a distress thermometer, and distress is already visible to anyone looking,
then the differentiated value has to come from the *contradiction* between
management's story and the cash — not from the score. This is the third
independent line of evidence pointing at the same product conclusion.

## Honest caveats on the control itself

- **N=10 scorable of 16.** Six failed: three later delisted via buyout
  (Nordstrom went private 2025, Foot Locker acquired 2025, Marathon Oil acquired
  2024 — themselves reverse-survivorship artifacts of using current tickers), and
  three had reorganization/holdco history breaking the PIT history (Apache/APA,
  Ovintiv, and a Gap ticker change). The missing names are mostly 2020 energy and
  retail — which, if anything, would flag high too and *reinforce* the
  distress-detection conclusion.
- Small N on both sides; curated, not random; directional not significant.
- The comparison is elevated-rate, not a formal AUC. A larger, matched, weighted
  study (the blind-validation-framework's Module C) is the rigorous version — but
  the direction here is clear enough to correct the pilot's headline now.

## Bottom line

The cheap experiment that could have inflated the project's evidence instead
deflated it — correctly. **The engine is a distress detector and attention
router, not a failure predictor.** That is a smaller and truer claim, and it is
the claim the product should now be built and validated around.
