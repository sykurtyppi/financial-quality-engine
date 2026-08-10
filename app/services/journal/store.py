"""Journal entry store — the single source of truth for the decision-impact
journal's markdown entries. Both the CLI and the web UI call this module, so an
entry opened in one is identical to one opened in the other.

The one methodological invariant lives here: a report cannot be generated until a
real BEFORE thesis exists (``has_thesis``), and once generated the entry is
stamped ``reported`` so it cannot be regenerated (no peek-then-edit).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ENTRIES = ROOT / "journal" / "entries"

IMPACT_CODES = ("changed_thesis", "changed_confidence", "new_investigation", "no_value")
VERDICTS = ("helped", "neutral", "hurt", "too_early")
CONVICTION_CHOICES = tuple(str(n) for n in range(1, 6))

# The protocol's biggest failure mode is forgetting to close the OUTCOME block
# weeks later. An entry reported this long ago without a verdict is "stale".
STALE_OUTCOME_DAYS = 14

# Fields whose template line carries a trailing "# guidance" comment. Only these
# get the comment stripped on read — never user free-text (which may contain '#').
_COMMENT_FIELDS = frozenset({"conviction", "impact", "conviction_after", "verdict"})

# Ticker must not escape the entries dir or break the filename scheme: leading
# alphanumeric (rejects "..", ".", "-"), then A-Z/0-9/./- only, max 12 chars.
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,11}$")


def safe_ticker(ticker: str) -> str:
    """Uppercased ticker restricted to a safe charset. Raises ValueError on
    anything with path separators, `..`, or that could break `TICKER_DATE.md`."""
    t = (ticker or "").strip().upper()
    if not _TICKER_RE.match(t):
        raise ValueError(f"invalid ticker: {ticker!r}")
    return t


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def entry_path(ticker: str, day: str | None = None) -> Path:
    return ENTRIES / f"{safe_ticker(ticker)}_{day or today()}.md"


def find_entry(ticker: str, day: str | None = None) -> Path | None:
    if day:
        p = entry_path(ticker, day)
        return p if p.exists() else None
    matches = sorted(ENTRIES.glob(f"{safe_ticker(ticker)}_*.md"))
    return matches[-1] if matches else None


def list_entries() -> list[Path]:
    return sorted(ENTRIES.glob("*.md")) if ENTRIES.exists() else []


def field(text: str, key: str) -> str | None:
    """Value of ``key:`` on its OWN line. Uses ``[ \\t]*`` (not ``\\s*``) so a blank
    field does not bleed into the next line's text. The template's ``# guidance``
    comment is stripped ONLY for the enum/numeric fields that carry one — never
    from user free-text, which may legitimately contain ``#``."""
    m = re.search(rf"^{re.escape(key)}:[ \t]*(.*)$", text, re.MULTILINE)
    if not m:
        return None
    val = m.group(1)
    if key in _COMMENT_FIELDS:
        val = val.split("#")[0]
    val = val.strip()
    return val or None


def has_thesis(text: str) -> bool:
    t = field(text, "thesis")
    return bool(t) and not t.startswith("<")


def is_reported(text: str) -> bool:
    # A value on the SAME line only — `\s` would span the newline into the next
    # heading and false-trip on a fresh entry.
    return bool(re.search(r"^reported:[ \t]*\S", text, re.MULTILINE))


def days_since(iso_ts: str | None, now: datetime | None = None) -> int | None:
    """Whole days elapsed since ``iso_ts`` (as written by ``now_iso``), or None if
    ``iso_ts`` is missing/unparseable. ``now`` is injectable for tests."""
    if not iso_ts:
        return None
    try:
        dt = datetime.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return ((now or datetime.now(timezone.utc)) - dt).days


