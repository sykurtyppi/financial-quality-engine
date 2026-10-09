"""The workbench's read side: what a ticker page, the watchlist table and the
recent-runs list are built from (r36).

Everything is read through `report_files` — the live run pinned once
(`read_live`), the archive as `generations()` lists it — so a page never
shows one run's card beside another's ledger, and a past run is found by
matching the ticker's own listed generations, never by a path built from
the URL. Flag counts per tier come from the card: the evidence ledger holds
the run's metrics (each `directional`), not the card's flags, so counting
its rows would count evidence, not flags.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.brief import sources as brief_sources
from app.services.journal import reporting
from app.services.reporting import report_files
from app.services.reporting.report_files import read_live, replacing
from app.services.valuation.observation import MarketObservation, write_observation
from app.services.watch import watchlist as wl
from app.services.workbench import setup, views

CARD = """# Decision Card — {t}

_As of {day}. 90-second triage; full report follows as appendix._

## Attention flags

**Tier 1 — validated (low false-positive):**
- ⚠ not checked this run: silent revisions (no vintage baseline yet) (see data quality)
- 8-K Item 4.02 non-reliance (2026-08-01)

**Tier 2 — directional (review in context):**
- Elevated concern: leverage_change (FY2027Q1)
- Elevated concern: current_ratio (FY2027Q1) — ⚠ reads a revised figure

**Tier 3 — context:**
- none surfaced this run

## Events & capital markets

- Checked — no securities-offering activity in the window.

## Scope limitation

**Examples of material risks not analyzed by this engine include:** a list.

---

# Full report (appendix)

## Block scores

