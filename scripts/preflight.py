#!/usr/bin/env python3
"""Season preflight: what an unattended `watch.py sweep` needs, checked
before the first print rather than discovered on it.

    .venv/bin/python scripts/preflight.py [--expect SHA] [--live] [--claude-login]
    .venv/bin/python scripts/preflight.py launchd > \\
        ~/Library/LaunchAgents/com.financial-quality-engine.watch-sweep.plist

Each check prints PASS, WARN or FAIL with what to do about it; the exit code
is 1 if anything FAILed, else 0. Offline by default. Two checks cost
something and run only when asked:

    --live          ONE EDGAR request (a fresh submissions fetch), proving the
                    identity, the network path and SEC's answer from here
    --claude-login  ONE headless `claude -p` run (a paid call) from a
                    scheduler-shaped environment, proving the CLI's login

`--expect SHA` fails unless this checkout is exactly that commit with no
changes to the engine code: the season runs from one pinned commit, and every
report states the one that built it (`- Engine:`).

`launchd` prints a LaunchAgent that runs the sweep hourly from this checkout
(docs/earnings_night_runbook.md prefers launchd to cron on a Mac that
sleeps). It is printed, never installed; load it with
`launchctl bootstrap gui/$(id -u) <plist>`.
"""

from __future__ import annotations

import argparse
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services import headless
from app.services.reporting.report_files import engine_commit
from app.services.watch import watchlist

LABEL = "com.financial-quality-engine.watch-sweep"
# What cron and launchd give a job: not the operator's shell profile.
SCHEDULER_PATH = "/usr/bin:/bin"
IDENTITY_PLACEHOLDER = "Your Name you@example.com"
PROBE_CIK = 320193  # Apple: any filer would do; this one is always there


@dataclass(frozen=True)
class Result:
    name: str
    status: str  # "PASS" | "WARN" | "FAIL"
    detail: str


def check_python() -> Result:
    v = sys.version_info
    ok = (v.major, v.minor) >= (3, 12)
    return Result("python", "PASS" if ok else "FAIL",
                  f"{platform.python_version()} ({sys.executable})"
                  + ("" if ok else "; the engine needs Python 3.12+"))


def _same_commit(stated_sha: str, expect: str) -> bool:
    """Short and full shas name one commit when one prefixes the other (at
    git's own 7-character minimum)."""
    a, b = stated_sha.lower(), expect.strip().lower()
    return min(len(a), len(b)) >= 7 and (a.startswith(b) or b.startswith(a))


def check_engine(expect: str | None) -> Result:
    stated = engine_commit()
    sha = stated.split(" ", 1)[0]
    if stated.startswith("unknown"):
        return Result("engine", "FAIL" if expect else "WARN", stated)
    if expect and not _same_commit(sha, expect):
        return Result("engine", "FAIL", f"{stated}; expected {expect}")
    if "(stated by" in stated:
        return Result("engine", "WARN", f"{stated}; git cannot confirm a stated commit")
    if "uncommitted changes" in stated or "could not check" in stated:
        return Result("engine", "FAIL" if expect else "WARN",
                      f"{stated}; reports will say so"
                      + ("" if expect else ". Pin a clean commit with --expect"))
    return Result("engine", "PASS", stated + ("" if expect else "; pass --expect to pin it"))


def check_identity() -> Result:
    identity = os.environ.get("EDGAR_IDENTITY", "").strip()
    if not identity:
        return Result("EDGAR identity", "FAIL",
                      'EDGAR_IDENTITY is not set; SEC fair access requires it, e.g. '
                      f'"{IDENTITY_PLACEHOLDER}". Set it in the scheduler\'s environment too')
    if identity == IDENTITY_PLACEHOLDER:
        return Result("EDGAR identity", "FAIL", "EDGAR_IDENTITY is still the placeholder")
    if "@" not in identity or " " not in identity:
        return Result("EDGAR identity", "WARN",
                      'set, but SEC asks for "Name email"; a bare name or address '
                      "may be throttled")
    return Result("EDGAR identity", "PASS", f"set ({len(identity)} characters)")


def scheduler_claude(env: Mapping[str, str] | None = None) -> str:
    """The CLI a scheduler would run: `headless.claude_command` resolved
    under the scheduler's PATH instead of this shell's."""
    src: Mapping[str, str] = os.environ if env is None else env
    explicit = src.get(headless.ENV_VAR, "").strip()
    if explicit:
        return explicit
    return shutil.which("claude", path=SCHEDULER_PATH) or str(headless.FALLBACK)


