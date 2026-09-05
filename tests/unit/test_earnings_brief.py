"""Earnings brief: deterministic source collection (exhibits by EDGAR type,
operator-supplied transcript, engine report/audit, prior brief) and the CLI's
file handling. The headless Claude run is never exercised here — the prompt
and the post-processing around it are.
"""

from __future__ import annotations

import importlib.util
from argparse import Namespace
from datetime import date
from pathlib import Path

import pytest

from app.services.brief import sources as bs
from app.services.ingestion import edgar_documents as ed

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("brief_cli", ROOT / "scripts" / "earnings_brief.py")
brief_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(brief_cli)

HEADER = (
    "&lt;DOCUMENT&gt;\n&lt;TYPE&gt;8-K\n&lt;SEQUENCE&gt;1\n&lt;FILENAME&gt;main.htm\n&lt;/DOCUMENT&gt;\n"
    "&lt;DOCUMENT&gt;\n&lt;TYPE&gt;EX-99.2\n&lt;SEQUENCE&gt;3\n&lt;FILENAME&gt;cfo.htm\n&lt;/DOCUMENT&gt;\n"
    "&lt;DOCUMENT&gt;\n&lt;TYPE&gt;EX-99.1\n&lt;SEQUENCE&gt;2\n&lt;FILENAME&gt;q2pr.htm\n&lt;/DOCUMENT&gt;\n"
    "&lt;DOCUMENT&gt;\n&lt;TYPE&gt;EX-99.3\n&lt;SEQUENCE&gt;4\n&lt;FILENAME&gt;slides.htm\n&lt;/DOCUMENT&gt;\n"
    "&lt;DOCUMENT&gt;\n&lt;TYPE&gt;EX-99.4\n&lt;SEQUENCE&gt;5\n&lt;FILENAME&gt;short.htm\n&lt;/DOCUMENT&gt;\n"
)
LONG = "<p>" + "Revenue grew and margins expanded in the quarter under review. " * 40 + "</p>"
SUBS = {
    "name": "NVIDIA CORP",
    "filings": {"recent": {
        "form": ["10-Q", "8-K", "8-K", "8-K"],
        "accessionNumber": ["q-1", "k-new", "k-other", "k-old"],
        "filingDate": ["2026-08-26", "2026-08-26", "2026-08-01", "2026-05-20"],
        "reportDate": ["2026-07-26", "2026-08-26", "2026-08-01", "2026-05-20"],
        "items": [None, "2.02,9.01", "5.02", "2.02,9.01"],
        "acceptanceDateTime": [None, "2026-08-26T20:21:00.000Z", None, None],
        "primaryDocument": [None] * 4,
    }},
}


class _Client:
    cache_dir = None

    def resolve_cik(self, ticker):
        return 1045810

    def submissions_by_cik(self, cik):
        return SUBS


@pytest.fixture
def archive(monkeypatch):
    files = {
        "k-new-index-headers.html": HEADER,
        "q2pr.htm": LONG,
        "cfo.htm": LONG.replace("Revenue", "Commentary"),
        "slides.htm": LONG,
        "short.htm": "<p>too short</p>",
    }
    monkeypatch.setattr(ed, "_fetch_archive", lambda c, cik, acc, doc: files[doc])
    monkeypatch.setattr(bs, "_fetch_archive", lambda c, cik, acc, doc: files[doc])
    return files


class TestLatestEarnings8K:
    def test_newest_202_wins_and_non_202_ignored(self):
        assert bs.latest_earnings_8k(SUBS).accession == "k-new"

    def test_explicit_accession_must_be_a_202(self):
        assert bs.latest_earnings_8k(SUBS, "k-old").accession == "k-old"
        with pytest.raises(bs.BriefSourceError, match="not an Item 2.02"):
            bs.latest_earnings_8k(SUBS, "k-other")


