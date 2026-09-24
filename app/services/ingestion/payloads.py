"""Typed failures and validated accessors for SEC payloads.

An evidence stream that fails must say WHY truthfully: SEC was unreachable,
SEC (or a stored SEC snapshot) returned something a parser cannot read, or
this code has a defect. The report used to infer that from the Python
exception class, and the classes overlap — malformed data raises the same
AttributeError / TypeError / KeyError a bug does — so the label was a guess.

The fix is to read every payload through an accessor that CHECKS its shape
and raises `ExternalPayloadError` when the shape is wrong. After that, an
exception of any other type escaping a parser is a defect by construction,
and the report can label it as one.

Validation is structural: a level that should be a dict or list and is not,
an element of the wrong type, a date that is not a date. Wrong shapes RAISE
rather than being skipped — silently dropping a filing row could hide the
very filing a section exists to report. The one tolerance kept is per-fact
field problems inside companyfacts rows (an undated fact is dropped by the
PIT rule, not an error; a row that is not an object is dropped the same
way).

Imports nothing from other ingestion modules.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any


class ExternalPayloadError(ValueError):
    """SEC, or a stored SEC snapshot, returned a shape a parser cannot read.
    A ValueError so existing `except ValueError` callers keep working."""


class SubmissionsMismatchError(ExternalPayloadError):
    """A submissions payload belongs to another filer than the pinned CIK."""


_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _mapping(obj: object, what: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ExternalPayloadError(f"{what} is {type(obj).__name__}, expected an object")
    return obj


def _type_names(types: type | tuple[type, ...]) -> str:
    seq = types if isinstance(types, tuple) else (types,)
    return " or ".join("null" if t is type(None) else t.__name__ for t in seq)


def sec_date(value: object, what: str) -> date:
    """A strict YYYY-MM-DD date from a payload."""
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise ExternalPayloadError(f"{what} {value!r} is not a YYYY-MM-DD date")
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise ExternalPayloadError(f"{what} {value!r} is not a real date") from e


def check_aligned(columns: dict[str, list[Any]], what: str) -> int:
    """The common length of SEC parallel arrays, or ExternalPayloadError.

    The filing index is one table stored column by column: row i is
    `form[i]`, `filingDate[i]`, `accessionNumber[i]`, ... Columns of unequal
    length cannot be re-aligned — nothing says WHICH element is missing — so
    every row after the gap may pair one filing's form with another's date.
    Truncating to the shortest column (what `zip` does) silently dropped
    filings and let a section say "none found" about rows it never read
    (Hermes audit round 3, finding 2). The only honest answer is to refuse.
    """
    lengths = {name: len(col) for name, col in columns.items()}
    if len(set(lengths.values())) > 1:
        shown = ", ".join(f"{name}={n}" for name, n in lengths.items())
        raise ExternalPayloadError(f"{what} columns have unequal lengths ({shown})")
    return next(iter(lengths.values()), 0)


def recent_filings(
    submissions: object,
    *,
    optional: dict[str, type | tuple[type, ...]] | None = None,
    **columns: type | tuple[type, ...],
) -> list[tuple[Any, ...]]:
    """Rows of `filings.recent` from a submissions payload, one tuple per
    filing with the requested columns in the order given, each element
    checked against its declared type.

    A payload with no `filings` / `recent` has no filings (a new filer). A
    `recent` block that has filings but lacks a requested column, holds a
    non-list, an element of the wrong type, or columns of unequal length is
    malformed. `optional` columns follow the required ones in each tuple: an
    absent optional column reads as None in every row, but one that IS
    present must line up with the rest like any other.
    """
    subs = _mapping(submissions, "submissions payload")
    if "filings" not in subs:
        return []
    filings = _mapping(subs["filings"], "submissions.filings")
    if "recent" not in filings:
        return []
    recent = _mapping(filings["recent"], "submissions.filings.recent")
    if not recent:
        return []
    wanted: dict[str, type | tuple[type, ...]] = dict(columns)
    present: dict[str, list[Any]] = {}
    for name in list(columns) + list(optional or {}):
        if name not in recent:
            if name in columns:
                raise ExternalPayloadError(f"filings.recent has no {name!r} column")
            continue
        col = recent[name]
        if not isinstance(col, list):
            raise ExternalPayloadError(
                f"filings.recent.{name} is {type(col).__name__}, expected a list"
            )
        present[name] = col
        if name not in wanted:
            wanted[name] = (optional or {})[name]
    n = check_aligned(present, "filings.recent")
    for name, col in present.items():
        types = wanted[name]
        for i in range(n):
            if not isinstance(col[i], types):
                raise ExternalPayloadError(
                    f"filings.recent.{name}[{i}] is {type(col[i]).__name__}, "
                    f"expected {_type_names(types)}"
                )
    order = list(columns) + list(optional or {})
    cols = [present.get(name, [None] * n) for name in order]
    return list(zip(*cols, strict=True)) if cols else []


def concept_rows(facts_json: object, taxonomy: str, tag: str, unit: str) -> list[dict[str, Any]]:
    """The fact rows companyfacts holds for one concept and unit.

    Share counts some filers mis-file under USD are read from there when no
    `shares` unit exists. A concept or unit that is absent has no rows; one
    present with the wrong shape is malformed. Row-level problems are not
    errors: a row that is not an object is dropped here, and callers drop an
    undatable fact row by row.
    """
    payload = _mapping(facts_json, "companyfacts payload")
    if "facts" not in payload:
        return []
    by_taxonomy = _mapping(payload["facts"], "companyfacts.facts")
    if taxonomy not in by_taxonomy:
        return []
    concepts = _mapping(by_taxonomy[taxonomy], f"companyfacts.facts.{taxonomy}")
    concept = concepts.get(tag)
    if concept is None:
        return []
    concept = _mapping(concept, f"{taxonomy}:{tag}")
    if "units" not in concept:
        return []
    units = _mapping(concept["units"], f"{taxonomy}:{tag}.units")
    rows = units.get(unit)
    if rows is None and unit == "shares":
        rows = units.get("USD")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ExternalPayloadError(
            f"{taxonomy}:{tag} {unit} rows are {type(rows).__name__}, expected a list"
        )
    # A row that is not an object cannot be a fact; it is dropped like any
    # other unusable fact row (tests/unit/test_vintages.py pins that a
    # malformed row is skipped, not fatal).
    facts = [row for row in rows if isinstance(row, dict)]
    for i, row in enumerate(facts):
        for key in _ROW_STRING_FIELDS:
            if key not in row:
                continue
            value = row[key]
            if isinstance(value, str) or (value is None and key == "start"):
                continue
            raise ExternalPayloadError(
                f"{taxonomy}:{tag} {unit} fact {i} has {key}={value!r}, expected a string"
            )
    return facts


# The fields that identify a fact. A string that does not parse ("not-a-date")
# is an unusable fact the callers drop; a non-string is a payload SEC did not
# send in this shape, and reading it would fail far from here (a dict `end`
# is unhashable, a numeric `form` has no `.endswith`).
_ROW_STRING_FIELDS = ("start", "end", "filed", "form", "accn")
