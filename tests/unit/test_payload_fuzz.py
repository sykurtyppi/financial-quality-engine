"""Malformed SEC payloads reach every evidence-stream parser as
`ExternalPayloadError`, and as nothing else.

That is the property the report's failure labels rest on: once a parser can
only fail on bad input with `ExternalPayloadError`, any other exception out of
a stream is a defect in this code, and the report can say so instead of
calling it a data gap. Example tests cannot establish it — the defects that
motivated it were shapes nobody wrote an example for — so this mutates valid
payloads at random nodes (seeded, reproducible) and checks every outcome.

A failure prints the target, seed and mutation path; rerun that seed alone
to reproduce.
"""

from __future__ import annotations

import copy
import random
from datetime import date, datetime

import pytest

from app.services.backtesting.events import fetch_entity_events
from app.services.ingestion import vintages
from app.services.ingestion.offerings import fetch_offerings
from app.services.ingestion.payloads import ExternalPayloadError
from app.services.ingestion.restatements import scan_restatements

CASES = 300
CIK = 320193

_JUNK = [None, 0, -1, 1.5, True, "", "x", "2024-13-45", [], {}, [None], {"a": 1}]

_PROSPECTUS = (
    b"<html><body>PROSPECTUS SUPPLEMENT 10,000,000 Shares of Common Stock "
    b"at a public offering price of $12.50 per share</body></html>"
)


def _submissions() -> dict:
    forms = ["424B5", "S-3", "8-K", "10-Q", "8-K", "424B3", "S-1"]
    return {
        "cik": str(CIK),
        "name": "TestCo",
        "sic": "3674",
        "sicDescription": "Semiconductors",
        "filings": {"recent": {
            "form": forms,
            "filingDate": ["2026-08-01", "2026-05-02", "2026-04-03", "2026-03-04",
                           "2025-12-05", "2025-11-06", "2025-10-07"],
            "accessionNumber": [f"0000320193-26-00000{i}" for i in range(len(forms))],
            "primaryDocument": ["p424.htm", "s3.htm", "8k.htm", "q.htm", "8k2.htm",
                                "p3.htm", "s1.htm"],
            "items": ["", "", "4.02,9.01", "", "2.02", "", ""],
        }},
    }


def _flow(start, end, val, filed, form="10-Q", accn="a1"):
    return {"start": start, "end": end, "val": val, "filed": filed, "form": form, "accn": accn}


def _inst(end, val, filed, form="10-Q", accn="a1"):
    return {"end": end, "val": val, "filed": filed, "form": form, "accn": accn}


def _companyfacts() -> dict:
    return {"cik": CIK, "entityName": "TestCo", "facts": {
        "us-gaap": {
            "Assets": {"units": {"USD": [
                _inst("2025-12-31", 1000.0, "2026-02-01"),
                _inst("2026-03-31", 1100.0, "2026-05-01"),
                _inst("2025-12-31", 1050.0, "2026-05-01", "10-Q/A", "a2"),
            ]}},
            "Revenues": {"units": {"USD": [
                _flow("2026-01-01", "2026-03-31", 500.0, "2026-05-01"),
                _flow("2025-10-01", "2025-12-31", 450.0, "2026-02-01"),
                _flow("2025-10-01", "2025-12-31", 470.0, "2026-05-01", "10-Q/A", "a2"),
            ]}},
            "LongTermDebtNoncurrent": {"units": {"USD": [
                _inst("2026-03-31", 300.0, "2026-05-01"),
            ]}},
        },
        "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
            _inst("2026-04-20", 1.0e9, "2026-05-01"),
        ]}}},
    }}


# --- the mutator ------------------------------------------------------------------

