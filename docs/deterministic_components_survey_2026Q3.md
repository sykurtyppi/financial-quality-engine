# Literature survey: deterministic components from LLM-era filing analysis
*(agent-researched 2026-07-31; fourth of four companion surveys)*

## Segmentation — the invisible error floor, now measurable

- **Lu, Chien, Yen & Chen (arXiv 2502.08875, v2 2026):** rule-based item
  segmentation = **0.9092 precision / 0.9001 recall** on a random sample. The
  "98% accurate regex parser" claims in the literature (Campbell et al. 2014)
  are conditioned on the **~65% of filings the parser could handle**; Brown &
  Tucker extracted 73% of MD&As. Honest characterization: ~98% on parseable
  filings, ~90% unconditioned.
- **Their 3,737-filing annotated benchmark is publicly downloadable** (κ=0.92
  gold test set) — the only large-scale item-segmentation gold standard.
  ACTION: wire it in as a validation harness; publish our precision/recall vs
  the 0.909 baseline; add their four documented failure modes as test fixtures
  (random errors; <100-line filings; mislabeled headings; out-of-order items).
- HTML→text: they found **inscriptis** preserves structure where BeautifulSoup/
  regex stripping fails; item boundaries coincide with line starts, so
  structure-preserving conversion is load-bearing.
- **edgar-crawler caveats (revises our earlier interest):** GPL-3.0 (copyleft —
  distribution-model check required), strips ALL tables (kills numeric work,
  and <table> is often layout → silent prose deletion), and publishes **no
  extraction accuracy or validation at all**. Do not adopt blind.
- Maintenance item: new items appear by rule (1A in 2005, 1C cybersecurity for
  FYs ≥ 2023-12-15) — rule-based segmenters can't discover them; schedule it.

## Cheap 8-K/NT wins (extends improvement-plan B3 and survey #10)

1. **Form 12b-25 with extension-date arithmetic** — flag NT 10-K/10-Q; Rule
   12b-25 grants +5 calendar days (10-Q) / +15 (10-K); **missing the extended
   deadline roughly doubles the negative reaction** (−2% met vs >−4% missed;
   NT 10-Q worse than NT 10-K; figures from the authors' deck, not the
   paywalled paper — treat as directional). Repeat-NT counter per CIK.
2. **NT → 8-K Item 4.02 within 30 days = the SEC's own enforcement heuristic**
   (Sept 2023 sweep targeted exactly this pattern). A two-form date join.
3. **Item 4.01 resignation-vs-dismissal split** — the item code alone is
   near-worthless, but the split flips the signal's sign (resignation bad,
   dismissal mildly positive; magnitudes unverified). Reg S-K 304(a)
   prescribes the language, so keywords are stable. Also: absence of the
   mandated "there were no disagreements" phrase = a disagreement disclosed.
4. **Item 8.01 is NOT boilerplate** — 9 of the 15 most price-reactive
   disclosure types most commonly arrive under 8.01 (Dolphin et al. 2026,
   preprint: item codes explain only 15% of reaction variance).
5. **Item 2.06 absence ≠ no impairment** — filed in only 9.84% of
   firm-quarters reporting impairments (Sanseverino & Suh, JAAF 2024).
6. **Item-count and filing-lag features** — 3-day abnormal volume scales
   monotonically with distinct item count (Lerman & Livnat, RAST 2010);
   filing lag and after-close timestamps are free fields. **Do NOT build a
   "Friday = hidden bad news" rule** — deHaan/Shevlin/Thornock find no lower
   Friday attention.

## Diffing (thread under-searched — arXiv thin; SSRN/Scholar pass warranted)

- **Brown & Tucker's operational fix, the important one:** benchmark each
  firm's filing change against the **same-year cross-sectional distribution**,
  not against zero — otherwise every new ASU/SEC rule produces mass false
  positives industry-wide. Small effort, direct FP reduction.
- Lazy Prices section targeting reconfirmed independently: Item 1A, Legal
  Proceedings, officer/management language.
- **No materiality-labeled filing-change benchmark exists** (verified empty on
  arXiv; accounting/legal-tech literatures not searched — budget died).
- **FinVerBench** (arXiv 2605.29586, 105 instances, unreviewed): four-category
  error taxonomy (arithmetic, cross-statement linkage, y/y, magnitude) = a
  ready spec for a deterministic consistency checker; its clean-statement set
  is a cry-wolf regression suite (9 of 15 LLMs produced 95–100% false
  positives on clean statements).

## XBRL validation harnesses + the recall ceiling

- **FiNER-139** (ACL 2022, CC-BY-SA-4.0): 1.12M sentences with auditor-applied
  gold XBRL tags — validation harness. Portable artifacts: sentence prefilter
  (−40% volume, −1% tagged loss), numeric-shape vocabulary. Text is lowercased
  and table-free — unusable where rules key on case/tables.
- **FNXL tail statistics:** top 150 US-GAAP labels = 58.8% of annotations →
  **a hand ruleset targeting hundreds of concepts structurally caps at
  ~60–75% recall of real filer behavior — state this in our docs.**
- **HiFi-KPI**: released rule-based iXBRL pipeline; collapse presentation/
  calculation linkbases bottom-up to normalize 218k filer labels. License
  unconfirmed.
- **NORA** (2026): filer-applied iXBRL tags are known-noisy (manual
  preparation); four-attribute contract (concept, time-relation, scale, sign)
  = the silent-failure axes. No quantified error rate — citable direction only.
- **TAT-QA** gold scale annotations (thousand/million/percent) = labeled test
  set for magnitude normalization — our most dangerous silent-failure mode.
  Non-commercial license.

## KPI-removal detector: anchor to regulation, not literature

No dataset exists. Build against **SEC Commission Guidance on MD&A (Jan 30,
2020)**: disclosed KPIs must carry definition + calculation; methodology
changes require disclosure and, where material, recasting. Three mechanical
rules: KPI present t−1 absent t; definition changed without change-disclosure;
KPI in earnings release but absent from MD&A. Citable authority.

## Earnings calls — confirmed: no clean free source

All serious transcript sources are paywalled/anti-redistribution; HF datasets
are provenance-broken or stale. **EX-99.2 route:** transcripts/prepared remarks
are sometimes furnished under Item 2.02 (EX-99.1 = press release); prevalence
claim "20–30%" is vendor marketing — spend half a day counting one quarter's
EX-99.2s to replace the guess with a fact. Deterministic transcript
discriminator: speaker-turn labels. Non-answer construct (Gow/Larcker/
Zakolyukina, JAR 2021: ~11% of 2.7M analyst questions get non-answers) —
phrase-list approximation possible, their ML precision not claimable.

## Licensing ledger (resolve before any productization)

edgar-crawler GPL-3.0 · LM master dictionary commercial license required ·
TAT-QA non-commercial · FinanceBench license conflict (CC-BY-NC vs MIT claims)
· FinQA/FNXL/KPI-EDGAR/HiFi-KPI unconfirmed · FiNER-139 CC-BY-SA-4.0 (share-
alike — check).

## Agent's ranked actions

1. Segmentation benchmark as validation harness + failure-mode fixtures.
2. NT 12b-25 flag + extension-follow-through check (best value/effort).
3. Diff targeting (1A/legal/officers) + same-year cross-sectional benchmark.
4. Item 4.01 resignation/dismissal split.
5. FiNER-139 harness + prefilter + linkbase collapsing.
6. KPI-removal vs the 2020 MD&A guidance.
