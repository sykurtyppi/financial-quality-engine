"""Normalization shared by the brief's acceptance checks.

Every comparison in `assessment.py` and `validation.py` is exact-string
against MODEL OUTPUT. Without normalization a brief that is correct in every
way a reader cares about gets rejected for a typographic apostrophe or a
bolded table cell — and a rejected brief is retried a few times and then the
print has none. So: compare meaning, not bytes.
"""

from __future__ import annotations

import re
import unicodedata

_SMART = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"',
    "\u2013": "-", "\u2014": "-", "\u2212": "-",
    "\u00a0": " ", "\u2009": " ", "\u202f": " ",
}
_WRAPPERS = ("**", "__", "`")
_MD_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|~>])")


def normalize(text: str) -> str:
    """NFKC, ASCII quotes and dashes, markdown escapes removed, whitespace
    collapsed. Applied to both sides of every comparison."""
    text = unicodedata.normalize("NFKC", text)
    for bad, good in _SMART.items():
        text = text.replace(bad, good)
    text = _MD_ESCAPE_RE.sub(r"\1", text)
    return " ".join(text.split())


def unwrap(cell: str) -> str:
    """Strip emphasis a model may add to a table cell: `**favorable**` means
    favorable. Applied before enum and label lookups."""
    cell = normalize(cell)
    changed = True
    while changed:
        changed = False
        for w in _WRAPPERS:
            if len(cell) > 2 * len(w) and cell.startswith(w) and cell.endswith(w):
                cell, changed = cell[len(w):-len(w)].strip(), True
    return cell


def split_row(line: str) -> list[str]:
    """Raw cells of a markdown table row (escaped pipes honoured)."""
    body = line.strip().strip("|")
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", body)]


def fold_row(cells: list[str], expected: int, pivot: set[str], pivot_at: int) -> list[str]:
    # `pivot_at` is the column index of the closed-vocabulary cell in a
    # well-formed row (1 for `label | read | evidence`, 2 for
    # `# | assumption | verdict | evidence`); the free-text cell is the one
    # before it.
    """Reshape a row that has too many cells into `expected` of them.

    Two different mistakes produce a surplus and they are not distinguishable
    by position: an unescaped `|` inside the free-text cell, and an extra
    column a model added on its own. They ARE distinguishable by content,
    because one column is a closed vocabulary — the verdict, or the read. So
    find that cell, treat everything before it (after the leading fixed
    cells) as the free text, and everything after it as the trailing cell.

    Returns the cells unchanged when there is no surplus or no pivot is
    found; the caller rejects a row it cannot use.
    """
    if len(cells) <= expected:
        return cells
    idx = [i for i in range(pivot_at, len(cells) - 1) if unwrap(cells[i]).lower() in pivot]
    if not idx:
        return cells
    i = idx[-1]
    lead = cells[:pivot_at - 1]
    free = " | ".join(cells[pivot_at - 1:i])
    tail = " | ".join(cells[i + 1:])
    return [*lead, free, cells[i], tail]
