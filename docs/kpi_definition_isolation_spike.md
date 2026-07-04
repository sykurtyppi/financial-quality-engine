# Definition-Isolation Spike (time-boxed) — result: STOP

Date: 2026-07-04 · Harness: `scripts/spike_definition_isolation.py` (throwaway diagnostic, not wired into scoring/config/product) · No app code changed; 237 tests still pass.

## Why this spike existed

Phase 4 (`kpi_llm_validation.md`) proved the KPI-drift recall was mostly
**extraction artifacts**: the prose-proximity extractor grabbed the first
definition-cued sentence near a KPI name, which caught revenue guidance (MiMedx),
boilerplate (Comscore), and reconciliation tables with period numbers (clean
companies) — not the actual non-GAAP definition. The identified prerequisite was a
"definition-isolation extractor." Rather than build it, we time-boxed a spike to
test whether a lightweight version clears the bar before committing to the full build.

## The hypothesis under test

> A parser that isolates the non-GAAP **add-back component set** (the reconciling
> line items that *define* a non-GAAP metric) — rather than nearby prose — will
> (a) recover the two genuine redefinitions (SunPower, WageWorks) and
> (b) not manufacture a definition from guidance/boilerplate (MiMedx, P&G).

The component set is the definition: "Adjusted EBITDA = net income before interest,
taxes, D&A, stock-based comp, restructuring" is defined by `{interest, taxes,
depreciation, amortization, stock_based_comp, restructuring}`. The structural bet:
guidance and boilerplate contain no such set, so they yield `None` — no false fire.

## Pre-committed kill criterion (set before the run)

- **Proceed** only if the parser recovers SunPower + WageWorks as genuine changes
  **and** yields no manufactured/changed definition on MiMedx + P&G.
- **Stop** otherwise — the extraction problem is harder than a note-parser solves.

## Result

| Anchor | Kind | Outcome | Verdict |
|---|---|---|---|
| SunPower | genuine (must recover) | change detected — but amid heavy quarter-to-quarter churn | PASS (by luck, see below) |
| WageWorks | genuine (must recover) | **MISSED** — isolated set stable across the whole window | **FAIL** |
| MiMedx | artifact (must reject) | guidance correctly → `None`, but a windowing delta still fired | **FAIL (false fire)** |
| P&G | artifact (must reject) | churning sets in every quarter; delta fired | **FAIL (false fire)** |

**3 of 4 fail. Verdict: STOP.**

## What the traces actually show (the important part)

The structural gate did its *primary* job: it correctly suppressed MiMedx's pure
**guidance** text — Adjusted EPS returned `None` in the guidance-only quarters,
confirming the Phase-4 diagnosis. But the component-set approach introduced a new,
**dominant** noise source that swamps the signal: **window instability.**

Because reconciliation tables are flattened to text and the parser reads a
fixed-width window (or a 4-sentence span), the set it recovers swings
quarter-to-quarter based on *what fell inside the window* — filing type (10-Q vs
8-K release), how the table flattened, table length — not on the definition
changing:

- **SunPower Adjusted EBITDA** bounces between `{gain_loss, impairment, litigation,
  one_time, restructuring, SBC, taxes}` → `{covid, litigation}` → `None` →
  `{amortization, contingent_consideration, depreciation, interest, taxes}` in four
  consecutive quarters. The real 2023 change is real, but it is **indistinguishable
  from the windowing churn around it** — the adjacent-quarter delta is large
  everywhere. The PASS is luck, not detection.
- **SunPower Gross margin** is nearly identical every quarter; its one "change"
  (`+depreciation, +taxes`) is FY2023Q2's window merely dropping two items — an
  artifact, on a metric whose definition did *not* change.
- **WageWorks Adjusted EBITDA** is *stable* the entire window
  (`{amort_intangibles, contingent_consideration, depreciation, gain_loss,
  interest, SBC, taxes}`, FY2015Q1→FY2018Q1). SBC and contingent consideration are
  **already present in 2015** — the genuine broadening Phase 4 identified predates
  the point-in-time window, or is sub-concept (specific wording, not concept
  membership). The isolation parser cannot see it. A genuine case, unrecoverable.
- **P&G** has no non-GAAP EBITDA definition at all, yet the parser produces a
  churning set every quarter by matching restructuring-program reconciliation
  tables near "free cash flow" / "gross margin." Pure windowing noise → false fire.

## Interpretation

The noise floor of concept-level component extraction from flattened HTML tables is
**higher than the signal**. Adjacent-quarter deltas are large for clean companies
and for genuine ones alike, so a "the set changed" rule cannot separate them. The
one case that passed (SunPower) passed by coincidence; the other genuine case
(WageWorks) is missed in principle, because its change is finer-grained than
concept membership or predates the visible window.

This also **sharpens the Phase-4 count**: of the two genuine cases, only SunPower is
even in-principle recoverable by set-diffing, and only amid noise. The cleanly
recoverable real core is closer to **1 of 10**, not 2.

## What the spike did *not* test (the honest boundary)

It tested a *lightweight* isolation: concept-level components over flattened-text
windows. It did **not** test structured **HTML `<table>` parsing** — aligning to
real table rows, walking GAAP-anchor row → non-GAAP-total row, and diffing the
actual line-item labels. That would remove the window-instability noise. But it is
a **materially larger build** (table-structure parsing, row alignment, label
normalization across filers, 10-Q-vs-8-K reconciliation matching) — precisely the
"larger than a note-parser" investment Phase 4 flagged.

## Decision

**STOP at the note-parser level.** Per the pre-committed logic, the full
definition-isolation extractor is worth building only if the surviving real signal
justifies it. The spike revises that signal *down* (≈1 of 10 cleanly recoverable,
and only via the heavier HTML-table approach), and shows the cheap path does not
work. The KPI-drift signal stays shelved. Reviving it would require the structured
HTML-table extractor **and** a fresh validation that it clears the noise floor — a
new, scoped decision, not a continuation of this line.

The spike did its job: it bought a go/no-go on the extraction bet for the cost of a
throwaway script, and the answer is no-go at this tier.