class TestCollectSources:
    def test_release_by_type_then_narrative_exhibits_only(self, archive, tmp_path):
        src = bs.collect_sources(_Client(), "nvda", out_root=tmp_path, transcript_root=tmp_path)
        roles = [(f.role, f.path.name) for f in src.files]
        assert roles == [("release", "release_EX-99_1.txt"), ("exhibit", "exhibit_EX-99_2.txt")]
        assert src.workdir == tmp_path / "NVDA" / "2026-08-26"
        assert "Revenue grew" in (src.workdir / "release_EX-99_1.txt").read_text()
        assert any("slides.htm: tables/slides" in d for d in src.diagnostics)
        assert any("short.htm" in d and "skipped" in d for d in src.diagnostics)
        assert not src.has_transcript
        assert any("no call transcript" in d for d in src.diagnostics)
        assert src.company == "NVIDIA CORP" and src.event_day == "2026-08-26"

    def test_transcript_auto_discovered_by_print_date(self, archive, tmp_path):
        folder = tmp_path / "NVDA"
        folder.mkdir()
        (folder / "2026-08-26.txt").write_text("Operator: welcome to the call.")
        src = bs.collect_sources(_Client(), "NVDA", out_root=tmp_path, transcript_root=tmp_path)
        assert src.has_transcript
        assert (src.workdir / "transcript.txt").read_text().startswith("Operator")

    def test_explicit_transcript_must_exist(self, archive, tmp_path):
        with pytest.raises(bs.BriefSourceError, match="transcript not found"):
            bs.collect_sources(_Client(), "NVDA", out_root=tmp_path,
                               transcript=tmp_path / "missing.txt")

    def test_report_and_audit_attached_when_present(self, archive, tmp_path):
        rep = tmp_path / "NVDA_2026-08-26.md"
        rep.write_text("# report")
        src = bs.collect_sources(_Client(), "NVDA", out_root=tmp_path, transcript_root=tmp_path,
                                 report=rep, audit=tmp_path / "nope.md")
        assert [f.role for f in src.files][-1] == "report"

    def test_no_typed_exhibits_is_a_diagnostic_not_a_crash(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ed, "_fetch_archive", lambda *a: "<html>no header</html>")
        src = bs.collect_sources(_Client(), "NVDA", out_root=tmp_path, transcript_root=tmp_path)
        assert src.files == []
        assert any("no typed EX-99" in d for d in src.diagnostics)


class TestFindTranscript:
    def test_exact_then_later_dated_file(self, tmp_path):
        folder = tmp_path / "NVDA"
        folder.mkdir()
        (folder / "2026-08-27.txt").write_text("x")
        (folder / "2026-05-21.txt").write_text("old")
        (folder / "notes.txt").write_text("not dated")
        assert bs.find_transcript("NVDA", date(2026, 8, 26), tmp_path).name == "2026-08-27.txt"
        (folder / "2026-08-26.txt").write_text("exact")
        assert bs.find_transcript("NVDA", date(2026, 8, 26), tmp_path).name == "2026-08-26.txt"
        assert bs.find_transcript("NVDA", date(2026, 9, 30), tmp_path) is None
        assert bs.find_transcript("AMD", date(2026, 8, 26), tmp_path) is None


class TestCliHelpers:
    def test_finalize_guarantees_footer_and_carries_useful(self):
        raw = "# NVDA — brief\n\n## Headline\nfine.\n\n---\nuseful: unset\n"
        assert brief_cli.finalize(raw, "yes").endswith("\n\n---\nuseful: yes\n")
        assert brief_cli.finalize("# x\n## Headline\nno footer").endswith("---\nuseful: unset\n")
        assert brief_cli.useful_value("...\nuseful: No\n") == "no"
        assert brief_cli.useful_value("nothing") == "unset"

    def test_prior_brief_is_the_newest_earlier_one(self, tmp_path):
        for d in ("2026-05-20", "2026-08-26", "2026-02-25"):
            (tmp_path / f"NVDA_{d}.md").write_text("x")
        assert brief_cli.prior_brief("NVDA", date(2026, 8, 26), tmp_path).name == "NVDA_2026-05-20.md"
        assert brief_cli.prior_brief("NVDA", date(2026, 2, 25), tmp_path) is None

    def test_digest_takes_headline_and_changed_sections(self, tmp_path):
        (tmp_path / "NVDA_2026-08-26.md").write_text(
            "# NVDA — FQ2-27 — earnings brief\n\n## Headline\nRevenue $96.2B.\n\n"
            "## Guidance\n| a | b |\n\n## Changed since last quarter\n- guide raised\n\n"
            "---\nuseful: yes\n")
        (tmp_path / "AAPL_2026-07-31.md").write_text("# AAPL\n## Headline\nold.\n")
        (tmp_path / "DIGEST_2026-09-01.md").write_text("ignored")
        paths = brief_cli.briefs_in_window(date(2026, 8, 1), tmp_path)
        assert [p.name for p in paths] == ["NVDA_2026-08-26.md"]
        text = brief_cli.build_digest(paths, date(2026, 8, 1), date(2026, 9, 5))
        assert "## NVDA — FQ2-27 — earnings brief" in text
        assert "Revenue $96.2B." in text and "- guide raised" in text
        assert "| a | b |" not in text
        assert "useful: yes" in text

    def test_build_prompt_lists_roles_and_diagnostics(self, archive, tmp_path):
        src = bs.collect_sources(_Client(), "NVDA", out_root=tmp_path, transcript_root=tmp_path)
        prompt = brief_cli.build_prompt(src)
        assert "earnings-brief skill" in prompt and "NVIDIA CORP" in prompt
        assert "- release: " in prompt and "- exhibit: " in prompt
        assert "no call transcript supplied" in prompt


