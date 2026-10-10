"""A cache entry must be the SHAPE its reader needs, not merely valid JSON.

`_cached_json` served any parseable JSON on a hit. A `[]` companyfacts entry
(an error page some proxy rendered as JSON, a hand-edited fixture, a partial
tool write) was served with no network call for a whole day, and the report
then died with AttributeError deep in `companyfacts_mapper._collect` — a
defect label for what is a bad cache entry. A freshly fetched payload was
checked for parseability only, so the same bad shape was written to disk to
poison the next day as well.

The archive cache had the same hole with no TTL behind it: a zero-byte or
whitespace-only document was a permanent hit, counted as "served from the
immutable archive cache", and an empty body from SEC was stored forever.

No network: every test fakes the client's transport.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.services.ingestion import sec_client as sc

CIK = 320193
FACTS = f"companyfacts_CIK{CIK:010d}.json"
SUBS = f"submissions_CIK{CIK:010d}.json"
PAGE = f"CIK{CIK:010d}-submissions-001.json"
GOOD_FACTS = {"cik": CIK, "facts": {"us-gaap": {}}}
GOOD_SUBS = {"cik": str(CIK), "filings": {"recent": {}, "files": []}}
GOOD_PAGE = {"accessionNumber": [], "filingDate": [], "form": []}
GOOD_TICKERS = {"0": {"ticker": "AAPL", "cik_str": CIK}}


def _client(tmp_path, bodies, *, fresh=False):
    c = sc.SecClient(fresh=fresh, cache_dir=tmp_path, identity="Test Suite test@example.com")
    c.requested = []
    queue = list(bodies)

    def fake_get(url):
        c.requested.append(url)
        if not queue:
            raise AssertionError(f"unexpected request {url}")
        return queue.pop(0)

    c._get = fake_get  # noqa: SLF001 - network seam
    return c


def _body(obj) -> bytes:
    return json.dumps(obj).encode()


READERS = {
    "companyfacts": (FACTS, lambda c: c.company_facts_by_cik(CIK), GOOD_FACTS),
    "submissions": (SUBS, lambda c: c.submissions_by_cik(CIK), GOOD_SUBS),
    "submissions page": (PAGE, lambda c: c.submissions_page(PAGE), GOOD_PAGE),
    "tickers": ("company_tickers.json", lambda c: c.resolve_cik("AAPL"), GOOD_TICKERS),
}

# A key that is ABSENT is not here: the readers define it (no `facts` is "no
# rows", no `filings` is "a new filer"). A key that is present with the wrong
# type, or a payload that is not an object at all, is.
BAD = {
    "companyfacts": [[], {"cik": CIK, "facts": []}, {"cik": CIK, "facts": None},
                     {"facts": "x"}, "facts", None],
    "submissions": [[], {"cik": CIK, "filings": []}, {"cik": CIK, "filings": None}, 7],
    "submissions page": [[], "page", None],
    "tickers": [[], {}, {"0": "AAPL"}, None],
}

CASES = [(reader, bad) for reader, bads in BAD.items() for bad in bads]


def _expected(reader, good):
    return CIK if reader == "tickers" else good


class TestCacheHitsAreShapeChecked:
    @pytest.mark.parametrize(("reader", "bad"), CASES)
    def test_a_wrong_shape_on_disk_is_refetched_once_and_replaced(self, tmp_path, reader, bad):
        """Defect: `[]` (or `{}`, or an object without the key the reader
        walks) was served from disk with no network call."""
        name, read, good = READERS[reader]
        (tmp_path / name).write_text(json.dumps(bad))
        c = _client(tmp_path, [_body(good)])
        assert read(c) == _expected(reader, good)
        assert len(c.requested) == 1
        assert json.loads((tmp_path / name).read_text()) == good
        # ...and the replacement is a real hit from then on.
        assert read(c) == _expected(reader, good)
        assert len(c.requested) == 1

    def test_the_discard_is_logged(self, tmp_path, caplog):
        (tmp_path / FACTS).write_text("[]")
        c = _client(tmp_path, [_body(GOOD_FACTS)])
        with caplog.at_level(logging.WARNING, logger=sc.__name__):
            c.company_facts_by_cik(CIK)
        assert any("discarding" in r.getMessage() and FACTS in r.getMessage()
                   for r in caplog.records)

    @pytest.mark.parametrize("reader", sorted(READERS))
    def test_a_good_shape_is_still_a_hit(self, tmp_path, reader):
        name, read, good = READERS[reader]
        (tmp_path / name).write_text(json.dumps(good))
        c = _client(tmp_path, [])
        assert read(c) == _expected(reader, good)
        assert c.requested == []

    def test_a_bad_entry_that_became_good_is_not_unlinked(self, tmp_path, monkeypatch):
        """Same race discipline as an unparseable entry: between the failed
        check and the unlink a concurrent writer may publish a good entry at
        the same path, and recovery must re-check (with the validator) before
        deleting it."""
        target = tmp_path / FACTS
        target.write_text("[]")
        real_lock = sc._publication_lock
        unlinked: list[str] = []
        real_unlink = Path.unlink

        def recording_unlink(self, *a, **kw):
            unlinked.append(str(self))
            return real_unlink(self, *a, **kw)

        @contextmanager
        def publish_then_lock(path):
            if path == target and not unlinked and target.read_text() == "[]":
                target.write_text(json.dumps(GOOD_FACTS))
            with real_lock(path):
                yield

        monkeypatch.setattr(sc, "_publication_lock", publish_then_lock)
        monkeypatch.setattr(Path, "unlink", recording_unlink)
        c = _client(tmp_path, [_body(GOOD_FACTS)])
        c.company_facts_by_cik(CIK)
        assert str(target) not in unlinked

    def test_the_readability_recheck_applies_the_validator(self, tmp_path):
        p = tmp_path / FACTS
        p.write_text("[]")
        assert sc._is_readable_json(p) is True  # parseable...
        assert sc._is_readable_json(p, sc._companyfacts_shape) is False  # ...but unusable
        p.write_text(json.dumps(GOOD_FACTS))
        assert sc._is_readable_json(p, sc._companyfacts_shape) is True


class TestFetchedPayloadsAreShapeChecked:
    @pytest.mark.parametrize(("reader", "bad"), CASES)
    def test_a_wrong_shape_from_sec_raises_and_is_never_written(self, tmp_path, reader, bad):
        """Defect: only parseability was checked, so the bad shape was cached
        and served for the next day too."""
        name, read, _ = READERS[reader]
        c = _client(tmp_path, [_body(bad)])
        with pytest.raises(sc.SecClientError, match="unusable") as e:
            read(c)
        assert "sec.gov" in str(e.value)  # names the URL that answered badly
        assert not (tmp_path / name).exists()
        assert not list(tmp_path.glob(f".{name}.*.tmp"))

    def test_a_wrong_shape_does_not_replace_a_good_entry(self, tmp_path):
        """--fresh with a bad answer must leave yesterday's good entry alone."""
        (tmp_path / FACTS).write_text(json.dumps(GOOD_FACTS))
        c = _client(tmp_path, [b"[]"], fresh=True)
        with pytest.raises(sc.SecClientError):
            c.company_facts_by_cik(CIK)
        assert json.loads((tmp_path / FACTS).read_text()) == GOOD_FACTS

    def test_an_unnamed_entry_is_not_shape_checked(self, tmp_path):
        """No validator, no opinion: callers that pass none keep the old
        parse-only contract."""
        c = _client(tmp_path, [b"[]"])
        assert c._cached_json("x.json", "https://example/x") == []


