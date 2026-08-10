"""Decision-journal schema v2 (P1-E).

Preregistration schema per VALIDATION_STRATEGY §5 and calibration_journal_survey
2026Q3. Key design constraints, from evidence:
- SPECIFICITY is the active ingredient (Brodeur 2024: bare preregistration does
  nothing; detailed plans do). Machine-checkable assumption rows enforce it.
- ONE concrete falsifier per rival hypothesis cuts bias (Arkes 1988: 58% -> 41%).
  Encouraged, not required (anti-annoyance).
- Don't force-snap conviction to coarse buckets (Mellers): keep 1-5 required and
  offer an optional 0-100 fine grain.
- Contamination is first-class: the 2026Q2 season showed lock-time contamination
  needs a home so the AFTER block measures ENGINE impact, not blended discussion.
- No literature shows journals improve outcomes -> the journal is the experiment
  itself; the schema stays honest about that.

Anti-annoyance rule (VALIDATION_STRATEGY §5): only `thesis + conviction + ONE
assumption row` are required to lock. Everything else is optional-but-nagged. A
journal too heavy to use produces n=0, which is worse than imperfect entries.

Storage format: JSON front-matter (stdlib) + markdown body (free-text thesis and
optional AFTER/OUTCOME prose). One file per entry, plus a lock hash INSIDE the
front-matter that `verify_lock` recomputes on read to detect BEFORE-block tamper.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator

SCHEMA_VERSION = 2

Comparator = Literal[">", "<", ">=", "<=", "==", "within"]
IntendedAction = Literal["hold", "trim", "add", "avoid", "no_position"]
Impact = Literal["changed_thesis", "changed_confidence", "new_investigation", "no_value"]
Verdict = Literal["helped", "neutral", "hurt", "too_early"]
ResolutionState = Literal["met", "violated", "unresolvable"]
SourceForm = Literal["10-K", "10-Q", "8-K", "10-K/A", "10-Q/A", "proxy", "other"]


class Assumption(BaseModel):
    """A machine-checkable claim the user is willing to be wrong about. The
    assumption resolves to met / violated / unresolvable when the named source
    filing arrives (auto-checkable when `metric` is an engine spec_id)."""

    metric: str = Field(min_length=1, description="XBRL concept or engine spec_id (e.g., 'revenue', 'cfo_to_net_income')")
    comparator: Comparator
    threshold: float | str = Field(description="Numeric threshold, or a symbolic one like 'positive'")
    window: str = Field(min_length=1, description="Fiscal window, e.g. FY2026Q2 or 'trailing_4q'")
    source: SourceForm = Field(description="Which filing type will resolve it")
    resolve_by: date = Field(description="Date by which the resolving filing is expected")


class BeforeBlock(BaseModel):
    """Everything the user commits to BEFORE reading the engine report. Locked
    by hash so it cannot be edited after `reported:` is stamped."""

    thesis: str = Field(min_length=1, description="Free-text thesis (unchanged from v1)")
    conviction: int = Field(ge=1, le=5, description="1 (low) - 5 (high)")
    conviction_fine: int | None = Field(
        default=None, ge=0, le=100,
        description="Optional 0-100 fine grain (Mellers: don't force-snap to buckets)",
    )
    intended_action: IntendedAction
    catalyst: str | None = Field(
        default=None,
        description="Expected event + date, e.g. 'Q2 print 2026-07-28'",
    )
    contamination: str | None = Field(
        default=None,
        description=(
            "Analysis/discussion that PRECEDED this entry. First-class field "
            "(2026Q2 season lesson): if not blank, the AFTER block should be "
            "read as measuring engine impact NET of that context."
        ),
    )
    p_outcome: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="Subjective probability of the named outcome; enables Brier scoring",
    )
    reference_class: str | None = Field(
        default=None,
        description="Which base rate applies (free text + optional engine ref)",
    )
    assumptions: list[Assumption] = Field(
        default_factory=list,
        description="Preregistered claims — >= 1 required to lock (specificity floor)",
    )
    falsifiers: list[str] = Field(
        default_factory=list,
        description='"I am wrong if <concrete observable>" — encouraged (Arkes 1988)',
    )

    @field_validator("falsifiers")
    @classmethod
    def _falsifiers_nonblank(cls, v: list[str]) -> list[str]:
        cleaned = [s.strip() for s in v if s and s.strip()]
        return cleaned


class Resolution(BaseModel):
    """Per-assumption outcome, auto-checkable when the metric is an engine
    spec_id (engine proposes met/violated, user confirms)."""

    assumption_index: int = Field(ge=0)
    state: ResolutionState
    observed: float | str | None = None
    at: date | None = None
    source_accession: str | None = None
    note: str | None = None


class AfterBlock(BaseModel):
    """Filled after reading the engine report — measures ENGINE impact only."""

    impact: Impact | None = None
    conviction_after: int | None = Field(default=None, ge=1, le=5)
    what_it_surfaced: str | None = None
    what_i_disagreed_with: str | None = None


class OutcomeBlock(BaseModel):
    """Filled weeks later when the catalyst / resolve_by dates land."""

    outcome_date: date | None = None
    what_happened: str | None = None
    verdict: Verdict | None = None
    brier: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="(p_outcome - y)^2 when both p_outcome and a binary y are present",
    )


class EntryV2(BaseModel):
    """One journal entry — the atomic unit of decision-impact evidence."""

    schema_version: int = SCHEMA_VERSION
    ticker: str
    day: date
    opened: datetime
    reported: datetime | None = None
    locked_at: datetime | None = None
    before_sha256: str | None = Field(
        default=None, description="Canonical sha256 of the BEFORE block at lock time"
    )
    before: BeforeBlock
    after: AfterBlock = Field(default_factory=AfterBlock)
    outcome: OutcomeBlock = Field(default_factory=OutcomeBlock)
    resolutions: list[Resolution] = Field(default_factory=list)

    @field_validator("ticker")
    @classmethod
    def _ticker_upper(cls, v: str) -> str:
        return v.strip().upper()


# ---------------------------------------------------------------------------
# Lock / verify — the tamper-detection layer
# ---------------------------------------------------------------------------


def can_lock(before: BeforeBlock) -> tuple[bool, str | None]:
    """Anti-annoyance: only thesis + conviction + >=1 assumption row required.
    pydantic already enforces thesis non-empty and conviction in [1, 5]."""
    if not before.assumptions:
        return False, "at least one assumption row is required to lock (specificity floor)"
    return True, None


def _canonical_before(before: BeforeBlock) -> str:
    """Sorted-keys JSON with default str conversion for dates — deterministic
    across saves so the same BEFORE always hashes the same."""
    data = json.loads(before.model_dump_json())
    return json.dumps(data, sort_keys=True, default=str, separators=(",", ":"))


def hash_before(before: BeforeBlock) -> str:
    return hashlib.sha256(_canonical_before(before).encode("utf-8")).hexdigest()


def lock_entry(entry: EntryV2, now: datetime | None = None) -> EntryV2:
    """Return a locked copy: BEFORE hash computed and `locked_at` / `reported`
    stamped. Raises ValueError if the lock rule is not met."""
    ok, reason = can_lock(entry.before)
    if not ok:
        raise ValueError(f"cannot lock: {reason}")
    ts = now or datetime.now(timezone.utc)
    return entry.model_copy(update={
        "before_sha256": hash_before(entry.before),
        "locked_at": ts,
        "reported": entry.reported or ts,
    })


def is_locked(entry: EntryV2) -> bool:
    return entry.before_sha256 is not None and entry.locked_at is not None


def verify_lock(entry: EntryV2) -> bool:
    """False iff the BEFORE block was edited after locking (tamper detection).
    An unlocked entry returns False (nothing to verify against)."""
    if not is_locked(entry):
        return False
    return hash_before(entry.before) == entry.before_sha256


# ---------------------------------------------------------------------------
# Serialization: JSON front-matter + markdown body
# ---------------------------------------------------------------------------

_FRONT_MATTER_RE = re.compile(r"\A---json\n(.*?)\n---\n?", re.DOTALL)


def render_entry(entry: EntryV2) -> str:
    """Serialize as a markdown file: ---json front-matter + human-readable body.

    The front-matter carries every structured field (source of truth for the
    machine); the body carries the thesis prose and optional AFTER/OUTCOME
    narrative for humans reading in an editor or web UI.
    """
    fm = entry.model_dump(mode="json", exclude_none=False)
    header = json.dumps(fm, indent=2, sort_keys=True, default=str)

    body_lines = [
        f"# {entry.ticker} — {entry.day.isoformat()}",
        "",
        "## Thesis (BEFORE)",
        "",
        entry.before.thesis.rstrip(),
        "",
    ]
    if entry.after.what_it_surfaced or entry.after.what_i_disagreed_with:
        body_lines += ["## AFTER (free text)", ""]
        if entry.after.what_it_surfaced:
            body_lines += [f"**Surfaced:** {entry.after.what_it_surfaced}", ""]
        if entry.after.what_i_disagreed_with:
            body_lines += [f"**Disagreed:** {entry.after.what_i_disagreed_with}", ""]
    if entry.outcome.what_happened:
        body_lines += ["## OUTCOME (free text)", "", entry.outcome.what_happened.rstrip(), ""]

    return f"---json\n{header}\n---\n\n" + "\n".join(body_lines)


def parse_entry(text: str) -> EntryV2:
    """Parse a v2 entry file. Raises ValueError if the front-matter is missing
    or malformed (v1 markdown-only entries are handled by the v1 parser)."""
    m = _FRONT_MATTER_RE.match(text)
    if not m:
        raise ValueError("v2 entry missing ---json front-matter (is this a v1 entry?)")
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise ValueError(f"v2 front-matter is not valid JSON: {e}") from e
    return EntryV2.model_validate(data)
