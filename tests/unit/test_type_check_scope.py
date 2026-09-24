"""The type check covers the operator scripts.

`earnings_brief.py assume --derive` shipped calling `derive_for_ticker`
without its required `as_of` (Hermes audit round 3, finding 4). mypy would
have refused it; it was never asked, because it checked `app/` only. The
scripts are in scope now, and the scripts that carry known debt are
relaxed only for narrowing codes — never for a missing or unexpected
argument.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MYPY = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["mypy"]


def test_scripts_are_type_checked():
    assert "scripts" in MYPY["files"]


def test_script_debt_never_relaxes_call_arguments():
    for override in MYPY.get("overrides", []):
        modules = override["module"]
        if not any("." not in m for m in modules):  # app.* overrides
            continue
        assert "call-arg" not in override.get("disable_error_code", [])
        assert not override.get("ignore_errors", False)


def test_the_print_night_scripts_carry_no_debt():
    relaxed = {m for o in MYPY.get("overrides", []) for m in o["module"] if "." not in m}
    assert not relaxed & {"earnings_brief", "generate_report"}
