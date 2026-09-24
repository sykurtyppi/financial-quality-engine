"""Which of several filed facts for one period is the current one.

Every module that asks "what does this period stand at?" — the mapper that
scores it, the restatement check, the vintage store, the journal resolver —
must answer the same way, or the evidence describes a different number from
the one scored. This is that answer:

1. the later `filed` date wins;
2. on one date, an amendment (`…/A`) supersedes an original — an amendment
   corrects a filing that already exists, so it cannot precede it;
3. on one date and level, the higher accession number wins. Accessions are
   `<filer-agent>-<yy>-<sequence>`, so this is submission order when one
   agent made both filings and merely deterministic otherwise;
4. a fact identical on all three keeps its place in SEC's order (the first).

Companyfacts records a filing DATE, not the acceptance time, so steps 2-4
stand in for an ordering the payload does not carry. The filing index has
`acceptanceDateTime`, but the vintage store, historical replay and backtests
hold only companyfacts — using it on the live path alone would make the live
report disagree with every other reading of the same facts. Where two facts
share a date and level but differ in value, the choice between them is
arbitrary by construction; `conflicts` finds them so callers can say so
instead of choosing silently.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date

Level = tuple[date, bool]
Rank = tuple[date, bool, str]


def is_amendment(form: str) -> bool:
    # A payload's form is not guaranteed to be a string; anything else is
    # not an amendment rather than a crash.
    return isinstance(form, str) and form.endswith("/A")


def level(filed: date, form: str) -> Level:
    """Filing date and amendment status: the part of the order the SEC's own
    data establishes. Facts sharing a level are ordered only by convention."""
    return (filed, is_amendment(form))


def rank(filed: date, form: str, accession: str) -> Rank:
    return (filed, is_amendment(form), accession if isinstance(accession, str) else str(accession))


def latest[T](items: Iterable[T], key: Callable[[T], Rank]) -> T:
    """The current fact: highest rank, the FIRST such on a full tie. Raises
    ValueError on an empty iterable, as max() does."""
    found: list[T] = []
    best_rank: Rank | None = None
    for item in items:
        r = key(item)
        if best_rank is None or r > best_rank:
            found[:] = [item]
            best_rank = r
    if not found:
        raise ValueError("latest() of an empty sequence")
    return found[0]


def earliest[T](items: Iterable[T], key: Callable[[T], Rank]) -> T:
    """The originally reported fact: lowest rank, the FIRST such on a tie."""
    found: list[T] = []
    best_rank: Rank | None = None
    for item in items:
        r = key(item)
        if best_rank is None or r < best_rank:
            found[:] = [item]
            best_rank = r
    if not found:
        raise ValueError("earliest() of an empty sequence")
    return found[0]


def conflicts[T](
    items: Iterable[T], key: Callable[[T], Rank], value: Callable[[T], float]
) -> list[list[T]]:
    """Groups of facts that share a level (date and amendment status) but
    report different values — the places where which one counts is a
    convention, not something the filings establish. In level order."""
    by_level: dict[Level, list[T]] = {}
    for item in items:
        r = key(item)
        by_level.setdefault((r[0], r[1]), []).append(item)
    return [
        group for _lvl, group in sorted(by_level.items(), key=lambda kv: kv[0])
        if len({value(i) for i in group}) > 1
    ]


def current_conflict[T](
    items: Iterable[T], key: Callable[[T], Rank], value: Callable[[T], float]
) -> list[T]:
    """The facts at the CURRENT level — the one `latest` picks from — when
    they disagree in value, else []. Only this level decides the value a
    period stands at; a disagreement in an older filing does not."""
    pool = list(items)
    if not pool:
        return []
    top = key(latest(pool, key))[:2]
    at_top = [i for i in pool if key(i)[:2] == top]
    return at_top if len({value(i) for i in at_top}) > 1 else []
