#!/usr/bin/env python3
"""In-repo mutation harness: does the suite notice when the evidence code is wrong?

Every past review round found defects behind a green suite, and each fix was
pinned by hand-written mutants run once. This runs that check weekly, on the
modules where a silent defect costs most, with no dependency:

    python tests/tools/mutate.py [--budget 60] [--wall 600] [--seed N] [--floor 0.8]
    python tests/tools/mutate.py --changed origin/main   # a pull request's targets only

Five operators, applied one at a time through `ast`:

- `cmp`   a comparison's boundary flips (`<`↔`<=`, `>`↔`>=`, `==`↔`!=`);
- `const` a pinned module constant (an UPPER_CASE number) moves by ε
          (+1 for an int, ×1.01 for a float);
- `if`    an `if` condition is negated;
- `minmax` `max` becomes `min` and back;
- `continue` a `continue` becomes `pass`.

Sites are enumerated in a fixed order and sampled with a seed (default: the
ISO week, so weekly runs rotate through them), round-robin across targets,
up to `--budget`. Each mutant runs its target's mapped tests (`pytest -x -q`)
in a temporary copy of the repository, so the working tree is never touched.
A mutant the tests do not fail on is a SURVIVOR: either a gap in the tests or
a mutant a person must judge. One that makes them hang past the timeout is
TIMED OUT and is not counted as killed. Judged survivors are recorded in
`mutation_exclusions.toml` as equivalent, accepted (with an owner and a
review date) or tracked; see `Exclusion`.
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXCLUSIONS = Path(__file__).resolve().with_name("mutation_exclusions.toml")
KINDS = ("equivalent", "accepted", "tracked")


@dataclass(frozen=True)
class Target:
    path: str  # repository-relative module
    tests: tuple[str, ...]  # repository-relative test files
    function: str | None = None  # restrict sites to one function's lines


TARGETS: tuple[Target, ...] = (
    Target("app/services/ingestion/restatements.py", (
        "tests/unit/test_restatements.py", "tests/unit/test_restatement_pit_and_tag.py",
        "tests/unit/test_restatement_scan.py", "tests/unit/test_properties_restatements.py",
        "tests/unit/test_debt_composition.py", "tests/unit/test_series_selection.py",
        "tests/unit/test_restatement_scan_truth.py", "tests/unit/test_same_day_precedence.py",
        "tests/unit/test_payload_fuzz.py",
    )),
    Target("app/services/ingestion/companyfacts_mapper.py", (
        "tests/unit/test_companyfacts_mapper.py", "tests/unit/test_coherent_derivation.py",
        "tests/unit/test_same_day_precedence.py", "tests/unit/test_provenance.py",
        "tests/integration/test_selection_snapshot.py", "tests/unit/test_build_dataset_as_of.py",
    )),
    Target("app/services/ingestion/vintages.py", (
        "tests/unit/test_vintages.py", "tests/unit/test_vintage_composed.py",
        "tests/unit/test_vintage_diff_report.py", "tests/unit/test_replay.py",
        "tests/unit/test_pit_agreement.py", "tests/unit/test_vintage_capture_from_reports.py",
        "tests/unit/test_vintage_amendment.py",
    )),
    Target("app/services/backtesting/pit.py", (
        "tests/unit/test_pit_agreement.py", "tests/unit/test_build_dataset_as_of.py",
        "tests/unit/test_backtesting.py",
    )),
    Target("app/services/ingestion/precedence.py", (
        "tests/unit/test_same_day_precedence.py", "tests/unit/test_pit_agreement.py",
        "tests/unit/test_restatements.py",
    )),
    Target("app/services/ingestion/composition.py", (
        "tests/unit/test_debt_composition.py", "tests/unit/test_strategy_resolution.py",
        "tests/integration/test_selection_snapshot.py",
    )),
    Target("app/services/ingestion/selection.py", (
        "tests/unit/test_series_selection.py", "tests/unit/test_debt_composition.py",
    )),
    Target("app/core/pipeline.py", (
        "tests/unit/test_distress_flags.py", "tests/integration/test_pipeline.py",
        "tests/integration/test_golden_report.py",
        "tests/unit/test_properties_engine.py",
    ), function="_generate_flags"),
    Target("app/services/provenance.py", (
        "tests/unit/test_provenance.py", "tests/unit/test_series_provenance.py",
        "tests/unit/test_ledger.py", "tests/unit/test_journal_resolver.py",
    )),
    Target("app/services/reporting/report_files.py", (
        "tests/unit/test_report_files.py", "tests/unit/test_earnings_brief.py",
        "tests/unit/test_watch_cli.py",
    )),
    Target("app/services/journal/resolver.py", (
        "tests/unit/test_journal_resolver.py", "tests/unit/test_assumption_vocabulary.py",
        "tests/unit/test_properties_engine.py",
    )),
)

_FLIPS: dict[type, type] = {
    ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
}
_SYMBOLS = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==", ast.NotEq: "!="}


@dataclass(frozen=True)
class Mutant:
    target: Target
    index: int  # the node's position in `ast.walk` order — stable for one source
    operator: str
    line: int
    change: str
    source: str = ""  # the original line, stripped — how an equivalent is recognised
    function: str = ""  # the innermost enclosing function, if any
    detail: str = ""  # the mutated expression, used only to tell apart twins on one line

    @property
    def label(self) -> str:
        return f"{self.target.path}:{self.line} [{self.operator}] {self.change}"


def _is_constant_assign(node: ast.AST) -> tuple[str, int | float] | None:
    """(name, value) for a module-level `NAME = <number>` (not a bool)."""
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target, value = node.targets[0], node.value
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        target, value = node.target, node.value
    else:
        return None
    if not (isinstance(target, ast.Name) and target.id.isupper()):
        return None
    if isinstance(value, ast.Constant) and type(value.value) in (int, float):
        return target.id, value.value
    return None


def _nudged(value: int | float) -> int | float:
    if isinstance(value, int):
        return value + 1
    return value * 1.01 if value else 0.01


def _function_range(tree: ast.Module, name: str) -> tuple[int, int]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node.lineno, node.end_lineno or node.lineno
    raise ValueError(f"function {name!r} not found")


def sites(target: Target, source: str) -> list[Mutant]:
    """Every mutation site in `source`, in a fixed order."""
    tree = ast.parse(source)
    text = source.splitlines()
    module_level = {id(n) for n in tree.body}
    functions = sorted(
        ((n.lineno, n.end_lineno or n.lineno, n.name) for n in ast.walk(tree)
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))),
        key=lambda f: f[1] - f[0],
    )
    lo, hi = _function_range(tree, target.function) if target.function else (0, 10**9)
    out: list[Mutant] = []

    def add(index: int, operator: str, line: int, change: str, detail: str = "") -> None:
        src = text[line - 1].strip() if 0 < line <= len(text) else ""
        if operator == "continue":
            # A bare `continue` says nothing about which one it is: key it
            # by the guard line above it, "<guard> continue".
            prev = next((t.strip() for t in reversed(text[: line - 1]) if t.strip()), "")
            src = f"{prev} continue"
        func = next((name for lo_, hi_, name in functions if lo_ <= line <= hi_), "")
        out.append(Mutant(target, index, operator, line, change, src, func, detail))

    for index, node in enumerate(ast.walk(tree)):
        line = getattr(node, "lineno", 0)
        if not lo <= line <= hi:
            continue
        if isinstance(node, ast.Compare) and type(node.ops[0]) in _FLIPS:
            old = type(node.ops[0])
            add(index, "cmp", line, f"{_SYMBOLS[old]} -> {_SYMBOLS[_FLIPS[old]]}",
                detail=ast.unparse(node))
        elif id(node) in module_level and (pinned := _is_constant_assign(node)):
            name, value = pinned
            add(index, "const", line, f"{name} = {value!r} -> {_nudged(value)!r}")
        elif isinstance(node, ast.If):
            add(index, "if", line, "condition negated")
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id in ("max", "min")):
            swap = "min" if node.func.id == "max" else "max"
            add(index, "minmax", line, f"{node.func.id} -> {swap}", detail=ast.unparse(node))
        elif isinstance(node, ast.Continue):
            add(index, "continue", line, "continue -> pass")
    # Two flips that read the same on one line (`a <= b and c <= d`) would
    # share a key, so no exclusion could name one of them: say which
    # comparison each flips.
    twins = Counter((m.line, m.operator, m.change) for m in out)
    return [replace(m, change=f"{m.change} in `{m.detail}`")
            if m.detail and twins[(m.line, m.operator, m.change)] > 1 else m for m in out]


@dataclass(frozen=True)
class Exclusion:
    """A surviving mutant a person has looked at, and what they concluded.
    Recognised by module, operator and the original line (not the line
    number, which moves with every edit above it), optionally narrowed by
    the enclosing `function` or the exact `change`.

    `kind` says what the conclusion was (Hermes audit round 6):

    - `equivalent`: no input can tell the mutant from the original.
      Excluded from the rate.
    - `accepted`: the mutant DOES behave differently on some input, and a
      person accepted that — a deliberate policy boundary (a display cap), or
      a difference reachable only on malformed input. Excluded from the rate
      but listed on its own, with an `owner` and a `review_by` date; the run
      fails once that date has passed.
    - `tracked`: a known gap being worked. NOT excluded: it still counts as a
      survivor; the summary only names it.
    """

    path: str
    operator: str
    source: str
    reason: str
    kind: str = "equivalent"
    function: str = ""  # optional: narrows to one enclosing function
    change: str = ""  # optional: narrows to one mutation of the line
    owner: str = ""
    review_by: dt.date | None = None

    def matches(self, m: Mutant) -> bool:
        return ((m.target.path, m.operator, m.source) == (self.path, self.operator, self.source)
                and (not self.function or m.function == self.function)
                and (not self.change or m.change == self.change))

    @property
    def excludes(self) -> bool:
        return self.kind in ("equivalent", "accepted")


def load_exclusions(path: Path = EXCLUSIONS) -> tuple[Exclusion, ...]:
    """The reviewed survivors. Every entry needs path, operator, source, a
    non-empty reason and a known kind; an `accepted` one also an owner and a
    review-by date. An entry short of any of these is refused."""
    if not path.exists():
        return ()
    entries = tomllib.loads(path.read_text()).get("exclusion", [])
    out = []
    for n, e in enumerate(entries):
        where = f"{path.name} entry {n + 1}"
        missing = [k for k in ("path", "operator", "source", "reason", "kind")
                   if not isinstance(e.get(k), str) or not e[k].strip()]
        if missing:
            raise ValueError(f"{where}: missing {', '.join(missing)}")
        if e["kind"] not in KINDS:
            raise ValueError(f"{where}: kind {e['kind']!r} is not one of {', '.join(KINDS)}")
        review_by = e.get("review_by")
        if isinstance(review_by, str):
            review_by = dt.date.fromisoformat(review_by)
        if e["kind"] == "accepted" and not (
            isinstance(e.get("owner"), str) and e["owner"].strip() and isinstance(review_by, dt.date)
        ):
            raise ValueError(f"{where}: an accepted mutation needs an owner and a review_by date")
        extra = {k: e[k] for k in ("function", "change", "owner") if isinstance(e.get(k), str)}
        out.append(Exclusion(e["path"], e["operator"], e["source"].strip(), e["reason"], e["kind"],
                             review_by=review_by if isinstance(review_by, dt.date) else None,
                             **extra))
    return tuple(out)


def invalid_exclusions(exclusions: tuple[Exclusion, ...], root: Path = ROOT,
                       targets: tuple[Target, ...] | None = None,
                       today: dt.date | None = None) -> list[str]:
    """Why each unusable entry is unusable. An entry must match exactly ONE
    current mutation site — none: the code it argued about has changed;
    several: the argument was made about one site and would silently excuse
    the others (name the `function` or `change`). An accepted entry past its
    review date must be looked at again."""
    today = today or dt.date.today()
    by_path = {t.path: t for t in (TARGETS if targets is None else targets)}
    problems = []
    for e in exclusions:
        label = f"{e.path} [{e.operator}] {e.source}"
        t = by_path.get(e.path)
        hits = [] if t is None else [m for m in sites(t, (root / t.path).read_text()) if e.matches(m)]
        if len(hits) != 1:
            problems.append(f"matches {len(hits)} sites, not exactly one: {label}")
        if e.kind == "accepted" and e.review_by is not None and e.review_by < today:
            problems.append(f"accepted mutation past its review date {e.review_by}: {label}")
    return problems


def apply(mutant: Mutant, source: str) -> str:
    """The module source with this one mutation applied."""
    tree = ast.parse(source)
    node = next(n for i, n in enumerate(ast.walk(tree)) if i == mutant.index)
    if mutant.operator == "cmp":
        assert isinstance(node, ast.Compare)
        node.ops[0] = _FLIPS[type(node.ops[0])]()
    elif mutant.operator == "const":
        assert isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None
        assert isinstance(node.value, ast.Constant)
        node.value.value = _nudged(node.value.value)
    elif mutant.operator == "if":
        assert isinstance(node, ast.If)
        node.test = ast.UnaryOp(op=ast.Not(), operand=node.test)
    elif mutant.operator == "minmax":
        assert isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        node.func.id = "min" if node.func.id == "max" else "max"
    elif mutant.operator == "continue":
        parent = next(p for p in ast.walk(tree)
                      for f in ast.iter_fields(p) if isinstance(f[1], list) and node in f[1])
        for _name, value in ast.iter_fields(parent):
            if isinstance(value, list) and node in value:
                value[value.index(node)] = ast.Pass()
    else:  # pragma: no cover - the operator set is closed
        raise ValueError(mutant.operator)
    return ast.unparse(ast.fix_missing_locations(tree)) + "\n"


def sample(per_target: list[list[Mutant]], budget: int, seed: int) -> list[Mutant]:
    """Up to `budget` mutants, drawn round-robin across targets so each gets
    a fair share, each target's order shuffled by `seed`."""
    pools = []
    for i, pool in enumerate(per_target):
        shuffled = list(pool)
        random.Random(f"{seed}:{i}").shuffle(shuffled)
        pools.append(iter(shuffled))
    out: list[Mutant] = []
    while len(out) < budget and pools:
        for it in list(pools):
            m = next(it, None)
            if m is None:
                pools.remove(it)
            else:
                out.append(m)
                if len(out) == budget:
                    break
    return out


