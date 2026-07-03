# Narrative Methodology (v0.4)

## Position in the system

The narrative layer is **deterministic text analytics** running after the
financial formula engine and before scoring. It never sees the LLM; a future
LLM layer consumes its evidence under the grounding contract
(docs/evidence_policy.md) and can only explain/classify, never add facts.

**Calibration status: entirely uncalibrated.** The Narrative Drift block had
no historical document corpus in the v0.3 backtest. Every threshold below is
judgment-based; outputs are review prompts.

## Document inputs

| Source | How | Notes |
|---|---|---|
| 10-K / 10-Q MD&A | `edgar_documents.fetch_documents` — Item 7 / Item 2 extraction | best-effort (see limits) |
| 10-K / 10-Q Risk Factors | Item 1A extraction | best-effort |
| Earnings releases | 8-K (item 2.02) EX-99 exhibits | exhibit naming heuristics |
| Transcripts | canonical JSON only (`DocumentType.TRANSCRIPT`) | **no free redistribution-safe source exists**; stated, not worked around |

Fiscal labels are structural (same fiscal-year-end logic as fundamentals) so
documents align with mapped periods. 8-K `reportDate` is the *event* date and
is snapped to the latest known quarter end before it.

### Extraction limits (found on real filings, tested)

- Section headings are matched on tag-stripped, Unicode-normalized text
  ("Management’s" → "Management's").
- The **last** heading occurrence wins (earlier ones are the TOC); sections
  under 200 words are treated as TOC hits.
- Section end = next item heading **with its known title** — bare "Item 1A"
  also appears inside prose as a cross-reference and must not terminate the
  section (bug found live on Apple's 10-Q).
- Failures return diagnostics; sections are never fabricated. Live check
  (2026-07): AAPL, KO, CRM — MD&A 4/4 each, risk factors and releases
  extracted, ≤2 diagnostics per company.

## Comparison discipline

Each detector compares the current documented period against, as available:
**QoQ** (previous documented period), **YoY** (same fiscal quarter last year,
label-matched — used preferentially for risk factors so 10-Ks compare to
10-Ks), and a **trailing-8-period baseline** (used for tone density,
adjustment recurrence, disclosure volume, high-severity term emergence).
Document types are compared like-for-like within a period group.

## Detectors

| Detector | Method | Scored metric |
|---|---|---|
| Adjustment recurrence | word-boundary term counts ("one-time", "restructuring", …) across trailing periods; recurrence = term in ≥3 periods | `adjustment_recurrence_ratio`, `recurring_adjustment_terms` |
| KPI additions/removals | curated KPI dictionary vs union of prior 2 documented periods | `kpi_removals` |
| KPI definition change | definitional sentences (cue words: "defined as", "excludes", …) token-Jaccard < 0.55 vs most recent prior definition → candidate, both excerpts surfaced | — (evidence only) |
| Disclosure reduction | latest period word count / trailing mean; finding below 0.75 | `disclosure_volume_change` |
| Defensive tone | hedging-lexicon density per 1k words vs trailing baseline | `defensive_tone_change` |
| Guidance shift | (positive − negative) guidance-term stance, QoQ delta | `guidance_shift` |
| Risk-factor expansion | risk-section word ratio, YoY preferred | `risk_factor_expansion` |
| High-severity emergence | first appearance of "material weakness", "going concern", "substantial doubt", "restatement", "SEC investigation", … in the trailing window | — (high-confidence evidence + finding) |

Scored metrics join the existing Narrative Drift block (weights within the
block renormalized; block weight unchanged at 10%).

## Metric-narrative mismatch checks

Four checks pair a positive narrative lexicon hit (verbatim excerpt) with
deterministic metrics pointing the other way:

1. Demand strength narrative vs receivables/inventory/DSO deterioration
2. Profitability emphasis vs CFO/FCF conversion weakness
3. Buyback emphasis vs a still-rising net share count (raw value trigger)
4. Growth-capex framing vs FCF-margin compression / capex regime shift

Trigger: linked metric concern ≥ 60 (or raw share-count increase for #3).
Confidence "high" requires concern ≥ 70 AND repeated narrative emphasis.
Every mismatch links a ledger entry and generates an analyst question. A
mismatch is a question, not a conclusion — framing rules in
docs/legal_framing.md apply.

## Known limitations

1. Lexicon methods miss paraphrase and can misfire on negation ("we do not
   expect restructuring…"); candidates carry excerpts precisely so the
   analyst reads the context.
2. KPI dictionary covers common disclosed KPIs only; company-specific KPIs
   need dictionary extension.
3. Definition-change detection is token overlap, not semantics — the LLM
   layer is the planned upgrade, under the grounding contract.
4. Earnings-release exhibit discovery is name-heuristic; some filers' naming
   evades it (reported as diagnostics).
5. Everything here is uncalibrated (no historical document corpus yet —
   building one is on the roadmap for the next calibration round).
