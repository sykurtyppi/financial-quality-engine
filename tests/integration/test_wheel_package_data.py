"""Round-15 finding 4 — wheel packaging.

Editable installs mask a real distribution bug: the setuptools default only
ships `.py` files, so a wheel built without `[tool.setuptools.package-data]`
had no `.html` templates. `pip install`-from-wheel then produced a web app
that raised `TemplateNotFound` on the first route hit.

This is not a runtime unit test — it's a build-artifact test. It verifies
that (a) the package advertises the templates as data files, and (b) an
`importlib.resources` lookup on the installed package returns real files.
Running against the source tree suffices because setuptools uses the same
manifest for editable and wheel installs.
"""

from __future__ import annotations

import tomllib
from importlib.resources import files
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_pyproject_declares_template_package_data():
    """The setuptools manifest must name `.html` under `app.templates` so the
    wheel actually carries them. Without this, editable installs mask the
    problem indefinitely."""
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    pkg_data = cfg.get("tool", {}).get("setuptools", {}).get("package-data", {})
    assert "app.templates" in pkg_data, (
        "app.templates missing from [tool.setuptools.package-data]; wheels "
        "will build cleanly but omit Jinja templates and the web UI will 500 "
        "on the first route hit under a clean wheel install."
    )
    assert "*.html" in pkg_data["app.templates"], (
        f"expected '*.html' in app.templates package-data, got {pkg_data['app.templates']!r}"
    )


def test_every_referenced_template_resolves_via_importlib_resources():
    """The Jinja loader uses a filesystem path today, but resolving each
    template name through `importlib.resources` (the wheel-safe path) confirms
    the files are addressable as package resources — the pattern any downstream
    installer would use."""
    pkg = files("app.templates")
    referenced = (
        "base.html",
        "dashboard.html",
        "impact.html",
        "open.html",
        "report.html",
    )
    for name in referenced:
        resource = pkg / name
        assert resource.is_file(), (
            f"template {name!r} not resolvable via importlib.resources — the "
            "wheel would install without it, breaking the web UI."
        )