Runner = Callable[[Path, tuple[str, ...], float], str]  # -> "pass" | "fail" | "timeout"


def run_tests(workspace: Path, tests: tuple[str, ...], timeout: float) -> str:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *tests],
            cwd=workspace, env=env, capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "timeout"
    return "pass" if proc.returncode == 0 else "fail"


@dataclass
class Report:
    killed: list[Mutant] = field(default_factory=list)
    survived: list[Mutant] = field(default_factory=list)
    timed_out: list[Mutant] = field(default_factory=list)
    not_run: list[Mutant] = field(default_factory=list)
    broken_baseline: list[Target] = field(default_factory=list)
    excluded: list[tuple[Mutant, Exclusion]] = field(default_factory=list)
    tracked: tuple[Exclusion, ...] = ()

    @property
    def run(self) -> int:
        return len(self.killed) + len(self.survived) + len(self.timed_out)

    @property
    def kill_rate(self) -> float | None:
        """Killed share of the mutants run, or None when none ran — nothing
        was measured, which is not a perfect score (Hermes audit round 5).
        A mutant that made the tests time out is NOT counted as killed
        (round 6): a hang is not a failing assertion, and it is listed."""
        return len(self.killed) / self.run if self.run else None

    def markdown(self, seed: int) -> str:
        rate = self.kill_rate
        shown = f"**{rate:.0%}**" if rate is not None else "**n/a — no mutant ran**"
        lines = [
            "## Mutation run",
            "",
            f"Seed {seed}. {self.run} mutant(s) run: {len(self.killed)} killed, "
            f"{len(self.survived)} survived, {len(self.timed_out)} timed out (not counted "
            f"as killed) — kill rate {shown}. Excluded after review: "
            f"{self._count('equivalent')} equivalent, {self._count('accepted')} accepted."
            + (f" {len(self.not_run)} not run (wall clock)." if self.not_run else ""),
        ]
        for t in self.broken_baseline:
            lines.append(f"\n**Baseline red, target skipped:** `{t.path}` — its tests fail "
                         "unmutated. The run fails: none of its mutants was measured.")
        equivalent = [(m, e) for m, e in self.excluded if e.kind == "equivalent"]
        accepted = [(m, e) for m, e in self.excluded if e.kind == "accepted"]
        if equivalent:
            lines += ["", "Equivalent — no input tells them from the original "
                      "(`tests/tools/mutation_exclusions.toml`):", "",
                      "| Module | Line | Operator | Change | Why |", "|---|---|---|---|---|"]
            lines += [f"| `{m.target.path}` | {m.line} | {m.operator} | `{m.change}` | {e.reason} |"
                      for m, e in equivalent]
        if accepted:
            lines += ["", "Accepted — they DO change behaviour on some input; a person "
                      "accepted that:", "",
                      "| Module | Line | Operator | Change | Why accepted | Owner | Review by |",
                      "|---|---|---|---|---|---|---|"]
            lines += [f"| `{m.target.path}` | {m.line} | {m.operator} | `{m.change}` | {e.reason} "
                      f"| {e.owner} | {e.review_by} |" for m, e in accepted]
        if self.timed_out:
            lines += ["", "Timed out — the tests hung under the mutant (not counted as killed):", "",
                      "| Module | Line | Operator | Change |", "|---|---|---|---|"]
            lines += [f"| `{m.target.path}` | {m.line} | {m.operator} | `{m.change}` |"
                      for m in self.timed_out]
        if self.survived:
            lines += ["", "Survivors — a gap in the tests, or a mutant not yet reviewed:", "",
                      "| Module | Line | Operator | Change | Tracked |", "|---|---|---|---|---|"]
            for m in self.survived:
                note = next((e.reason for e in self.tracked if e.matches(m)), "")
                lines.append(f"| `{m.target.path}` | {m.line} | {m.operator} | `{m.change}` | {note} |")
        return "\n".join(lines) + "\n"

    def _count(self, kind: str) -> int:
        return sum(1 for _m, e in self.excluded if e.kind == kind)


