# Real-Data Validation (v0.2)

Date: 2026-07-03. Scope: harden EDGAR/XBRL ingestion against real filings.
Harnesses: `scripts/validate_real_data.py` (coverage sweep) and
`scripts/verify_against_filings.py` (source-filing verification).

## Companies tested (8, cross-sector)

| Ticker | Sector | Field coverage (final) | Notes |
|---|---|---|---|
| MSFT | Software | 99% | SG&A via composite (S&M + G&A); D&A via composite (Depreciation + Amortization) |
| NVDA | Semiconductors | 95% | clean |
| CAT | Industrials | 93% | clean |
| KO | Consumer staples | 88% | clean |
| CRM | Software (SaaS) | 88% | Jan FYE, 52/53-week; exposed the fy/fp metadata bug |
| AAPL | Hardware | 85% | Sep FYE; goodwill/interest expense legitimately undisclosed |
| WMT | Retail | 86% | Jan-31 FYE; cover-date share counts |
| XOM | Energy | 63% | sector presentation limits (see Limitations) |

The full pipeline ran end-to-end on all 8 with no crashes and no fabricated
values; every gap surfaces as `missing_data` in the report.

## Bugs found and fixed (each has a regression test)

1. **Duration ambiguity** — 10-Qs report QTD and YTD durations for the same
   concept and end date; naive end-date keying silently mixes them (values up
   to 3x too large). Fixed: duration classification; only ~91-day facts are
   used directly. (`TestDirectAndDerivedFlows`)
2. **Missing Q4 flows** — filers report FY, not Q4. Fixed: Q4 = FY − (Q1+Q2+Q3)
   when all three are known; method recorded as `fy_minus_3q`.
3. **YTD-only cash-flow items** — CFO/capex/buybacks/SBC appear only
   cumulatively in 10-Qs. Fixed: differencing of consecutive YTD facts sharing
   a fiscal-year start; method `ytd_diff`.
4. **Window-edge quarters unmappable** — the first requested quarter had no
   in-window prior quarter to difference against (hit every company). Fixed:
   internal 4-quarter buffer, trimmed after mapping. (`test_window_edge_...`)
5. **Tag switches break series** — XOM receivables: first-matching-tag
   selection produced a 2/8 series because the filer changed tags. Fixed:
   every candidate tag is scored; best in-window coverage wins; tags are never
   mixed within a series. (`TestAmendmentsAndTagSelection`)
6. **Cover-page share dates** — `dei:EntityCommonStockSharesOutstanding` is
   stamped with the cover date, weeks after quarter end (WMT: 0/8 coverage).
   Fixed: bounded 60-day nearest-forward matching, method `nearest`, with a
   diagnostic note. (`TestCoverDateTolerance`)
7. **SEC fy/fp metadata is wrong on real filings** — CRM's 10-K for the fiscal
   year ended 2026-01-31 carries `fy: 2025` in companyfacts, producing
   duplicate `FY2025Q4` labels. Fixed: labels are derived structurally from
   the fiscal-year-end month (mode of annual-duration end months, with 52/53-
   week boundary handling: ends on day ≤ 4 attribute to the prior month).
   FY numbering = calendar year in which the fiscal year ends.
   (`TestFiscalLabels`, `TestSalesforceMapping`)
8. **Amendments/comparatives** — same period re-reported across filings;
   latest-filed now wins. (`test_latest_filed_wins`)
9. **Debt double-counting** — `LongTermDebt` (a total) must not be summed with
   its own current/noncurrent split. Fixed: split preferred; total only as
   fallback; composition recorded in diagnostics. (`TestDebtComposition`)
10. **Depreciation-only D&A** — MSFT resolves to a depreciation-only tag,
    understating D&A. Fixed: composite (Depreciation + AmortizationOfIntangibleAssets)
    when it covers more quarters; explicit note otherwise.
11. **Non-additive share counts** — weighted-average diluted shares cannot be
    Q4-derived by subtraction; they now stay missing with an explanatory note
    rather than being fabricated. (`TestNonAdditiveShares`)

## Verification against source filings (3 companies)

`scripts/verify_against_filings.py`, all checks **pass exactly (to the dollar)**:

- **Spot check**: AAPL FY2026Q2 revenue = $111,184,000,000 — matches
  10-Q accession 0000320193-26-000013 (filed 2026-05-01).
- **Annual reconciliation** (the strong test: derived quarterlies must sum to
  the independently filed 10-K totals, exercising `ytd_diff` and
  `fy_minus_3q` end-to-end):
  - AAPL FY2025: revenue $416.161B, net income $112.010B, CFO $111.482B,
    capex $12.715B, SBC $12.863B — all reconcile.
  - MSFT FY2025 (ended 2025-06-30): revenue $281.724B, net income $101.832B,
    CFO $136.162B, capex $64.551B, SBC $11.974B — all reconcile.
  - KO FY2025: revenue $47.941B, net income $13.107B, CFO $7.408B,
    capex $2.112B, SBC $0.279B — all reconcile.

## Fixtures

Real trimmed companyfacts (public-domain SEC data) are committed under
`tests/fixtures/real/` for AAPL (Sep FYE), KO (calendar FYE), and CRM (Jan
FYE, 52/53-week): only mapped tags, entries ending ≥ 2023-06-01, ~100 KB each.
Regenerate with `scripts/make_real_fixtures.py`.

## Remaining limitations (explicit)

1. **Sector presentation gaps are real**: XOM (energy) does not tag COGS,
   operating income, SBC, or diluted weighted-average shares under the
   standard concepts (63% coverage). These surface as `missing_data`; a
   custom-tag/extension-mapping layer is future work. Similar patterns are
   expected for utilities and REITs (untested).
2. **Banks/insurers remain excluded by design**; ingestion does not yet check
   SIC codes — exclusion relies on the caller setting
   `is_financial_institution`.
3. **Diluted weighted-average shares are missing for fiscal Q4s** (correct
   behavior — non-additive), so dilution metrics thin out in Q4 periods;
   `shares_outstanding` (instant) covers the gap for net-share-count metrics.
4. **Restatements**: the latest-filed value wins silently. Point-in-time
   storage (seeing both original and restated values) is roadmap v0.3.
5. **fy/fp label convention**: FY numbering = calendar year in which the
   fiscal year ends. NVDA brands its fiscal years one ahead (their "FY2027"
   ends Jan 2027 — matches our label); other filers may brand differently
   from our labels even when the math is right.
6. **No document ingestion from EDGAR yet** — narrative metrics require
   documents supplied via the canonical JSON.
7. **Scores remain v0 heuristics** — real-data ingestion does not change the
   calibration status; every score still carries the uncalibrated caveat.
8. **Amended-filing lag**: companyfacts reflects processed filings; very
   recent filings (same-day) may be absent.

## Report changes in v0.2

§7 of the report now separates and explains four situations: computed
metrics, data unavailable (coverage gap, not a concern signal), not
meaningful (with the reason), and sector/model caveats — with a coverage
summary line. This prevents "missing data" from reading as "red flag".
