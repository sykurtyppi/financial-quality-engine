"""Which metrics read a figure that was revised — and which figure.

The restatement scan (`restatements.RestatementScan`) and the silent-revision
check (`vintages.VintageDiffReport`) report revised FIGURES; the card reports
METRICS. A metric computed from a restated figure could read as independent
corroboration — the earnings-night drill's CRM run put "Receivables growing
in line with revenue" under Checked and clean purely because a 10-Q/A raised
the revenue it divides by, with nothing tying the two. This module joins
them: a metric is marked when a value it read (exactly, per
`provenance.sources_for`) is one a revision touched, naming the input — its
period, original → current, and what revised it. The metric is never called
wrong; the input's vintage is what the reader needs.

Matching is by the filed fact where one exists (concept, period, accession:
a revised year-to-date fact behind a derived quarter is caught), else by the
field and quarter (a derived or composed figure that moved).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.schemas.financials import CompanyDataset, SourcedValue
from app.schemas.metrics import MetricResult, MetricStatus
from app.services.formulas.registry import MetricsBundle
from app.services.provenance import sources_for

MAX_LISTED = 2


@dataclass(frozen=True)
class Revision:
    """One revised input: the figure, how it moved, and what moved it."""

    field: str
    period_end: date
    original: float
    current: float
    how: str

    def describe(self, label: str | None = None) -> str:
        return (f"{self.field} {label or self.period_end} {self.original:,.0f} → "
                f"{self.current:,.0f} ({self.how})")


@dataclass
class RevisionIndex:
    """Revised figures, keyed the two ways a cited value can be matched."""

    by_fact: dict[tuple[str, date | None, date, str], Revision] = field(default_factory=dict)
    by_cell: dict[tuple[str, date], Revision] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.by_fact or self.by_cell)


def revision_index(scan: Any = None, vintage: Any = None) -> RevisionIndex:
    """Every revision the two checks found. A check that did not run (or
    failed — the caller passes None) adds nothing; the card already says it
    was not checked. A filing-history footprint wins over a silent change
    for the same fact: it names the filing."""
    idx = RevisionIndex()
    if vintage is not None and getattr(vintage, "compared", False):
        older = vintage.previous.captured if vintage.previous is not None else "?"
        newer = vintage.newest.captured if vintage.newest is not None else "?"
        for c in vintage.changes_since_previous:
            if (c.kind != "revised" or c.new_value is None or c.scope != "scored"
                    or c.explained_by_filing):
                continue
            rev = Revision(c.field_name, c.key.end, c.old_value, c.new_value,
                           f"changed silently between snapshots {older} → {newer}")
            if c.key.taxonomy == "composed" or not c.new_accession:
                idx.by_cell[(c.field_name, c.key.end)] = rev
            else:
                idx.by_fact[(f"{c.key.taxonomy}:{c.key.tag}", c.key.start, c.key.end,
                             c.new_accession)] = rev
    if scan is not None:
        for d in scan.derived:
            moved = ", ".join(f"{form} {accn}" for form, accn in d.moved_by) or "later filings"
            idx.by_cell[(d.field_name, d.period_end)] = Revision(
                d.field_name, d.period_end, d.original_value, d.current_value,
                f"derived quarter moved by {moved}")
        for fp in scan.footprints:
            how = (f"amended by {fp.amendment_form} {fp.amendment_accession}" if fp.is_amendment
                   else f"revised by a later {fp.current_form} {fp.current_accession}")
            idx.by_fact[(fp.tag, fp.period_start, fp.period_end, fp.current_accession)] = Revision(
                fp.field_name, fp.period_end, fp.original_value, fp.current_value, how)
    return idx


def _cells(dataset: CompanyDataset) -> dict[int, tuple[str, date]]:
    """Each period's sourced value -> (field, quarter end). `sources_for`
    returns these very objects, so identity names the figure it cited."""
    return {id(sv): (name, p.period_end) for p in dataset.periods for name, sv in p.sources.items()}


def revised_inputs(
    dataset: CompanyDataset,
    bundle: MetricsBundle,
    metric: MetricResult | None,
    index: RevisionIndex,
    *,
    cells: dict[int, tuple[str, date]] | None = None,
) -> list[Revision]:
    """The revisions among the values `metric` read, in the order it cites
    them, each once. Empty for a metric that computed nothing."""
    if metric is None or not index or metric.status is not MetricStatus.OK:
        return []
    cells = _cells(dataset) if cells is None else cells
    out: list[Revision] = []

    def add(rev: Revision | None) -> None:
        if rev is not None and rev not in out:
            out.append(rev)

    values: Iterable[SourcedValue] = (
        sv for group in sources_for(dataset, metric, bundle=bundle).values() for sv in group)
    for sv in values:
        cell = cells.get(id(sv))
        if cell is not None:
            add(index.by_cell.get(cell))
        for ref in sv.inputs:
            add(index.by_fact.get((ref.concept, ref.start, ref.end, ref.accession)))
    return out


def note(revisions: list[Revision], labels: dict[date, str] | None = None) -> str:
    """One line for the card or the ledger; the first MAX_LISTED in full."""
    labels = labels or {}
    shown = "; ".join(r.describe(labels.get(r.period_end)) for r in revisions[:MAX_LISTED])
    more = len(revisions) - MAX_LISTED
    noun = "figure" if len(revisions) == 1 else "figures"
    return f"reads a revised {noun}: {shown}" + (f"; +{more} more" if more > 0 else "")


def _history(bundle: MetricsBundle, name: str, label: str) -> MetricResult | None:
    return next((m for m in bundle.history.get(name, []) if m.fiscal_label == label), None)


@dataclass
class CardNotes:
    """Markers for the decision card: change lines by their label, flags by
    (title, fiscal label)."""

    changes: dict[str, str] = field(default_factory=dict)
    flags: dict[tuple[str, str], str] = field(default_factory=dict)


def card_notes(dataset: CompanyDataset, bundle: MetricsBundle, flags: Iterable[Any],
               index: RevisionIndex) -> CardNotes:
    """What the card must say about revised inputs. A change line compares a
    metric at two periods (as `pipeline._what_changed` picks them); a flag
    rests on its evidence metrics at its own period."""
    from app.core.pipeline import _CHANGE_METRICS

    notes = CardNotes()
    if not index:
        return notes
    cells = _cells(dataset)
    labels = {p.period_end: p.fiscal_label for p in dataset.periods}
    for name, line_label, _fmt in _CHANGE_METRICS:
        series = [m for m in bundle.history.get(name, []) if m.status is MetricStatus.OK]
        if len(series) < 2:
            continue
        revs: list[Revision] = []
        for m in (series[-2], series[-1]):
            revs += [r for r in revised_inputs(dataset, bundle, m, index, cells=cells)
                     if r not in revs]
        if revs:
            notes.changes[line_label] = note(revs, labels)
    for f in flags:
        revs = []
        for name in f.evidence_metrics:
            at = _history(bundle, name, f.fiscal_label)
            revs += [r for r in revised_inputs(dataset, bundle, at, index, cells=cells)
                     if r not in revs]
        if revs:
            notes.flags[(f.title, f.fiscal_label)] = note(revs, labels)
    return notes