| Block | Score |
|---|---|
| Earnings Quality | 29 |
"""


def _ledger(ticker: str, day: str, *, valuation: str | None = None) -> str:
    doc = {"ticker": ticker, "generated_on": day, "config_version": "0.3.0",
           "coverage": 0.92, "fresh": False, "items": [], "unsourced": []}
    if valuation is not None:
        doc["valuation"] = {"state": valuation}
    return json.dumps(doc)


def publish(ticker: str, day: str, text: str | None = None, *, ledger: str | None = None,
            now: datetime | None = None) -> report_files.Published:
    """One run published as `build_report` publishes it: staged, sealed,
    and swapped live (a generation of its own)."""
    out = reporting.REPORTS / f"{ticker}_{day}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    with report_files.recording() as made, replacing(out, now=now) as staged:
        staged.report.write_text(text if text is not None else CARD.format(t=ticker, day=day))
        staged.ledger.write_text(ledger if ledger is not None else _ledger(ticker, day))
    return made[-1]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(reporting, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(reporting, "MARKET", tmp_path / "journal" / "market")
    monkeypatch.setattr(wl, "WATCHLIST", tmp_path / "journal" / "watchlist.json")
    monkeypatch.setattr(brief_sources, "BRIEFS", tmp_path / "reports" / "briefs")
    (tmp_path / "journal").mkdir()
    return tmp_path


# --- the card and its counts ----------------------------------------------------------


def test_split_puts_the_card_and_scope_notice_first_and_the_appendix_apart():
    text = CARD.format(t="KO", day="2026-10-09")
    card, appendix = views.split_report(text)
    assert card.startswith("# Decision Card — KO") and "## Scope limitation" in card
    assert "Full report (appendix)" not in card and "| Earnings Quality | 29 |" not in card
    assert appendix.startswith("## Block scores") and "| Earnings Quality | 29 |" in appendix
    # Nothing is lost or duplicated between the two halves.
    assert card in text and appendix in text and len(card) + len(appendix) < len(text)


def test_split_without_the_marker_is_all_card():
    assert views.split_report("# Some older report\n\nbody\n") == ("# Some older report\n\nbody\n", "")


def test_tier_counts_count_flags_not_placeholders_and_not_checked_apart():
    counts = views.tier_counts(CARD.format(t="KO", day="2026-10-09"))
    assert counts == views.TierCounts(tier1=1, tier2=2, tier3=0, not_checked=1)
    assert counts.flags == 3


def test_tier_counts_of_a_card_without_tiers_are_none():
    assert views.tier_counts("# Some older report\n\n- a bullet\n") is None


def test_tier_counts_stop_at_the_next_section():
    card = ("## Attention flags\n\n**Tier 1 — validated (low false-positive):**\n"
            "- none surfaced this run\n\n**Tier 2 — directional (review in context):**\n"
            "- none surfaced this run\n\n**Tier 3 — context:**\n- Elevated: x (FY1)\n\n"
            "## Checked and clean\n\n- Strong free-cash-flow generation (TTM)\n- Another\n")
    assert views.tier_counts(card) == views.TierCounts(0, 0, 1, 0)


# --- a ticker's runs --------------------------------------------------------------------


def test_no_run_is_an_empty_view():
    v = views.ticker_view("ko")
    assert v.ticker == "KO" and v.latest is None and v.runs == [] and v.problems == []


def test_the_view_reads_the_live_run_whole():
    made = publish("KO", "2026-10-09", ledger=_ledger("KO", "2026-10-09", valuation="produced"))
    v = views.ticker_view("KO")
    run = v.latest
    assert run is not None and run.generation_id == made.generation_id and run.live
    assert run.day == "2026-10-09" and run.card.startswith("# Decision Card — KO")
    assert "| Earnings Quality | 29 |" in run.appendix
    assert run.tiers == views.TierCounts(1, 2, 0, 1)
    assert run.engine and run.engine == report_files.engine_commit()
    assert run.ledger is not None and run.ledger.valuation == "produced"
    assert run.ledger.generated_on == "2026-10-09" and run.ledger.coverage == pytest.approx(0.92)
    assert run.ledger_problem is None
    assert [r.generation_id for r in v.runs] == [made.generation_id]


def test_history_spans_days_and_reruns_newest_first():
    t0 = datetime(2026, 10, 7, 12, tzinfo=UTC)
    a = publish("KO", "2026-10-07", now=t0)
    b = publish("KO", "2026-10-09", now=t0 + timedelta(days=2))
    c = publish("KO", "2026-10-09", now=t0 + timedelta(days=2, hours=1))
    publish("KOF", "2026-10-09", now=t0)  # a ticker that only starts the same
    v = views.ticker_view("KO")
    assert [r.generation_id for r in v.runs] == [c.generation_id, b.generation_id, a.generation_id]
    assert [r.live for r in v.runs] == [True, False, True]
    assert [r.day for r in v.runs] == ["2026-10-09", "2026-10-09", "2026-10-07"]
    assert v.runs[0].built == t0 + timedelta(days=2, hours=1)
    assert v.latest.generation_id == c.generation_id
    # A day asked for is that day's live run.
    assert views.ticker_view("KO", "2026-10-07").latest.generation_id == a.generation_id
    assert views.ticker_view("KO", "2026-10-08").latest is None


def test_a_past_run_is_found_only_among_the_tickers_own_generations():
    t0 = datetime(2026, 10, 9, 12, tzinfo=UTC)
    old = publish("KO", "2026-10-09", CARD.format(t="KO", day="old"), now=t0)
    new = publish("KO", "2026-10-09", now=t0 + timedelta(hours=1))
    crm = publish("CRM", "2026-10-09", now=t0)
    run = views.past_run("KO", old.generation_id)
    assert run is not None and not run.live and run.generation_id == old.generation_id
    assert "_As of old." in run.card
    assert views.past_run("KO", new.generation_id).live
    # Another ticker's generation is not this ticker's run.
    assert views.past_run("KO", crm.generation_id) is None
    for bad in ("../../etc", "..", "", "current", old.generation_id.upper(),
                old.generation_id[:-1], f"{old.generation_id}/x", "0" * 32):
        assert views.past_run("KO", bad) is None, bad


def test_a_run_whose_ledger_cannot_be_read_says_so():
    publish("KO", "2026-10-09", ledger='{"generation_id": null, "not": "a ledger"}')
    run = views.ticker_view("KO").latest
    assert run is not None and run.ledger is None
    assert run.ledger_problem and "cannot be read" in run.ledger_problem


def test_a_reports_dir_the_view_cannot_read_is_a_problem_not_a_crash(monkeypatch):
    publish("KO", "2026-10-09")

    def boom(report):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(report_files, "read_live", boom)
    v = views.ticker_view("KO")
    assert v.latest is None and v.problems and "Input/output error" in v.problems[0]


def test_audit_and_briefs_are_listed_read_only(isolated):
    made = publish("KO", "2026-10-09")
    gen = Path(made.report).parent
    os.chmod(gen, 0o755)
    (gen / "KO_2026-10-09_audit.md").write_text(f"# audit\n<!-- generation: {made.generation_id} -->\n")
    briefs = isolated / "reports" / "briefs"
    briefs.mkdir(parents=True)
    (briefs / "KO_2026-10-08.md").write_text("brief")
    (briefs / "KOF_2026-10-08.md").write_text("not KO's")
    (briefs / "KO_notaday.md").write_text("not a brief name")
    v = views.ticker_view("KO")
    assert v.latest.audit == gen / "KO_2026-10-09_audit.md"
    assert v.briefs == [briefs / "KO_2026-10-08.md"]


def test_the_observation_and_its_problems_are_shown(isolated):
    now = datetime.now(UTC)
    obs = MarketObservation(ticker="KO", price=66.25, observed_at=now - timedelta(hours=1),
                            source="NYSE close", recorded_at=now)
    write_observation(reporting.MARKET.parent, obs)
    v = views.ticker_view("KO")
    assert v.observation is not None and v.observation.observation.price == 66.25
    assert v.observation_error is None
    (reporting.MARKET / "KO.json").write_text("{not json")
    v = views.ticker_view("KO")
    assert v.observation is None and "not JSON" in v.observation_error


def test_the_watch_row_is_found(isolated):
    wl.add_entry({"ticker": "KO", "print_at": "2026-10-21T11:00:00+00:00"})
    assert views.ticker_view("KO").watch.ticker == "KO"
    assert views.ticker_view("CRM").watch is None


# --- the watchlist table --------------------------------------------------------------


def test_watchlist_rows_show_latest_run_counts_and_staleness(isolated):
    now = datetime(2026, 10, 9, 15, tzinfo=UTC)
    wl.add_entry({"ticker": "KO", "print_at": "2026-10-21T11:00:00+00:00", "label": "FQ3-26"})
    wl.add_entry({"ticker": "CRM", "print_at": "2026-09-03T20:20:00+00:00"})
    wl.add_entry({"ticker": "AAPL", "print_at": "2026-10-30T20:30:00+00:00"})
    publish("KO", "2026-10-09", now=now - timedelta(hours=1))
    publish("CRM", "2026-09-01", now=datetime(2026, 9, 1, 12, tzinfo=UTC))
    rows, problem = views.watchlist_rows(now=now)
    assert problem is None
    by = {r.ticker: r for r in rows}
    assert [r.ticker for r in rows] == ["CRM", "KO", "AAPL"]  # by next print, as the loader sorts
    assert by["KO"].latest.day == "2026-10-09" and by["KO"].tiers == views.TierCounts(1, 2, 0, 1)
    assert by["KO"].stale is None and by["KO"].label == "FQ3-26"
    # CRM printed on 09-03; its latest run is from before the print.
    assert by["CRM"].stale and "before the print" in by["CRM"].stale
    assert by["AAPL"].latest is None and by["AAPL"].stale is None


def test_a_run_older_than_the_limit_is_stale_even_before_a_print(isolated):
    now = datetime(2026, 10, 9, 15, tzinfo=UTC)
    wl.add_entry({"ticker": "KO", "print_at": "2026-10-21T11:00:00+00:00"})
    publish("KO", "2026-09-30", now=now - timedelta(days=views.RUN_STALE_DAYS, minutes=1))
    (row,), _ = views.watchlist_rows(now=now)
    assert row.stale and f"older than {views.RUN_STALE_DAYS} days" in row.stale
    assert views.stale_reason(row.print_at, now - timedelta(days=views.RUN_STALE_DAYS - 1), now) is None


def test_a_watchlist_that_cannot_be_read_is_said_once(isolated):
    wl.WATCHLIST.write_text("{not json")
    rows, problem = views.watchlist_rows()
    assert rows == [] and problem and "invalid JSON" in problem


def test_recent_runs_across_tickers_newest_first(isolated):
    t0 = datetime(2026, 10, 1, 12, tzinfo=UTC)
    publish("KO", "2026-10-01", now=t0)
    publish("CRM", "2026-10-02", now=t0 + timedelta(days=1))
    publish("KO", "2026-10-03", now=t0 + timedelta(days=2))
    (reporting.REPORTS / "KO_2026-10-03.replay.md").write_text("a replay is never the latest")
    (reporting.REPORTS / "notes.md").write_text("not a report")
    runs = views.recent_runs()
    assert [(r.ref.ticker, r.ref.day) for r in runs] == [
        ("KO", "2026-10-03"), ("CRM", "2026-10-02"), ("KO", "2026-10-01")]
    assert runs[0].tiers == views.TierCounts(1, 2, 0, 1)
    assert [r.ref.day for r in views.recent_runs(limit=1)] == ["2026-10-03"]


def test_recent_runs_with_no_reports_dir_is_empty():
    assert views.recent_runs() == []


# --- setup ----------------------------------------------------------------------------


def test_setup_problems_name_a_missing_or_malformed_identity(monkeypatch, isolated):
    monkeypatch.delenv("EDGAR_IDENTITY", raising=False)
    (p,) = setup.setup_problems()
    assert "EDGAR_IDENTITY is not set" in p
    for bad in ("jane", "jane@example.com", "Jane Doe", "   "):
        monkeypatch.setenv("EDGAR_IDENTITY", bad)
        (p,) = setup.setup_problems()
        assert "does not look like" in p, bad
    monkeypatch.setenv("EDGAR_IDENTITY", "Jane Doe jane@example.com")
    assert setup.setup_problems() == []


def test_setup_problems_name_an_unwritable_reports_dir(monkeypatch, isolated):
    monkeypatch.setenv("EDGAR_IDENTITY", "Jane Doe jane@example.com")
    reporting.REPORTS.parent.mkdir(parents=True, exist_ok=True)
    reporting.REPORTS.write_text("a file, not a directory")
    (p,) = setup.setup_problems()
    assert str(reporting.REPORTS) in p and "cannot be written" in p
    # Asking writes nothing behind.
    reporting.REPORTS.unlink()
    reporting.REPORTS.mkdir()
    assert setup.setup_problems() == [] and list(reporting.REPORTS.iterdir()) == []


def test_read_live_is_the_only_reader_of_a_live_run(monkeypatch):
    """The view never reads a live name itself: one pinned read per run."""
    publish("KO", "2026-10-09")
    calls = []
    real = report_files.read_live

    def spy(report):
        calls.append(report)
        return real(report)

    monkeypatch.setattr(report_files, "read_live", spy)
    views.ticker_view("KO")
    assert calls and all(isinstance(c, Path) for c in calls)
    assert read_live is real  # the module's own name is untouched


# --- edges (from the r36 mutation run) ------------------------------------------------


def test_a_reports_folder_that_cannot_be_listed_is_no_runs(monkeypatch):
    publish("KO", "2026-10-09")
    real = Path.iterdir

    def iterdir(self):
        if self == reporting.REPORTS:
            raise PermissionError(13, "Permission denied")
        return real(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    # The generations folder still lists the day.
    assert [r.day for r in views.runs("KO")] == ["2026-10-09"]
    assert views.recent_runs() == []


def test_a_day_whose_runs_cannot_be_listed_is_a_problem(isolated):
    made = publish("KO", "2026-10-09")
    home = Path(made.report).parent.parent
    moved = home.with_name("moved")
    home.rename(moved)
    os.symlink(moved, home)  # a link where the engine's own folder should be
    problems: list[str] = []
    assert views.runs("KO", problems) == []
    assert len(problems) == 1 and "KO 2026-10-09 cannot be listed" in problems[0]
    assert views.runs("KO") == []  # without a list to say it in: nothing, no crash
    v = views.ticker_view("KO")
    assert v.latest is None and "cannot be listed" in v.problems[0]


def test_a_run_without_its_own_ledger_says_which_case(isolated):
    made = publish("KO", "2026-10-09")
    ledger = Path(made.report).with_name("KO_2026-10-09.ledger.json")
    os.chmod(ledger, 0o644)
    ledger.write_text(json.dumps({"generation_id": "f" * 32}))
    run = views.ticker_view("KO").latest
    assert run.ledger is None and "another run's" in run.ledger_problem
    ledger.unlink()
    run = views.ticker_view("KO").latest
    assert run.ledger is None and run.ledger_problem == "This run has no evidence ledger."


def test_stale_boundaries():
    now = datetime(2026, 10, 9, 15, tzinfo=UTC)
    assert views.RUN_STALE_DAYS == 7  # a week, as the observation's own staleness
    # A print exactly now, the run built before it: stale.
    assert views.stale_reason(now, now - timedelta(minutes=1), now)
    # Built at the very moment of a past print: not before it.
    assert views.stale_reason(now - timedelta(hours=1), now - timedelta(hours=1), now) is None
    # Exactly the limit old is not older than it.
    future = now + timedelta(days=30)
    assert views.stale_reason(future, now - timedelta(days=7), now) is None
    assert views.stale_reason(future, now - timedelta(days=7, seconds=1), now)
