#!/usr/bin/env python3
"""Headless audit: run the earnings-audit skill over a generated engine report.

    scripts/run_audit.py reports/auto/NVDA_2026-08-26.md

Invokes the Claude Code CLI non-interactively (`claude -p`) from the repo root
so `.claude/skills/earnings-audit` is discoverable, and writes the audit next
to the report as `<report stem>_audit.md`. The season's finding was that the
audit loop — artifact corrections, primary-source verification, strongest
benign explanation per flag, self-computed valuation — is where the value
lives; this makes that loop part of the automatic (ticker-only) track instead
of something the analyst has to remember to run.

Deliberately minimal: one subprocess, one prompt, no retries, no orchestration.
A failed or timed-out audit exits 1 and leaves the engine report untouched.

The audit reads one pinned report generation and is written into that
generation, naming it on its first line: a report rebuilt during the audit
(which can take 30 minutes) is never given an audit of the run it replaced —
the audit stays with the run it read, and exits 1 so it is rerun.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.headless import claude_command  # noqa: E402
from app.services.reporting.report_files import (  # noqa: E402
    READ_ONLY,
    LiveRun,
    current_generation,
    generation_of,
    link_audit,
    live_name,
    publish_lock,
    read_live,
    write_atomic,
)

DEFAULT_TIMEOUT_S = 1800.0


def build_prompt(ticker: str, report_path: Path) -> str:
    return (
        f"Use the earnings-audit skill to audit {ticker}. "
        f"The deterministic engine report is at {report_path} — read it first, "
        "then run the full loop (phases 0-7 including the engine-artifact "
        "correction table). You are running headlessly: for inputs that need "
        "the analyst (unverifiable consensus figures, portfolio context), "
        "state UNAVAILABLE rather than guessing. Do not write any files; "
        "output the complete audit as your final response."
    )


def audit_output_path(report_path: Path) -> Path:
    return report_path.with_name(f"{report_path.stem}_audit.md")


def run_audit(report_path: Path, timeout: float = DEFAULT_TIMEOUT_S) -> int:
    live = read_live(report_path)
    if live is None:
        print(f"No such report: {report_path}", file=sys.stderr)
        return 1
    ticker = report_path.stem.split("_")[0]
    # The pinned file, not the live name: a rebuild during the audit must not
    # change what the auditor reads.
    prompt = build_prompt(ticker, live.report)
    try:
        proc = subprocess.run(
            [claude_command(), "-p", prompt],
            capture_output=True, text=True, timeout=timeout, cwd=ROOT,
        )
    except FileNotFoundError:
        print(f"Claude CLI not found at {claude_command()!r} — set CLAUDE_BIN or put "
              "`claude` on PATH; cannot run the headless audit.", file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired:
        print(f"Audit timed out after {timeout / 60:.0f} min.", file=sys.stderr)
        return 1
    if proc.returncode != 0 or not proc.stdout.strip():
        print(f"Audit failed (exit {proc.returncode}).", file=sys.stderr)
        if proc.stderr:
            print(proc.stderr, file=sys.stderr)
        return 1
    return publish_audit(report_path, live, proc.stdout)


def publish_audit(report_path: Path, live: LiveRun, text: str) -> int:
    """Write the audit of the run ``live`` pinned. In a generation it goes
    into that generation, whatever is live by now: it can never sit beside
    another run's report, and if the report was rebuilt (or set aside) while
    the audit ran it exits 1, so the live run is audited too. Files from
    before generations have no generation to keep it in: the audit is
    written beside them only if they are still the run it read."""
    body = (f"<!-- generation: {live.generation_id} -->\n\n"
            if live.generation_id else "") + text
    if live.generation_dir is not None:
        # Given a generation's own path, the lock and the live-run check are
        # the live name's: nothing is created inside the generation.
        report_path = live_name(report_path)
        out = audit_output_path(live.report)
        write_atomic(out, body, mode=READ_ONLY)
        with publish_lock(report_path):
            link_audit(report_path)  # shown at the live name only if its run is live
        if current_generation(report_path) != live.generation_dir:
            print(f"{report_path.name}'s live run is not the one audited (rebuilt, set aside "
                  f"or restored while the audit ran, or an earlier run was named); the audit "
                  f"is kept with the run it read ({out}).", file=sys.stderr)
            return 1
        print(f"audit -> {audit_output_path(report_path)}")
        return 0
    out = audit_output_path(report_path)
    with publish_lock(report_path):
        if not report_path.is_file() or report_path.is_symlink():
            print(f"Audit discarded: {report_path.name} is no longer the run it read.",
                  file=sys.stderr)
            return 1
        now = generation_of(report_path)
        if now != live.generation_id:
            print(f"Audit discarded: {report_path.name} was rebuilt while it ran "
                  f"(audited generation {live.generation_id}, live {now}). Rerun the audit.",
                  file=sys.stderr)
            return 1
        write_atomic(out, body)
    print(f"audit -> {out}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("report", type=Path, help="path to the engine report markdown")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                   help=f"seconds before giving up (default {DEFAULT_TIMEOUT_S:.0f})")
    args = p.parse_args()
    return run_audit(args.report, timeout=args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
