"""Guards for the tooling added with CI lint (PR 1.7).

- Every script imports. Lint autofixes remove imports ruff sees as unused,
  and a name one module re-imports for another (scripts/run_restatement_
  control.py took P90 and `band` through restatement_control) would break
  only when that script next ran. Scripts are all `__main__`-guarded, so
  importing them runs nothing.
- The lock pins every pyproject requirement at an allowed version
  (scripts/check_lock.py --static; the installed-set comparison runs in CI,
  where the environment is built from the lock).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = sorted((ROOT / "scripts").glob("*.py"))


def _load(path: Path):
    name = f"_script_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # Registered first, as a real import would be: @dataclass resolves its
    # class's module through sys.modules.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


@pytest.mark.parametrize("path", SCRIPTS, ids=[p.name for p in SCRIPTS])
def test_every_script_imports(path):
    _load(path)


def test_the_lock_pins_every_pyproject_requirement():
    check = _load(ROOT / "scripts" / "check_lock.py")
    pins = check.lock_pins((ROOT / "requirements.lock").read_text())
    reqs = check.pyproject_requirements((ROOT / "pyproject.toml").read_text())
    assert check.static_problems(pins, reqs) == []


def test_the_lock_check_catches_an_unpinned_transitive_and_a_version_skew():
    check = _load(ROOT / "scripts" / "check_lock.py")
    pins = {"pytest": "9.1.1", "pydantic": "2.13.4"}
    env = {"pytest": "9.1.1", "pydantic": "2.13.5", "packaging": "26.3",
           "pip": "25.0", "financial-quality-engine": "0.4.0"}
    assert check.environment_problems(pins, env) == [
        "packaging==26.3 is installed but not in the lock (unpinned transitive?)",
        "pydantic: lock pins 2.13.4, installed 2.13.5",
    ]
    from packaging.requirements import Requirement

    assert check.static_problems({"pydantic": "2.6.0"}, [Requirement("pydantic>=2.7")]) == [
        "pydantic>=2.7 is required by pyproject but the lock pins 2.6.0"
    ]
    assert check.static_problems({}, [Requirement("hypothesis>=6")]) == [
        "hypothesis>=6 is required by pyproject but not pinned in the lock"
    ]