def open_entry(
    ticker: str,
    thesis: str | None = None,
    conviction: int | str | None = None,
    action: str | None = None,
) -> Path:
    """Create a new entry. Raises ValueError on a bad ticker, FileExistsError if
    one already exists for today."""
    t = safe_ticker(ticker)
    ENTRIES.mkdir(parents=True, exist_ok=True)
    path = entry_path(t)
    if path.exists():
        raise FileExistsError(path)
    # Whitespace-only thesis -> placeholder, so the report lock still refuses it
    # (unifies CLI and web, which both must treat a blank thesis as "no thesis").
    if thesis is not None and not thesis.strip():
        thesis = None
    thesis = thesis or "<one or two sentences: your view BEFORE reading the report>"
    conviction = conviction if conviction is not None else "<1-5>"
    action = action or "<hold / trim / add / avoid / no position>"
    path.write_text(
        f"# {t} — {today()}\n"
        f"opened: {now_iso()}\n"
        f"reported:\n\n"
        f"## BEFORE  (write before reading the report)\n"
        f"thesis: {thesis}\n"
        f"conviction: {conviction}        # 1 (low) - 5 (high)\n"
        f"intended_action: {action}\n\n"
        f"## AFTER  (fill after reading the report)\n"
        f"impact:                         # any of: {', '.join(IMPACT_CODES)}\n"
        f"conviction_after:               # 1-5\n"
        f"what_it_surfaced:\n"
        f"what_i_disagreed_with:\n\n"
        f"## OUTCOME  (fill weeks later)\n"
        f"outcome_date:\n"
        f"what_happened:\n"
        f"verdict:                        # helped / neutral / hurt / too_early\n"
    )
    return path


def mark_reported(path: Path) -> None:
    text = path.read_text()
    text = re.sub(r"^reported:[ \t]*$", f"reported: {now_iso()}", text, count=1, flags=re.MULTILINE)
    path.write_text(text)


def set_field(path: Path, key: str, value: str) -> None:
    """Write ``key: value`` on the field's single line. Newlines in ``value`` are
    collapsed to spaces so a multiline textarea can't break the one-field-per-line
    structure the parser assumes. A function replacement is used so backslashes or
    ``\\g`` sequences in user text are treated literally, not as regex refs."""
    value = re.sub(r"\s*\n\s*", " ", value).strip()
    text = path.read_text()
    new, n = re.subn(rf"^{re.escape(key)}:.*$", lambda _m: f"{key}: {value}", text,
                     count=1, flags=re.MULTILINE)
    if n:
        path.write_text(new)


def parse_entry(path: Path) -> dict:
    text = path.read_text()
    impact = field(text, "impact")
    return {
        "name": path.stem,
        "ticker": path.stem.split("_")[0],
        "day": path.stem.split("_", 1)[1] if "_" in path.stem else "",
        "opened": field(text, "opened"),
        "reported": field(text, "reported"),
        "thesis": field(text, "thesis"),
        "conviction": field(text, "conviction"),
        "intended_action": field(text, "intended_action"),
        "impact": impact,
        "conviction_after": field(text, "conviction_after"),
        "what_it_surfaced": field(text, "what_it_surfaced"),
        "what_i_disagreed_with": field(text, "what_i_disagreed_with"),
        "outcome_date": field(text, "outcome_date"),
        "what_happened": field(text, "what_happened"),
        "verdict": field(text, "verdict"),
        "has_thesis": has_thesis(text),
        "is_reported": is_reported(text),
        "needs_after": impact is None,
        "needs_outcome": field(text, "verdict") is None,
        "days_since_reported": days_since(field(text, "reported")),
    }


# ---------------------------------------------------------------------------
# v2 storage (P1-E). Same TICKER_DATE.md filename convention; entries are told
# apart by the ---json front-matter fence. v1 entries continue to parse via the
# functions above; a mixed journal is fine.
# ---------------------------------------------------------------------------


def is_v2(path: Path) -> bool:
    """True iff the file starts with the v2 front-matter fence."""
    try:
        with path.open("r", encoding="utf-8") as f:
            return f.readline().rstrip("\n") == "---json"
    except OSError:
        return False


