"""The vintage CLI: the command an operator reaches for, usually because
something already went wrong. Nothing here was covered before."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from app.services.ingestion import vintages as v

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("vintage_cli", ROOT / "scripts" / "vintage.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


def _facts(val, filed="2026-08-01"):
    return {"facts": {"us-gaap": {"Assets": {"units": {"USD": [
        {"end": "2026-06-30", "filed": filed, "val": val, "form": "10-Q", "accn": "a"}]}}}}}


class _Client:
    def __init__(self, *payloads):
        self._p = list(payloads) or [_facts(1000.0)]
        self.n = 0

    def resolve_cik(self, ticker):
        if ticker == "NOPE":
            raise cli.SecClientError("Ticker not found in SEC registry: NOPE")
        return 1045810

    def company_facts_by_cik(self, cik):
        self.n += 1
        return self._p[min(self.n - 1, len(self._p) - 1)]


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Never the real archive, never the network."""
    monkeypatch.setattr(v, "VINTAGES", tmp_path / "vintages")
    monkeypatch.setattr(cli, "SecClient", lambda *a, **k: _Client())
    monkeypatch.setattr(cli, "PORTFOLIO", tmp_path / "portfolio.txt")
    return tmp_path


def _two_vintages(monkeypatch):
    """One client instance across calls, so the second capture sees the
    second document rather than restarting the sequence."""
    client = _Client(_facts(1000.0), _facts(1200.0, "2026-11-01"))
    monkeypatch.setattr(cli, "SecClient", lambda *a, **k: client)
    _run(["capture", "NVDA"], monkeypatch)
    _run(["capture", "NVDA", "--force"], monkeypatch)
    assert len(v.list_vintages(1045810)) == 2


def _run(argv, monkeypatch) -> int:
    monkeypatch.setattr(sys, "argv", ["vintage.py", *argv])
    return cli.main()


class TestCapture:
    def test_captures_named_tickers(self, monkeypatch, capsys):
        assert _run(["capture", "nvda"], monkeypatch) == 0
        assert "captured" in capsys.readouterr().out
        assert len(v.list_vintages(1045810)) == 1

    def test_portfolio_flag_reads_the_holdings_file(self, monkeypatch, isolated, capsys):
        (isolated / "portfolio.txt").write_text("# holdings\nNVDA\n\nnvda  # duplicate\n")
        assert _run(["capture", "--portfolio"], monkeypatch) == 0
        assert capsys.readouterr().out.count("NVDA:") == 1

    def test_a_missing_portfolio_file_is_a_clear_error(self, monkeypatch, isolated, capsys):
        assert _run(["capture", "--portfolio", str(isolated / "nope.txt")], monkeypatch) == 1
        assert "not found" in capsys.readouterr().err

    def test_no_tickers_at_all_says_so(self, monkeypatch, capsys):
        assert _run(["capture"], monkeypatch) == 1
        assert "no tickers" in capsys.readouterr().err

    def test_one_bad_name_does_not_end_the_batch(self, monkeypatch, capsys):
        assert _run(["capture", "NOPE", "NVDA"], monkeypatch) == 1
        out = capsys.readouterr()
        assert "NOPE" in out.err and "NVDA: captured" in out.out

    def test_a_filesystem_failure_does_not_end_the_batch(self, monkeypatch, capsys):
        # The CLI is what an operator runs to catch up after a problem; it
        # must not die partway through on the first unwritable directory.
        def boom(*a, **k):
            raise OSError("read-only file system")
        monkeypatch.setattr(cli, "capture", boom)
        assert _run(["capture", "NVDA", "AAPL"], monkeypatch) == 1
        assert capsys.readouterr().err.count("OSError") == 2


class TestListAndDiff:
    def test_list_reports_an_empty_store_without_failing(self, monkeypatch, capsys):
        assert _run(["list", "NVDA"], monkeypatch) == 0
        assert "no snapshots yet" in capsys.readouterr().out

    def test_diff_needs_two_snapshots(self, monkeypatch, capsys):
        _run(["capture", "NVDA"], monkeypatch)
        assert _run(["diff", "NVDA"], monkeypatch) == 2
        assert "needs two" in capsys.readouterr().err

    def test_diff_of_the_newest_two(self, monkeypatch, capsys):
        _two_vintages(monkeypatch)
        assert _run(["diff", "NVDA"], monkeypatch) == 0
        out = capsys.readouterr().out
        assert "total_assets" in out and "20.0%" in out and "Context, not an alarm" in out

    def test_diff_uses_capture_order_when_same_day_hash_order_is_opposite(
            self, monkeypatch, capsys):
        client = _Client(_facts(1200.0), _facts(1000.0, "2026-11-01"))
        monkeypatch.setattr(cli, "SecClient", lambda *a, **k: client)
        _run(["capture", "NVDA"], monkeypatch)
        _run(["capture", "NVDA", "--force"], monkeypatch)
        assert _run(["diff", "NVDA"], monkeypatch) == 0
        out = capsys.readouterr().out
        assert "1,200" in out and "1,000" in out and "16.7%" in out

    def test_from_and_to_match_a_content_addressed_name(self, monkeypatch, capsys):
        # The filename is `<date>-<sha12>.json.gz`; a date lookup that split
        # on "." would never match one.
        _two_vintages(monkeypatch)
        day = cli.snapshot_day(v.list_vintages(1045810)[0])
        assert _run(["diff", "NVDA", "--from", day, "--to", day], monkeypatch) == 0
        assert "No prior-period figure changed" in capsys.readouterr().out

    def test_an_unknown_day_names_what_is_available(self, monkeypatch, capsys):
        _two_vintages(monkeypatch)
        assert _run(["diff", "NVDA", "--from", "1999-01-01"], monkeypatch) == 2
        assert "have" in capsys.readouterr().err

    def test_a_bad_since_date_is_an_error_not_a_traceback(self, monkeypatch, capsys):
        _two_vintages(monkeypatch)
        assert _run(["diff", "NVDA", "--since", "not-a-date"], monkeypatch) == 1

    def test_an_unreadable_snapshot_is_reported_and_kept(self, monkeypatch, capsys):
        _two_vintages(monkeypatch)
        victim = v.list_vintages(1045810)[0]
        victim.write_bytes(b"\x1f\x8b\x08truncated")
        assert _run(["diff", "NVDA"], monkeypatch) == 1
        assert "never delete it" in capsys.readouterr().err
        assert victim.exists()