def check_claude_cli() -> Result:
    path = scheduler_claude()
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        return Result("claude CLI", "FAIL",
                      f"a scheduler would run {path}, which is not an executable file; "
                      f"set {headless.ENV_VAR} to the CLI's absolute path. Audits and "
                      "briefs fail without it (sweep exits 4/5)")
    return Result("claude CLI", "PASS", f"a scheduler would run {path}")


def check_claude_login(run: Callable = subprocess.run) -> Result:
    """The runbook's `env -i … claude -p` check: the CLI's own stored login,
    from an environment with nothing of this shell in it."""
    path = scheduler_claude()
    env = {"HOME": os.environ.get("HOME", ""), "USER": os.environ.get("USER", ""),
           "PATH": SCHEDULER_PATH}
    try:
        proc = run([path, "-p", "Reply with exactly: OK"], env=env,
                   capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as e:
        return Result("claude login", "FAIL", f"{path} could not run: {e}")
    if proc.returncode == 0 and proc.stdout.strip() == "OK":
        return Result("claude login", "PASS", "headless run answered from a scheduler-shaped env")
    said = (proc.stderr or proc.stdout).strip().splitlines()
    return Result("claude login", "FAIL",
                  f"exit {proc.returncode}: {said[-1] if said else 'no output'}; run `claude` "
                  "in a terminal and /login, then re-check")


def check_journal(root: Path, now: datetime | None = None) -> list[Result]:
    now = now or datetime.now(UTC)
    out: list[Result] = []
    portfolio = root / "journal" / "portfolio.txt"
    try:
        held = watchlist.read_portfolio(portfolio)
        out.append(Result("portfolio", "PASS", f"{len(held)} names in {portfolio.name}"))
    except Exception as e:  # noqa: BLE001 - a check reports; it never takes the others down
        if not portfolio.is_file():
            out.append(Result("portfolio", "WARN",
                              f"no {portfolio.relative_to(root)}: `sweep --portfolio` needs it; "
                              "without it only names already on the watchlist are swept"))
        else:
            out.append(Result("portfolio", "FAIL",
                              f"{portfolio.relative_to(root)} cannot be read "
                              f"({type(e).__name__}: {e}); save it as UTF-8 text, one ticker "
                              "per line"))
    path = root / "journal" / "watchlist.json"
    try:
        watches = watchlist.load(path)
    except Exception as e:  # noqa: BLE001 - a malformed row must be a FAIL, not a traceback
        return [*out, Result("watchlist", "FAIL", f"{type(e).__name__}: {e}")]
    if not watches:
        return [*out, Result("watchlist", "WARN", f"{path.relative_to(root)} watches nothing")]
    unarmed = [w.ticker for w in watches if not w.event_armed]
    past = [w.ticker for w in watches if not w.is_before_print(now)]
    no_thesis = [w.ticker for w in watches if w.thesis_entry is None]
    out.append(Result("watchlist", "FAIL" if unarmed else "PASS",
                      f"{len(watches)} names, next {watches[0].ticker} at "
                      f"{watches[0].print_at:%Y-%m-%d %H:%M}Z"
                      + (f"; not event-armed (never fire): {', '.join(unarmed)} — "
                         "`watch.py add` them again" if unarmed else "")))
    if past:
        out.append(Result("print hints", "WARN",
                          f"already past: {', '.join(past)}. A hint only starts polling, "
                          "so a filed event still triggers; check these re-armed"))
    if no_thesis:
        out.append(Result("theses", "WARN",
                          f"{len(no_thesis)} of {len(watches)} have no linked thesis "
                          "(auto track only): write, lock and `watch.py link` before each print"))
    return out


def check_watchlist_private(root: Path, run: Callable = subprocess.run) -> Result:
    """`sync` writes holdings into journal/watchlist.json, which git tracks."""
    try:
        proc = run(["git", "diff", "--quiet", "HEAD", "--", "journal/watchlist.json"],
                   cwd=root, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return Result("watchlist privacy", "WARN", "could not ask git")
    if proc.returncode == 1:
        return Result("watchlist privacy", "WARN",
                      "journal/watchlist.json differs from the commit: it names your "
                      "holdings once synced. Do not commit or push it")
    return Result("watchlist privacy", "PASS", "journal/watchlist.json is as committed")


def check_scheduler(run: Callable = subprocess.run, system: str | None = None) -> Result:
    system = system or platform.system()
    argv, needle = (["launchctl", "list"], LABEL) if system == "Darwin" else (
        ["crontab", "-l"], "watch.py sweep")
    try:
        proc = run(argv, capture_output=True, text=True, timeout=10)
        found = proc.returncode == 0 and needle in proc.stdout
    except (OSError, subprocess.SubprocessError):
        found = False
    if found:
        return Result("scheduler", "PASS", f"`{' '.join(argv)}` lists {needle}")
    fix = ("`scripts/preflight.py launchd`" if system == "Darwin"
           else "the crontab line in docs/earnings_night_runbook.md")
    return Result("scheduler", "WARN",
                  f"no hourly sweep found by `{' '.join(argv)}`; install one ({fix}) "
                  "or poll by hand on print nights")


def check_edgar_live(client_factory: Callable | None = None) -> Result:
    """One fresh request, into a throwaway cache: nothing it fetches is
    kept or served to a report."""
    from app.services.ingestion.sec_client import SecClient, SecClientError

    factory = client_factory or (lambda cache: SecClient(cache_dir=cache, fresh=True))
    with tempfile.TemporaryDirectory() as cache:
        try:
            subs = factory(cache).submissions_by_cik(PROBE_CIK)
        except SecClientError as e:
            hint = (" SEC answers 403 to a missing/odd identity or a rate block "
                    "(about 10 minutes); wait before retrying" if "403" in str(e) else "")
            return Result("EDGAR live", "FAIL", f"{e}.{hint}")
    name = subs.get("name", "?") if isinstance(subs, dict) else "?"
    return Result("EDGAR live", "PASS", f"fresh submissions for CIK {PROBE_CIK} ({name})")


def run_checks(args: argparse.Namespace, root: Path = ROOT) -> list[Result]:
    results = [check_python(), check_engine(args.expect), check_identity(), check_claude_cli()]
    if args.claude_login:
        results.append(check_claude_login())
    results += check_journal(root)
    results += [check_watchlist_private(root), check_scheduler()]
    if args.live:
        results.append(check_edgar_live())
    return results


def launchd_plist(root: Path = ROOT, interval_s: int = 3600,
                  env: Mapping[str, str] | None = None) -> bytes:
    src: Mapping[str, str] = os.environ if env is None else env
    venv = root / ".venv" / "bin" / "python"
    python = str(venv) if venv.exists() else sys.executable
    log = str(root / "journal" / "watch.log")
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [python, str(root / "scripts" / "watch.py"), "sweep",
                             "--portfolio", "journal/portfolio.txt"],
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {
            "EDGAR_IDENTITY": src.get("EDGAR_IDENTITY", "").strip() or IDENTITY_PLACEHOLDER,
            headless.ENV_VAR: scheduler_claude(src),
            "PATH": SCHEDULER_PATH,
        },
        "StartInterval": interval_s,
        "RunAtLoad": True,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    })


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--expect", help="the pinned engine commit (sha or prefix)")
    p.add_argument("--live", action="store_true", help="make ONE EDGAR request")
    p.add_argument("--claude-login", action="store_true",
                   help="make ONE headless claude run (paid)")
    sub = p.add_subparsers(dest="cmd")
    ld = sub.add_parser("launchd", help="print a LaunchAgent for the hourly sweep")
    ld.add_argument("--interval", type=int, default=3600, help="seconds between passes")
    args = p.parse_args(argv)

    if args.cmd == "launchd":
        if args.interval < 600:
            p.error("--interval below 600s: every pass makes one EDGAR request per name")
        sys.stdout.buffer.write(launchd_plist(interval_s=args.interval))
        if not os.environ.get("EDGAR_IDENTITY", "").strip():
            print("note: EDGAR_IDENTITY was not set; edit the placeholder in the plist",
                  file=sys.stderr)
        return 0

    results = run_checks(args)
    width = max(len(r.name) for r in results)
    for r in results:
        print(f"{r.status:4}  {r.name:<{width}}  {r.detail}")
    failed = [r for r in results if r.status == "FAIL"]
    warned = [r for r in results if r.status == "WARN"]
    print(f"\n{'NOT READY' if failed else 'ready'}: {len(failed)} failed, {len(warned)} "
          f"warnings{'' if args.live else '; EDGAR not contacted (--live)'}"
          f"{'' if args.claude_login else ', CLI login not tried (--claude-login)'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
