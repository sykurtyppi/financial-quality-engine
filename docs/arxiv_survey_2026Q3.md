# Literature survey: deterministic improvement candidates (agent-researched, 2026-07-31)

Produced by a dedicated research agent against the measured failures in
[season_2026Q2_engine_performance.md](season_2026Q2_engine_performance.md), under the
standing constraints (no ML scoring, free data only, evidence-never-score). Every
ranked item was verified in-session against a paper page, publisher record, or
author PDF; unverifiable items were quarantined, not ranked. Editorial notes in
[accuracy_improvement_plan.md](accuracy_improvement_plan.md) §Addendum.

## Ranked candidates (condensed; verify URLs in the plan addendum before citing)

1. **Non-GAAP adjustment recurrence ledger** — Doyle, Lundholm & Soliman (*RAST* 2003):
   exclusions' predictive content is entirely compositional — special items predict
   ~+$1 of future cash flow per $1; *other* (recurring-type) exclusions predict ~−$3.
   S&P 100 evidence: ~half of non-GAAP disclosers repeated the "one-time" label for the
   same item across years. Rule: per-issuer ledger of named exclusion labels + dollar
   amounts from the reconciliation table; finding fires on recurrence (≥3 of trailing 8
   quarters) or cumulative "one-time" charges ≥ X% of cumulative GAAP operating income.
   Replaces keyword presence entirely. Maps to failure 1. Small–medium.
2. **Accrual corroboration gate** — Ball, Hoberg & Maksimovic (SSRN 2260371, working
   paper — venue unconfirmed): discretionary accruals *explained by MD&A business-change
   facts* are not associated with restatements/litigation; unexplained residual is.
   Deterministic variant: accrual flag + matched newly-filed business-change fact
   (acquisition tags, 8-K 2.01, policy change) → "explained"; unmatched → unresolved.
   Maps to failure 1 and the mismatch detector. Medium.
3. **Peer 4-gram boilerplate index + firm 8-gram stickiness residual** — Dyer, Lang &
   Stice-Lawrence (*JAE* 2017); Cazier & Pfeiffer (2016/2017: repetition rises with
   litigation risk and poor performance). Nothing fires unless new-for-filer AND
   not-ubiquitous-across-filers-this-period. General gate for every text detector +
   the novelty ranking. Maps to failures 1 and 5. Medium (real infrastructure).
4. **Risk-factor add/remove set-difference** — Lyle, Riedl & Siano (*TAR* 2023):
   added risk topics raise perceived risk, removals lower it; repeated factors carry
   ~nothing. Segment Item 1A on headings, diff sets vs prior filing, report ADDED /
   REMOVED only. Strictly better than the current word-count `risk_factor_expansion`.
   Small–medium.
5. **Fraud-model false-positive economics as presentation policy** — Beneish & Vorst
   (*TAR* 2022): best published misstatement models trade >100 false positives per
   true positive. Quantitative vindication of the frozen composite; adds per-finding
   base-rate disclosure ("N of M covered filers also show this"). Small.
6. **SEC comment-letter stream (UPLOAD/CORRESP)** — Dechow, Lawrence & Ryans (*TAR*
   2016): insider sales ~70% above normal in the 5 days before revenue-recognition
   letters go public; letters are under-downloaded (inattention). New free evidence
   stream: letter dates, topic keywords, rounds, resolution. Rare, high-specificity.
   Small–medium. (Do not quote the drift numbers — 2016 sample, unreplicated.)
7. **Section-weighted four-measure change ensemble** — Cohen, Malloy & Nguyen (*JF*
   2020): compute cosine/Jaccard/edit/simple similarity per SECTION; exec-team,
   litigation, and risk-factor changes rank highest. Use section ranking + measure
   agreement; never the alpha (return claims frozen; post-2014 decay per Bowles et
   al. *JF* 2024). Small.
8. **Covenant-violation/waiver text detection** — Nini, Smith & Sufi (*RFS* 2012):
   10–20%/yr of firms disclose violations; violations immediately precede capex cuts,
   deleveraging, payout cuts, CEO turnover. Phrase-search the debt footnote for
   compliance language ("was not in compliance", "obtained a waiver"); where present,
   this *replaces* the broken net-debt/EBITDA ratio flag with a disclosed fact.
   Maps to failure 2. Small. (Covenant-lite growth means absence ≠ safety.)
9. **Text-based constraint measure on "Liquidity and Capital Resources"** — Hoberg &
   Maksimovic (*RFS* 2015): CAPLIQ-subsection language outperforms KZ/WW indices for
   predicting investment cuts. Y/y jump in constraint language in that subsection =
   finding; their free annual data library = validation cross-check. Medium.
10. **NT 10-K / NT 10-Q late-filing notices with reason grading** — *Advances in
    Accounting* 2010: reaction depends on stated reason; absent/boilerplate reasons
    are the bad ones. Extract Part III explanation, grade attribution present/absent +
    cause class. Pair with 8-K 4.02. Small.
11. **Specificity grading of newly added text** — Hope, Hu & Lu (*RAST* 2016):
    named-entity density predicts better risk assessment. Deterministic proxy only
    (numerals/dates/amounts/proper-noun density) — NOT their NER measure; label as
    inspired-by. "New but generic" vs escalate. Small–medium.
12. **Segmentation error calibration** — Lu et al., arXiv:2502.08875: rule-based item
    segmentation macro-F1 ≈ 0.905 → ~1 in 10 extractions mis-bounded; a floor on
    narrative-detector FP rates that is currently invisible. Add per-section sanity
    checks + extraction-confidence flag; consider edgar-crawler (WWW 2025).
    Small. Part of the season's 11/11 may be segmentation artifacts.

## Not ranked / honest gaps (from the agent, verbatim in substance)

- **XBRL custom-tag rate**: evidence contradictory (complexity signal vs processing
  cost); surface as neutral context and flag *changes* only — never a concern.
- **Unresearched, do not build on intuition**: non-operating-funding literature
  (failure 3), sponsor-selldown/lockup literature (failure 4), post-2023 10b5-1
  checkbox information content, quarterly-normalization literature (engineering
  convention: TTM + same-quarter-y/y + Q4-separate), decision-journal tooling,
  earnings-call event studies. The agent's WebSearch budget exhausted before these
  threads closed; they are flagged, not guessed.

## Agent's do-first: (1) recurrence ledger, (2) n-gram gates + segmentation checks,
(3) comment-letter stream.
