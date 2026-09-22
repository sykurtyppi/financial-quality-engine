"""`hypothesis` is a test-time dependency only. The runtime stays pydantic +
fastapi (AGENTS.md §1); an `app/` module importing it would install fine in
CI — the lock carries it for the tests — and then fail on any deployment
built from `[project].dependencies`."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_no_app_module_imports_hypothesis():
    pattern = re.compile(r"^\s*(import|from)\s+hypothesis\b", re.MULTILINE)
    offenders = [
        str(p.relative_to(ROOT)) for p in (ROOT / "app").rglob("*.py")
        if pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_hypothesis_is_declared_dev_only():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert not any(d.startswith("hypothesis") for d in project["dependencies"])
    assert any(d.startswith("hypothesis") for d in project["optional-dependencies"]["dev"])