class TestAMissingKeyIsTheReadersToInterpret:
    """Defect (review of r23): the validators rejected a payload with no
    `facts` / `filings` key at all, although the readers define that case —
    `payloads.concept_rows` reads a missing `facts` as no rows, and
    `payloads.recent_filings` a missing `filings` as a new filer. A bare
    companyfacts payload then failed as "could not be ACQUIRED — retry, or
    check EDGAR_IDENTITY and the network" instead of "could not be mapped",
    and a filer with no filings read as "filing index unavailable"."""

    @pytest.mark.parametrize(("reader", "payload"), [
        ("companyfacts", {}),
        ("companyfacts", {"cik": CIK, "entityName": "X"}),
        ("submissions", {}),
        ("submissions", {"cik": str(CIK), "name": "X"}),
    ])
    def test_it_is_fetched_cached_and_served(self, tmp_path, reader, payload):
        name, read, _ = READERS[reader]
        c = _client(tmp_path, [_body(payload)])
        assert read(c) == payload
        assert json.loads((tmp_path / name).read_text()) == payload
        assert read(c) == payload  # a cache hit, not a refetch
        assert len(c.requested) == 1

    def test_the_wrong_type_is_named_as_such(self, tmp_path):
        c = _client(tmp_path, [_body({"cik": CIK, "facts": None})])
        with pytest.raises(sc.SecClientError, match="companyfacts.facts is NoneType"):
            c.company_facts_by_cik(CIK)

    def test_a_bare_companyfacts_payload_is_unmappable_not_unacquirable(
            self, tmp_path, monkeypatch, capsys):
        import importlib
        import sys

        root = Path(__file__).resolve().parents[2]
        monkeypatch.syspath_prepend(str(root / "scripts"))
        cli = importlib.import_module("generate_report")

        def fake_get(url):
            if "company_tickers" in url:
                return _body({"0": {"ticker": "XCO", "cik_str": 21344}})
            if "companyfacts" in url:
                return _body({"cik": 21344, "entityName": "X"})
            raise AssertionError(f"unexpected request {url}")

        def client(**kw):
            c = sc.SecClient(cache_dir=tmp_path / "cache",
                             identity="Test Suite test@example.com", **kw)
            c._get = fake_get  # noqa: SLF001 - network seam
            return c

        monkeypatch.setattr(cli, "SecClient", client)
        monkeypatch.setattr(cli, "ROOT", tmp_path)
        monkeypatch.setattr(sys, "argv", ["generate_report.py", "XCO", "--no-docs", "--no-vintage"])
        assert cli._main() == 2
        err = capsys.readouterr().err
        assert "could not be mapped" in err
        assert "could not be acquired" not in err

    def test_a_filer_without_filings_is_not_an_unavailable_index(self, tmp_path):
        from app.services.ingestion.edgar_adapter import fetch_submissions_snapshot

        def fake_get(url):
            if "company_tickers" in url:
                return _body(GOOD_TICKERS)
            return _body({"cik": str(CIK), "name": "New Filer"})

        c = sc.SecClient(cache_dir=tmp_path, identity="Test Suite test@example.com")
        c._get = fake_get  # noqa: SLF001 - network seam
        assert fetch_submissions_snapshot("AAPL", c) == {"cik": str(CIK), "name": "New Filer"}


