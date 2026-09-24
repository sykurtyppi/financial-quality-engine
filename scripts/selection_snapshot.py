#!/usr/bin/env python3
"""Snapshot what the companyfacts mapper selects and builds, per field.

PR 1.4 replaces the mapper's three tag selectors and three composition rules
with one. That refactor touches how every scored value is built, so it must
reproduce today's output exactly — and a snapshot taken AFTER the refactor
would prove nothing. This tool records, for each field of
`companyfacts_mapper.build_dataset`: the tag (or composed tags) used, the
derivation methods, the missing periods, the mapping notes and every value,
plus the quarter ends, fiscal labels and warnings of the run, and how each
reported quarter's value was built (strategy, components, method, partial).

    python scripts/selection_snapshot.py golden [--check]
        (Re)write tests/golden_reports/selection_snapshot.json from the
        three real fixtures, point-in-time cuts of them, and the synthetic
        branch cases in tests/fixtures/selection_cases.py. --check compares
        instead of writing (exit 1 on any difference).

    python scripts/selection_snapshot.py vintages --out F [--as-of D] [--root DIR]
        Dump every filer in the vintage store from its newest snapshot taken
        on or before D (default: today). The input is pinned by the store,
        so two runs with the same --as-of differ only by code. This is the
        archive-rerun gate for PR 1.4: run it on main and on the branch, then
        `diff` the two files; an empty diff is the gate.

    python scripts/selection_snapshot.py diff A B
        Per case and per field, what changed between two dumps. Exit 1 on
        any difference.

Values are written as JSON floats, which round-trip exactly (repr), so the
comparison is exact equality, not a tolerance.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.backtesting.pit import filter_as_of
from app.services.ingestion import vintages
from app.services.ingestion.companyfacts_mapper import build_dataset

FIXTURES = ROOT / "tests" / "fixtures" / "real"
GOLDEN = ROOT / "tests" / "golden_reports" / "selection_snapshot.json"
REAL_TICKERS = ("AAPL", "KO", "CRM")
# Point-in-time cuts of the real fixtures: what the mapper built from the
# facts filed by each date (the backtest path).
PIT_CUTS = (date(2024, 6, 30), date(2025, 3, 31))
N_QUARTERS = 8


def dump(facts: dict, ticker: str, *, n_quarters: int = N_QUARTERS) -> dict:
    """One mapper run, as plain JSON data. A payload the mapper refuses
    (fewer than two quarter ends) is recorded as its error, so a change in
    what is refused shows up too."""
    try:
        ds, diag = build_dataset(facts, ticker, n_quarters=n_quarters)
    except ValueError as e:
        return {"error": str(e)}
    fields = []
    for d in diag.fields:
        values = {
            p.period_end.isoformat(): getattr(p, d.field_name)
            for p in ds.periods
            if getattr(p, d.field_name) is not None
        }
        fields.append({
            "field": d.field_name,
            "tag_used": d.tag_used,
            "periods_filled": d.periods_filled,
            "periods_total": d.periods_total,
            "methods": d.methods,
            "missing_periods": d.missing_periods,
            "notes": d.notes,
            "values": values,
            "period_sources": {
                q: src.model_dump() for q, src in sorted(d.period_sources.items())
            },
        })
    return {
        "entity_name": diag.entity_name,
        "fiscal_year_end_month": diag.fiscal_year_end_month,
        "quarter_ends": diag.quarter_ends,
        "labels": [p.fiscal_label for p in ds.periods],
        "warnings": diag.warnings,
        "fields": fields,
    }


def render(cases: dict[str, dict]) -> str:
    """Stable text: cases sorted, one field per line, so a review diff shows
    exactly which field of which case moved."""
    out = ["{"]
    names = sorted(cases)
    for i, name in enumerate(names):
        case = cases[name]
        out.append(f" {json.dumps(name)}: {{")
        keys = sorted(case)
        for j, key in enumerate(keys):
            comma = "," if j < len(keys) - 1 else ""
            if key == "fields":
                out.append('  "fields": [')
                for k, rec in enumerate(case[key]):
                    sep = "," if k < len(case[key]) - 1 else ""
                    out.append(f"   {json.dumps(rec, sort_keys=True)}{sep}")
                out.append(f"  ]{comma}")
            else:
                out.append(f"  {json.dumps(key)}: {json.dumps(case[key], sort_keys=True)}{comma}")
        out.append(" }" + ("," if i < len(names) - 1 else ""))
    out.append("}")
    return "\n".join(out) + "\n"


def golden_cases() -> dict[str, dict]:
    # Imported here: the synthetic cases live with the tests, and only the
    # golden subcommand needs them.
    from tests.fixtures.selection_cases import CASES

    cases: dict[str, dict] = {}
    for ticker in REAL_TICKERS:
        facts = json.loads((FIXTURES / f"companyfacts_{ticker}_trimmed.json").read_text())
        cases[f"real/{ticker}"] = dump(facts, ticker)
        for cut in PIT_CUTS:
            # Exactly what `pit.build_pit_dataset` feeds the mapper.
            cases[f"pit/{ticker}@{cut}"] = dump(filter_as_of(facts, cut), ticker)
    for name, build in CASES.items():
        cases[f"synthetic/{name}"] = dump(build(), name.upper())
    return cases


def vintage_cases(as_of: date, root: Path | None = None) -> dict[str, dict]:
    """Each filer in the store, from its newest snapshot on or before
    `as_of` (the same visibility rule the report's silent-revision diff
    uses). A filer with no snapshot by then is left out."""
    base = root or vintages.VINTAGES
    cases: dict[str, dict] = {}
    if not base.is_dir():
        return cases
    for d in sorted(base.glob("CIK*")):
        if not d.is_dir() or not d.name[3:].isdigit():
            continue
        cik = int(d.name[3:])
        visible = [
            o for o in vintages.observed_vintages(cik, root)
            if date.fromisoformat(o.captured) <= as_of
        ]
        if not visible:
            continue
        newest = visible[-1]
        facts = vintages.load_vintage(newest.path)
        case = dump(facts, d.name)
        case["snapshot"] = {"captured": newest.captured, "sha256": newest.sha256}
        cases[d.name] = case
    return cases


def _fields_by_name(case: dict) -> dict[str, dict]:
    return {f["field"]: f for f in case.get("fields", [])}


def diff_cases(a: dict[str, dict], b: dict[str, dict]) -> list[str]:
    """Human-readable differences, empty when the dumps are identical."""
    lines: list[str] = []
    for name in sorted(a.keys() - b.keys()):
        lines.append(f"{name}: only in the first dump")
    for name in sorted(b.keys() - a.keys()):
        lines.append(f"{name}: only in the second dump")
    for name in sorted(a.keys() & b.keys()):
        ca, cb = a[name], b[name]
        if ca == cb:
            continue
        for key in sorted((ca.keys() | cb.keys()) - {"fields"}):
            if ca.get(key) != cb.get(key):
                lines.append(f"{name}: {key}: {ca.get(key)!r} → {cb.get(key)!r}")
        fa, fb = _fields_by_name(ca), _fields_by_name(cb)
        if [f["field"] for f in ca.get("fields", [])] != [f["field"] for f in cb.get("fields", [])]:
            lines.append(f"{name}: field order differs")
        for field in sorted(fa.keys() | fb.keys()):
            ra, rb = fa.get(field), fb.get(field)
            if ra == rb:
                continue
            if ra is None or rb is None:
                lines.append(f"{name}: {field}: only in the {'second' if ra is None else 'first'} dump")
                continue
            for key in sorted((ra.keys() | rb.keys()) - {"values", "field"}):
                if ra.get(key) != rb.get(key):
                    lines.append(f"{name}: {field}.{key}: {ra.get(key)!r} → {rb.get(key)!r}")
            va, vb = ra.get("values", {}), rb.get("values", {})
            for q in sorted(va.keys() | vb.keys()):
                if va.get(q) != vb.get(q):
                    lines.append(f"{name}: {field}[{q}]: {va.get(q)!r} → {vb.get(q)!r}")
    return lines


def _load(path: Path) -> dict[str, dict]:
    return json.loads(path.read_text())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("golden", help="write (or --check) the committed snapshot")
    g.add_argument("--check", action="store_true")
    g.add_argument("--out", type=Path, default=GOLDEN)
    v = sub.add_parser("vintages", help="dump every filer in the vintage store")
    v.add_argument("--out", type=Path, required=True)
    v.add_argument("--as-of", type=date.fromisoformat, default=None)
    v.add_argument("--root", type=Path, default=None)
    d = sub.add_parser("diff", help="compare two dumps")
    d.add_argument("a", type=Path)
    d.add_argument("b", type=Path)
    args = parser.parse_args(argv)

    if args.cmd == "golden":
        text = render(golden_cases())
        if args.check:
            if not args.out.exists():
                print(f"{args.out} does not exist", file=sys.stderr)
                return 1
            lines = diff_cases(_load(args.out), json.loads(text))
            if lines or args.out.read_text() != text:
                print("\n".join(lines) or "formatting differs", file=sys.stderr)
                return 1
            print(f"{args.out}: unchanged")
            return 0
        args.out.write_text(text)
        print(f"Wrote {args.out}")
        return 0
    if args.cmd == "vintages":
        as_of = args.as_of or date.today()
        cases = vintage_cases(as_of, args.root)
        args.out.write_text(render(cases))
        print(f"Wrote {args.out}: {len(cases)} filer(s) as of {as_of}")
        return 0
    lines = diff_cases(_load(args.a), _load(args.b))
    for line in lines:
        print(line)
    if lines:
        print(f"{len(lines)} difference(s)", file=sys.stderr)
        return 1
    print("identical")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
