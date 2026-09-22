"""Shared hypothesis strategies for the property tests.

Generators only — no assertions. Each produces the input shape one family of
past defects needed: filing trails with undated and same-day facts (PIT and
tie rules), companyfacts payloads over the scored and composite tags
(restatement scan), random finite period datasets including distress states
(engine invariants), and structural mutations of valid payloads (parsers).
"""

from __future__ import annotations

import copy
from datetime import date, timedelta

from hypothesis import strategies as st

from app.schemas.financials import (
    CompanyDataset,
    CompanyProfile,
    PeriodFinancials,
    PeriodType,
)
from app.services.ingestion.fields import FIELDS

AS_OF = date(2025, 8, 15)
FIRST_END = date(2023, 3, 31)

# Twelve consecutive calendar quarter ends and their structural labels.
QUARTER_ENDS = [
    date(2023, 3, 31), date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31),
    date(2024, 3, 31), date(2024, 6, 30), date(2024, 9, 30), date(2024, 12, 31),
    date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31),
]


def _label(d: date) -> str:
    return f"FY{d.year}Q{(d.month - 1) // 3 + 1}"


# --- companyfacts rows -------------------------------------------------------------

FORMS = ("10-Q", "10-K", "10-Q/A")
# Collisions (unchanged values) and both material and immaterial revisions
# are all common. 0.1 / 0.2 / 0.3 make a three-term sum depend on its order
# in IEEE-754 ((0.1 + 0.2) + 0.3 != (0.2 + 0.3) + 0.1), which is what an
# order-invariance property needs to be able to see.
VALUES = st.sampled_from([100.0, 100.0, 101.0, 130.0, 200.0, 0.0, 1e9 + 0.25, 0.1, 0.2, 0.3])


@st.composite
def fact_rows(draw, *, flow: bool, as_of: date = AS_OF, max_rows: int = 10):
    """One concept's rows: random period ends over nine quarters, filed dates
    in [as_of - 600d, as_of + 90d] or undated (~10%), random forms."""
    rows = []
    for i in range(draw(st.integers(0, max_rows))):
        end = FIRST_END + timedelta(days=91 * draw(st.integers(0, 8)))
        row = {"end": end.isoformat(), "val": draw(VALUES),
               "form": draw(st.sampled_from(FORMS)), "accn": f"a{i}"}
        if flow:
            row["start"] = (end - timedelta(days=90)).isoformat()
        if draw(st.integers(0, 9)):  # 1 in 10 undated
            row["filed"] = (as_of + timedelta(days=draw(st.integers(-600, 90)))).isoformat()
        rows.append(row)
    return rows


# (concept, is_flow): single-tag fields, the SG&A composite components and
# the debt roles, so the scan exercises every selection shape.
CONCEPTS = (
    ("Assets", False), ("Revenues", True), ("SalesRevenueNet", True),
    ("SellingAndMarketingExpense", True), ("GeneralAndAdministrativeExpense", True),
    ("LongTermDebtNoncurrent", False), ("LongTermDebtCurrent", False),
    ("CommercialPaper", False),
)

SELECTED = {
    "total_assets": "us-gaap:Assets",
    "revenue": "us-gaap:Revenues",
    "sga_expense": "SellingAndMarketingExpense+GeneralAndAdministrativeExpense",
    "total_debt": "LongTermDebtNoncurrent+LongTermDebtCurrent+CommercialPaper",
}


@st.composite
def companyfacts(draw, *, as_of: date = AS_OF):
    concepts = {}
    for concept, flow in CONCEPTS:
        rows = draw(fact_rows(flow=flow, as_of=as_of))
        if rows:
            concepts[concept] = {"units": {"USD": rows}}
    return {"entityName": "T", "facts": {"us-gaap": concepts}}


@st.composite
def same_day_trails(draw):
    """Several facts for ONE period whose filed dates cluster within a few
    days, so same-day ties are the common case rather than the rare one."""
    end = date(2025, 6, 30)
    n = draw(st.integers(2, 6))
    return [
        {"end": end.isoformat(), "val": float(10 * i + 1), "form": "10-Q", "accn": f"a{i}",
         "filed": (date(2025, 8, 1) + timedelta(days=draw(st.integers(0, 3)))).isoformat()}
        for i in range(n)
    ]