def _paths(node, prefix=()):
    """Every addressable node below the root: (path, parent, key)."""
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            out.append((prefix + (k,), node, k))
            out += _paths(v, prefix + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out.append((prefix + (i,), node, i))
            out += _paths(v, prefix + (i,))
    return out


def _mutate(payload, rng: random.Random):
    """1-3 structural mutations: replace a node with junk, delete a key or
    list item, or truncate a list. Returns (mutated, description)."""
    doc = copy.deepcopy(payload)
    done = []
    for _ in range(rng.randint(1, 3)):
        nodes = _paths(doc)
        if not nodes:
            break
        path, parent, key = rng.choice(nodes)
        op = rng.choice(["replace", "replace", "delete", "truncate"])
        if op == "replace":
            junk = copy.deepcopy(rng.choice(_JUNK))
            parent[key] = junk
            done.append(f"{path} := {junk!r}")
        elif op == "delete":
            del parent[key]
            done.append(f"del {path}")
        elif isinstance(parent[key], list) and parent[key]:
            parent[key] = parent[key][: rng.randrange(len(parent[key]))]
            done.append(f"truncate {path}")
        else:
            parent[key] = copy.deepcopy(rng.choice(_JUNK))
            done.append(f"{path} := junk")
    return doc, "; ".join(done)


def _check(target: str, seed: int, how: str, fn) -> None:
    try:
        fn()
    except ExternalPayloadError:
        pass
    except Exception as e:  # noqa: BLE001 - the property under test
        pytest.fail(
            f"{target} seed={seed} [{how}] raised {type(e).__name__}: {e} — a malformed "
            "payload must surface as ExternalPayloadError"
        )


class _Client:
    def resolve_cik(self, ticker):
        return CIK

    def _get(self, url):
        return _PROSPECTUS


# --- the valid baselines are valid ---------------------------------------------------

def test_the_base_payloads_parse_cleanly():
    """Otherwise every mutation below "passes" by failing for the same reason."""
    timeline = fetch_offerings(_Client(), "T", as_of=date(2026, 9, 21), submissions=_submissions())
    assert timeline.acquisition_error is None and timeline.filings
    events = fetch_entity_events(_Client(), "T", submissions=_submissions())
    assert events.non_reliance_8k_dates == [date(2026, 4, 3)] and events.sic == 3674
    scan = scan_restatements(_companyfacts(), period_since=date(2024, 1, 1), as_of=date(2026, 9, 21))
    assert scan.footprints


# --- the property ---------------------------------------------------------------

@pytest.mark.parametrize("seed", range(CASES))
def test_offerings_reject_malformed_submissions_only_as_payload_errors(seed):
    rng = random.Random(seed)
    payload, how = _mutate(_submissions(), rng)
    _check("fetch_offerings", seed, how, lambda: fetch_offerings(
        _Client(), "T", as_of=date(2026, 9, 21), submissions=payload))


@pytest.mark.parametrize("seed", range(CASES))
def test_events_reject_malformed_submissions_only_as_payload_errors(seed):
    rng = random.Random(10_000 + seed)
    payload, how = _mutate(_submissions(), rng)
    _check("fetch_entity_events", seed, how,
           lambda: fetch_entity_events(_Client(), "T", submissions=payload))


@pytest.mark.parametrize("seed", range(CASES))
def test_restatements_reject_malformed_companyfacts_only_as_payload_errors(seed):
    rng = random.Random(20_000 + seed)
    payload, how = _mutate(_companyfacts(), rng)
    tags = {"total_assets": "us-gaap:Assets", "revenue": "us-gaap:Revenues",
            "total_debt": "LongTermDebtNoncurrent+none+none"} if seed % 2 else None
    _check("scan_restatements", seed, how, lambda: scan_restatements(
        payload, period_since=date(2024, 1, 1), as_of=date(2026, 9, 21), selected_tags=tags))


@pytest.mark.parametrize("seed", range(CASES // 3))
def test_vintage_diff_rejects_a_malformed_snapshot_only_as_a_payload_error(seed, tmp_path):
    rng = random.Random(30_000 + seed)
    payload, how = _mutate(_companyfacts(), rng)
    vintages.store_snapshot(CIK, _companyfacts(), now=datetime(2026, 9, 19, 12), root=tmp_path)
    vintages.store_snapshot(CIK, payload, now=datetime(2026, 9, 20, 12), root=tmp_path)
    _check("report_diff", seed, how, lambda: vintages.report_diff(
        CIK, as_of=date(2026, 9, 21), since=date(2023, 1, 1), root=tmp_path))
