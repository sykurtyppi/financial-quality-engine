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
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

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
    if not report_path.is_file():
        print(f"No such report: {report_path}", file=sys.stderr)
        return 1
    ticker = report_path.stem.split("_")[0]
    prompt = build_prompt(ticker, report_path)
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=timeout, cwd=ROOT,
        )
    except FileNotFoundError:
        print("`claude` CLI not found on PATH — cannot run the headless audit.",
              file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired:
        print(f"Audit timed out after {timeout / 60:.0f} min.", file=sys.stderr)
        return 1
    if proc.returncode != 0 or not proc.stdout.strip():
        print(f"Audit failed (exit {proc.returncode}).", file=sys.stderr)
        if proc.stderr:
            print(proc.stderr, file=sys.stderr)
        return 1
    out = audit_output_path(report_path)
    out.write_text(proc.stdout)
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
