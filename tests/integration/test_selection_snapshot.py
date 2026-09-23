"""The mapper's field selection is pinned before PR 1.4 rewrites it.

`tests/golden_reports/selection_snapshot.json` records, per case and per
field, the tag the mapper chose, how each period's value was obtained, the
notes and every value — from the three real fixtures, point-in-time cuts of
them, and synthetic payloads built to reach the branches the real fixtures
never do. PR 1.4 replaces the selectors and composition rules; it must leave
this file byte-identical.

Regenerate ONLY for a deliberate change to what the mapper builds, and
review the diff field by field:

    python scripts/selection_snapshot.py golden
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from app.services.ingestion import companyfacts_mapper
from app.services.ingestion.vintages import observed_vintages, store_snapshot
from tests.fixtures import selection_cases

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "selection_snapshot.py"
GOLDEN = ROOT / "tests" / "golden_reports" / "selection_snapshot.json"
CIK = 1045810


def _load_script():
    name = "_script_selection_snapshot"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


snap = _load_script()


@pytest.fixture(scope="module")
def golden() -> dict[str, dict]:
    return json.loads(GOLDEN.read_text())


def _field(case: dict, name: str) -> dict:
    return next(f for f in case["fields"] if f["field"] == name)


# ---------------------------------------------------------------------------
# 1. Today's mapper reproduces the committed snapshot exactly


def test_mapper_reproduces_the_selection_snapshot_exactly():
    cases = snap.golden_cases()
    text = snap.render(cases)
    committed = GOLDEN.read_text()
    if text != committed:
        lines = snap.diff_cases(json.loads(committed), cases)
        shown = "\n".join(lines[:80]) or "values equal; formatting differs"
        pytest.fail(
            "The mapper no longer builds what selection_snapshot.json records.\n"
            f"{shown}\n"
            "If the change is deliberate, regenerate with "
            "`python scripts/selection_snapshot.py golden` and review the diff."
        )


def test_every_case_is_in_the_snapshot(golden):
    expected = {f"real/{t}" for t in snap.REAL_TICKERS}
    expected |= {f"pit/{t}@{c}" for t in snap.REAL_TICKERS for c in snap.PIT_CUTS}
    expected |= {f"synthetic/{n}" for n in selection_cases.CASES}
    assert set(golden) == expected
    assert not [n for n, c in golden.items() if "error" in c]


# ---------------------------------------------------------------------------
# 2. Coverage: the snapshot reaches every branch a rewrite could break

METHODS = {"direct", "ytd_diff", "fy_minus_3q", "nearest", "composite"}


def test_every_derivation_method_appears(golden):
    seen = {m for case in golden.values() for f in case["fields"] for m in f["methods"]}
    assert seen == METHODS


def _pattern(node: ast.expr) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return re.escape(node.value)
    if isinstance(node, ast.JoinedStr):
        return "".join(
            re.escape(v.value) if isinstance(v, ast.Constant) else ".+?" for v in node.values
        )
    raise AssertionError(f"unrecognised note expression: {ast.dump(node)}")


def _note_templates() -> list[str]:
    """Every note the mapper can emit, read from its source: arguments of
    `notes.append(...)` and string literals in a returned notes list."""
    tree = ast.parse(Path(companyfacts_mapper.__file__).read_text())
    found: list[ast.expr] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "notes"
        ):
            found.append(node.args[0])
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple):
            for elt in node.value.elts:
                if isinstance(elt, ast.List):
                    found += [e for e in elt.elts if isinstance(e, ast.Constant | ast.JoinedStr)]
    return [_pattern(n) for n in found]


def test_every_mapper_note_appears(golden):
    templates = _note_templates()
    assert len(templates) >= 11, templates  # the parse found the notes at all
    notes = {n for case in golden.values() for f in case["fields"] for n in f["notes"]}
    missing = [t for t in templates if not any(re.fullmatch(t, n) for n in notes)]
    assert not missing, f"no snapshot case reaches these mapper notes: {missing}"
    # And the parse is complete: every note the snapshot holds is one of them.
    unknown = [n for n in notes if not any(re.fullmatch(t, n) for t in templates)]
    assert not unknown, unknown


# Each synthetic case reaches the branch it was written for. The golden
# equality test pins the values; this pins the intent, so a regenerated
# golden cannot quietly stop covering a branch.
BRANCHES = {
    ("synthetic/tag_choice", "receivables"): "us-gaap:AccountsReceivableNetCurrent",
    ("synthetic/tag_choice", "inventory"): "us-gaap:InventoryGross",
    ("synthetic/tag_choice", "cash_and_equivalents"): "us-gaap:CashAndCashEquivalentsAtCarryingValue",
    ("synthetic/tag_choice", "accounts_payable"): "us-gaap:AccountsPayableTradeCurrent",
    ("synthetic/tag_choice", "shares_outstanding"): "dei:EntityCommonStockSharesOutstanding",
    ("synthetic/tag_choice", "sga_expense"): "SellingAndMarketingExpense+GeneralAndAdministrativeExpense",
    ("synthetic/tag_choice", "depreciation_amortization"): "Depreciation+AmortizationOfIntangibleAssets",
    ("synthetic/composites_lose", "sga_expense"): "us-gaap:SellingGeneralAndAdministrativeExpense",
    ("synthetic/composites_lose", "depreciation_amortization"): "us-gaap:Depreciation",
    ("synthetic/debt_full_breakdown", "total_debt"): (
        "LongTermDebtNoncurrent+LongTermDebtCurrent+ShortTermBorrowings"
        "+FinanceLeaseLiabilityNoncurrent+FinanceLeaseLiabilityCurrent"
    ),
    ("synthetic/debt_lease_inclusive", "total_debt"): (
        "LongTermDebtAndCapitalLeaseObligations+LongTermDebtCurrent+none"
        "+FinanceLeaseLiabilityCurrent"
    ),
    ("synthetic/debt_both_lease_inclusive", "total_debt"): (
        "LongTermDebtAndCapitalLeaseObligations+LongTermDebtAndCapitalLeaseObligationsCurrent"
        "+DebtCurrent"
    ),
    ("synthetic/debt_noncurrent_only", "total_debt"): "LongTermDebtNoncurrent+none+none",
    ("synthetic/debt_total_fallback_with_leases", "total_debt"): (
        "LongTermDebt+CommercialPaper+FinanceLeaseLiabilityNoncurrent"
    ),
    ("synthetic/debt_total_fallback_plain", "total_debt"): "LongTermDebt+none",
    ("synthetic/debt_none", "total_debt"): None,
    ("synthetic/quarter_ends_from_revenue", "revenue"): "us-gaap:Revenues",
}


@pytest.mark.parametrize(("case", "field"), list(BRANCHES), ids=lambda v: str(v))
def test_synthetic_case_reaches_its_branch(golden, case, field):
    assert _field(golden[case], field)["tag_used"] == BRANCHES[(case, field)]


def test_synthetic_derivation_and_tie_branches(golden):
    flows = golden["synthetic/flows"]
    assert _field(flows, "revenue")["methods"] == {"direct": 6, "fy_minus_3q": 2}
    assert _field(flows, "cfo")["methods"] == {"direct": 2, "ytd_diff": 6}
    # Amendment: the latest filed value wins.
    assert _field(flows, "net_income")["values"]["2023-06-30"] == 778.0
    # Same-day tie: the first fact in payload order is kept.
    assert _field(flows, "cost_of_revenue")["values"]["2024-03-31"] == 608.0
    assert _field(golden["synthetic/tag_choice"], "deferred_revenue")["values"]["2024-03-31"] == 78.0
    # Two year-to-date facts end on one quarter end: the latest filed pair
    # is differenced (64 − 30), not the original (20 − 10).
    assert _field(flows, "share_issuance_proceeds")["values"]["2024-06-30"] == 34.0
    # Non-additive flow: no Q4 derivation.
    assert _field(flows, "shares_diluted")["missing_periods"] == ["FY2023Q4", "FY2024Q4"]
    # An undated fact is kept only when nothing dated covers its period.
    buybacks = _field(flows, "buybacks")["values"]
    assert buybacks["2023-03-31"] == 44.0 and buybacks["2024-09-30"] == 55.0
    # Quarter ends and labels.
    assert golden["synthetic/quarter_ends_from_revenue"]["quarter_ends"][-1] == "2024-12-31"
    assert golden["synthetic/unknown_fiscal_year_end"]["labels"][0] == "P2023-03-31"
    assert golden["synthetic/fifty_two_week"]["fiscal_year_end_month"] == 9


# ---------------------------------------------------------------------------
# 3. The operator's tools: diff and the vintage-store dump


def _write(path: Path, cases: dict) -> Path:
    path.write_text(snap.render(cases))
    return path


def test_diff_of_identical_dumps_is_empty(tmp_path, capsys):
    cases = {"synthetic/flows": snap.dump(selection_cases.flows(), "FLOWS")}
    a = _write(tmp_path / "a.json", cases)
    b = _write(tmp_path / "b.json", cases)
    assert snap.main(["diff", str(a), str(b)]) == 0
    assert "identical" in capsys.readouterr().out


def test_diff_names_the_case_field_period_and_values(tmp_path, capsys):
    before = {"synthetic/flows": snap.dump(selection_cases.flows(), "FLOWS")}
    after = json.loads(json.dumps(before))
    _field(after["synthetic/flows"], "cfo")["values"]["2024-06-30"] = 1.5
    _field(after["synthetic/flows"], "revenue")["tag_used"] = "us-gaap:Revenues"
    a = _write(tmp_path / "a.json", before)
    b = _write(tmp_path / "b.json", after)
    assert snap.main(["diff", str(a), str(b)]) == 1
    out = capsys.readouterr().out
    old = _field(before["synthetic/flows"], "cfo")["values"]["2024-06-30"]
    assert f"synthetic/flows: cfo[2024-06-30]: {old!r} → 1.5" in out
    assert (
        "synthetic/flows: revenue.tag_used: "
        "'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax' → 'us-gaap:Revenues'"
    ) in out


def test_diff_reports_cases_only_on_one_side(tmp_path, capsys):
    case = snap.dump(selection_cases.debt_none(), "X")
    a = _write(tmp_path / "a.json", {"one": case})
    b = _write(tmp_path / "b.json", {"two": case})
    assert snap.main(["diff", str(a), str(b)]) == 1
    out = capsys.readouterr().out
    assert "one: only in the first dump" in out and "two: only in the second dump" in out


def test_vintages_dumps_the_newest_snapshot_at_or_before_as_of(tmp_path):
    root = tmp_path / "store"
    first, second = selection_cases.flows(), selection_cases.tag_choice()
    day1 = datetime(2026, 9, 1, 12, tzinfo=UTC)
    store_snapshot(CIK, first, now=day1, root=root)
    store_snapshot(CIK, second, now=day1 + timedelta(days=3), root=root)
    obs = observed_vintages(CIK, root)
    assert len(obs) == 2
    d1, d2 = (date.fromisoformat(o.captured) for o in obs)

    def run(as_of: date) -> dict:
        out = tmp_path / f"dump-{as_of}.json"
        assert snap.main(
            ["vintages", "--out", str(out), "--as-of", as_of.isoformat(), "--root", str(root)]
        ) == 0
        return json.loads(out.read_text())

    name = f"CIK{CIK:010d}"
    at_first = run(d1)
    expected = snap.dump(first, name)
    expected["snapshot"] = {"captured": obs[0].captured, "sha256": obs[0].sha256}
    assert at_first == {name: expected}
    # A later snapshot is invisible before its day, and chosen from it on.
    at_second = run(d2)
    assert at_second[name]["snapshot"]["sha256"] == obs[1].sha256
    assert at_second[name]["entity_name"] == "Tag Choice Co"
    assert run(d1 - timedelta(days=1)) == {}


def test_vintages_on_a_missing_store_is_empty(tmp_path):
    out = tmp_path / "dump.json"
    assert snap.main(
        ["vintages", "--out", str(out), "--as-of", "2026-09-01", "--root", str(tmp_path / "none")]
    ) == 0
    assert json.loads(out.read_text()) == {}


def test_golden_check_passes_on_the_committed_file():
    assert snap.main(["golden", "--check"]) == 0


def test_golden_check_fails_on_a_changed_file(tmp_path, capsys):
    stale = json.loads(GOLDEN.read_text())
    _field(stale["real/AAPL"], "revenue")["values"] = {}
    path = _write(tmp_path / "stale.json", stale)
    assert snap.main(["golden", "--check", "--out", str(path)]) == 1
    assert "real/AAPL: revenue[" in capsys.readouterr().err
