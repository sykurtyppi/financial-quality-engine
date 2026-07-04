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


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def entry_path(ticker: str, day: str | None = None) -> Path:
    return ENTRIES / f"{ticker.upper()}_{day or today()}.md"


def find_entry(ticker: str, day: str | None = None) -> Path | None:
    if day:
        p = entry_path(ticker, day)
        return p if p.exists() else None
    matches = sorted(ENTRIES.glob(f"{ticker.upper()}_*.md"))
    return matches[-1] if matches else None


def list_entries() -> list[Path]:
    return sorted(ENTRIES.glob("*.md")) if ENTRIES.exists() else []


def field(text: str, key: str) -> str | None:
    """Value of ``key:`` on its own line, with any trailing ``# comment`` stripped."""
    m = re.search(rf"^{re.escape(key)}:\s*(.*)$", text, re.MULTILINE)
    if not m:
        return None
    val = m.group(1).split("#")[0].strip()
    return val or None


def has_thesis(text: str) -> bool:
    t = field(text, "thesis")
    return bool(t) and not t.startswith("<")


def is_reported(text: str) -> bool:
    # A value on the SAME line only — `\s` would span the newline into the next
    # heading and false-trip on a fresh entry.
    return bool(re.search(r"^reported:[ \t]*\S", text, re.MULTILINE))


def open_entry(
    ticker: str,
    thesis: str | None = None,
    conviction: int | str | None = None,
    action: str | None = None,
) -> Path:
    """Create a new entry. Raises FileExistsError if one already exists for today."""
    ENTRIES.mkdir(parents=True, exist_ok=True)
    path = entry_path(ticker)
    if path.exists():
        raise FileExistsError(path)
    thesis = thesis or "<one or two sentences: your view BEFORE reading the report>"
    conviction = conviction if conviction is not None else "<1-5>"
    action = action or "<hold / trim / add / avoid / no position>"
    path.write_text(
        f"# {ticker.upper()} — {today()}\n"
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
    """Replace the same-line value for ``key`` (dropping any guiding comment)."""
    text = path.read_text()
    new, n = re.subn(rf"^{re.escape(key)}:.*$", f"{key}: {value}", text, count=1, flags=re.MULTILINE)
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
    }


def tally() -> dict:
    entries = [parse_entry(p) for p in list_entries()]
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
        "gate_ready": total >= 20 and sum(1 for e in entries if e["verdict"]) >= 15,
    }
