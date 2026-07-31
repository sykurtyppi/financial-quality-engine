# Literature survey: textual-measure validity (agent-researched, 2026-07-31)

Third of three companion surveys ([arxiv_survey_2026Q3.md](arxiv_survey_2026Q3.md),
[calibration_journal_survey_2026Q3.md](calibration_journal_survey_2026Q3.md)).
[FULL] = agent read the primary PDF; [ABS] = publisher-verified abstract;
[PARTIAL] = existence verified, magnitudes not.

## The two governing principles (verified across every surviving measure)

1. **Change > level.** Every measure that holds up does so in within-filer
   change form (Hope/Hu/Lu changes spec; Lazy Prices; special-item recurrence).
   Levels are contaminated by documented 1996–2013 boilerplate drift.
2. **Density > presence.** All replicated deterministic measures are ratios
   (entities/words, numbers/words, constraining-words/words, file size) — none
   is a keyword-presence flag. Quantified precedent for our 11/11 failure:
   83% of Diction's "optimistic" and 70% of its "pessimistic" words are
   misclassified in 10-K context (LM 2015, *J. Behavioral Finance*).

## Implementable measures, with verified constructions

1. **Item 1A specificity** — Hope, Hu & Lu (*RAST* 2016) [FULL]. Specificity =
   named entities (7 categories) / words; +1 SD → +8bp |CAR|; the first-
   differences spec is also significant. **4 of 7 categories are pure regex**
   ($ amounts, % values, dates, times) — deterministic approximation defensible
   (label as inspired-by; their NER version used Stanford v3.2.0; human-rater
   validation r=0.51). Rules: flag y/y specificity DROPS ("risk factors got
   vaguer") and score newly added factors (zero entities = boilerplate; named
   customer/$ = evidence-grade). Compare within-firm only. Small effort.
2. **MD&A numbers-to-words ratio** — Siano & Wysocki (SSRN 3223757) [FULL].
   r = −0.446 with Fog on 77,144 firm-years + pre-EDGAR out-of-sample check;
   subsumes Li (2008) and Lehavy et al. readability results. Verified recipe:
   count a number only if $-prefixed, magnitude-suffixed (million/billion), or
   %-suffixed; table block = ≥200 whitespaces AND num/words>0.25, or >0.50.
   Flag within-filer declines ("MD&A got less quantitative"). Small. Caveat:
   working paper; use as change descriptor, not concern.
3. **File size as complexity** — LM 2014 (*JF* 69) [ABS]: gross EDGAR file size
   outperforms Fog for post-filing volatility/dispersion; LM 2020 reframe:
   complexity, not readability; strip HTML/XBRL bloat or use document-level
   sizes. Flag within-filer jumps. Trivial. Never use Fog.
4. **Constraining-words list** — Bodnaruk, Loughran & McDonald (*JFQA* 2015)
   [ABS]: low correlation with KZ/WW/SA indexes and predicts dividend
   omissions/equity recycling BETTER — predicts filing-observable events, not
   returns (fits our constraints). Complements Hoberg-Maksimovic CAPLIQ
   (survey #9). Small.
5. **Special-item recurrence from XBRL** — Johnson, Lopez & Sánchez
   (*Acct. Horizons* 2011) [ABS]: special items rising for 30 years; subsequent
   reporting is an increasing function of prior reporting ("one-time" recurs);
   **22% of Compustat special-item amounts don't reconcile to statements** →
   compute from XBRL tags ourselves, never aggregators. Direct support for the
   adjustment-ledger design (serial one-timer counter, ≥3 consecutive years).
6. **Document-similarity alert ranking** — Lazy Prices [PARTIAL here;
   PDF-verified in the calibration survey: headline 34–58bp/mo, 188bp is the
   Risk-Factors section]. Rank findings by inverse similarity to the filer's
   own prior filing; surface changed sentences as evidence.
7. **Bog index** — Bonsall et al. (*JAE* 2017): not computable deterministically
   (proprietary engine), but precomputed scores for all 10-Ks 1994–2023 are a
   free download (Miller's data page) — usable as a static benchmark join.

## Product-relevant cautions

- **LM master dictionary requires a commercial license for commercial use**
  (free academic; actively maintained, updated 2026-03, per-word vintages —
  good for pinned-version determinism). Budget or substitute before any
  productization.
- Garcia, Hu & Rohrer (*JFE* 2023): return-derived dictionaries beat LM
  out-of-sample for price reactions — but they are ML-derived and we don't
  predict returns; weakens LM less for our use than for academic use.
- Dyer et al. exact redundancy/stickiness formulas could NOT be verified
  (paywalled) — n-gram sentence-overlap by reputation; check the paper before
  citing constructions.
- Cazier & Pfeiffer: never flag "filing got longer" alone — length decomposes
  into complexity + redundancy + discretionary disclosure; pair with segment
  count and redundancy change.

## Not covered by this thread (no claims made)

Blankespoor/deHaan/Marinovic processing-cost taxonomy details, alert-fatigue
override rates, Beneish out-of-sample decay, ESG aggregation confusion.
