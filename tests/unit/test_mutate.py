"""The mutation harness (`tests/tools/mutate.py`) finds what it claims to.

Its weekly verdict is only worth the operators' faithfulness: each must
change exactly the construct it names, the sample must be reproducible from
its seed, and the accounting must not count a hang as a kill, a target
whose tests were already red as covered, or an unreviewed survivor as
anything but a survivor.
"""

from __future__ import annotations

import ast
import datetime as dt
from pathlib import Path

import pytest

from tests.tools import mutate
from tests.tools.mutate import Exclusion, Mutant, Report, Target, apply, sample, sites

ROOT = Path(__file__).resolve().parents[2]

SOURCE = '''\
LIMIT = 5
RATE = 0.25
flag = True
lower = 3


def pick(xs):
    out = []
    for x in xs:
        if x < LIMIT:
            continue
        out.append(max(x, RATE))
    return out


def other(a, b):
    if a == b:
        return min(a, b)
    return a >= b
'''

T = Target("pkg/mod.py", ("tests/test_mod.py",))


def _by_op(found):
    return {op: [m for m in found if m.operator == op] for op in
            ("cmp", "const", "if", "minmax", "continue")}


def test_every_operator_finds_its_sites_and_only_those():
    ops = _by_op(sites(T, SOURCE))
    assert sorted(m.change for m in ops["cmp"]) == ["< -> <=", "== -> !=", ">= -> >"]
    assert [m.change for m in ops["const"]] == ["LIMIT = 5 -> 6", "RATE = 0.25 -> 0.2525"]
    assert len(ops["if"]) == 2
    assert sorted(m.change for m in ops["minmax"]) == ["max -> min", "min -> max"]
    assert [m.change for m in ops["continue"]] == ["continue -> pass"]


def test_each_mutant_changes_exactly_its_construct():
    base = ast.dump(ast.parse(SOURCE))
    for m in sites(T, SOURCE):
        mutated = apply(m, SOURCE)
        assert ast.dump(ast.parse(mutated)) != base, m.label
        compile(mutated, "mod.py", "exec")
    ops = _by_op(sites(T, SOURCE))
    lt = next(m for m in ops["cmp"] if m.change == "< -> <=")
    assert "if x <= LIMIT" in apply(lt, SOURCE)
    assert "LIMIT = 6" in apply(ops["const"][0], SOURCE)
    negated = [apply(m, SOURCE) for m in ops["if"]]
    assert any("if not x < LIMIT" in text for text in negated)
    assert any("if not a == b" in text for text in negated)
    to_min = next(m for m in ops["minmax"] if m.change == "max -> min")
    assert "min(x, RATE)" in apply(to_min, SOURCE)
    body = apply(ops["continue"][0], SOURCE)
    assert "continue" not in body and "pass" in body


def test_a_function_target_mutates_only_that_function():
    only = Target("pkg/mod.py", ("t",), function="other")
    lines = {m.line for m in sites(only, SOURCE)}
    lo = SOURCE.splitlines().index("def other(a, b):") + 1
    assert lines and min(lines) > lo
    assert not [m for m in sites(only, SOURCE) if m.operator in ("const", "continue")]


def test_the_sample_is_reproducible_from_its_seed_and_fair_across_targets():
    a = [Mutant(T, i, "cmp", i, "x") for i in range(10)]
    b = [Mutant(Target("b.py", ("t",)), i, "if", i, "y") for i in range(3)]
    first = sample([a, b], 6, seed=7)
    assert first == sample([a, b], 6, seed=7)
    assert first != sample([a, b], 6, seed=8)
    assert sum(1 for m in first if m.target.path == "b.py") == 3  # round-robin
    assert len(sample([a, b], 100, seed=1)) == 13  # the budget caps, it does not pad


def _fake(outcomes: dict[str, str], baseline: dict[str, str] | None = None):
    """A runner answering by which module is currently mutated."""
    def runner(workspace, tests, timeout):
        for path, outcome in outcomes.items():
            if (workspace / path).read_text() != (ROOT / path).read_text():
                return outcome
        return (baseline or {}).get(tests[0], "pass")
    return runner


