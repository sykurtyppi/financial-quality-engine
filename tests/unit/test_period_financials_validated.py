"""A `PeriodFinancials` is only ever built validated (Hermes audit of #121,
residual item).

The formula guards compare share counts and other inputs with `<= 0` (about
35 of them, e.g. the share-change formulas' "Non-positive prior/current share
count"). They are type-safe only because every `PeriodFinancials` the
pipeline reads went through pydantic validation, which coerces each field to
a float or refuses it. Hermes showed the hole: a non-numeric share count
injected through `PeriodFinancials.model_construct` reaches the guard as a
string and the comparison raises TypeError. `model_copy(update=...)` skips
validation the same way. Neither is used on a period today (verified at
6677280: no `model_construct` anywhere in `app/` or `scripts/`; the
`model_copy(update=...)` calls are on MetricResult, FactRef, journal entries
and provenance; `ttm.annualize` rebuilds through `PeriodFinancials(**data)`).
The user chose to pin that rather than harden every formula: these tests fail
the day either bypass appears.
"""

from __future__ import annotations

import ast
import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.pipeline import analyze
from app.schemas.financials import PeriodFinancials, PeriodType
from app.services.formulas import ttm
from app.services.formulas.capital_structure import diluted_share_growth
from app.services.formulas.registry import compute_metrics
from app.services.ingestion.companyfacts_mapper import build_dataset
from app.services.valuation.observation import LoadedObservation
from tests.unit.test_valuation_report import _build, _obs

ROOT = Path(__file__).resolve().parents[2]
REAL = ROOT / "tests" / "fixtures" / "real"
TICKERS = ["AAPL", "CRM", "KO"]
BYPASS = "model_construct"


def _period(**fields) -> PeriodFinancials:
    return PeriodFinancials(period_end=date(2025, 6, 30), period_type=PeriodType.QUARTER,
                            fiscal_label="FY2025Q2", **fields)


# --- 1. static: no unvalidated construction anywhere in the shipped code --------


def _construct_lines(source: str) -> list[int]:
    """Lines of `source` that name `model_construct`: a call, or a reference
    that could be called later (`f = Model.model_construct`). Docstrings and
    comments are not code, so the schema's own mention of it is not one."""
    return sorted(
        node.lineno for node in ast.walk(ast.parse(source))
        if (isinstance(node, ast.Attribute) and node.attr == BYPASS)
        or (isinstance(node, ast.Name) and node.id == BYPASS)
    )


def test_the_scan_finds_a_planted_call():
    """The scanner itself: it sees a call, an alias and a bare name, and not a
    string or a comment."""
    assert _construct_lines("p = PeriodFinancials.model_construct(shares_diluted='x')\n") == [1]
    assert _construct_lines("f = PeriodFinancials.model_construct\nf()\n") == [1]
    assert _construct_lines("from x import model_construct\nmodel_construct()\n") == [2]
    assert _construct_lines('"""model_construct"""\n# model_construct()\n') == []


def test_no_model_construct_in_app_or_scripts():
    """Any model, not just `PeriodFinancials`: a `CompanyDataset` built with
    `model_construct` would carry its periods unvalidated too, and the AST
    cannot tell which class a call is on."""
    files = sorted(p for d in ("app", "scripts") for p in (ROOT / d).rglob("*.py"))
    assert len(files) > 100  # the tree was found, not an empty glob
    found = {str(p.relative_to(ROOT)): lines for p in files
             if (lines := _construct_lines(p.read_text(encoding="utf-8")))}
    assert found == {}


# --- 2. runtime: the real offline pipeline never bypasses validation ------------


@pytest.fixture
def bypasses(monkeypatch) -> list[tuple[str, object]]:
    """Every `model_construct` and `model_copy` on a `PeriodFinancials`, with
    the copy's `update`, and every attribute assignment on one (the model
    does not set `validate_assignment`, so `p.field = x` skips validation
    too). Each still does its job: only the record is new."""
    calls: list[tuple[str, object]] = []
    construct = PeriodFinancials.model_construct.__func__  # type: ignore[attr-defined]
    copy = PeriodFinancials.model_copy
    assign = PeriodFinancials.__setattr__

    def recording_construct(cls, *args, **kwargs):
        calls.append(("model_construct", kwargs))
        return construct(cls, *args, **kwargs)

    def recording_copy(self, *, update=None, deep=False):
        calls.append(("model_copy", update))
        return copy(self, update=update, deep=deep)

    def recording_assign(self, name, value):
        calls.append(("setattr", name))
        assign(self, name, value)

    monkeypatch.setattr(PeriodFinancials, "model_construct", classmethod(recording_construct))
    monkeypatch.setattr(PeriodFinancials, "model_copy", recording_copy)
    monkeypatch.setattr(PeriodFinancials, "__setattr__", recording_assign)
    return calls


