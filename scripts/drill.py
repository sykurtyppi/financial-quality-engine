#!/usr/bin/env python3
"""Earnings-night drill: rehearse a filing night end to end, offline, and
leave a log an operator can sign (Hermes audit round 7, finding 3).

    .venv/bin/python scripts/drill.py                     # bundled AAPL fixture
    .venv/bin/python scripts/drill.py --ticker NVDA --cache data/cache
    .venv/bin/python scripts/drill.py --only 1,2,3        # a subset of steps

Every step runs the operator's own commands (generate_report.py, journal.py)
in a subprocess, against a COPY of `app/` and `scripts/` in a scratch
workspace whose SEC cache is seeded from the inputs, with the network pointed
at a closed port. Nothing reaches SEC and nothing under the real `reports/`,
`data/` or `journal/` is touched. Scenario edits (an amendment arriving,
conflicting facts, a missing statement, short history, SEC down, a stale
cache) are applied to whatever payload the inputs supply, so the drill runs
on the fixture in CI and on a real cached name before a season.

`--cache DIR` is an SEC cache directory (`data/cache` after a report run):
`company_tickers.json`, `companyfacts_CIK*.json`, `submissions_CIK*.json` and
any `archive_*` documents. Its entries are treated as fetched just now.

Output: `drills/<UTC stamp>/drill_log.{json,md}` plus each step's reports and
command output. Exit 0 only when every check of every step passed. The
runbook is docs/earnings_night_drill.md.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURE = ROOT / "tests" / "fixtures" / "real" / "companyfacts_AAPL_trimmed.json"
FIXTURE_CIK = 320193

# The lines a rerun may change without the report changing: when the data was
# fetched, and whether the vintage store already held this payload. Everything
# else in a same-day rerun on the same inputs must be byte-identical.
VOLATILE_LINES = ("- Data fetched:", "- Vintage snapshot:")
VOLATILE_LEDGER_KEYS = frozenset({"fetched_at"})

_TENQ_HTML = """<html><body>
<p>Item 2. Management's Discussion and Analysis of Financial Condition and Results of Operations</p>
<p>Revenue grew on strong demand. We believe margins will remain stable.</p>
<p>Item 1A. Risk Factors</p>
<p>Our business depends on consumer demand, which may decline.</p>
<p>Item 6. Exhibits</p>
</body></html>"""


# --- results ---------------------------------------------------------------------

@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    # A defect the drill found and that is reported, not yet fixed: the check
    # is expected to FAIL. It does not fail the step while it reproduces, and
    # fails it once it stops (the marker is then stale and must be removed).
    known: str = ""

    @property
    def passed(self) -> bool:
        return self.ok != bool(self.known)


@dataclass
class Command:
    argv: list[str]
    returncode: int
    seconds: float
    stdout: str
    stderr: str


@dataclass
class Step:
    number: int
    slug: str
    title: str
    checks: list[Check] = field(default_factory=list)
    commands: list[Command] = field(default_factory=list)
    seconds: float = 0.0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.checks) and all(c.passed for c in self.checks)

    def check(self, name: str, ok: bool, detail: str = "", *, known: str = "") -> bool:
        self.checks.append(Check(name, bool(ok), detail, known))
        return bool(ok)


# --- inputs and workspaces ---------------------------------------------------------

def _index(cik: int, ticker: str, today: date) -> dict:
    """The bundled filing index: a 10-Q and an 8-K Item 4.02 inside the
    report's windows (the shape tests/integration/test_cli_drills.py uses)."""
    filed = [(today - timedelta(days=d)).isoformat() for d in (20, 10)]
    return {
        "cik": str(cik), "name": f"{ticker} (drill fixture)", "sic": "3571", "tickers": [ticker],
        "filings": {"recent": {
            "form": ["10-Q", "8-K"],
            "items": ["", "4.02"],
            "filingDate": filed,
            "accessionNumber": [f"{cik:010d}-{today:%y}-000010", f"{cik:010d}-{today:%y}-000011"],
            "primaryDocument": ["q.htm", "k.htm"],
            "reportDate": [(today - timedelta(days=60)).isoformat(), filed[1]],
            "acceptanceDateTime": [f"{d}T16:00:00.000Z" for d in filed],
        }},
    }