def _workspace(root: Path) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="mutate-"))
    ignore = shutil.ignore_patterns(".git", ".venv", "__pycache__", ".mypy_cache",
                                    ".ruff_cache", ".pytest_cache", "data", "reports")
    shutil.copytree(root, tmp / "repo", ignore=ignore)
    return tmp / "repo"


def run(
    targets: tuple[Target, ...] = TARGETS,
    *,
    budget: int = 60,
    wall: float = 600.0,
    per_mutant: float = 180.0,
    seed: int | None = None,
    root: Path = ROOT,
    runner: Runner = run_tests,
    clock: Callable[[], float] = time.monotonic,
    exclusions: tuple[Exclusion, ...] = (),
) -> tuple[Report, int]:
    seed = dt.date.today().isocalendar().week if seed is None else seed
    workspace = _workspace(root)
    report = Report(tracked=tuple(e for e in exclusions if not e.excludes))
    try:
        live: list[Target] = []
        for t in targets:
            if runner(workspace, t.tests, per_mutant) == "pass":
                live.append(t)
            else:
                report.broken_baseline.append(t)
        pools: list[list[Mutant]] = []
        for t in live:
            pool = []
            for m in sites(t, (root / t.path).read_text()):
                match = next((e for e in exclusions if e.excludes and e.matches(m)), None)
                if match is None:
                    pool.append(m)
                else:
                    report.excluded.append((m, match))
            pools.append(pool)
        chosen = sample(pools, budget, seed)
        start = clock()
        for n, m in enumerate(chosen):
            if clock() - start > wall:
                report.not_run = chosen[n:]
                break
            path = workspace / m.target.path
            original = path.read_text()
            path.write_text(apply(m, original))
            try:
                outcome = runner(workspace, m.target.tests, per_mutant)
            finally:
                path.write_text(original)
            {"pass": report.survived, "fail": report.killed,
             "timeout": report.timed_out}[outcome].append(m)
    finally:
        shutil.rmtree(workspace.parent, ignore_errors=True)
    return report, seed