def test_the_recorder_sees_both_bypasses(bypasses):
    """The runtime test below is not vacuous: the patch takes, and each
    bypass still does what it did."""
    p = PeriodFinancials.model_construct(shares_diluted="x")
    assert p.shares_diluted == "x"
    assert _period().model_copy(update={"shares_diluted": "x"}).shares_diluted == "x"
    q = _period()
    q.shares_diluted = "x"  # type: ignore[assignment]
    assert q.shares_diluted == "x"
    assert bypasses == [("model_construct", {"shares_diluted": "x"}),
                        ("model_copy", {"shares_diluted": "x"}),
                        ("setattr", "shares_diluted")]


@pytest.mark.parametrize("ticker", TICKERS)
def test_the_offline_pipeline_builds_every_period_validated(ticker, bypasses, monkeypatch,
                                                             tmp_path):
    """The mapper (latest and a point-in-time cut), the metrics with their TTM
    windows, the analysis, and the report with a recorded price: its streams
    and the valuation plane's as-filed rebuild of the raw payload."""
    windows: list[PeriodFinancials] = []
    annualize = ttm.annualize

    def recording_annualize(periods, idx):
        out = annualize(periods, idx)
        if out is not None:
            windows.append(out)
        return out

    monkeypatch.setattr(ttm, "annualize", recording_annualize)
    facts = json.loads((REAL / f"companyfacts_{ticker}_trimmed.json").read_text())

    latest, _ = build_dataset(facts, ticker)
    ends = [p.period_end for p in latest.sorted_periods()]
    cut, _ = build_dataset(facts, ticker, as_of=ends[-3] + timedelta(days=75))
    assert cut.periods and cut.sorted_periods()[-1].period_end < ends[-1]
    for ds in (latest, cut):
        compute_metrics(ds)
        analyze(ds)
    assert windows  # the TTM path ran, and built its windows validated

    _, _, ledger = _build(ticker, tmp_path, with_client=True,
                          observation=LoadedObservation.of(_obs(ticker)), requested=True)
    assert ledger.valuation is not None and ledger.valuation.state == "produced"

    assert [c for c in bypasses if c[0] == "model_construct"] == []
    assert [c for c in bypasses if c[0] == "model_copy" and c[1]] == []
    assert [c for c in bypasses if c[0] == "setattr"] == []


# --- 3. the boundary: validation is what makes the guards type-safe -------------


@pytest.mark.parametrize("bad", ["x", [1], {"v": 1}])
def test_a_non_numeric_share_count_is_refused_on_construction(bad):
    with pytest.raises(ValidationError, match="shares_diluted"):
        _period(shares_diluted=bad)


def test_a_numeric_string_is_coerced_to_a_float():
    p = _period(shares_diluted="4313000000", shares_outstanding="4.3e9")
    assert p.shares_diluted == 4_313_000_000.0 and isinstance(p.shares_diluted, float)
    assert p.shares_outstanding == 4.3e9 and isinstance(p.shares_outstanding, float)


def test_validated_periods_reach_the_guard_and_an_unvalidated_one_breaks_it():
    """Hermes's finding, reproduced: the same non-numeric count that
    construction refuses reaches the share-change guard through
    `model_construct` and the `<= 0` comparison raises. Validated, the guard
    reads floats and refuses a bad count by its note."""
    prior = _period(shares_diluted=100.0)
    assert diluted_share_growth(_period(shares_diluted="110"), prior).value == pytest.approx(0.1)
    assert diluted_share_growth(_period(shares_diluted=0), prior).note == (
        "Non-positive current share count")
    unvalidated = PeriodFinancials.model_construct(
        period_end=date(2025, 9, 30), period_type=PeriodType.QUARTER, fiscal_label="FY2025Q3",
        shares_diluted="x")
    with pytest.raises(TypeError):
        diluted_share_growth(unvalidated, prior)