class Inputs:
    """The SEC cache every workspace starts from."""

    def __init__(self, ticker: str, cache: Path | None, staging: Path):
        self.ticker = ticker.upper()
        self.staging = staging
        staging.mkdir(parents=True, exist_ok=True)
        if cache is None:
            if self.ticker != "AAPL":
                raise SystemExit("the bundled fixture is AAPL; pass --cache for another ticker")
            self.source = f"bundled fixture {FIXTURE.relative_to(ROOT)} + synthetic filing index"
            self.cik = FIXTURE_CIK
            today = datetime.now(UTC).date()
            (staging / "company_tickers.json").write_text(json.dumps(
                {"0": {"cik_str": self.cik, "ticker": self.ticker, "title": "Apple Inc."}}))
            shutil.copy(FIXTURE, staging / f"companyfacts_CIK{self.cik:010d}.json")
            (staging / f"submissions_CIK{self.cik:010d}.json").write_text(
                json.dumps(_index(self.cik, self.ticker, today)))
            (staging / f"archive_{self.cik:010d}{today:%y}000010_q.htm").write_text(_TENQ_HTML)
        else:
            self.source = f"SEC cache {cache}"
            for p in cache.iterdir():
                if p.is_file():
                    shutil.copy(p, staging / p.name)
            self.cik = self._cik_from_tickers()
        for name in (self.facts_name, "company_tickers.json"):
            if not (staging / name).is_file():
                raise SystemExit(f"inputs: {name} missing from {self.source}")

    def _cik_from_tickers(self) -> int:
        path = self.staging / "company_tickers.json"
        if not path.is_file():
            raise SystemExit("inputs: company_tickers.json missing from the cache")
        for row in json.loads(path.read_text()).values():
            if str(row.get("ticker", "")).upper() == self.ticker:
                return int(row["cik_str"])
        raise SystemExit(f"inputs: {self.ticker} is not in the cache's company_tickers.json")

    @property
    def facts_name(self) -> str:
        return f"companyfacts_CIK{self.cik:010d}.json"

    @property
    def index_name(self) -> str:
        return f"submissions_CIK{self.cik:010d}.json"


