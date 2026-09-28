"""The earnings-night drill runs green on the bundled fixture, and its own
machinery (masking, scenario edits, known-issue markers) does what its log
claims (Hermes audit round 7, finding 3)."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import drill  # noqa: E402

REAL_TREES = ("reports", "data/vintages", "data/cache", "journal/entries")


def _listing() -> dict[str, float]:
    return {str(p): p.stat().st_mtime for t in REAL_TREES for p in (ROOT / t).rglob("*")
            if p.is_file()}


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    out = tmp_path_factory.mktemp("drill") / "out"
    before = _listing()
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "drill.py"), "--out", str(out)],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
    )
    return proc, out, before, _listing()


class TestTheDrill:
    def test_every_step_passes_on_the_fixture(self, run):
        proc, out, _, _ = run
        assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
        log = json.loads((out / "drill_log.json").read_text())
        assert log["passed"] is True
        assert [s["slug"] for s in log["steps"]] == [slug for slug, _, _ in drill.STEPS]
        assert all(s["ok"] for s in log["steps"])

    def test_no_known_issue_is_open(self, run):
        """The amendment-as-silent-revision defect the drill found is fixed
        and its marker removed; a new marker must be added deliberately."""
        _, out, _, _ = run
        log = json.loads((out / "drill_log.json").read_text())
        assert log["known_issues"] == []
        assert "Known issues reproduced: 0" in (out / "drill_log.md").read_text()
        assert not [c for s in log["steps"] for c in s["checks"] if c["known"]]

    def test_the_log_carries_what_an_operator_signs(self, run):
        _, out, _, _ = run
        md = (out / "drill_log.md").read_text()
        assert "## Operator notes" in md and "- Decision" in md
        for n in range(1, len(drill.STEPS) + 1):
            assert list((out / "steps").glob(f"{n:02d}_*/cmd1.stdout.txt")) or n == 10
        # The code copies are removed; what the runs wrote is kept.
        assert not (out / "work" / "night" / "app").exists()
        assert list((out / "work" / "night" / "reports" / "archive").glob("*.md"))

    def test_the_real_trees_are_untouched(self, run):
        """Reports, vintages, the SEC cache and journal entries of the checkout
        the drill runs from: nothing added, nothing rewritten."""
        _, out, before, after = run
        assert after == before
        assert list((out / "work").rglob("reports/AAPL_*.md"))

    def test_a_used_output_directory_is_refused(self, tmp_path):
        (tmp_path / "x").write_text("")
        assert drill.main(["--out", str(tmp_path)]) == 2


class TestKnownIssueMarkers:
    def test_a_reproducing_known_issue_does_not_fail_the_step(self):
        step = drill.Step(1, "s", "t")
        step.check("fine", True)
        step.check("defect", False, known="reported elsewhere")
        assert step.ok

    def test_a_known_issue_that_stops_reproducing_fails_the_step(self):
        """Strict: a fixed defect must take its marker with it, or a later
        regression would read as the known issue."""
        step = drill.Step(1, "s", "t")
        step.check("fine", True)
        step.check("defect", True, known="reported elsewhere")
        assert not step.ok

    def test_an_ordinary_failure_fails_the_step(self):
        step = drill.Step(1, "s", "t")
        step.check("fine", True)
        step.check("broken", False)
        assert not step.ok

    def test_a_step_with_no_checks_or_an_error_fails(self):
        assert not drill.Step(1, "s", "t").ok
        step = drill.Step(1, "s", "t", error="KeyError: x")
        step.check("fine", True)
        assert not step.ok


class TestNormalisation:
    REPORT = "\n".join([
        "# AAPL", "- Data fetched: 2026-09-26 13:18 UTC (EDGAR JSON caches)",
        "- Vintage snapshot: captured x.json.gz (5 KB)", "- Coverage: 90%",
    ])

    def test_only_the_volatile_lines_are_masked(self):
        rerun = (self.REPORT.replace("13:18", "13:19")
                 .replace("captured x.json.gz (5 KB)", "unchanged since the last snapshot"))
        assert drill.normalise_report(self.REPORT) == drill.normalise_report(rerun)
        moved = self.REPORT.replace("90%", "89%")
        assert drill.normalise_report(self.REPORT) != drill.normalise_report(moved)

    def test_a_masked_prefix_inside_another_line_is_not_masked(self):
        other = self.REPORT.replace("- Coverage: 90%", "- Note: see - Data fetched: above")
        assert "- Note: see - Data fetched: above" in drill.normalise_report(other)

    def test_only_fetched_at_is_masked_in_the_ledger(self):
        a = {"fetched_at": "13:18", "claims": [{"id": "EV-1", "fetched_at": "13:18", "v": 1}]}
        b = copy.deepcopy(a)
        b["fetched_at"] = b["claims"][0]["fetched_at"] = "13:19"
        assert drill.normalise_ledger(a) == drill.normalise_ledger(b)
        b["claims"][0]["v"] = 2
        assert drill.normalise_ledger(a) != drill.normalise_ledger(b)


class TestScenarioEdits:
    @pytest.fixture
    def facts(self):
        return json.loads(drill.FIXTURE.read_text())

    def test_the_amended_fact_is_the_one_the_revenue_series_reads(self, facts):
        cur = drill.current_revenue_fact(facts, "AAPL")
        assert (cur.taxonomy, cur.quarter.isoformat()) == ("us-gaap", "2026-03-28")
        assert cur.fact["end"] == "2026-03-28" and cur.fact["form"] == "10-Q"

    def test_after_an_amendment_the_amendment_is_current(self, facts):
        cur = drill.current_revenue_fact(facts, "AAPL")
        drill.add_fact(facts, cur, val=1, form="10-Q/A", filed=cur.fact["filed"],
                       accn="0000320193-26-000001")
        again = drill.current_revenue_fact(facts, "AAPL")
        # Same day, lower accession: the /A still outranks the original.
        assert again.fact["form"] == "10-Q/A" and again.fact["val"] == 1

    def test_an_added_fact_carries_no_frame(self, facts):
        cur = drill.current_revenue_fact(facts, "AAPL")
        row = drill.add_fact(facts, cur, val=1)
        assert "frame" not in row

    def test_listing_a_filing_keeps_every_column_aligned(self):
        index = {"filings": {"recent": {
            "form": ["10-Q"], "accessionNumber": ["a"], "isXBRL": [1], "size": [10],
            "items": [""],
        }}}
        drill.list_filing(index, form="10-Q/A", accessionNumber="b")
        recent = index["filings"]["recent"]
        assert {len(c) for c in recent.values()} == {2}
        assert recent["form"] == ["10-Q/A", "10-Q"]
        assert recent["isXBRL"][0] == 0 and recent["items"][0] == ""

    def test_one_period_is_left(self, facts):
        newest = drill.keep_one_period(facts)
        ends = {r["end"] for c in facts["facts"].values() for spec in c.values()
                for rows in spec["units"].values() for r in rows}
        assert ends == {newest}

    def test_dropping_concepts_reports_what_went(self, facts):
        dropped = drill.drop_concepts(facts, lambda tag: tag != "NetIncomeLoss")
        assert dropped == ["us-gaap:NetIncomeLoss"]
        assert "NetIncomeLoss" not in facts["facts"]["us-gaap"]


class TestManifestCheck:
    """Step 10's append-only check (seen failing on 2026-09-27): two snapshots
    taken on one day sort by content hash, so the baseline need not stay first."""

    A = {"captured": "2026-09-27", "file": "2026-09-27-1055f0a6220b.json.gz", "sha256": "a"}
    B = {"captured": "2026-09-27", "file": "2026-09-27-01e16367e6c3.json.gz", "sha256": "b"}
    KEY = "CIK0000320193/manifest.json"

    def _files(self, *entries):
        return {self.KEY: json.dumps({"snapshots": list(entries)}).encode()}

    def test_a_later_snapshot_sorting_first_keeps_the_baseline(self):
        assert drill._manifest_keeps(self._files(self.A), self._files(self.B, self.A))

    def test_a_dropped_or_changed_entry_fails(self):
        assert not drill._manifest_keeps(self._files(self.A), self._files(self.B))
        assert not drill._manifest_keeps(self._files(self.A),
                                         self._files({**self.A, "sha256": "x"}, self.B))

    def test_a_missing_or_empty_manifest_fails(self):
        assert not drill._manifest_keeps(self._files(self.A), {})
        assert not drill._manifest_keeps(self._files(), self._files(self.A))
        assert not drill._manifest_keeps({}, self._files(self.A))


def test_a_relative_out_directory_works(tmp_path):
    """Hermes re-audit F4: `--out` relative to the caller's directory failed
    every step (0/12): the workspaces ran with that relative path as their
    cwd and were also handed paths rooted in it, applying it twice."""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "drill.py"), "--only", "1,2",
         "--out", "evidence/drill-aapl"],
        cwd=tmp_path, capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    log = json.loads((tmp_path / "evidence" / "drill-aapl" / "drill_log.json").read_text())
    assert log["passed"] is True and [s["slug"] for s in log["steps"]] == ["baseline", "rerun"]


@pytest.mark.parametrize("case", ["out_is_a_file", "out_not_empty", "cache_missing",
                                  "other_ticker_without_cache"])
def test_bad_arguments_are_refused_before_anything_is_written(tmp_path, case):
    """Review of #101: `--out` naming a file raised NotADirectoryError, and a
    missing `--cache` (or another ticker without one) raised only after the
    output directory was half built, so the same `--out` was then refused
    as not empty. Each is refused up front, and nothing is created."""
    out = tmp_path / "out"
    args = ["--out", str(out), "--only", "1"]
    if case == "out_is_a_file":
        out.write_text("a file")
    elif case == "out_not_empty":
        out.mkdir()
        (out / "keep").write_text("")
    elif case == "cache_missing":
        args += ["--ticker", "KO", "--cache", str(tmp_path / "nope")]
    else:
        args += ["--ticker", "KO"]
    before = sorted(p.name for p in tmp_path.rglob("*"))
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "drill.py"), *args],
                          cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 2, proc.stderr[-1500:]
    assert proc.stderr.startswith("error: ") and "Traceback" not in proc.stderr
    assert sorted(p.name for p in tmp_path.rglob("*")) == before
