"""Earnings-night drills: the operator commands, run as the operator runs them.

`earnings_brief.py assume --derive` crashed on its first line — a missing
keyword argument — and every unit test passed, because they called the
function underneath, not the command (Hermes audit round 3, finding 4).
These drills run the real scripts in a subprocess, offline, against a
seeded SEC cache, and then break the inputs the way a filing night does:
a truncated cache entry, a malformed filing index, a missing filing
document, SEC unreachable for everything but companyfacts.

Isolation: the scripts write under their repository root (reports/,
data/vintages/), so each drill runs a COPY of `app/` and `scripts/` in a
temp directory whose `data/cache` holds the seeded payloads. The network is
unreachable (proxies point at a closed port) and a `sitecustomize` shim
turns the client's retry back-off sleeps into no-ops, so a failed fetch
fails in milliseconds instead of seconds. Nothing reaches SEC.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "real" / "companyfacts_AAPL_trimmed.json"
CIK = 320193
TODAY = datetime.now(UTC).date()
ACCN_10Q = "0000320193-26-000010"
ACCN_402 = "0000320193-26-000011"


def _index(*, misaligned: bool = False) -> dict:
    """A 10-Q and an 8-K 4.02, both filed within the report's windows."""
    filed = [(TODAY - timedelta(days=d)).isoformat() for d in (20, 10)]
    recent = {
        "form": ["10-Q", "8-K"],
        "items": ["", "4.02"],
        "filingDate": filed,
        "accessionNumber": [ACCN_10Q, ACCN_402],
        "primaryDocument": ["q.htm", "k.htm"],
        "reportDate": [(TODAY - timedelta(days=60)).isoformat(), filed[1]],
        "acceptanceDateTime": [f"{d}T16:00:00.000Z" for d in filed],
    }
    if misaligned:
        recent["filingDate"] = recent["filingDate"][:1]  # Hermes's shape
    return {"cik": str(CIK), "name": "Apple Inc.", "sic": "3571", "tickers": ["AAPL"],
            "filings": {"recent": recent}}


_TENQ_HTML = """<html><body>
<p>Item 2. Management's Discussion and Analysis of Financial Condition and Results of Operations</p>
<p>Revenue grew on strong demand. We believe margins will remain stable.</p>
<p>Item 1A. Risk Factors</p>
<p>Our business depends on consumer demand, which may decline.</p>
<p>Item 6. Exhibits</p>
</body></html>"""


