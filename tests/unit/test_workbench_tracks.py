"""The workbench publishes under ``reports/workbench/``, never over the
journal's or the auto track's runs (independent review of 9d00328, H1+H2).

A workbench run used to publish at ``reports/<T>_<today>.md``: the journal
case's own live name. It could publish over a case PENDING its audit (the
sweep's audit was then refused as "not the live run", its attempt counter
climbed to abandon) and replace an audited case's live run in the review
console. Its runs now live in their own namespace, as the auto track's live
in ``reports/auto/``: the same layout (generations, ledger, archive), a
different directory. Readers of the journal and auto tracks
(`earnings_brief.latest_report`, the watch's briefs) do not look there.

What it still shares with them, on purpose: the market observation
(``journal/market/<T>.json``) a run reads, and the operator's eyes. The
card is readable before a thesis is written, so `journal.py openv2` says
so when a workbench run of the ticker exists, and the ticker page says when
a journal case of the ticker is open today.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.services.brief import sources as brief_sources
from app.services.journal import reporting, review, store
from app.services.reporting import report_files
from app.services.reporting.report_files import read_live, replacing
from app.services.watch import watchlist as wl
from app.services.workbench import jobs
from app.web import app
from scripts import earnings_brief, run_audit
from scripts import journal as J

TODAY = date.today().isoformat()
IDENTITY = "Workbench Test test@example.com"
OPEN = ["openv2", "KO", "--thesis", "margins hold", "--conviction", "3", "--action", "hold",
        "--assumption", "revenue,>,1000,FY2026Q3,,2026-12-31"]


def _report_text(ticker: str, tag: str) -> str:
    return (f"# Decision Card — {ticker}\n\n**Tier 2 — directional (review in context):**\n"
            f"- {tag}\n\n---\n\n# Full report (appendix)\n\nbody {tag}\n")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(reporting, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(reporting, "MARKET", tmp_path / "journal" / "market")
    monkeypatch.setattr(store, "ENTRIES", tmp_path / "journal" / "entries")
    monkeypatch.setattr(wl, "WATCHLIST", tmp_path / "journal" / "watchlist.json")
    monkeypatch.setattr(brief_sources, "BRIEFS", tmp_path / "reports" / "briefs")
    monkeypatch.setattr(jobs, "REGISTRY", jobs.Registry())
    monkeypatch.setenv("EDGAR_IDENTITY", IDENTITY)
    (tmp_path / "journal").mkdir()
    return tmp_path


@pytest.fixture
def builds(env, monkeypatch):
    """`build_report` as it publishes: under ``out_dir`` when given, else
    ``reports/`` (the journal's live names), one generation per call. Both
    the workbench (which looks the name up at call time) and `journal.py`
    (which imported it) build through it."""
    calls: list[dict] = []

    def build(ticker, with_docs=True, quarters=8, report_day=None, fresh=False,
              out_dir=None, **kw):
        calls.append({"ticker": ticker, "out_dir": out_dir, "report_day": report_day})
        out = (out_dir or reporting.REPORTS) / f"{ticker.upper()}_{report_day or TODAY}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        with replacing(out) as staged:
            staged.report.write_text(_report_text(ticker.upper(), f"run {len(calls)}"))
            staged.ledger.write_text(json.dumps({
                "ticker": ticker.upper(), "generated_on": TODAY, "config_version": "0.3.0",
                "items": [], "unsourced": []}))
        return out, "no acute signals"

    monkeypatch.setattr(reporting, "build_report", build)
    monkeypatch.setattr(J, "build_report", build)
    return calls


@pytest.fixture
def client(env):
    return TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000),
                      follow_redirects=False)


def _journal(*argv: str) -> int:
    args = J.build_parser().parse_args(list(argv))
    return args.func(args)


def _workbench_run(client, ticker: str = "KO") -> jobs.Job:
    assert client.post(f"/t/{ticker}/run", data={"fresh": "0"}).status_code == 303
    job = jobs.latest(ticker)
    done = jobs.wait(job.id, timeout=30)
    assert done is not None and done.state == jobs.DONE, done
    return done


def _tree(root: Path, skip: Path) -> dict[str, object]:
    """Every file, link and folder under ``root`` but ``skip``: bytes for a
    file, the target for a link (never followed)."""
    out: dict[str, object] = {}
    for p in sorted(root.rglob("*")):
        if p == skip or skip in p.parents:
            continue
        rel = str(p.relative_to(root))
        if p.is_symlink():
            out[rel] = ("link", os.readlink(p))
        elif p.is_file():
            out[rel] = p.read_bytes()
        else:
            out[rel] = "dir"
    return out


def _case_reported_pending(tmp_path: Path) -> tuple[str, Path, Path]:
    """A v2 case of today, reported with ``--defer-mark`` as the sweep
    reports it: its report published (G1), the entry PENDING its audit."""
    assert _journal(*OPEN) == 0
    result = tmp_path / "result.json"
    assert _journal("report", "KO", "--date", TODAY, "--no-docs", "--defer-mark",
                    "--result-file", str(result)) == 0
    res = json.loads(result.read_text())
    entry = store.find_entry("KO", TODAY)
    assert store.pending_marker(entry).generation_id == res["generation_id"]
    return res["generation_id"], Path(res["report"]), entry


def _console(ticker: str, day: str) -> tuple:
    run = review.read_run(ticker, day)
    return run.live.generation_id, run.live.text, run.audit, run.audit_text, run.problems


# --- the namespace --------------------------------------------------------------------


def test_a_workbench_run_publishes_under_reports_workbench(client, builds, env):
    done = _workbench_run(client)
    workbench = env / "reports" / "workbench" / f"KO_{TODAY}.md"
    live = read_live(workbench)
    assert live is not None and live.generation_id == done.generation_id
    # Nothing at the journal's live names, and its own generations beside it.
    assert not (env / "reports" / f"KO_{TODAY}.md").exists()
    assert not (env / "reports" / report_files.GENERATIONS_DIR).exists()
    assert (env / "reports" / "workbench" / report_files.GENERATIONS_DIR / f"KO_{TODAY}").is_dir()
    assert builds[0]["out_dir"] == env / "reports" / "workbench"


def test_a_case_pending_its_audit_is_untouched_by_a_workbench_run(client, builds, env):
    """Reviewer's collide.py (b): the sweep reported the case with
    --defer-mark and its audit is about to run (or running). A workbench
    run of the same ticker and day changes nothing of the case: its live
    run, pending marker, attempt counter, every file of the journal track;
    and the audit of G1 then lands as the live run's (rc 0)."""
    g1, g1_report, entry = _case_reported_pending(env)
    before = _tree(env, env / "reports" / "workbench")
    console = _console("KO", TODAY)
    wb = _workbench_run(client)
    assert wb.generation_id != g1
    assert _tree(env, env / "reports" / "workbench") == before
    assert _console("KO", TODAY) == console
    assert read_live(reporting.report_path("KO")).generation_id == g1
    assert store.pending_marker(entry).generation_id == g1
    # The sweep's audit of G1, after the workbench publish: G1 is still live.
    assert run_audit.publish_audit(g1_report, read_live(g1_report), "# audit of G1\n") == 0
    assert review.read_run("KO", TODAY).audit == "matches"


def test_a_reported_audited_case_reads_the_same_after_a_workbench_run(client, builds, env):
    """Reviewer's collide.py (c): an audited, stamped case's live run, its
    audit and what the review console shows are byte-identical after a
    workbench run of the same ticker and day."""
    g1, g1_report, entry = _case_reported_pending(env)
    assert run_audit.publish_audit(g1_report, read_live(g1_report), "# audit of G1\n") == 0
    assert _journal("mark-reported", "KO", "--date", TODAY, "--generation", g1) == 0
    assert store.pending_marker(entry) is None
    before = _tree(env, env / "reports" / "workbench")
    console = _console("KO", TODAY)
    assert console[0] == g1 and console[2] == "matches"
    _workbench_run(client)
    _workbench_run(client)  # and a rerun of it
    assert _tree(env, env / "reports" / "workbench") == before
    assert _console("KO", TODAY) == console
    page = client.get(f"/review/KO?date={TODAY}")
    assert page.status_code == 200 and g1 in page.text
    assert earnings_brief.audit_for(reporting.report_path("KO")) == read_live(
        reporting.report_path("KO")).audit


def test_the_journal_and_auto_tracks_never_pick_a_workbench_run(client, builds, env,
                                                                   monkeypatch):
    """`earnings_brief.latest_report` (what the watch's brief builds from
    when no report is named) reads ``reports/auto/`` and ``reports/``: a
    newer workbench run never wins over the journal's or the auto track's."""
    monkeypatch.setattr(earnings_brief, "REPORT_DIRS",
                        (env / "reports" / "auto", env / "reports"))
    journal_run = env / "reports" / "KO_2026-10-01.md"
    journal_run.parent.mkdir(parents=True)
    with replacing(journal_run) as staged:
        staged.report.write_text(_report_text("KO", "journal"))
        staged.ledger.write_text("{}")
    _workbench_run(client)
    assert earnings_brief.latest_report("KO") == journal_run
    auto_run = env / "reports" / "auto" / "KO_2026-09-30.md"
    auto_run.parent.mkdir(parents=True)
    with replacing(auto_run) as staged:
        staged.report.write_text(_report_text("KO", "auto"))
        staged.ledger.write_text("{}")
    _workbench_run(client)
    assert earnings_brief.latest_report("KO") == auto_run
    assert earnings_brief.latest_report("CRM") is None


# --- reading the card before the thesis ------------------------------------------------


def test_openv2_says_a_workbench_run_was_there_to_read(client, builds, env, capsys):
    """Not set automatically, and the exit code is unchanged: whether the
    operator read the card is theirs to say."""
    done = _workbench_run(client)
    capsys.readouterr()
    assert _journal(*OPEN) == 0
    err = capsys.readouterr().err
    assert "workbench" in err and done.generation_id in err and TODAY in err
    assert f'--contamination "saw the workbench card for KO on {TODAY}"' in err
    entry = store.load_v2(store.find_entry("KO", TODAY))
    assert entry.before.contamination is None


def test_openv2_says_nothing_without_a_workbench_run(env, builds, capsys):
    # The journal's own run of the ticker is not one.
    out = reporting.REPORTS / "KO_2026-10-01.md"
    out.parent.mkdir(parents=True)
    with replacing(out) as staged:
        staged.report.write_text(_report_text("KO", "journal"))
        staged.ledger.write_text("{}")
    assert _journal(*OPEN) == 0
    assert "workbench" not in capsys.readouterr().err


def test_openv2_with_an_unreadable_workbench_folder_still_opens(env, builds, capsys):
    (env / "reports").mkdir()
    (env / "reports" / "workbench").write_text("a file where the folder should be")
    assert _journal(*OPEN) == 0
    assert store.find_entry("KO", TODAY) is not None


def test_the_ticker_page_says_a_journal_case_is_open_today(client, builds, env):
    assert "journal case is open" not in client.get("/t/KO").text
    assert _journal(*OPEN) == 0
    page = client.get("/t/KO").text
    assert f"A journal case is open for KO today ({TODAY})" in page
    assert "this card is the workbench's, not the case's" in page
    # Another ticker's case is not this one's; neither is another day's.
    assert "journal case is open" not in client.get("/t/CRM").text
    assert _journal(*[a if a != "KO" else "PEP" for a in OPEN], "--date", "2026-01-02") == 0
    assert "journal case is open" not in client.get("/t/PEP").text