def save_v2(entry, path: Path | None = None, *, allow_update: bool = False) -> Path:
    """Serialize a v2 entry to disk. Defaults to `journal/entries/TICKER_DAY.md`.

    Round-12 finding 1 (the whole tamper-evidence guarantee): by DEFAULT this
    refuses to overwrite ANY existing entry — v1 or v2. Repeating `openv2` for
    the same TICKER_DATE used to silently replace the earlier preregistration
    with a new hash, which broke the "no peek-then-edit" protocol at the
    filesystem layer (the on-disk file passed `verify_lock` because it carried
    a fresh matching hash — the ORIGINAL commitment was simply gone).

    Legitimate updates (persisting a new resolution via `resolve --commit`;
    stamping `reported` from the report step) pass `allow_update=True`. Those
    call sites must not change the BEFORE block, and the guard verifies that:
    the incoming entry's `before_sha256` MUST equal the on-disk entry's.
    Anything else is refused.
    """
    from app.services.journal.schema_v2 import render_entry  # avoid import cycle at load

    target = path or entry_path(entry.ticker, entry.day.isoformat())
    if target.exists():
        if not is_v2(target):
            raise FileExistsError(
                f"v1 entry exists at {target}; refusing to overwrite. Move or delete it first."
            )
        if not allow_update:
            raise FileExistsError(
                f"v2 entry already exists at {target}; refusing to overwrite. "
                "A locked preregistration cannot be replaced (would erase the "
                "tamper-evidence guarantee). Use a different --date, delete "
                "the file explicitly, or update in place via `resolve`/`report`."
            )
        # Update path: BEFORE must match the on-disk hash. This defends against
        # any caller that accidentally passes allow_update=True with a modified
        # BEFORE block. Compares hashes (not full BEFORE) to avoid loading a
        # possibly corrupt on-disk BEFORE into a pydantic model.
        try:
            existing = load_v2(target)
        except (ValueError, OSError) as e:  # noqa: BLE001 - guard, not silent-swallow
            raise ValueError(f"cannot update {target}: existing file unreadable ({e})") from e
        if existing.before_sha256 != entry.before_sha256 or entry.before_sha256 is None:
            existing_h = (existing.before_sha256 or "")[:16] or "<unset>"
            new_h = (entry.before_sha256 or "")[:16] or "<unset>"
            raise ValueError(
                f"refusing to update {target}: BEFORE hash changed "
                f"({existing_h} -> {new_h}). Updates must preserve the locked BEFORE block."
            )
    ENTRIES.mkdir(parents=True, exist_ok=True)
    target.write_text(render_entry(entry), encoding="utf-8")
    return target


def load_v2(path: Path):
    """Read a v2 entry file. Raises ValueError on a v1 entry (mixed cases should
    dispatch via is_v2 first)."""
    from app.services.journal.schema_v2 import parse_entry

    return parse_entry(path.read_text(encoding="utf-8"))


# Brier / calibration threshold from VALIDATION_STRATEGY §4 (Murphy decomposition
# floor): below this many resolved p_outcome samples, report raw tallies only —
# no calibration claim.
BRIER_MIN_N = 50