def test_outcomes_are_counted_honestly(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("X = 1\n")
    (pkg / "b.py").write_text("Y = 2\n")
    (pkg / "c.py").write_text("Z = 3\n")
    targets = (Target("pkg/a.py", ("ta",)), Target("pkg/b.py", ("tb",)),
               Target("pkg/c.py", ("tc",)), Target("pkg/c.py", ("broken",)))

    def runner(workspace, tests, timeout):
        if tests == ("broken",):
            return "fail"  # red before any mutation
        mutated = {p for p in ("pkg/a.py", "pkg/b.py", "pkg/c.py")
                   if (workspace / p).read_text() != (tmp_path / p).read_text()}
        return {"pkg/a.py": "fail", "pkg/b.py": "pass", "pkg/c.py": "timeout"}.get(
            next(iter(mutated), ""), "pass")

    report, seed = mutate.run(targets, budget=10, seed=3, root=tmp_path, runner=runner)
    assert seed == 3
    assert [m.target.path for m in report.killed] == ["pkg/a.py"]
    assert [m.target.path for m in report.survived] == ["pkg/b.py"]
    assert [m.target.path for m in report.timed_out] == ["pkg/c.py"]
    assert [t.tests for t in report.broken_baseline] == [("broken",)]
    # A hang is not a failing assertion: only the one kill counts (round 6).
    assert report.kill_rate == pytest.approx(1 / 3)
    text = report.markdown(seed)
    assert "1 survived, 1 timed out (not counted as killed)" in text
    assert "Timed out" in text and "| `pkg/c.py` | 1 | const | `Z = 3 -> 4` |" in text
    assert "| `pkg/b.py` | 1 | const | `Y = 2 -> 3` |" in text
    assert "Baseline red, target skipped:** `pkg/c.py`" in text
    # The working tree is never touched.
    assert (tmp_path / "pkg" / "a.py").read_text() == "X = 1\n"


def test_the_wall_clock_leaves_the_rest_not_run(tmp_path):
    (tmp_path / "m.py").write_text("A = 1\nB = 2\nC = 3\n")
    ticks = iter([0.0, 0.0, 5.0, 50.0])  # start, then before each mutant
    calls = iter(["pass", "fail", "fail", "fail"])  # the baseline passes
    report, _ = mutate.run((Target("m.py", ("t",)),), budget=3, wall=10, seed=1, root=tmp_path,
                           runner=lambda w, t, s: next(calls), clock=lambda: next(ticks))
    assert len(report.killed) == 2 and len(report.not_run) == 1
    assert "1 not run (wall clock)" in report.markdown(1)


def test_the_floor_decides_the_exit_code(monkeypatch, tmp_path):
    weak = Report(killed=[Mutant(T, 0, "cmp", 1, "x")], survived=[Mutant(T, 1, "if", 2, "y")])
    monkeypatch.setattr(mutate, "run", lambda **kw: (weak, 1))
    summary = tmp_path / "s.md"
    assert mutate.main(["--floor", "0.8", "--summary", str(summary)]) == 1
    assert mutate.main(["--floor", "0.5", "--summary", str(summary)]) == 0
    assert mutate.main(["--summary", str(summary)]) == 0  # informational
    assert "kill rate **50%**" in summary.read_text()


def test_every_configured_target_and_test_file_exists():
    for t in mutate.TARGETS:
        assert (ROOT / t.path).exists(), t.path
        for test in t.tests:
            assert (ROOT / test).exists(), test
        assert sites(t, (ROOT / t.path).read_text()), t.path


def test_end_to_end_a_tested_mutant_dies_and_an_untested_one_survives(tmp_path):
    """A real pytest subprocess in a temp copy of a tiny project."""
    (tmp_path / "lib.py").write_text(
        "def cap(x):\n    if x > 10:\n        return 0\n    return x\n\n"
        "def unused(x):\n    return x >= 0\n"
    )
    (tmp_path / "test_lib.py").write_text(
        "from lib import cap\n\ndef test_cap():\n    assert cap(11) == 0\n"
        "    assert cap(10) == 10\n    assert cap(3) == 3\n"
    )
    target = Target("lib.py", ("test_lib.py",))
    report, _ = mutate.run((target,), budget=10, seed=1, root=tmp_path, per_mutant=60)
    killed = {m.change for m in report.killed}
    survived = {m.change for m in report.survived}
    assert "condition negated" in killed and "> -> >=" in killed
    assert survived == {">= -> >"}  # `unused` has no test


def test_a_pull_request_run_covers_only_the_targets_it_touched(monkeypatch, tmp_path):
    assert mutate.changed_targets(["app/services/ingestion/precedence.py", "README.md"]) == tuple(
        t for t in mutate.TARGETS if t.path == "app/services/ingestion/precedence.py"
    )
    assert mutate.changed_targets(["docs/x.md"]) == ()
    seen = {}

    def fake_run(**kw):
        seen["targets"] = kw["targets"]
        return Report(killed=[Mutant(T, 0, "cmp", 1, "x")]), 1

    monkeypatch.setattr(mutate, "run", fake_run)
    summary = tmp_path / "s.md"
    monkeypatch.setattr(mutate, "_changed_since",
                        lambda base, root=mutate.ROOT: ["app/services/journal/resolver.py"])
    assert mutate.main(["--changed", "origin/main", "--summary", str(summary)]) == 0
    assert [t.path for t in seen["targets"]] == ["app/services/journal/resolver.py"]
    assert "Targets changed since `origin/main`: `app/services/journal/resolver.py`" in summary.read_text()
    # Nothing touched: no run, and the summary says so.
    seen.clear()
    monkeypatch.setattr(mutate, "_changed_since", lambda base, root=mutate.ROOT: ["docs/x.md"])
    assert mutate.main(["--changed", "origin/main", "--summary", str(summary)]) == 0
    assert "targets" not in seen
    assert "No mutation target changed since `origin/main`." in summary.read_text()


# --- Hermes audit round 5: the harness must not report assurance it lacks ----------


def test_a_red_baseline_is_not_a_perfect_score(monkeypatch, tmp_path):
    """Hermes's reproduction: one target, red before mutation, `--floor 1`.
    Nothing ran, so the kill rate is not 100% — it is not measured — and the
    run fails with or without a floor."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("X = 1\n")
    report, _ = mutate.run((Target("pkg/a.py", ("t",)),), budget=5, seed=1, root=tmp_path,
                           runner=lambda w, t, s: "fail")
    assert report.run == 0 and len(report.broken_baseline) == 1
    assert report.kill_rate is None
    assert "n/a — no mutant ran" in report.markdown(1)

    monkeypatch.setattr(mutate, "run", lambda **kw: (report, 1))
    summary = tmp_path / "s.md"
    assert mutate.main(["--floor", "1", "--summary", str(summary)]) == 2
    assert mutate.main(["--summary", str(summary)]) == 2  # no floor: still a failure


def test_a_floor_with_nothing_run_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(mutate, "run", lambda **kw: (Report(), 1))
    summary = tmp_path / "s.md"
    assert mutate.main(["--floor", "0.8", "--summary", str(summary)]) == 1
    assert mutate.main(["--summary", str(summary)]) == 0  # informational, and says n/a
    assert "n/a — no mutant ran" in summary.read_text()


def _eq_project(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("def f(x):\n    if x > 0:\n        return 1\n    return x < 5\n")
    return Target("pkg/m.py", ("t",))


def _runner_for(tmp_path, survives):
    def runner(workspace, tests, timeout):
        text = (workspace / "pkg" / "m.py").read_text()
        if text == (tmp_path / "pkg" / "m.py").read_text():
            return "pass"  # baseline
        return "pass" if any(s in text for s in survives) else "fail"
    return runner


def test_equivalent_and_accepted_mutants_are_excluded_and_listed_apart(tmp_path):
    t = _eq_project(tmp_path)
    eq = Exclusion("pkg/m.py", "cmp", "if x > 0:", "x is never 0 here")
    acc = Exclusion("pkg/m.py", "cmp", "return x < 5", "a display cap", kind="accepted",
                    owner="repo owner", review_by=dt.date(2099, 1, 1))
    runner = _runner_for(tmp_path, ("x >= 0", "x <= 5"))
    report, _ = mutate.run((t,), budget=10, seed=1, root=tmp_path, runner=runner,
                           exclusions=(eq, acc))
    assert sorted((m.source, e.kind) for m, e in report.excluded) == [
        ("if x > 0:", "equivalent"), ("return x < 5", "accepted")]
    assert report.survived == [] and report.kill_rate == 1.0
    text = report.markdown(1)
    assert "Excluded after review: 1 equivalent, 1 accepted." in text
    equivalent, accepted = text.split("Accepted —")
    assert "x is never 0 here" in equivalent and "a display cap" not in equivalent
    assert "| a display cap | repo owner | 2099-01-01 |" in accepted


def test_a_tracked_survivor_still_counts_against_the_rate(tmp_path):
    t = _eq_project(tmp_path)
    tracked = Exclusion("pkg/m.py", "cmp", "if x > 0:", "gap: test due 10-08", kind="tracked")
    report, _ = mutate.run((t,), budget=10, seed=1, root=tmp_path,
                           runner=_runner_for(tmp_path, ("x >= 0",)), exclusions=(tracked,))
    assert [m.source for m in report.survived] == ["if x > 0:"] and report.excluded == []
    assert report.kill_rate is not None and report.kill_rate < 1.0
    assert "| gap: test due 10-08 |" in report.markdown(1)


def _write(tmp_path, body):
    f = tmp_path / "ex.toml"
    f.write_text(body)
    return f


ENTRY = '[[exclusion]]\npath = "pkg/m.py"\noperator = "cmp"\nsource = "if x > 0:"\n'


def test_an_exclusion_needs_a_reason_and_a_known_kind(tmp_path):
    with pytest.raises(ValueError, match="missing reason, kind"):
        mutate.load_exclusions(_write(tmp_path, ENTRY))
    with pytest.raises(ValueError, match="missing kind"):
        mutate.load_exclusions(_write(tmp_path, ENTRY + 'reason = "r"\n'))
    with pytest.raises(ValueError, match="'probably' is not one of equivalent, accepted, tracked"):
        mutate.load_exclusions(_write(tmp_path, ENTRY + 'reason = "r"\nkind = "probably"\n'))
    (e,) = mutate.load_exclusions(_write(tmp_path, ENTRY + 'reason = "r"\nkind = "equivalent"\n'))
    assert e.kind == "equivalent" and e.excludes


def test_an_accepted_exclusion_needs_an_owner_and_a_review_date(tmp_path):
    base = ENTRY + 'reason = "r"\nkind = "accepted"\n'
    for extra in ("", 'owner = "me"\n', "review_by = 2026-11-20\n", 'owner = " "\nreview_by = 2026-11-20\n'):
        with pytest.raises(ValueError, match="needs an owner and a review_by date"):
            mutate.load_exclusions(_write(tmp_path, base + extra))
    for date in ("2026-11-20", '"2026-11-20"'):  # a TOML date or an ISO string
        (e,) = mutate.load_exclusions(_write(tmp_path, base + f'owner = "me"\nreview_by = {date}\n'))
        assert (e.owner, e.review_by, e.excludes) == ("me", dt.date(2026, 11, 20), True)


def test_an_exclusion_must_match_a_current_site_and_an_acceptance_expires(tmp_path):
    t = _eq_project(tmp_path)
    live = Exclusion("pkg/m.py", "cmp", "if x > 0:", "r")
    gone = Exclusion("pkg/m.py", "cmp", "if x > 1:", "r")
    assert mutate.invalid_exclusions((live, gone), root=tmp_path, targets=(t,)) == [
        "matches 0 sites, not exactly one: pkg/m.py [cmp] if x > 1:"]
    acc = Exclusion("pkg/m.py", "cmp", "if x > 0:", "r", kind="accepted", owner="me",
                    review_by=dt.date(2026, 11, 20))
    on_the_day = dt.date(2026, 11, 20)
    assert mutate.invalid_exclusions((acc,), root=tmp_path, targets=(t,), today=on_the_day) == []
    assert mutate.invalid_exclusions((acc,), root=tmp_path, targets=(t,),
                                     today=on_the_day + dt.timedelta(days=1)) == [
        "accepted mutation past its review date 2026-11-20: pkg/m.py [cmp] if x > 0:"]


def test_the_committed_exclusions_are_valid_and_honestly_classified():
    exclusions = mutate.load_exclusions()
    assert mutate.invalid_exclusions(exclusions) == []
    # The display cap changes what renders (21 rows show instead of 20 plus
    # "+1 more"): it is accepted policy, never called equivalent (round 6).
    (cap,) = [e for e in exclusions if e.source.startswith("_MAX_ROWS")]
    assert cap.kind == "accepted"
    for e in exclusions:
        if e.kind == "equivalent":
            assert "NOT" not in e.reason, e.source


def test_an_invalid_exclusion_fails_the_run(monkeypatch, tmp_path):
    gone = Exclusion("app/core/pipeline.py", "cmp", "if never_here > 0:", "r")
    monkeypatch.setattr(mutate, "load_exclusions", lambda path=None: (gone,))
    monkeypatch.setattr(mutate, "run", lambda **kw: (_ for _ in ()).throw(AssertionError("ran")))
    assert mutate.main(["--summary", str(tmp_path / "s.md")]) == 2

    def refuse(path=None):
        raise ValueError("entry 1: missing kind")
    monkeypatch.setattr(mutate, "load_exclusions", refuse)
    assert mutate.main(["--summary", str(tmp_path / "s.md")]) == 2


def test_targets_only_lists_the_changed_modules_for_the_workflow(monkeypatch, capsys):
    monkeypatch.setattr(mutate, "_changed_since",
                        lambda base, root=mutate.ROOT: ["app/core/pipeline.py", "docs/x.md"])
    monkeypatch.setattr(mutate, "run", lambda **kw: (_ for _ in ()).throw(AssertionError("ran")))
    assert mutate.main(["--changed", "origin/main", "--targets-only"]) == 0
    assert capsys.readouterr().out.split() == ["app/core/pipeline.py"]
    monkeypatch.setattr(mutate, "_changed_since", lambda base, root=mutate.ROOT: ["docs/x.md"])
    assert mutate.main(["--changed", "origin/main", "--targets-only"]) == 0
    assert capsys.readouterr().out == ""
    with pytest.raises(SystemExit):
        mutate.main(["--targets-only"])


def test_a_continue_is_keyed_by_the_guard_above_it():
    src = "def f(xs):\n    for x in xs:\n        if x:\n            continue\n        if not x:\n            continue\n"
    found = [m.source for m in sites(T, src) if m.operator == "continue"]
    assert found == ["if x: continue", "if not x: continue"]


def test_an_exclusion_must_match_exactly_one_site(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text(
        "def f(xs):\n    for x in xs:\n        if x:\n            continue\n\n"
        "def g(xs):\n    for x in xs:\n        if x:\n            continue\n"
        "\ndef h(a, b):\n    return a == 1 and b > 2\n"
    )
    t = Target("pkg/m.py", ("t",))
    both = Exclusion("pkg/m.py", "continue", "if x: continue", "r")
    in_g = Exclusion("pkg/m.py", "continue", "if x: continue", "r", function="g")
    line = Exclusion("pkg/m.py", "cmp", "return a == 1 and b > 2", "r")
    one = Exclusion("pkg/m.py", "cmp", "return a == 1 and b > 2", "r", change="> -> >=")
    # Ambiguous entries are refused; naming the function or the change fixes it.
    assert mutate.invalid_exclusions((both, in_g, line, one), root=tmp_path, targets=(t,)) == [
        "matches 2 sites, not exactly one: pkg/m.py [continue] if x: continue",
        "matches 2 sites, not exactly one: pkg/m.py [cmp] return a == 1 and b > 2"]
    (m,) = [m for m in sites(t, (tmp_path / "pkg" / "m.py").read_text()) if in_g.matches(m)]
    assert m.function == "g"


def test_twin_flips_on_one_line_are_told_apart():
    # `a <= b and c <= d` flips twice the same way: without naming the
    # comparison no exclusion could single one out.
    src = "def f(a, b, c, d):\n    return a <= b and c <= d and a > 0\n"
    cmps = [m for m in sites(T, src) if m.operator == "cmp"]
    assert [m.change for m in cmps] == ["<= -> < in `a <= b`", "<= -> < in `c <= d`", "> -> >="]
    only_cd = Exclusion("pkg/mod.py", "cmp", cmps[0].source, "r", change="<= -> < in `c <= d`")
    assert [m for m in cmps if only_cd.matches(m)] == [cmps[1]]
