"""The validation corpus: real filings with expectations a human pinned.

Every past review round found defects the test suite could not see, because
the tests were the author's own examples. The corpus is the opposite: cases
chosen for a reason (an amendment that moved a scored figure, one that did
not, a non-reliance 8-K, a taxonomy migration, a 52/53-week calendar, …),
each a trimmed copy of the filer's companyfacts and filing index, and each
with expectations pinned from the engine's OBSERVED output only after a
person read the filings and agreed with it (`reviewed`).

`observe` runs exactly what a report runs, through the same functions and
windows — the mapper as of the case date, the restatement scan on the
mapper's selections, the card's Tier-1 promotion, the 8-K 4.02 window — and
`evaluate`/`metrics` score it. The gates:

- false-clean rate = 0: no case with a pinned signal reads clean;
- restatement recall = 1.0 on the pinned footprints;
- amended precision = 1.0: every amendment the scan promotes is pinned;
- evidence coverage: each case inspects at least its `coverage_min` share
  of the scored fields, and names every field it could not inspect.

A case marked `synthetic` exercises the harness; it is never evidence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

CASE_FILE = "case.json"
FACTS_FILE = "companyfacts.json"
SUBMISSIONS_FILE = "submissions.json"


class Review(BaseModel):
    """Who read the filings behind a case, and which ones."""

    model_config = ConfigDict(frozen=True)

    by: str = Field(min_length=1)
    on: date
    filings_read: list[str] = Field(min_length=1)


class ExpectedFootprint(BaseModel):
    model_config = ConfigDict(frozen=True)

    field: str
    period_end: date
    amended: bool  # the revision has a /A filing behind it (a Tier-1 event)


class Expected(BaseModel):
    model_config = ConfigDict(frozen=True)

    tier1: bool  # the card must show at least one Tier-1 event
    footprints: list[ExpectedFootprint] = Field(default_factory=list)
    non_reliance: list[str] = Field(default_factory=list)  # 8-K 4.02 accessions
    selections: dict[str, str] = Field(default_factory=dict)  # field -> tag_used
    coverage_min: float = Field(default=0.0, ge=0.0, le=1.0)
    uninspected: list[str] = Field(default_factory=list)  # must be named as not inspected


class CorpusCase(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    ticker: str
    cik: int | None = None
    as_of: date
    since: date
    why: str = Field(min_length=1)
    synthetic: bool = False
    reviewed: Review | None = None
    expected: Expected


@dataclass(frozen=True)
class ObservedFootprint:
    field: str
    period_end: date
    amended: bool
    accession: str  # the amendment's accession when amended, else the current value's
    # A derived quarter (a year-to-date or fiscal-year difference, or a sum)
    # that moved between the filings behind it while no raw fact moved
    # materially (`RestatementScan.derived`). Pinned like any footprint.
    derived: bool = False


@dataclass(frozen=True)
class Observation:
    """What a report on the case date would have said."""

    selections: dict[str, str]
    footprints: tuple[ObservedFootprint, ...]
    tier1: tuple[str, ...]
    non_reliance: tuple[str, ...]
    inspected: tuple[str, ...]
    uninspected: dict[str, str]

    @property
    def coverage(self) -> float:
        """Share of the scored fields the revision check could inspect."""
        total = len(self.inspected) + len(self.uninspected)
        return len(self.inspected) / total if total else 0.0

    @property
    def clean(self) -> bool:
        return not self.tier1 and not self.footprints


def load_case(directory: Path) -> tuple[CorpusCase, dict[str, Any], dict[str, Any] | None]:
    """A case and its payloads. The submissions index is optional."""
    case = CorpusCase.model_validate_json((directory / CASE_FILE).read_text())
    facts = json.loads((directory / FACTS_FILE).read_text())
    subs_path = directory / SUBMISSIONS_FILE
    submissions = json.loads(subs_path.read_text()) if subs_path.exists() else None
    return case, facts, submissions


def observe(
    facts: dict[str, Any],
    submissions: dict[str, Any] | None,
    ticker: str,
    as_of: date,
    since: date,
) -> Observation:
    """Run what a report dated `as_of` runs, with the report's own helpers."""
    from app.services.backtesting.events import fetch_entity_events
    from app.services.ingestion.companyfacts_mapper import build_dataset
    from app.services.ingestion.restatements import scan_restatements
    from app.services.reporting.report_builder import (
        _derived_tier1_lines,
        _pit_dates,
        _restatement_tier1_lines,
    )

    _ds, diag = build_dataset(facts, ticker, as_of=as_of)
    scan = scan_restatements(
        facts, period_since=since, as_of=as_of, selected_tags=diag.selected_series()
    )
    tier1 = list(_restatement_tier1_lines(scan.footprints)) + _derived_tier1_lines(scan.derived)
    non_reliance: list[str] = []
    if submissions is not None:
        events = fetch_entity_events(None, ticker, submissions=submissions)  # type: ignore[arg-type]
        floor = date(as_of.year - 2, as_of.month, min(as_of.day, 28))
        visible = set(_pit_dates(events.non_reliance_8k_dates, floor, as_of))
        for filed, accession, _form in events.non_reliance_8k_filings:
            if filed in visible:
                non_reliance.append(accession)
                tier1.append(f"8-K Item 4.02 non-reliance (restatement announced) filed {filed}")
    return Observation(
        selections={f.field_name: f.tag_used for f in diag.fields if f.tag_used},
        footprints=tuple(
            ObservedFootprint(
                fp.field_name, fp.period_end, fp.is_amendment,
                fp.amendment_accession if fp.is_amendment and fp.amendment_accession
                else fp.current_accession,
            )
            for fp in scan.footprints
        ) + tuple(
            ObservedFootprint(
                dr.field_name, dr.period_end, dr.is_amendment,
                next((a for form, a in dr.moved_by if form.endswith("/A")), None)
                or (dr.moved_by[0][1] if dr.moved_by else ""),
                derived=True,
            )
            for dr in scan.derived
        ),
        tier1=tuple(tier1),
        non_reliance=tuple(non_reliance),
        inspected=tuple(scan.inspected),
        uninspected=dict(scan.uninspected),
    )


