#!/usr/bin/env python3
"""Build a validation-corpus case: fetch, trim, observe, and write a DRAFT.

    EDGAR_IDENTITY="Name email" .venv/bin/python scripts/make_corpus_case.py \\
        wageworks_10qa WAGE --cik 1366649 --as-of 2018-06-30 --since 2016-01-01 \\
        --why "10-Q/A that moved scored revenue"

Writes `tests/corpus/<name>/companyfacts.json` and `submissions.json` (trimmed)
and a `case.json` whose expectations are COPIED FROM WHAT THE ENGINE
OBSERVED, with `reviewed: null`. It prints that observation. The corpus test
refuses a case with no review: read the filings the observation names, correct
`expected` wherever the engine is wrong (a missed footprint is added by hand
from the filing; that is the point), then fill in `reviewed` with who read
which accessions. Expectations are never typed from memory.

`--from-facts F [--from-submissions S]` reads payloads already on disk instead
of fetching (no SEC access needed); `--synthetic` marks a harness self-test,
which is never counted as evidence.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.corpus import (  # noqa: E402
    CASE_FILE,
    FACTS_FILE,
    SUBMISSIONS_FILE,
    CorpusCase,
    draft_expectations,
    observe,
)
from app.services.ingestion.fields import all_tags  # noqa: E402
from app.services.ingestion.payloads import check_aligned  # noqa: E402

CORPUS = ROOT / "tests" / "corpus"
HISTORY_YEARS = 3  # facts kept before `since`: the mapper's buffer and the scan's window
SUBMISSION_COLUMNS = ("form", "filingDate", "accessionNumber", "primaryDocument", "items",
                      "reportDate")


def trim_facts(facts: dict, since: date) -> dict:
    """Only the concepts the engine reads, and only facts ending after
    `since` minus the history the mapper and the scan need."""
    floor = date(since.year - HISTORY_YEARS, since.month, min(since.day, 28)).isoformat()
    out: dict = {"cik": facts.get("cik"), "entityName": facts.get("entityName"), "facts": {}}
    for taxonomy, tag in sorted(all_tags()):
        concept = facts.get("facts", {}).get(taxonomy, {}).get(tag)
        if not concept:
            continue
        units = {
            unit: kept for unit, rows in concept.get("units", {}).items()
            if (kept := [r for r in rows if str(r.get("end", "")) >= floor])
        }
        if units:
            out["facts"].setdefault(taxonomy, {})[tag] = {"units": units}
    return out


def trim_submissions(submissions: dict, as_of: date) -> dict:
    """The filing index as of the case date: the columns the evidence streams
    read, rows filed on or before `as_of` (later rows could only leak)."""
    recent = submissions.get("filings", {}).get("recent", {})
    columns = [c for c in SUBMISSION_COLUMNS if c in recent]
    # Aligned or refused: a pinned case built from a truncated index would
    # pin a corpus missing the very filings it was built to check.
    n = check_aligned({c: recent[c] for c in columns}, "filings.recent")
    keep = [i for i in range(n) if str(recent["filingDate"][i]) <= as_of.isoformat()]
    return {
        "cik": submissions.get("cik"), "name": submissions.get("name"),
        "sic": submissions.get("sic"), "sicDescription": submissions.get("sicDescription"),
        "filings": {"recent": {c: [recent[c][i] for i in keep] for c in columns}},
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("name", help="case directory name under tests/corpus/")
    p.add_argument("ticker")
    p.add_argument("--cik", type=int)
    p.add_argument("--as-of", required=True, type=date.fromisoformat)
    p.add_argument("--since", required=True, type=date.fromisoformat)
    p.add_argument("--why", required=True, help="what this case exists to test")
    p.add_argument("--from-facts", type=Path)
    p.add_argument("--from-submissions", type=Path)
    p.add_argument("--synthetic", action="store_true", help="a harness self-test, not evidence")
    p.add_argument("--corpus", type=Path, default=CORPUS)
    args = p.parse_args(argv)

    if args.from_facts is not None:
        facts = json.loads(args.from_facts.read_text())
        submissions = (json.loads(args.from_submissions.read_text())
                       if args.from_submissions else None)
    else:
        from app.services.ingestion.sec_client import SecClient

        client = SecClient()
        cik = args.cik or client.resolve_cik(args.ticker)
        facts = client.company_facts_by_cik(cik)
        submissions = client.submissions_by_cik(cik)

    facts = trim_facts(facts, args.since)
    if submissions is not None:
        submissions = trim_submissions(submissions, args.as_of)
    obs = observe(facts, submissions, args.ticker, args.as_of, args.since)
    case = CorpusCase(
        name=args.name, ticker=args.ticker.upper(), cik=args.cik, as_of=args.as_of,
        since=args.since, why=args.why, synthetic=args.synthetic, reviewed=None,
        expected=draft_expectations(obs),
    )

    out = args.corpus / args.name
    out.mkdir(parents=True, exist_ok=True)
    (out / FACTS_FILE).write_text(json.dumps(facts, indent=1, sort_keys=True) + "\n")
    if submissions is not None:
        (out / SUBMISSIONS_FILE).write_text(json.dumps(submissions, indent=1, sort_keys=True) + "\n")
    (out / CASE_FILE).write_text(case.model_dump_json(indent=1) + "\n")

    print(f"{args.name}: DRAFT written to {out} — read the filings, correct `expected`, "
          "then fill in `reviewed`.")
    print(f"  Tier-1 events: {list(obs.tier1) or 'none'}")
    for fp in obs.footprints:
        print(f"  footprint: {fp.field} {fp.period_end} "
              f"{'AMENDED' if fp.amended else 'revised'}"
              f"{' (derived quarter)' if fp.derived else ''} ({fp.accession})")
    print(f"  8-K 4.02: {list(obs.non_reliance) or 'none'}")
    print(f"  evidence coverage: {obs.coverage:.0%}; not inspected: "
          f"{', '.join(sorted(obs.uninspected)) or 'none'}")
    print(f"  selections: {len(obs.selections)} field(s) mapped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
