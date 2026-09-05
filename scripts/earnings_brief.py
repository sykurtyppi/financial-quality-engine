#!/usr/bin/env python3
"""Earnings brief: the release, the call, and the engine's findings — one page.

    # after a print (the sweep does this automatically after the audit)
    EDGAR_IDENTITY="Name email" scripts/earnings_brief.py build NVDA

    # the call transcript is not on EDGAR: drop it in and re-run
    scripts/earnings_brief.py build NVDA --transcript ~/Downloads/nvda_q2_call.txt
    #   (or save it as journal/transcripts/NVDA/<print date>.txt and re-run bare)

    # one page across the season
    scripts/earnings_brief.py digest --since 2026-10-01
    scripts/earnings_brief.py tally          # useful: yes/no across briefs

`build` collects primary sources deterministically (8-K EX-99 exhibits by
exhibit TYPE, the transcript if present, the engine report and its audit,
last quarter's brief), writes them beside the brief, and hands the paths to
one headless Claude run using .claude/skills/earnings-brief. The brief's
numbers must trace to those files; a missing source is marked UNAVAILABLE.

Output: reports/briefs/<TICKER>_<print date>.md, ending in a `useful:` line
the reader flips. Regenerating (e.g. once the transcript arrives) keeps a
`useful` value already set.

Exit codes: 0 brief written · 1 sources/CLI failure · 2 headless run failed
or produced no brief (sources are kept in reports/briefs/<TICKER>/<date>/).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.brief.sources import BRIEFS, BriefSourceError, BriefSources, collect_sources
from app.services.ingestion.sec_client import SecClient, SecClientError
from app.services.journal.store import safe_ticker

REPORT_DIRS = (ROOT / "reports" / "auto", ROOT / "reports")
DEFAULT_TIMEOUT_S = 1800.0
DIGEST_WINDOW_DAYS = 21
_USEFUL_RE = re.compile(r"^useful:\s*(yes|no|unset)\s*$", re.M | re.I)
_HEADING_RE = re.compile(r"^## (.+)$", re.M)


def latest_report(ticker: str) -> Path | None:
    """Newest engine report for the ticker across both tracks (by mtime)."""
    matches = [p for d in REPORT_DIRS for p in d.glob(f"{ticker}_*.md")
               if not p.stem.endswith("_audit")]
    return max(matches, key=lambda p: p.stat().st_mtime) if matches else None


def audit_for(report: Path | None) -> Path | None:
    if report is None:
        return None
    a = report.with_name(f"{report.stem}_audit.md")
    return a if a.is_file() else None


def prior_brief(ticker: str, before: date, root: Path | None = None) -> Path | None:
    """The newest earlier brief for the ticker (top-level files only)."""
    older = sorted(
        p for p in (root or BRIEFS).glob(f"{ticker}_*.md")
        if p.stem[len(ticker) + 1:] < before.isoformat()
    )
    return older[-1] if older else None


def brief_path(ticker: str, event_day: str, root: Path | None = None) -> Path:
    return (root or BRIEFS) / f"{ticker}_{event_day}.md"


def build_prompt(src: BriefSources) -> str:
    lines = [
        f"Use the earnings-brief skill to write the earnings brief for {src.ticker} "
        f"({src.company}). Print date {src.event_day}; 8-K {src.filing.accession}.",
        "Read every file below in full, then output the complete brief as your final "
        "response — nothing else, no preamble. Do not write any files. Use only these "
        "files; where a role is absent, the corresponding section is UNAVAILABLE.",
        "",
        "Files (role: path — label):",
    ]
    lines += [f"- {f.role}: {f.path} — {f.label}" for f in src.files]
    lines += ["", "Diagnostics from source collection (list them under Sources):"]
    lines += [f"- {d}" for d in src.diagnostics] or ["- none"]
    return "\n".join(lines)


def useful_value(text: str) -> str:
    m = _USEFUL_RE.search(text)
    return m.group(1).lower() if m else "unset"


def finalize(brief: str, keep_useful: str = "unset") -> str:
    """Guarantee the footer, carrying over a `useful` already set on the
    brief being replaced."""
    body = _USEFUL_RE.sub("", brief).rstrip()
    body = re.sub(r"\n-{3,}\s*$", "", body).rstrip()
    return f"{body}\n\n---\nuseful: {keep_useful}\n"


def run_headless(prompt: str, timeout: float) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt], capture_output=True, text=True, timeout=timeout, cwd=ROOT,
        )
    except FileNotFoundError:
        return 127, "", "`claude` CLI not found on PATH — cannot run the headless brief."
    except subprocess.TimeoutExpired:
        return 124, "", f"Brief timed out after {timeout / 60:.0f} min."
    return proc.returncode, proc.stdout, proc.stderr


def cmd_build(args: argparse.Namespace) -> int:
    ticker = safe_ticker(args.ticker)
    try:
        client = SecClient(fresh=True)
    except SecClientError as e:
        print(f"EDGAR unavailable: {e}", file=sys.stderr)
        return 1
    report = Path(args.report) if args.report else latest_report(ticker)
    if report is None:
        print(f"{ticker}: no engine report under reports/ or reports/auto/ — generate one "
              f"first (watch.py poll/sweep, or journal.py report).", file=sys.stderr)
        return 1
    try:
        # prior brief needs the print date, which the 8-K establishes: collect
        # once without it, then attach.
        src = collect_sources(
            client, ticker, accession=args.accession,
            transcript=Path(args.transcript) if args.transcript else None,
            report=report, audit=audit_for(report),
        )
        prior = prior_brief(ticker, src.filing.filing_date)
        if prior is not None:
            src.files.append(type(src.files[0])("prior_brief", prior, prior.name))
    except (BriefSourceError, SecClientError) as e:
        print(f"{ticker}: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"{ticker}: source collection failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    prompt = build_prompt(src)
    out = brief_path(ticker, src.event_day)
    print(f"{ticker}: 8-K {src.filing.accession} filed {src.event_day}; "
          f"{len(src.files)} source file(s); call {'present' if src.has_transcript else 'UNAVAILABLE'}")
    for d in src.diagnostics:
        print(f"  note: {d}")
    if args.dry_run:
        print(prompt)
        print(f"(dry run — would write {out})")
        return 0

    rc, stdout, stderr = run_headless(prompt, args.timeout)
    if rc != 0 or "## Headline" not in stdout:
        print(f"brief FAILED (exit {rc}); sources kept in {src.workdir}", file=sys.stderr)
        if stderr:
            print(stderr, file=sys.stderr)
        return 2
    keep = useful_value(out.read_text()) if out.exists() else "unset"
    out.write_text(finalize(stdout, keep))
    print(f"brief -> {out}" + (f" (useful: {keep} carried over)" if keep != "unset" else ""))
    return 0


def _section(text: str, title: str) -> str:
    m = re.search(rf"^## {re.escape(title)}\s*$\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


def briefs_in_window(since: date, root: Path | None = None) -> list[Path]:
    out = []
    for p in sorted((root or BRIEFS).glob("*_*.md")):
        day = p.stem.rsplit("_", 1)[-1]
        if p.stem.startswith("DIGEST") or not re.match(r"\d{4}-\d{2}-\d{2}$", day):
            continue
        if day >= since.isoformat():
            out.append(p)
    return out


def build_digest(paths: list[Path], since: date, today: date) -> str:
    lines = [f"# Earnings digest — prints since {since.isoformat()}",
             f"_Compiled {today.isoformat()} from {len(paths)} brief(s). Each entry is the "
             f"brief's own Headline and Changed-since-last-quarter sections, verbatim._", ""]
    for p in paths:
        text = p.read_text()
        title = text.splitlines()[0].lstrip("# ").strip() if text else p.stem
        lines += [f"## {title}", f"_{p.name} · useful: {useful_value(text)}_", ""]
        head = _section(text, "Headline") or "_(no Headline section)_"
        lines += [head, ""]
        changed = _section(text, "Changed since last quarter")
        if changed:
            lines += ["**Changed since last quarter**", changed, ""]
    return "\n".join(lines).rstrip() + "\n"


def cmd_digest(args: argparse.Namespace) -> int:
    today = date.today()
    since = date.fromisoformat(args.since) if args.since else today - timedelta(days=DIGEST_WINDOW_DAYS)
    paths = briefs_in_window(since)
    if not paths:
        print(f"no briefs dated on/after {since.isoformat()} under {BRIEFS}")
        return 0
    text = build_digest(paths, since, today)
    out = Path(args.out) if args.out else BRIEFS / f"DIGEST_{today.isoformat()}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print(text)
    print(f"digest -> {out}")
    return 0


def cmd_tally(args: argparse.Namespace) -> int:
    paths = briefs_in_window(date(1970, 1, 1))
    if not paths:
        print(f"no briefs under {BRIEFS}")
        return 0
    counts = {"yes": 0, "no": 0, "unset": 0}
    for p in paths:
        v = useful_value(p.read_text())
        counts[v] += 1
        print(f"  {v:5}  {p.name}")
    rated = counts["yes"] + counts["no"]
    print(f"\n{len(paths)} brief(s): useful yes {counts['yes']}, no {counts['no']}, "
          f"unrated {counts['unset']}"
          + (f" — {counts['yes'] / rated:.0%} of rated briefs useful" if rated else ""))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="write the brief for a ticker's latest earnings 8-K")
    b.add_argument("ticker")
    b.add_argument("--accession", help="a specific 8-K accession instead of the newest 2.02")
    b.add_argument("--transcript", help="call transcript text file (default: "
                   "journal/transcripts/<TICKER>/<print date>.txt if present)")
    b.add_argument("--report", help="engine report path (default: newest for the ticker)")
    b.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    b.add_argument("--dry-run", action="store_true",
                   help="collect sources and print the prompt; no headless run")
    b.set_defaults(fn=cmd_build)

    d = sub.add_parser("digest", help="one page across recent briefs (deterministic)")
    d.add_argument("--since", help=f"YYYY-MM-DD (default: last {DIGEST_WINDOW_DAYS} days)")
    d.add_argument("--out", help="output path (default reports/briefs/DIGEST_<today>.md)")
    d.set_defaults(fn=cmd_digest)

    t = sub.add_parser("tally", help="count useful: yes/no across briefs")
    t.set_defaults(fn=cmd_tally)

    args = p.parse_args()
    try:
        return args.fn(args)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