# --- archive documents ------------------------------------------------------------

ACCN, DOC = "0000320193-26-000001", "aapl-10q.htm"
ARCHIVE = f"archive_{ACCN.replace('-', '')}_{DOC}"


class TestEmptyArchiveEntries:
    @pytest.mark.parametrize("stored", ["", "   \n\t  \n"])
    def test_an_empty_cached_document_is_a_miss(self, tmp_path, stored):
        """Defect: a zero-byte entry was a permanent hit (no TTL) — '' was
        returned, counted as served from cache, and never refetched."""
        (tmp_path / ARCHIVE).write_text(stored)
        c = _client(tmp_path, [b"<html>10-Q</html>"])
        assert c.archive_text(CIK, ACCN, DOC) == "<html>10-Q</html>"
        assert len(c.requested) == 1
        assert (c.archives_fetched, c.archives_from_cache) == (1, 0)
        assert (tmp_path / ARCHIVE).read_text() == "<html>10-Q</html>"

    def test_an_empty_entry_is_removed_even_if_the_refetch_fails(self, tmp_path):
        (tmp_path / ARCHIVE).write_text("")
        c = _client(tmp_path, [])
        with pytest.raises(AssertionError, match="unexpected request"):
            c.archive_text(CIK, ACCN, DOC)
        assert not (tmp_path / ARCHIVE).exists()

    def test_an_entry_that_filled_in_meanwhile_is_not_unlinked(self, tmp_path, monkeypatch):
        """The emptiness re-check runs under the entry's lock, so a document a
        concurrent fetch just published is kept, not deleted."""
        target = tmp_path / ARCHIVE
        target.write_text("")
        real_lock = sc._publication_lock

        @contextmanager
        def publish_then_lock(path):
            if path == target and target.read_text() == "":
                target.write_text("<html>published meanwhile</html>")
            with real_lock(path):
                yield

        monkeypatch.setattr(sc, "_publication_lock", publish_then_lock)
        c = _client(tmp_path, [b"<html>mine</html>"])
        c.archive_text(CIK, ACCN, DOC)
        assert target.exists()

    @pytest.mark.parametrize("body", [b"", b"  \r\n  "])
    def test_an_empty_body_raises_and_is_never_cached(self, tmp_path, body):
        """Defect: an empty answer was written to a cache with no TTL, so the
        document read as empty for good."""
        c = _client(tmp_path, [body])
        with pytest.raises(sc.SecClientError, match="empty"):
            c.archive_text(CIK, ACCN, DOC)
        assert not (tmp_path / ARCHIVE).exists()
        assert not list(tmp_path.glob(f".{ARCHIVE}.*.tmp"))
        assert (c.archives_fetched, c.archives_from_cache) == (0, 0)

    def test_a_real_document_is_still_a_permanent_hit(self, tmp_path):
        (tmp_path / ARCHIVE).write_text("<html>filed</html>")
        c = _client(tmp_path, [])
        assert c.archive_text(CIK, ACCN, DOC) == "<html>filed</html>"
        assert (c.archives_fetched, c.archives_from_cache) == (0, 1)
