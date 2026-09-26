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
a revised year-to-date fact behind a derived quarter is caught; a summed
field's revision by the component the revising filing carries), else by the
field and quarter (a derived or composed figure that moved — and any other
field built from the very same filed facts, which the scan reports once).
A revised fact longer than the quarter is described as what it is (the
twelve months to the quarter), never with the quarter's label alone.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from app.schemas.financials import CompanyDataset, SourcedValue
from app.schemas.metrics import MetricResult, MetricStatus
from app.services.formulas.registry import MetricsBundle
from app.services.provenance import sources_for

MAX_LISTED = 2
QUARTER_DAYS = 100  # a filed period longer than this is not a quarter's own


@dataclass(frozen=True)
class Revision:
    """One revised input: the figure, how it moved, and what moved it."""

    field: str
    period_end: date
    original: float
    current: float
    how: str
    # The revised fact's own start, when it is longer than a quarter (a
    # year-to-date or annual figure behind a derived quarter): its values
    # are that period's, and saying so keeps them from reading as the
    # quarter's.
    period_start: date | None = None

    def describe(self, label: str | None = None) -> str:
        at = label or str(self.period_end)
        if self.period_start is not None:
            months = round((self.period_end - self.period_start).days / 30.4375)
            at = f"for the {months} months to {at}"
        return f"{self.field} {at} {self.original:,.0f} → {self.current:,.0f} ({self.how})"


def _span(start: date | None, end: date) -> date | None:
    """`start` when the period is longer than a quarter, else None."""
    return start if start is not None and (end - start).days > QUARTER_DAYS else None


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
        newer = vintage.newest.captured if vintage.newest is not None else "?"
        # Both windows the card's silent-revision lines read, in their order:
        # since the thesis-day snapshot, then since the previous one.
        windows = []
        if vintage.changes_since_baseline is not None and vintage.baseline is not None:
            windows.append((vintage.changes_since_baseline, vintage.baseline.captured))
        older = vintage.previous.captured if vintage.previous is not None else "?"
        windows.append((vintage.changes_since_previous, older))
        for changes, since in windows:
            for c in changes:
                if (c.kind != "revised" or c.new_value is None or c.scope != "scored"
                        or c.explained_by_filing):
                    continue
                # A scored change carries the quarter's own values, whatever
                # fact the quarter was read from: never a longer period's.
                rev = Revision(c.field_name, c.key.end, c.old_value, c.new_value,
                               f"changed silently between snapshots {since} → {newer}")
                if c.key.taxonomy == "composed" or not c.new_accession:
                    idx.by_cell.setdefault((c.field_name, c.key.end), rev)
                else:
                    idx.by_fact.setdefault((f"{c.key.taxonomy}:{c.key.tag}", c.key.start,
                                            c.key.end, c.new_accession), rev)
    if scan is not None:
        for d in scan.derived:
            moved = ", ".join(f"{form} {accn}" for form, accn in d.moved_by) or "later filings"
            idx.by_cell[(d.field_name, d.period_end)] = Revision(
                d.field_name, d.period_end, d.original_value, d.current_value,
                f"derived quarter moved by {moved}")
        for fp in scan.footprints:
            how = (f"amended by {fp.amendment_form} {fp.amendment_accession}" if fp.is_amendment
                   else f"revised by a later {fp.current_form} {fp.current_accession}")
            rev = Revision(fp.field_name, fp.period_end, fp.original_value, fp.current_value, how,
                           _span(fp.period_start, fp.period_end))
            # A summed field's footprint is tagged "a+b": its values are the
            # sum's, and the revising filing is the one a component carries.
            for concept in fp.tag.split("+"):
                idx.by_fact[(concept, fp.period_start, fp.period_end, fp.current_accession)] = rev
    return idx


@dataclass(frozen=True)
class _Cells:
    """Each period's sourced value -> (field, quarter end), and the fields
    of a quarter built from the very same filed facts (`ebit` and
    `operating_income` from one OperatingIncomeLoss): the scan reports such
    a figure once, under one field, and every twin read it."""

    at: dict[int, tuple[str, date]]
    twins: dict[tuple[str, date], tuple[tuple[str, date], ...]]


def _cells(dataset: CompanyDataset) -> _Cells:
    """`sources_for` returns these very objects, so identity names the
    figure it cited."""
    at: dict[int, tuple[str, date]] = {}
    groups: dict[tuple, list[tuple[str, date]]] = {}
    for p in dataset.periods:
        for name, sv in p.sources.items():
            at[id(sv)] = (name, p.period_end)
            if sv.inputs:
                groups.setdefault((p.period_end, sv.method, sv.inputs), []).append(
                    (name, p.period_end))
    twins = {cell: tuple(g) for g in groups.values() if len(g) > 1 for cell in g}
    return _Cells(at, twins)


def revised_inputs(
    dataset: CompanyDataset,
    bundle: MetricsBundle,
    metric: MetricResult | None,
    index: RevisionIndex,
    *,
    cells: _Cells | None = None,
) -> list[Revision]:
    """The revisions among the values `metric` read, in the order it cites
    them, each once, named by the field the metric read. Empty for a metric
    that computed nothing."""
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
        cell = cells.at.get(id(sv))
        found = [index.by_fact.get((ref.concept, ref.start, ref.end, ref.accession))
                 for ref in sv.inputs]
        if cell is not None:
            found.insert(0, next((r for c in (cell, *cells.twins.get(cell, ()))
                                  if (r := index.by_cell.get(c)) is not None), None))
        for rev in found:
            add(replace(rev, field=cell[0]) if rev is not None and cell is not None else rev)
    return out


def note(revisions: list[Revision], labels: dict[date, str] | None = None) -> str:
    """One line for the card or the ledger; the first MAX_LISTED in full."""
    labels = labels or {}
    shown = "; ".join(r.describe(labels.get(r.period_end)) for r in revisions[:MAX_LISTED])
    more = len(revisions) - MAX_LISTED
    lead = "reads a revised figure" if len(revisions) == 1 else "reads revised figures"
    return f"{lead}: {shown}" + (f"; +{more} more" if more > 0 else "")


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