@dataclass
class CaseResult:
    case: CorpusCase
    observation: Observation
    false_clean: bool
    false_alarm: bool  # a Tier-1 event on a case pinned as having none
    expected_found: int
    expected_total: int
    amended_matched: int
    amended_observed: int
    problems: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.problems


def evaluate(case: CorpusCase, obs: Observation) -> CaseResult:
    """Score one observation against its pinned expectations."""
    exp = case.expected
    problems: list[str] = []
    signal = exp.tier1 or bool(exp.footprints) or bool(exp.non_reliance)
    false_clean = signal and obs.clean
    if false_clean:
        problems.append("FALSE CLEAN: a pinned signal, and the report reads clean")
    false_alarm = not exp.tier1 and bool(obs.tier1)
    if false_alarm:
        problems.append(f"Tier-1 event(s) on a case pinned as having none: {list(obs.tier1)}")
    if exp.tier1 and not obs.tier1:
        problems.append("no Tier-1 event, but the case pins one")

    found = 0
    for want in exp.footprints:
        hit = any(
            o.field == want.field and o.period_end == want.period_end
            and (o.amended or not want.amended)
            for o in obs.footprints
        )
        if hit:
            found += 1
        else:
            problems.append(f"missed footprint: {want.field} {want.period_end}"
                            + (" (amended)" if want.amended else ""))

    pinned_amended = {(w.field, w.period_end) for w in exp.footprints if w.amended}
    observed_amended = [o for o in obs.footprints if o.amended]
    matched = sum(1 for o in observed_amended if (o.field, o.period_end) in pinned_amended)
    for o in observed_amended:
        if (o.field, o.period_end) not in pinned_amended:
            problems.append(f"unpinned amended {'derived ' if o.derived else ''}footprint: "
                            f"{o.field} {o.period_end} ({o.accession})")

    for accession in exp.non_reliance:
        if accession not in obs.non_reliance:
            problems.append(f"missed 8-K 4.02 {accession}")
    for name, tag in exp.selections.items():
        if obs.selections.get(name) != tag:
            problems.append(f"selection {name}: pinned {tag}, observed {obs.selections.get(name)}")
    if obs.coverage < exp.coverage_min:
        problems.append(f"evidence coverage {obs.coverage:.0%} below the pinned {exp.coverage_min:.0%}")
    for name in exp.uninspected:
        if name not in obs.uninspected:
            problems.append(f"{name} is pinned as not inspectable but was not named as such")

    return CaseResult(case, obs, false_clean, false_alarm, found, len(exp.footprints),
                      matched, len(observed_amended), problems)


@dataclass(frozen=True)
class Metrics:
    cases: int
    real_cases: int
    false_clean_rate: float
    restatement_recall: float
    amended_precision: float
    mean_coverage: float

    @property
    def gates_pass(self) -> bool:
        return (self.false_clean_rate == 0.0 and self.restatement_recall == 1.0
                and self.amended_precision == 1.0)

    def summary(self) -> str:
        return (
            f"corpus: {self.cases} case(s), real cases: {self.real_cases}"
            + ("" if self.real_cases else " (synthetic self-tests only — NOT validation)")
            + f"; false-clean rate {self.false_clean_rate:.0%} (gate 0%)"
            f"; restatement recall {self.restatement_recall:.0%} (gate 100%)"
            f"; amended precision {self.amended_precision:.0%} (gate 100%)"
            f"; mean evidence coverage {self.mean_coverage:.0%}"
        )


def metrics(results: list[CaseResult]) -> Metrics:
    """The four corpus metrics. An empty denominator is a perfect score:
    nothing pinned, nothing missed — the summary's case counts say how much
    that is worth."""
    with_signal = [r for r in results if r.false_clean or r.case.expected.tier1
                   or r.case.expected.footprints or r.case.expected.non_reliance]
    expected = sum(r.expected_total for r in results)
    observed_amended = sum(r.amended_observed for r in results)
    return Metrics(
        cases=len(results),
        real_cases=sum(1 for r in results if not r.case.synthetic),
        false_clean_rate=(sum(r.false_clean for r in with_signal) / len(with_signal)
                          if with_signal else 0.0),
        restatement_recall=(sum(r.expected_found for r in results) / expected
                            if expected else 1.0),
        amended_precision=(sum(r.amended_matched for r in results) / observed_amended
                           if observed_amended else 1.0),
        mean_coverage=(sum(r.observation.coverage for r in results) / len(results)
                       if results else 0.0),
    )


def draft_expectations(obs: Observation) -> Expected:
    """Expectations copied from an observation — a DRAFT for a person to
    check against the filings before pinning, never evidence by itself."""
    return Expected(
        tier1=bool(obs.tier1),
        footprints=[ExpectedFootprint(field=o.field, period_end=o.period_end, amended=o.amended)
                    for o in obs.footprints],
        non_reliance=list(obs.non_reliance),
        selections=dict(sorted(obs.selections.items())),
        coverage_min=round(obs.coverage, 2),
        uninspected=sorted(obs.uninspected),
    )
