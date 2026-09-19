"""Append-only companyfacts vintage store, and the diff over it (P1-F Tier 2).

`detect_restatements` recovers a revision WITHIN one companyfacts document:
when two filings both present the same period, the difference is visible
today. The quieter case leaves nothing to compare — a company revises a prior
figure and simply does not re-present the original, so companyfacts holds only
the new value and the change is invisible from any single fetch, forever.

The only way to see it is to have kept what the number used to be. So this
stores the raw document, unmodified, each time it changes, and diffs one
vintage against another. Nothing here can be back-filled: a quarter that goes
uncaptured is gone, which is why capture runs from the sweep from the day it
lands rather than waiting for the surfacing work.

Layout (gitignored — it is bulk source data, ~0.3 MB gzipped per snapshot):

    data/vintages/CIK##########/manifest.json
    data/vintages/CIK##########/<YYYY-MM-DD>.json.gz

The manifest records every snapshot with its sha256, and the last date the
source was checked at all — so an unchanged document costs one fetch a day,
not one an hour, and "we looked and it was identical" stays distinguishable
from "we never looked".
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from app.services.ingestion.companyfacts_mapper import (
    FLOW_FIELDS,
    INSTANT_FIELDS,
    _parse_date,
    _unit_for,
)
from app.services.ingestion.restatements import SPLIT_ADJUSTED_FIELDS

ROOT = Path(__file__).resolve().parents[3]
VINTAGES = ROOT / "data" / "vintages"
MANIFEST = "manifest.json"
# Same floor as the within-snapshot detector, so the two tiers speak in one
# voice about what counts as a revision.
DEFAULT_MATERIALITY_PCT = 0.005


def cik_dir(cik: int, root: Path | None = None) -> Path:
    return (root or VINTAGES) / f"CIK{int(cik):010d}"


def _manifest_path(cik: int, root: Path | None = None) -> Path:
    return cik_dir(cik, root) / MANIFEST


def read_manifest(cik: int, root: Path | None = None) -> dict:
    """{"last_checked": "YYYY-MM-DD"|None, "snapshots": [{captured, sha256, file, bytes}]}.
    A missing or unreadable manifest reads as empty — the snapshots on disk are
    the real record; the manifest is an index over them."""
    try:
        data = json.loads(_manifest_path(cik, root).read_text())
    except (OSError, ValueError):
        return {"last_checked": None, "snapshots": []}
    if not isinstance(data, dict) or not isinstance(data.get("snapshots"), list):
        return {"last_checked": None, "snapshots": []}
    data.setdefault("last_checked", None)
    return data


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class Capture:
    """What one capture attempt did. `path` is None when nothing was written."""

    cik: int
    checked: date
    path: Path | None
    sha256: str
    reason: str  # "captured" | "unchanged" | "already checked today"

    @property
    def wrote(self) -> bool:
        return self.path is not None


def capture(
    client,
    ticker: str,
    *,
    now: datetime | None = None,
    root: Path | None = None,
    force: bool = False,
) -> Capture:
    """Snapshot `ticker`'s companyfacts if it has changed since the last one.

    Once a day per name unless `force`: the document changes on filing days,
    not hourly, and the sweep calls this on every pass. Identical content is
    never stored twice — the manifest records that we looked.
    """
    now = now or datetime.now(timezone.utc)
    today = now.date()
    cik = client.resolve_cik(ticker)
    man = read_manifest(cik, root)
    if not force and man.get("last_checked") == today.isoformat():
        newest = man["snapshots"][-1]["sha256"] if man["snapshots"] else ""
        return Capture(cik, today, None, newest, "already checked today")

    raw = json.dumps(client.company_facts_by_cik(cik), sort_keys=True,
                     separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    man["last_checked"] = today.isoformat()
    if man["snapshots"] and man["snapshots"][-1]["sha256"] == digest:
        _write_json_atomic(_manifest_path(cik, root), man)
        return Capture(cik, today, None, digest, "unchanged")

    out = cik_dir(cik, root) / f"{today.isoformat()}.json.gz"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    try:
        # mtime=0: the archive bytes depend only on the content, so an
        # unchanged document cannot look changed to anything comparing files.
        with gzip.GzipFile(filename="", mode="wb", fileobj=tmp.open("wb"), mtime=0) as fh:
            fh.write(raw)
        os.replace(tmp, out)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    man["snapshots"] = [s for s in man["snapshots"] if s.get("file") != out.name]
    man["snapshots"].append({
        "captured": today.isoformat(), "sha256": digest,
        "file": out.name, "bytes": out.stat().st_size,
    })
    man["snapshots"].sort(key=lambda s: (s["captured"], s["file"]))
    _write_json_atomic(_manifest_path(cik, root), man)
    return Capture(cik, today, out, digest, "captured")


def list_vintages(cik: int, root: Path | None = None) -> list[Path]:
    """Snapshots oldest first, taken from disk rather than the manifest so a
    lost index never hides data that is still there."""
    d = cik_dir(cik, root)
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.json.gz") if not p.name.startswith("."))


def load_vintage(path: Path) -> dict:
    with gzip.open(path, "rb") as fh:
        return json.loads(fh.read())


# --------------------------------------------------------------------------
# Diff


@dataclass(frozen=True)
class FactKey:
    taxonomy: str
    tag: str
    unit: str
    start: date | None
    end: date

    @property
    def period(self) -> str:
        return f"{self.start} → {self.end}" if self.start else str(self.end)


@dataclass(frozen=True)
class VintageChange:
    """One prior-period figure that moved between two snapshots.

    `kind` is `revised` (the scored value changed) or `withdrawn` (the fact is
    gone from the later document). Both are invisible to the within-snapshot
    detector when the filer does not re-present the original.
    """

    kind: str
    field_name: str
    key: FactKey
    old_value: float
    old_filed: date | None
    old_accession: str
    old_form: str
    new_value: float | None = None
    new_filed: date | None = None
    new_accession: str = ""
    new_form: str = ""
    pct_change: float | None = None


def _scored_tags(include_split_adjusted: bool = False) -> dict[tuple[str, str], tuple[str, str]]:
    """(taxonomy, tag) -> (field_name, unit) for every field the engine scores.

    Share counts are excluded by default, for the reason the within-snapshot
    detector excludes them: a stock split retroactively rewrites every prior
    share count, which is a corporate action and not a revision. Measured on
    the first real capture — NVDA's June 2024 ten-for-one split produced four
    "revisions" of exactly 900%, and they were the only findings in the file.
    That is precisely the noise this store would otherwise drown in.
    """
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for field_name, candidates in {**INSTANT_FIELDS, **FLOW_FIELDS}.items():
        if not include_split_adjusted and field_name in SPLIT_ADJUSTED_FIELDS:
            continue
        unit = _unit_for(field_name)
        for taxonomy, tag in candidates:
            out.setdefault((taxonomy, tag), (field_name, unit))
    return out


def _latest_by_key(facts: dict, scored_only: bool,
                   include_split_adjusted: bool = False) -> dict[FactKey, dict]:
    """The value each (tag, period) currently stands at — latest filed wins,
    exactly what the mapper scores."""
    wanted = _scored_tags(include_split_adjusted)
    out: dict[FactKey, dict] = {}
    for taxonomy, tags in (facts.get("facts") or {}).items():
        if not isinstance(tags, dict):
            continue
        for tag, concept in tags.items():
            if scored_only and (taxonomy, tag) not in wanted:
                continue
            for unit, rows in ((concept or {}).get("units") or {}).items():
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    try:
                        end = _parse_date(row["end"])
                        filed = _parse_date(row["filed"])
                        val = float(row["val"])
                        start = _parse_date(row["start"]) if row.get("start") else None
                    except (KeyError, TypeError, ValueError):
                        continue
                    k = FactKey(taxonomy, tag, unit, start, end)
                    prev = out.get(k)
                    if prev is None or filed > prev["filed"]:
                        out[k] = {"filed": filed, "val": val,
                                  "accn": row.get("accn", ""), "form": row.get("form", "")}
    return out


def diff_vintages(
    older: dict,
    newer: dict,
    *,
    materiality_pct: float = DEFAULT_MATERIALITY_PCT,
    scored_only: bool = True,
    since: date | None = None,
    include_split_adjusted: bool = False,
) -> list[VintageChange]:
    """Prior-period figures that changed or vanished between two snapshots.

    Facts ADDED are deliberately not reported: an ordinary new filing adds
    facts for new periods, which is not a revision, and separating a genuine
    back-fill from that needs a period-age rule there is no data to calibrate
    yet. Revisions and withdrawals are the blind spot this store exists for.

    Share counts are excluded unless `include_split_adjusted`: see
    `_scored_tags`. A split is not a restatement.
    """
    a = _latest_by_key(older, scored_only, include_split_adjusted)
    b = _latest_by_key(newer, scored_only, include_split_adjusted)
    names = _scored_tags(include_split_adjusted)
    changes: list[VintageChange] = []
    for k, old in a.items():
        if since is not None and k.end < since:
            continue
        field_name = names.get((k.taxonomy, k.tag), ("", ""))[0] or k.tag
        new = b.get(k)
        if new is None:
            changes.append(VintageChange(
                "withdrawn", field_name, k, old["val"], old["filed"],
                old.get("accn", ""), old.get("form", "")))
            continue
        if new["val"] == old["val"]:
            continue
        pct = None if old["val"] == 0 else abs(new["val"] - old["val"]) / abs(old["val"])
        if pct is not None and pct < materiality_pct:
            continue
        changes.append(VintageChange(
            "revised", field_name, k, old["val"], old["filed"],
            old.get("accn", ""), old.get("form", ""), new["val"], new["filed"],
            new.get("accn", ""), new.get("form", ""), pct))
    changes.sort(key=lambda c: (c.key.end, c.field_name, c.key.tag), reverse=True)
    return changes


def render_changes(changes: list[VintageChange], older: str, newer: str) -> str:
    """One markdown section. Says plainly when nothing moved — an empty diff
    is the expected result most of the time and is worth stating."""
    head = f"### Vintage diff — {older} → {newer}\n"
    if not changes:
        return head + "\nNo prior-period figure changed or disappeared between these snapshots.\n"
    lines = [head, "", "| Field | Period | Was | Now | Change | Originally filed |",
             "|---|---|---|---|---|---|"]
    for c in changes:
        now = "withdrawn" if c.kind == "withdrawn" else f"{c.new_value:,.0f}"
        pct = f"{c.pct_change:.1%}" if c.pct_change is not None else "—"
        filed = f"{c.old_filed} {c.old_form} {c.old_accession}".strip()
        lines.append(
            f"| {c.field_name} | {c.key.period} | {c.old_value:,.0f} | {now} | {pct} | {filed} |")
    return "\n".join(lines) + "\n"