class Workspace:
    """A scratch copy of the code the operator runs, with its own reports/,
    data/ and journal/, and an SEC cache seeded from the inputs."""

    def __init__(self, path: Path, inputs: Inputs, code: Path):
        self.path = path
        self.inputs = inputs
        for part in ("app", "scripts", "_shim"):
            shutil.copytree(code / part, path / part)
        self.cache = path / "data" / "cache"
        shutil.copytree(inputs.staging, self.cache)
        self.touch_cache()

    def touch_cache(self) -> None:
        """Every entry fetched just now (the <24h TTL serves it)."""
        now = time.time()
        for p in self.cache.iterdir():
            os.utime(p, (now, now))

    @property
    def reports(self) -> Path:
        return self.path / "reports"

    def live(self, suffix: str = ".md") -> Path:
        return self.reports / f"{self.inputs.ticker}_{datetime.now(UTC).date():%Y-%m-%d}{suffix}"

    def facts(self) -> dict:
        return json.loads((self.cache / self.inputs.facts_name).read_text())

    def write_facts(self, facts: dict) -> None:
        (self.cache / self.inputs.facts_name).write_text(json.dumps(facts))

    def index(self) -> dict:
        return json.loads((self.cache / self.inputs.index_name).read_text())

    def write_index(self, index: dict) -> None:
        (self.cache / self.inputs.index_name).write_text(json.dumps(index))

    def run(self, step: Step, script: str, *args: str,
            env_extra: dict[str, str] | None = None) -> Command:
        env = {k: v for k, v in os.environ.items()
               if k.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
        closed = "http://127.0.0.1:9"  # discard port: nothing listens
        env.update({
            "HTTP_PROXY": closed, "HTTPS_PROXY": closed, "http_proxy": closed,
            "https_proxy": closed,
            "EDGAR_IDENTITY": "Earnings Drill drill@example.com",
            "PYTHONPATH": str(self.path / "_shim"),
            "PYTHONDONTWRITEBYTECODE": "1",
            **(env_extra or {}),
        })
        argv = [script, *args]
        t0 = time.monotonic()
        proc = subprocess.run(
            [sys.executable, str(self.path / "scripts" / script), *args],
            cwd=self.path, env=env, capture_output=True, text=True, timeout=600,
        )
        cmd = Command(argv, proc.returncode, round(time.monotonic() - t0, 2),
                      proc.stdout, proc.stderr)
        step.commands.append(cmd)
        return cmd


# --- payload edits (generic over any companyfacts payload) --------------------------

@dataclass
class CurrentFact:
    taxonomy: str
    tag: str
    unit: str
    quarter: date
    fact: dict


def current_revenue_fact(facts: dict, ticker: str) -> CurrentFact:
    """The fact the scored revenue series reads for its newest directly
    reported quarter: what an amendment or a same-day conflict must hit."""
    from app.services.ingestion.companyfacts_mapper import build_dataset
    from app.services.ingestion.precedence import rank

    _, diag = build_dataset(facts, ticker)
    (rev,) = (f for f in diag.fields if f.field_name == "revenue")
    direct = sorted(
        (date.fromisoformat(str(q)), src) for q, src in rev.period_sources.items()
        if src.method == "direct" and len(src.components) == 1
    )
    if not direct:
        raise ValueError("no directly reported revenue quarter to amend")
    quarter, src = direct[-1]
    taxonomy, _, tag = src.components[0].partition(":")
    for unit, rows in facts["facts"][taxonomy][tag]["units"].items():
        quarterly = [
            r for r in rows
            if r.get("end") == quarter.isoformat() and r.get("start")
            and 60 <= (quarter - date.fromisoformat(r["start"])).days <= 110
        ]
        if quarterly:
            best = max(quarterly, key=lambda r: rank(
                date.fromisoformat(r["filed"]), r.get("form", ""), r.get("accn", "")))
            return CurrentFact(taxonomy, tag, unit, quarter, best)
    raise ValueError(f"no quarterly {taxonomy}:{tag} fact ending {quarter}")


def add_fact(facts: dict, cur: CurrentFact, **changes) -> dict:
    row = {**cur.fact, **changes}
    row.pop("frame", None)  # a frame names one fact per period; the copy has none
    facts["facts"][cur.taxonomy][cur.tag]["units"][cur.unit].append(row)
    return row


def list_filing(index: dict, **entry: str) -> None:
    """Prepend a filing to the index's recent block (newest first), keeping
    every column the same length."""
    recent = index["filings"]["recent"]
    for key, col in recent.items():
        if key in entry:
            col.insert(0, entry[key])
        else:
            col.insert(0, 0 if col and isinstance(col[0], int | float) else "")


def drop_concepts(facts: dict, keep: Callable[[str], bool]) -> list[str]:
    dropped = []
    for taxonomy, concepts in facts.get("facts", {}).items():
        for tag in list(concepts):
            if not keep(tag):
                del concepts[tag]
                dropped.append(f"{taxonomy}:{tag}")
    return dropped


def keep_one_period(facts: dict) -> str:
    """Every fact but those ending on the newest period end: one quarter of
    history, which cannot be scored."""
    ends = [r["end"] for c in facts["facts"].values() for spec in c.values()
            for rows in spec["units"].values() for r in rows if r.get("end")]
    newest = max(ends)  # a cover date may be newer than any quarter end: still one period
    for concepts in facts["facts"].values():
        for spec in concepts.values():
            for unit in list(spec["units"]):
                spec["units"][unit] = [r for r in spec["units"][unit] if r.get("end") == newest]
    return newest


# --- normalisation for the determinism check ----------------------------------------

def normalise_report(text: str) -> str:
    return "\n".join(
        "<volatile>" if line.startswith(VOLATILE_LINES) else line for line in text.splitlines()
    )


def normalise_ledger(obj):
    if isinstance(obj, dict):
        return {k: ("<volatile>" if k in VOLATILE_LEDGER_KEYS else normalise_ledger(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [normalise_ledger(v) for v in obj]
    return obj


def first_difference(a: str, b: str) -> str:
    for n, (x, y) in enumerate(zip(a.splitlines(), b.splitlines(), strict=False), 1):
        if x != y:
            return f"line {n}: {x!r} != {y!r}"
    return f"lengths differ ({len(a.splitlines())} vs {len(b.splitlines())} lines)"


# --- the drill ---------------------------------------------------------------------

def _archived(cmd: Command) -> list[Path]:
    return [Path(m) for m in re.findall(r"^previous run archived: (.+)$", cmd.stdout, re.M)]


def _vintage_files(ws: Workspace) -> dict[str, bytes]:
    root = ws.path / "data" / "vintages"
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _manifest_prefix(before: dict[str, bytes], after: dict[str, bytes]) -> bool:
    """Every manifest's snapshot list after is the list before, extended."""
    names = [k for k in before if k.endswith("manifest.json")]
    for k in names:
        if k not in after:
            return False
        old = json.loads(before[k]).get("snapshots", [])
        new = json.loads(after[k]).get("snapshots", [])
        if new[:len(old)] != old:
            return False
    return bool(names)


class Drill:
    def __init__(self, inputs: Inputs, out: Path, code: Path):
        self.inputs = inputs
        self.out = out
        self.code = code
        self.state: dict = {}
        self._main: Workspace | None = None

    def workspace(self, name: str) -> Workspace:
        return Workspace(self.out / "work" / name, self.inputs, self.code)

    @property
    def main(self) -> Workspace:
        """The workspace steps 1-3 and 10 share: one ticker's night, in order."""
        if self._main is None:
            self._main = self.workspace("night")
        return self._main

    def generate(self, step: Step, ws: Workspace, *extra: str,
                 env_extra: dict[str, str] | None = None) -> Command:
        return ws.run(step, "generate_report.py", self.inputs.ticker, *extra, env_extra=env_extra)

    def no_traceback(self, step: Step, cmd: Command) -> None:
        step.check(f"`{' '.join(cmd.argv)}`: no traceback", "Traceback" not in cmd.stderr,
                   cmd.stderr[-600:] if "Traceback" in cmd.stderr else "")

    def wrote_report(self, step: Step, ws: Workspace, cmd: Command) -> str:
        ok = step.check(f"`{' '.join(cmd.argv)}` exits 0", cmd.returncode == 0,
                        f"exit {cmd.returncode}: {cmd.stderr[-400:]}")
        self.no_traceback(step, cmd)
        live = ws.live()
        step.check("report written", live.is_file(), str(live))
        step.check("evidence ledger written beside it",
                   live.with_suffix(".ledger.json").is_file())
        return live.read_text() if ok and live.is_file() else ""

    def refused(self, step: Step, ws: Workspace, cmd: Command, needle: str) -> None:
        step.check(f"`{' '.join(cmd.argv)}` exits 2", cmd.returncode == 2,
                   f"exit {cmd.returncode}")
        self.no_traceback(step, cmd)
        step.check(f"stderr says {needle!r}", needle in cmd.stderr, cmd.stderr[-400:])
        step.check("stderr says no report was written", "no report written" in cmd.stderr)
        step.check("no report file", not ws.live().exists())

    # 1 ---------------------------------------------------------------------------
    def s1_baseline(self, step: Step) -> None:
        ws = self.main
        cmd = self.generate(step, ws)
        report = self.wrote_report(step, ws, cmd)
        step.check("thermometer line on stdout", "distress signals:" in cmd.stdout)
        step.check("vintage captured", "Vintage snapshot: captured" in report)
        self.state["s1_report"] = report
        self.state["s1_ledger"] = ws.live(".ledger.json").read_text() if report else ""
        self.state["s1_vintages"] = _vintage_files(ws)

    # 2 ---------------------------------------------------------------------------
    def s2_rerun(self, step: Step) -> None:
        ws = self.main
        cmd = self.generate(step, ws)
        report = self.wrote_report(step, ws, cmd)
        moved = _archived(cmd)
        self.state["s1_archived"] = moved
        md = [p for p in moved if p.name.endswith(".md") and not p.name.endswith("_audit.md")]
        step.check("step-1 report archived, not overwritten",
                   len(md) == 1 and md[0].read_text() == self.state.get("s1_report"),
                   ", ".join(map(str, moved)) or "nothing archived")
        a, b = normalise_report(self.state.get("s1_report", "")), normalise_report(report)
        step.check("report identical to step 1 (fetch time and vintage lines masked)",
                   bool(report) and a == b, "" if a == b else first_difference(a, b))
        la = normalise_ledger(json.loads(self.state.get("s1_ledger") or "null"))
        lb = normalise_ledger(json.loads(ws.live(".ledger.json").read_text())) if report else None
        step.check("ledger identical to step 1 (fetched_at masked)", la is not None and la == lb)
        step.check("vintage unchanged (same payload)",
                   "unchanged since the last snapshot" in report)

    # 3 ---------------------------------------------------------------------------
    def s3_amendment(self, step: Step) -> None:
        ws = self.main
        facts = ws.facts()
        cur = current_revenue_fact(facts, self.inputs.ticker)
        today = datetime.now(UTC).date()
        accn = f"{self.inputs.cik:010d}-{today:%y}-990001"
        new_val = round(cur.fact["val"] * 1.10)
        add_fact(facts, cur, val=new_val, form="10-Q/A", filed=today.isoformat(), accn=accn)
        ws.write_facts(facts)
        index = ws.index()
        list_filing(index, form="10-Q/A", items="", filingDate=today.isoformat(),
                    accessionNumber=accn, primaryDocument="qa.htm",
                    reportDate=cur.quarter.isoformat(),
                    acceptanceDateTime=f"{today.isoformat()}T21:00:00.000Z")
        ws.write_index(index)
        (ws.cache / f"archive_{accn.replace('-', '')}_qa.htm").write_text(_TENQ_HTML)
        step.check("scenario: 10-Q/A lands", True,
                   f"{cur.taxonomy}:{cur.tag} {cur.quarter}: {cur.fact['val']:,} -> {new_val:,} "
                   f"({accn}, filed {today})")
        self.state["s3_accn"] = accn

        cmd = self.generate(step, ws)
        report = self.wrote_report(step, ws, cmd)
        step.check("Tier-1 restatement line names the /A and the quarter",
                   f"Restatement (10-Q/A) affecting {cur.quarter}" in report)
        ledger = ws.live(".ledger.json").read_text() if report else ""
        step.check("ledger cites the /A accession", accn in ledger)
        step.check("vintage captured the new payload", "Vintage snapshot: captured" in report)
        # Found by this drill on 2026-09-26: the /A was also promoted as a
        # Tier-1 "Silent revision" under an appendix saying nothing there had
        # an amended filing behind it (fixed in vintages.explained_by_filing).
        step.check("the /A is not also called a silent revision", "Silent revision:" not in report)
        m = re.search(r"Silent-revision check: compared .*?: (\d+) change\(s\) "
                      r"\(\+(\d+) moved with a later filing, not silent\)", report)
        step.check("silent-revision check compared two snapshots and attributed the move "
                   "to a filing", m is not None and m.group(1) == "0" and int(m.group(2)) >= 1,
                   m.group(0) if m else "no comparison line naming a filed move")
        step.check("the appendix names the /A as what revised it",
                   f"10-Q/A {accn} |" in report.split("**Moved with a later filing")[-1])
        card = report.split("# Full report (appendix)")[0]
        marked = [ln for ln in card.splitlines() if "⚠ reads a revised figure: revenue" in ln]
        step.check("card lines that read the amended revenue say so, naming the /A",
                   bool(marked) and all(f"amended by 10-Q/A {accn}" in ln for ln in marked),
                   f"{len(marked)} marked line(s)")
        step.check("lines that do not read revenue are not marked",
                   not any("⚠ reads a revised" in ln for ln in card.splitlines()
                           if ln.startswith(("- Total accruals", "- CFO / Net income"))))
        moved = _archived(cmd)
        self.state["s2_archived"] = moved
        step.check("step-2 report archived", any(p.suffix == ".md" for p in moved),
                   ", ".join(map(str, moved)))

    # 4 ---------------------------------------------------------------------------
    def s4_conflict(self, step: Step) -> None:
        ws = self.workspace("conflict")
        facts = ws.facts()
        cur = current_revenue_fact(facts, self.inputs.ticker)
        other = round(cur.fact["val"] * 1.02)
        accn = f"{self.inputs.cik:010d}-{datetime.now(UTC):%y}-990002"
        add_fact(facts, cur, val=other, accn=accn)
        ws.write_facts(facts)
        step.check("scenario: a second value filed the same day, same form", True,
                   f"{cur.taxonomy}:{cur.tag} {cur.quarter}: {cur.fact['val']:,} and {other:,} "
                   f"filed {cur.fact['filed']} ({cur.fact.get('form')})")
        cmd = self.generate(step, ws, "--no-docs")
        report = self.wrote_report(step, ws, cmd)
        step.check("field note names the same-day conflict",
                   f"Same-day conflicting facts for {cur.taxonomy}:{cur.tag}" in report)
        step.check("not reported as a restatement",
                   f"Restatement ({cur.fact.get('form')}) affecting {cur.quarter}" not in report)

    # 5 ---------------------------------------------------------------------------
    def s5_partial(self, step: Step) -> None:
        from app.services.ingestion.fields import field as field_spec

        ws = self.workspace("partial")
        spec = field_spec("cfo")
        cfo_tags = {tag for s in spec.strategies for _, tag in s.all_tags()}
        facts = ws.facts()
        dropped = drop_concepts(
            facts, lambda tag: tag not in cfo_tags and not tag.startswith("NetCashProvidedByUsedIn"))
        ws.write_facts(facts)
        step.check("scenario: cash-flow statement absent", bool(dropped), ", ".join(dropped))
        cmd = self.generate(step, ws, "--no-docs")
        report = self.wrote_report(step, ws, cmd)
        step.check("report says CFO is missing", "Critical field 'cfo' missing" in report)
        step.check("the card is marked incomplete", "(incomplete:" in report)

    # 6 ---------------------------------------------------------------------------
    def s6_short_history(self, step: Step) -> None:
        ws = self.workspace("short_history")
        facts = ws.facts()
        newest = keep_one_period(facts)
        ws.write_facts(facts)
        step.check("scenario: one period of history", True, f"only facts ending {newest}")
        cmd = self.generate(step, ws, "--no-docs")
        self.refused(step, ws, cmd, "could not be mapped")

    # 7 ---------------------------------------------------------------------------
    def s7_index_down(self, step: Step) -> None:
        ws = self.workspace("index_down")
        (ws.cache / self.inputs.index_name).unlink()
        step.check("scenario: SEC reachable for companyfacts only", True,
                   "filing index not cached; network closed")
        cmd = self.generate(step, ws)
        report = self.wrote_report(step, ws, cmd)
        step.check("filing index reported unavailable", "filing index unavailable" in report)
        step.check("streams that need it say UNAVAILABLE", "UNAVAILABLE" in report)
        step.check("no clean offering claim", "No offering-related filings found" not in report)

    # 8 ---------------------------------------------------------------------------
    def s8_stale(self, step: Step) -> None:
        ws = self.workspace("stale")
        old = time.time() - 25 * 3600
        os.utime(ws.cache / self.inputs.facts_name, (old, old))
        step.check("scenario: companyfacts cache 25 h old, SEC unreachable", True)
        cmd = self.generate(step, ws, "--no-docs")
        self.refused(step, ws, cmd, "SEC request failed")
        ws.touch_cache()
        cmd = self.generate(step, ws, "--no-docs", "--fresh")
        self.refused(step, ws, cmd, "SEC request failed")

    # 9 ---------------------------------------------------------------------------
    def s9_journal(self, step: Step) -> None:
        ws = self.workspace("journal")
        t = self.inputs.ticker
        today = datetime.now(UTC).date()
        assumption = f"revenue,>,1,FY{today.year + 1}Q1,,{today + timedelta(days=120)}"
        cmd = ws.run(step, "journal.py", "openv2", t, "--thesis", "drill: no view",
                     "--conviction", "3", "--action", "no_position",
                     "--assumption", assumption,
                     "--falsifier", "I am wrong if the drill passes by accident")
        step.check("thesis locked (openv2 exits 0)", cmd.returncode == 0, cmd.stderr[-400:])
        cmd = ws.run(step, "journal.py", "report", t, "--no-fresh", "--no-docs")
        step.check("journal report exits 0", cmd.returncode == 0, cmd.stderr[-400:])
        self.no_traceback(step, cmd)
        step.check("journal report written", ws.live().is_file())
        cmd = ws.run(step, "journal.py", "report", t, "--no-fresh", "--no-docs")
        step.check("a second report on a reported entry is refused", cmd.returncode != 0,
                   f"exit {cmd.returncode}: {(cmd.stdout + cmd.stderr)[-300:]}")
        self.no_traceback(step, cmd)
        note = "drill: rejected the conflict note as immaterial"
        cmd = ws.run(step, "journal.py", "after", t, "--impact", "no_value",
                     "--conviction-after", "3", "--disagreed", note)
        step.check("analyst override recorded (after --disagreed exits 0)",
                   cmd.returncode == 0, cmd.stderr[-400:])
        entries = list((ws.path / "journal" / "entries").glob(f"{t}_*.md"))
        step.check("the override is in the entry", any(note in p.read_text() for p in entries))
        cmd = ws.run(step, "journal.py", "verify", t)
        step.check("the locked thesis still verifies", cmd.returncode == 0,
                   (cmd.stdout + cmd.stderr)[-300:])

    # 10 --------------------------------------------------------------------------
    def s10_rollback(self, step: Step) -> None:
        ws = self.main
        from app.services.reporting.report_files import archive_existing

        archived = [p for p in self.state.get("s1_archived", []) if p.name.endswith(".md")
                    and not p.name.endswith("_audit.md")]
        if not step.check("step 1's archived report is available", len(archived) == 1):
            return
        live = ws.live()
        # The runbook's restore, as the operator does it: put the current run
        # aside, then copy the chosen archived run back to the live names.
        aside = archive_existing(live)
        step.check("current (step-3) run put aside", any(p.suffix == ".md" for p in aside),
                   ", ".join(map(str, aside)))
        shutil.copy2(archived[0], live)
        shutil.copy2(archived[0].with_name(archived[0].name.removesuffix(".md") + ".ledger.json"),
                     live.with_suffix(".ledger.json"))
        step.check("restored report is byte-identical to step 1",
                   live.read_text() == self.state.get("s1_report"))
        step.check("restored ledger is byte-identical to step 1",
                   live.with_suffix(".ledger.json").read_text() == self.state.get("s1_ledger"))
        before, after = self.state.get("s1_vintages", {}), _vintage_files(ws)
        snaps_before = {k: v for k, v in before.items() if k.endswith(".json.gz")}
        snaps_after = {k: v for k, v in after.items() if k.endswith(".json.gz")}
        step.check("vintage store is append-only: step-1 snapshots byte-identical",
                   bool(snaps_before) and all(snaps_after.get(k) == v for k, v in snaps_before.items()),
                   f"{len(snaps_before)} -> {len(snaps_after)} snapshot(s)")
        step.check("the manifest's step-1 entries are unchanged and first",
                   _manifest_prefix(before, after))
        step.check("the amendment's snapshot is kept after the rollback",
                   len(snaps_after) > len(snaps_before))


    # 11 --------------------------------------------------------------------------
    def s11_failed_rebuild(self, step: Step) -> None:
        """Hermes audit round 8, finding 2: the earlier run was archived before
        the rebuild, so a rebuild that failed left no live report or ledger."""
        ws = self.workspace("failed_rebuild")
        first = self.generate(step, ws, "--no-docs")
        self.wrote_report(step, ws, first)
        live, ledger = ws.live(), ws.live(".ledger.json")
        before = {p: p.read_bytes() for p in (live, ledger) if p.is_file()}
        archive = ws.reports / "archive"
        archived = sorted(archive.iterdir()) if archive.is_dir() else []
        cmd = self.generate(step, ws, "--no-docs", env_extra={"FQE_DRILL_FAIL_BUILD": "1"})
        injected = "drill: injected report-build failure" in cmd.stderr
        step.check("scenario: the report build raises after the data was acquired", injected,
                   "" if injected else cmd.stderr[-400:])
        step.check("the failed rebuild exits nonzero", cmd.returncode != 0, f"exit {cmd.returncode}")
        step.check("the live report and ledger are byte-identical to before",
                   len(before) == 2 and all(p.is_file() and p.read_bytes() == b
                                            for p, b in before.items()))
        step.check("nothing was archived by the failed rebuild",
                   (sorted(archive.iterdir()) if archive.is_dir() else []) == archived)
        staging = ws.reports / ".staging"
        step.check("no staged file is left behind",
                   not staging.exists() or not any(staging.iterdir()))
        again = self.generate(step, ws, "--no-docs")
        moved = {p.name.split(".", 2)[-1]: p for p in _archived(again)}  # "md" / "ledger.json"
        step.check("the next rebuild succeeds", again.returncode == 0,
                   f"exit {again.returncode}: {again.stderr[-300:]}")
        step.check("it archives the first run byte for byte (report and ledger)",
                   set(moved) == {"md", "ledger.json"}
                   and moved["md"].read_bytes() == before.get(live)
                   and moved["ledger.json"].read_bytes() == before.get(ledger),
                   ", ".join(map(str, moved.values())) or "nothing archived")
        step.check("the new report and ledger are live, and nothing is left staged",
                   live.is_file() and ledger.is_file()
                   and (not staging.exists() or not any(staging.iterdir())))


STEPS: tuple[tuple[str, str, Callable[[Drill, Step], None]], ...] = (
    ("baseline", "Baseline report", Drill.s1_baseline),
    ("rerun", "Rerun on the same inputs: deterministic, first run archived", Drill.s2_rerun),
    ("amendment", "A 10-Q/A lands after the first run", Drill.s3_amendment),
    ("conflict", "Same-day conflicting facts", Drill.s4_conflict),
    ("partial", "A statement missing (cash flow)", Drill.s5_partial),
    ("short_history", "One period of history: refused, not a traceback", Drill.s6_short_history),
    ("index_down", "SEC down except companyfacts", Drill.s7_index_down),
    ("stale", "Stale cache and --fresh with SEC unreachable", Drill.s8_stale),
    ("journal", "Journal path and analyst override", Drill.s9_journal),
    ("rollback", "Roll back to the first run", Drill.s10_rollback),
    ("failed_rebuild", "A rebuild that fails keeps the live report", Drill.s11_failed_rebuild),
)


# The workspaces' `sitecustomize`: retry back-off sleeps become no-ops, and
# FQE_DRILL_FAIL_BUILD=1 makes the report build raise once the data is in
# hand (step 11), in the workspace's copy of the code only.
_SHIM = """import time
time.sleep = lambda seconds: None  # drill: retries fail fast

import os
if os.environ.get("FQE_DRILL_FAIL_BUILD"):
    import sys
    sys.path.insert(0, os.getcwd())
    import app.services.reporting.report_builder as _rb

    def _fail(*args, **kwargs):
        raise RuntimeError("drill: injected report-build failure")

    _rb.build_report = _fail
"""


# --- output ------------------------------------------------------------------------

def _git_head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _save_step(out: Path, step: Step) -> None:
    d = out / "steps" / f"{step.number:02d}_{step.slug}"
    d.mkdir(parents=True, exist_ok=True)
    for n, cmd in enumerate(step.commands, 1):
        (d / f"cmd{n}.stdout.txt").write_text(cmd.stdout)
        (d / f"cmd{n}.stderr.txt").write_text(cmd.stderr)


def _markdown(log: dict) -> str:
    lines = [
        f"# Earnings-night drill — {log['ticker']} — {log['started']}",
        "",
        f"- Result: **{'PASS' if log['passed'] else 'FAIL'}** "
        f"({sum(s['ok'] for s in log['steps'])}/{len(log['steps'])} steps)",
        f"- Known issues reproduced: {len(log['known_issues'])} (marked [!] under their step; "
        "reported, not fixed)",
        f"- Inputs: {log['inputs']}",
        f"- Code: `{log['git_head'] or 'unknown'}`, Python {log['python']}",
        f"- Duration: {log['seconds']:.1f}s",
        "",
        "| # | Step | Result | Seconds |",
        "|---|---|---|---|",
    ]
    for s in log["steps"]:
        lines.append(f"| {s['number']} | {s['title']} | {'PASS' if s['ok'] else 'FAIL'} "
                     f"| {s['seconds']:.1f} |")
    for s in log["steps"]:
        lines += ["", f"## {s['number']}. {s['title']} — {'PASS' if s['ok'] else 'FAIL'}", ""]
        if s["error"]:
            lines += [f"- **error:** `{s['error']}`"]
        for c in s["checks"]:
            if c["known"]:
                state = ("KNOWN ISSUE, reproduces" if not c["ok"]
                         else "KNOWN ISSUE NO LONGER REPRODUCES: remove the marker")
                lines.append(f"- [{'!' if not c['ok'] else ' '}] {c['name']} — {state}: {c['known']}")
                continue
            shown = c["detail"] and (not c["ok"] or c["name"].startswith("scenario"))
            detail = f" — {c['detail']}" if shown else ""
            lines.append(f"- [{'x' if c['ok'] else ' '}] {c['name']}{detail}")
        for c in s["commands"]:
            lines.append(f"- `{' '.join(c['argv'])}` → exit {c['returncode']} in {c['seconds']:.1f}s")
    lines += [
        "",
        "## Operator notes",
        "",
        "_Filled in by the person who ran the drill. A PASS proves the commands "
        "behave on these inputs; it does not prove the reports are right._",
        "",
        "- Run by:",
        "- Inputs were real (which cache, captured when) / fixture:",
        "- Anything slow, surprising, or read wrongly:",
        "- Reports read end to end (which steps):",
        "- Decision (ready for the season / not ready, and why):",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ticker", default="AAPL")
    parser.add_argument("--cache", type=Path, help="an SEC cache directory (default: the "
                        "bundled AAPL fixture with a synthetic filing index)")
    parser.add_argument("--out", type=Path, help="output directory (default: drills/<UTC stamp>)")
    parser.add_argument("--only", help="comma-separated step numbers (default: all)")
    parser.add_argument("--keep-work", action="store_true",
                        help="keep the scratch workspaces' code copies (default: removed)")
    args = parser.parse_args(argv)

    started = datetime.now(UTC)
    out = args.out or ROOT / "drills" / started.strftime("%Y%m%dT%H%M%SZ")
    if out.exists() and any(out.iterdir()):
        print(f"error: {out} is not empty", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    only = {int(n) for n in args.only.split(",")} if args.only else None
    if only is not None and only & {2, 3, 10} and 1 not in only:
        only.add(1)  # steps 2, 3 and 10 continue the baseline's night
    if only is not None and 10 in only:
        only |= {2, 3}

    # The code the drill runs: this checkout's app/ and scripts/, plus the
    # shim that turns the SEC client's retry back-off into no-ops.
    code = out / "work" / "_code"
    for part in ("app", "scripts"):
        shutil.copytree(ROOT / part, code / part,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (code / "_shim").mkdir()
    (code / "_shim" / "sitecustomize.py").write_text(_SHIM)

    inputs = Inputs(args.ticker, args.cache, out / "work" / "_inputs")
    drill = Drill(inputs, out, code)
    steps: list[Step] = []
    t_all = time.monotonic()
    for number, (slug, title, run) in enumerate(STEPS, 1):
        if only is not None and number not in only:
            continue
        step = Step(number, slug, title)
        t0 = time.monotonic()
        try:
            run(drill, step)
        except Exception as e:  # noqa: BLE001 - a broken step is a FAIL, not a stop
            step.error = f"{type(e).__name__}: {e}"
        step.seconds = round(time.monotonic() - t0, 2)
        _save_step(out, step)
        steps.append(step)
        print(f"{'PASS' if step.ok else 'FAIL'}  {number:>2}. {title} ({step.seconds:.1f}s)")
        for c in step.checks:
            if c.known:
                print(f"        ! {c.name}: "
                      + ("known issue reproduces" if not c.ok else "KNOWN ISSUE GONE: remove marker"))
            elif not c.ok:
                print(f"        x {c.name}: {c.detail}")
        if step.error:
            print(f"        x error: {step.error}")

    for ws_dir in (out / "work").iterdir():
        # Keep what the runs wrote (reports/, data/, journal/); drop code copies.
        if not args.keep_work:
            for part in ("app", "scripts", "_shim"):
                shutil.rmtree(ws_dir / part, ignore_errors=True)
    if not args.keep_work:
        shutil.rmtree(code, ignore_errors=True)

    step_rows: list[dict] = [{**asdict(s), "ok": s.ok} for s in steps]
    for row in step_rows:
        for c in row["commands"]:
            c["stdout"], c["stderr"] = c["stdout"][-4000:], c["stderr"][-4000:]
    log = {
        "ticker": inputs.ticker,
        "started": started.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "inputs": inputs.source,
        "git_head": _git_head(),
        "python": platform.python_version(),
        "seconds": round(time.monotonic() - t_all, 2),
        "passed": bool(steps) and all(s.ok for s in steps),
        "known_issues": sorted({c.known for s in steps for c in s.checks if c.known and not c.ok}),
        "steps": step_rows,
    }
    (out / "drill_log.json").write_text(json.dumps(log, indent=2))
    (out / "drill_log.md").write_text(_markdown(log))
    print(f"{'PASS' if log['passed'] else 'FAIL'}: {out / 'drill_log.md'}")
    return 0 if log["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