# --- period datasets ---------------------------------------------------------------

# Fields that can legitimately be negative: losses, cash burn, and the
# signed cash-flow lines. Everything else is a balance or a count.
_SIGNED = {"net_income", "operating_income", "ebit", "cfo", "interest_expense"}

# Log-uniform magnitudes from 1 to ~1e11: plain float strategies cluster at
# the extremes and almost never produce comparable magnitudes across fields.
_MAGNITUDE = st.builds(
    lambda mantissa, exponent: mantissa * 10.0 ** exponent,
    st.floats(min_value=1.0, max_value=9.99, allow_nan=False),
    st.integers(0, 10),
)


def _field_value(name: str):
    choices = [st.none(), st.just(0.0), _MAGNITUDE, _MAGNITUDE]
    if name in _SIGNED:
        choices.append(_MAGNITUDE.map(lambda v: -v))
    return st.one_of(*choices)


# A distressed period: operating losses, cash burn, non-positive EBITDA and
# debt well above cash — the states P0-9 scores at maximum concern because
# the ratios' denominators turn meaningless. Random draws alone almost never
# land here, and these are the components the flag invariant exists for.
_DISTRESS = {
    "net_income": _MAGNITUDE.map(lambda v: -v),
    "cfo": _MAGNITUDE.map(lambda v: -v),
    "ebit": _MAGNITUDE.map(lambda v: -v),
    "operating_income": _MAGNITUDE.map(lambda v: -v),
    "depreciation_amortization": st.floats(min_value=0.0, max_value=1.0),
    "total_debt": st.floats(min_value=1e9, max_value=1e10),
    "cash_and_equivalents": st.floats(min_value=0.0, max_value=1e6),
}


@st.composite
def period_datasets(draw, min_n: int = 2, max_n: int = 10):
    n = draw(st.integers(min_n, max_n))
    ends = QUARTER_ENDS[-n:]
    distressed = draw(st.booleans())
    periods = []
    for e in ends:
        values = {f.name: draw(_field_value(f.name)) for f in FIELDS}
        if distressed:
            values.update({k: draw(v) for k, v in _DISTRESS.items()})
        periods.append(PeriodFinancials(
            period_end=e, period_type=PeriodType.QUARTER, fiscal_label=_label(e), **values,
        ))
    return CompanyDataset(profile=CompanyProfile(ticker="PROP"), periods=periods)


# --- structural mutation of valid payloads -------------------------------------------

JUNK = st.recursive(
    st.none() | st.booleans() | st.integers(-5, 5) | st.floats(allow_nan=False)
    | st.sampled_from(["", "x", "2024-13-45", "2024-02-30", "8-K"]),
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.sampled_from(["a", "form", "end"]), inner, max_size=3),
    max_leaves=6,
)


def _nodes(node, path=()):
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            out.append((path + (k,), node, k))
            out += _nodes(v, path + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out.append((path + (i,), node, i))
            out += _nodes(v, path + (i,))
    return out


@st.composite
def mutated(draw, payload: dict):
    """A deep copy of `payload` with 1-3 structural mutations: a node replaced
    by junk, a key or item deleted, or a list truncated. Shrinks toward a
    single, shallow mutation."""
    doc = copy.deepcopy(payload)
    for _ in range(draw(st.integers(1, 3))):
        nodes = _nodes(doc)
        if not nodes:
            break
        _path, parent, key = draw(st.sampled_from(nodes))
        op = draw(st.sampled_from(["replace", "delete", "truncate"]))
        if op == "delete":
            del parent[key]
        elif op == "truncate" and isinstance(parent[key], list) and parent[key]:
            parent[key] = parent[key][: draw(st.integers(0, len(parent[key]) - 1))]
        else:
            parent[key] = draw(JUNK)
    return doc
