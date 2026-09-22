#!/usr/bin/env python3
"""Fail if the installed environment is not exactly requirements.lock.

CI installs `pip install -r requirements.lock`, then the project with
--no-deps. Two gaps made that less reproducible than it looked:

- A transitive dependency missing from the lock is still installed — at
  whatever version is newest that day. `packaging` (pytest's dependency) was
  missing, so every CI run resolved it afresh.
- Nothing checked that the pyproject ranges and the lock agree, so a
  dependency added to pyproject but not to the lock surfaced only as an
  ImportError, or not at all.

This compares the installed distributions (importlib.metadata; works in pip
and uv environments) with the lock, by normalized name and exact version,
and checks every pyproject requirement (runtime, dev, web) is pinned in the
lock at a version its specifier allows. Exit 1 with a list of differences.

    python scripts/check_lock.py            # CI, after installing the lock
    python scripts/check_lock.py --static   # pyproject-vs-lock only (no env)
"""

from __future__ import annotations

import re
import sys
import tomllib
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "requirements.lock"
PYPROJECT = ROOT / "pyproject.toml"
PROJECT = "financial-quality-engine"
# Installer plumbing `pip freeze` also leaves out.
NOT_LOCKED = {"pip", "setuptools", "wheel", "distribute"}


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def lock_pins(text: str) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, version = line.partition("==")
        if not sep:
            raise SystemExit(f"requirements.lock: not an exact pin: {line!r}")
        key = normalize(name)
        if key in pins:
            raise SystemExit(f"requirements.lock: {name} pinned twice")
        pins[key] = version
    return pins


def pyproject_requirements(text: str) -> list[Requirement]:
    project = tomllib.loads(text)["project"]
    reqs = list(project["dependencies"])
    for extra in ("dev", "web"):
        reqs += project.get("optional-dependencies", {}).get(extra, [])
    return [Requirement(r) for r in reqs]


def static_problems(pins: dict[str, str], reqs: list[Requirement]) -> list[str]:
    problems = []
    for req in reqs:
        pinned = pins.get(normalize(req.name))
        if pinned is None:
            problems.append(f"{req} is required by pyproject but not pinned in the lock")
        elif not req.specifier.contains(Version(pinned), prereleases=True):
            problems.append(f"{req} is required by pyproject but the lock pins {pinned}")
    return problems


def installed() -> dict[str, str]:
    out: dict[str, str] = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if name:
            out[normalize(name)] = dist.version
    return out


def environment_problems(pins: dict[str, str], env: dict[str, str]) -> list[str]:
    problems = []
    env = {k: v for k, v in env.items() if k not in NOT_LOCKED and k != PROJECT}
    for name in sorted(env.keys() - pins.keys()):
        problems.append(f"{name}=={env[name]} is installed but not in the lock (unpinned transitive?)")
    for name in sorted(pins.keys() - env.keys()):
        problems.append(f"{name}=={pins[name]} is in the lock but not installed")
    for name in sorted(pins.keys() & env.keys()):
        if pins[name] != env[name]:
            problems.append(f"{name}: lock pins {pins[name]}, installed {env[name]}")
    return problems


def main(argv: list[str]) -> int:
    pins = lock_pins(LOCK.read_text())
    problems = static_problems(pins, pyproject_requirements(PYPROJECT.read_text()))
    if "--static" not in argv:
        problems += environment_problems(pins, installed())
    for p in problems:
        print(f"lock: {p}", file=sys.stderr)
    if problems:
        print(f"lock: {len(problems)} problem(s); see scripts/check_lock.py", file=sys.stderr)
        return 1
    print(f"lock: OK ({len(pins)} pins)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