@pytest.fixture(scope="module")
def tree(tmp_path_factory) -> Path:
    """A copy of the code the scripts run, shared by the drills."""
    base = tmp_path_factory.mktemp("drill_tree")
    for part in ("app", "scripts"):
        shutil.copytree(ROOT / part, base / part,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shim = base / "_shim"
    shim.mkdir()
    (shim / "sitecustomize.py").write_text(
        "import time\ntime.sleep = lambda seconds: None  # drills: retries fail fast\n"
    )
    return base


@pytest.fixture
def wd(tree, tmp_path) -> Path:
    """A fresh working copy with an empty output tree and a seeded cache."""
    work = tmp_path / "work"
    work.mkdir()
    for part in ("app", "scripts", "_shim"):
        # Hard links, not symlinks: the scripts resolve() their ROOT, which
        # would follow a symlink back to the shared tree.
        shutil.copytree(tree / part, work / part, copy_function=os.link)
    cache = work / "data" / "cache"
    cache.mkdir(parents=True)
    (cache / "company_tickers.json").write_text(
        json.dumps({"0": {"cik_str": CIK, "ticker": "AAPL", "title": "Apple Inc."}}))
    shutil.copy(FIXTURE, cache / f"companyfacts_CIK{CIK:010d}.json")
    (cache / f"submissions_CIK{CIK:010d}.json").write_text(json.dumps(_index()))
    (cache / f"archive_{ACCN_10Q.replace('-', '')}_q.htm").write_text(_TENQ_HTML)
    return work


def _run(wd: Path, script: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
    closed = "http://127.0.0.1:9"  # discard port: nothing listens
    env.update({
        "HTTP_PROXY": closed, "HTTPS_PROXY": closed, "http_proxy": closed, "https_proxy": closed,
        "EDGAR_IDENTITY": "CLI Drill drill@example.com",
        "PYTHONPATH": str(wd / "_shim"),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return subprocess.run(
        [sys.executable, str(wd / "scripts" / script), *args],
        cwd=wd, env=env, capture_output=True, text=True, timeout=300,
    )


def _report(wd: Path) -> str:
    (out,) = sorted((wd / "reports").glob("AAPL_*.md"))
    return out.read_text()


def _no_traceback(proc: subprocess.CompletedProcess[str]) -> None:
    assert "Traceback" not in proc.stderr, proc.stderr[-3000:]


# --- the commands work offline -------------------------------------------------

def test_generate_report_from_the_cache(wd):
    proc = _run(wd, "generate_report.py", "AAPL", "--no-vintage")
    assert proc.returncode == 0, proc.stderr[-3000:]
    _no_traceback(proc)
    report = _report(wd)
    assert "8-K Item 4.02 non-reliance" in report
    assert "1 served from the immutable archive cache" in report
    assert "UNAVAILABLE" not in report


def test_generate_report_without_documents(wd):
    proc = _run(wd, "generate_report.py", "AAPL", "--no-docs", "--no-vintage")
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "8-K Item 4.02 non-reliance" in _report(wd)


def test_assume_derive_runs_and_names_its_cutoff(wd):
    """Finding 4 itself: this command raised TypeError before its first line
    of output."""
    proc = _run(wd, "earnings_brief.py", "assume", "AAPL", "--derive", "--as-of", "2025-06-30")
    assert proc.returncode == 0, proc.stderr[-3000:]
    _no_traceback(proc)
    assert "filed on or before 2025-06-30" in proc.stdout
    default = _run(wd, "earnings_brief.py", "assume", "AAPL", "--derive")
    assert default.returncode == 0, default.stderr[-3000:]
    assert f"filed on or before {TODAY.isoformat()}" in default.stdout


def test_vintage_capture_says_sec_is_unreachable(wd):
    """Capture always asks SEC (a cached payload is not a new observation),
    so offline it must fail per name, loudly, and still exit."""
    proc = _run(wd, "vintage.py", "capture", "AAPL")
    assert proc.returncode == 1
    _no_traceback(proc)
    assert "AAPL: SecClientError: SEC request failed" in proc.stderr
    assert not list((wd / "data" / "vintages").rglob("*.json.gz"))
    listing = _run(wd, "vintage.py", "list", "AAPL")
    assert listing.returncode == 0, listing.stderr[-3000:]


# --- drills: a filing night going wrong ------------------------------------------

def test_drill_truncated_cache_entry_fails_with_the_sec_error(wd):
    facts = wd / "data" / "cache" / f"companyfacts_CIK{CIK:010d}.json"
    facts.write_text(facts.read_text()[:500])
    proc = _run(wd, "generate_report.py", "AAPL", "--no-docs", "--no-vintage")
    assert proc.returncode != 0
    _no_traceback(proc)
    assert "SEC request failed" in proc.stderr
    assert not (wd / "reports").exists() or not list((wd / "reports").glob("AAPL_*.md"))


def test_drill_malformed_filing_index_is_unavailable_not_clean(wd):
    (wd / "data" / "cache" / f"submissions_CIK{CIK:010d}.json").write_text(
        json.dumps(_index(misaligned=True)))
    proc = _run(wd, "generate_report.py", "AAPL", "--no-vintage")
    assert proc.returncode == 0, proc.stderr[-3000:]
    report = _report(wd)
    assert "unequal lengths" in report
    assert "No offering-related filings found" not in report
    assert "8-K Item 4.02 non-reliance" not in report  # never read, so never claimed
    assert "UNAVAILABLE" in report
    assert "filing index malformed" in report


def test_drill_missing_filing_document(wd):
    (wd / "data" / "cache" / f"archive_{ACCN_10Q.replace('-', '')}_q.htm").unlink()
    proc = _run(wd, "generate_report.py", "AAPL", "--no-vintage")
    assert proc.returncode == 0, proc.stderr[-3000:]
    report = _report(wd)
    assert f"10-Q {ACCN_10Q}: fetch/extract failed" in report


def test_drill_sec_down_except_companyfacts(wd):
    (wd / "data" / "cache" / f"submissions_CIK{CIK:010d}.json").unlink()
    proc = _run(wd, "generate_report.py", "AAPL", "--no-vintage")
    assert proc.returncode == 0, proc.stderr[-3000:]
    _no_traceback(proc)
    report = _report(wd)
    assert "UNAVAILABLE" in report
    assert "No offering-related filings found" not in report
    assert "filing index unavailable" in report
    assert "8-K Item 4.02 non-reliance" not in report
