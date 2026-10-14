"""Test helpers for builders faked under `report_files.replacing`."""

from __future__ import annotations

import re

_GENERATION = re.compile(r"\n\n- Engine: .*\n- Generation: [0-9a-f]{32} \(.*\)\n\Z")


def write_ledger(kwargs: dict) -> None:
    """A faked builder still writes the evidence ledger: a publish without
    one is refused (Hermes deep audit, finding 2)."""
    out = kwargs.get("ledger_out")
    if out is not None:
        out.write_text("{}")


def without_generation(text: str) -> str:
    """A published report less the engine and generation lines the publish
    appends."""
    return _GENERATION.sub("", text)