def v2_tally(today_iso: str | None = None) -> dict:
    """Aggregate stats for v2 entries. v1 entries are ignored here (they're
    tallied by the v1 `tally()` above).

    Round-10 hardening:
      - **finding 2**: broken/unlocked entries are counted for AUDIT
        (`total`, `lock_broken`) but EXCLUDED from every inferential metric
        (resolutions, open queue, overdue queue, Brier). A tampered
        preregistration cannot be evidence.
      - **finding 3**: Brier scores `p_outcome` against `OutcomeBlock.y` (the
        observed binary outcome the user preregistered), NOT the AFTER
        `verdict` (which scored engine usefulness). Also requires
        `before.outcome_definition` — a Brier without a preregistered event
        definition is not a probability score.
      - **finding 4**: `pending` resolutions don't count as terminal; only
        met/violated/unresolvable populate the resolutions histogram.

    Fields:
      total, locked, lock_broken, resolutions{met,violated,unresolvable,pending},
      open_assumptions, overdue_assumptions (past resolve_by, unresolved),
      resolved_with_p_outcome, brier (None below the Murphy floor or without y).
    """
    from datetime import date as _date

    from app.services.journal.schema_v2 import (
        add_resolution,  # noqa: F401 - kept exported through this module
        open_assumption_indices,
        verify_lock,
    )

    import sys as _sys  # local import — do not couple module load to stderr

    today = _date.fromisoformat(today_iso) if today_iso else _date.today()
    v2_paths = [p for p in list_entries() if is_v2(p)]

    # Round-12 finding 2: a single corrupt v2 file used to crash the whole
    # `tally` command via unhandled ValidationError. Now every file is loaded
    # individually and parse failures are counted, not fatal.
    entries = []
    parse_failed: list[str] = []
    for p in v2_paths:
        try:
            entries.append(load_v2(p))
        except Exception as e:  # noqa: BLE001 - a bad file must not kill the aggregate
            parse_failed.append(p.name)
            print(f"warning: v2 entry {p.name} could not be parsed ({e})", file=_sys.stderr)

    total = len(entries)
    locked = sum(1 for e in entries if verify_lock(e))
    lock_broken = sum(1 for e in entries if e.before_sha256 and not verify_lock(e))
    # Only legitimately-locked entries contribute to inferential metrics.
    trusted = [e for e in entries if verify_lock(e)]

    res_counts = {"met": 0, "violated": 0, "unresolvable": 0, "pending": 0}
    open_assumptions = 0
    overdue: list[tuple[str, str, str]] = []  # (ticker, day, metric)
    for e in trusted:
        for r in e.resolutions:
            res_counts[r.state] = res_counts.get(r.state, 0) + 1
        for i in open_assumption_indices(e):
            open_assumptions += 1
            a = e.before.assumptions[i]
            if a.resolve_by <= today:
                overdue.append((e.ticker, e.day.isoformat(), a.metric))

    # Brier: preregistered probability of a preregistered binary event, scored
    # against the observed y. A user must have supplied BOTH
    # `outcome_definition` (BEFORE) and `y` (OUTCOME) — otherwise the Brier
    # score has no defined target and we withhold it.
    pairs: list[tuple[float, int]] = []
    for e in trusted:
        if not e.before.outcome_definition:
            continue
        p, y = e.before.p_outcome, e.outcome.y
        if p is None or y is None:
            continue
        pairs.append((p, 1 if y else 0))

    brier = (
        sum((p - y) ** 2 for p, y in pairs) / len(pairs)
        if len(pairs) >= BRIER_MIN_N
        else None
    )

    return {
        "total": total,
        "locked": locked,
        "lock_broken": lock_broken,
        "parse_failed": parse_failed,
        "resolutions": res_counts,
        "open_assumptions": open_assumptions,
        "overdue_assumptions": overdue,
        "resolved_with_p_outcome": len(pairs),
        "brier": brier,
        "brier_min_n": BRIER_MIN_N,
    }


def tally() -> dict:
    # v2 entries are counted separately by `v2_tally()`; the v1 markdown parser
    # can't read a JSON front-matter file (would yield an entry with all fields
    # None and quietly inflate v1 stats).
    entries = [parse_entry(p) for p in list_entries() if not is_v2(p)]
    total = len(entries)
    scored = [e for e in entries if e["impact"]]
    impact_counts = {code: sum(1 for e in scored if code in (e["impact"] or "")) for code in IMPACT_CODES}
    verdicts: dict[str, int] = {}
    conv_moved = conv_same = 0
    for e in entries:
        if e["verdict"]:
            verdicts[e["verdict"]] = verdicts.get(e["verdict"], 0) + 1
        cb, ca = e["conviction"], e["conviction_after"]
        if cb and ca and cb.isdigit() and ca.isdigit():
            conv_moved += int(cb) != int(ca)
            conv_same += int(cb) == int(ca)
    changed_any = len(scored) - impact_counts["no_value"] if scored else 0
    # AFTER already filled but no verdict yet, oldest first — the "don't forget to
    # close this out" queue. Must require `impact` too: a reported case whose AFTER
    # block is still blank needs THAT step first, not an outcome (needs_outcome
    # alone is true for both states and would conflate them).
    awaiting_outcome = sorted(
        (e for e in entries if e["is_reported"] and e["impact"] and e["needs_outcome"]),
        key=lambda e: e["days_since_reported"] or 0,
        reverse=True,
    )
    return {
        "entries": entries,
        "total": total,
        "scored": len(scored),
        "with_outcome": sum(1 for e in entries if e["verdict"]),
        "impact_counts": impact_counts,
        "changed_any": changed_any,
        "conv_moved": conv_moved,
        "conv_same": conv_same,
        "verdicts": verdicts,
        "awaiting_outcome": awaiting_outcome,
        "oldest_awaiting_days": awaiting_outcome[0]["days_since_reported"] if awaiting_outcome else None,
        "stale_outcome_days": STALE_OUTCOME_DAYS,
        "gate_ready": total >= 20 and sum(1 for e in entries if e["verdict"]) >= 15,
    }