class TestCliBuild:
    @pytest.fixture
    def env(self, archive, monkeypatch, tmp_path):
        monkeypatch.setattr(brief_cli, "SecClient", lambda fresh=False: _Client())
        monkeypatch.setattr(brief_cli, "BRIEFS", tmp_path)
        monkeypatch.setattr(bs, "BRIEFS", tmp_path)
        monkeypatch.setattr(bs, "TRANSCRIPTS", tmp_path / "transcripts")
        rep = tmp_path / "engine" / "NVDA_2026-09-01.md"
        rep.parent.mkdir()
        rep.write_text("# report")
        monkeypatch.setattr(brief_cli, "latest_report", lambda t: rep)
        return tmp_path

    def _args(self, **over):
        base = dict(ticker="nvda", accession=None, transcript=None, report=None,
                    timeout=5.0, dry_run=False)
        base.update(over)
        return Namespace(**base)

    def test_writes_the_brief_with_footer(self, env, monkeypatch):
        monkeypatch.setattr(brief_cli, "run_headless",
                            lambda prompt, timeout: (0, "# NVDA\n## Headline\nok\n", ""))
        assert brief_cli.cmd_build(self._args()) == 0
        out = env / "NVDA_2026-08-26.md"
        assert out.read_text().endswith("---\nuseful: unset\n")

    def test_regeneration_keeps_a_set_useful_value(self, env, monkeypatch):
        out = env / "NVDA_2026-08-26.md"
        out.write_text("# old\n## Headline\nold\n\n---\nuseful: yes\n")
        monkeypatch.setattr(brief_cli, "run_headless",
                            lambda prompt, timeout: (0, "# new\n## Headline\nnew\n", ""))
        assert brief_cli.cmd_build(self._args()) == 0
        assert "new" in out.read_text() and out.read_text().endswith("useful: yes\n")

    def test_headless_failure_exits_2_and_keeps_sources(self, env, monkeypatch, capsys):
        monkeypatch.setattr(brief_cli, "run_headless", lambda prompt, timeout: (1, "", "boom"))
        assert brief_cli.cmd_build(self._args()) == 2
        assert not (env / "NVDA_2026-08-26.md").exists()
        assert (env / "NVDA" / "2026-08-26" / "release_EX-99_1.txt").exists()
        assert "boom" in capsys.readouterr().err

    def test_output_without_headline_is_a_failure(self, env, monkeypatch):
        monkeypatch.setattr(brief_cli, "run_headless",
                            lambda prompt, timeout: (0, "I could not read the files.", ""))
        assert brief_cli.cmd_build(self._args()) == 2

    def test_dry_run_prints_prompt_and_writes_nothing(self, env, monkeypatch, capsys):
        monkeypatch.setattr(brief_cli, "run_headless",
                            lambda prompt, timeout: pytest.fail("must not run"))
        assert brief_cli.cmd_build(self._args(dry_run=True)) == 0
        assert "would write" in capsys.readouterr().out
        assert not (env / "NVDA_2026-08-26.md").exists()

    def test_no_engine_report_is_a_setup_error(self, env, monkeypatch):
        monkeypatch.setattr(brief_cli, "latest_report", lambda t: None)
        assert brief_cli.cmd_build(self._args()) == 1