def changed_targets(changed: Iterable[str], targets: tuple[Target, ...] = TARGETS) -> tuple[Target, ...]:
    """The targets whose module is among `changed` (repository-relative
    paths) — a pull request's mutation run covers the code it touched."""
    touched = set(changed)
    return tuple(t for t in targets if t.path in touched)


def _changed_since(base: str, root: Path = ROOT) -> list[str]:
    out = subprocess.run(["git", "diff", "--name-only", f"{base}...HEAD"], cwd=root,
                         capture_output=True, text=True, check=True)
    return [line for line in out.stdout.splitlines() if line]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--budget", type=int, default=60)
    p.add_argument("--wall", type=float, default=600.0, help="seconds for all mutants")
    p.add_argument("--seed", type=int)
    p.add_argument("--floor", type=float, help="exit 1 below this kill rate (0-1)")
    p.add_argument("--summary", type=Path, help="markdown output (default: $GITHUB_STEP_SUMMARY)")
    p.add_argument("--list", action="store_true", help="list sites per target and exit")
    p.add_argument("--changed", metavar="BASE",
                   help="only the targets whose module changed since BASE (a pull request's run)")
    p.add_argument("--targets-only", action="store_true",
                   help="with --changed: print the changed targets' modules and exit")
    args = p.parse_args(argv)
    if args.targets_only and not args.changed:
        p.error("--targets-only needs --changed")

    try:
        exclusions = load_exclusions()
    except ValueError as e:
        print(f"mutation_exclusions.toml: {e}")
        return 2
    if problems := invalid_exclusions(exclusions):
        for problem in problems:
            print(f"mutation_exclusions.toml: {problem}")
        return 2

    if args.list:
        for t in TARGETS:
            found = sites(t, (ROOT / t.path).read_text())
            print(f"{t.path}{'::' + t.function if t.function else ''}: {len(found)} site(s)")
        return 0
    targets = TARGETS
    if args.changed:
        targets = changed_targets(_changed_since(args.changed))
        if args.targets_only:
            for t in targets:
                print(t.path)
            return 0
        if not targets:
            text = f"## Mutation run\n\nNo mutation target changed since `{args.changed}`.\n"
            print(text)
            out = args.summary or (Path(s) if (s := os.environ.get("GITHUB_STEP_SUMMARY")) else None)
            if out is not None:
                with out.open("a") as fh:
                    fh.write(text)
            return 0
    report, seed = run(targets=targets, budget=args.budget, wall=args.wall, seed=args.seed,
                       exclusions=exclusions)
    text = report.markdown(seed)
    if args.changed:
        text = f"Targets changed since `{args.changed}`: " + ", ".join(
            f"`{t.path}`" for t in targets) + "\n\n" + text
    print(text)
    out = args.summary or (Path(s) if (s := os.environ.get("GITHUB_STEP_SUMMARY")) else None)
    if out is not None:
        with out.open("a") as fh:
            fh.write(text)
    if report.broken_baseline:
        print("baseline red: " + ", ".join(t.path for t in report.broken_baseline)
              + " — their mutants were not measured")
        return 2
    if args.floor is not None:
        rate = report.kill_rate
        if rate is None:
            print(f"no mutant ran; the floor {args.floor:.0%} cannot be met")
            return 1
        if rate < args.floor:
            print(f"kill rate {rate:.0%} below the floor {args.floor:.0%}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
