"""Offline tests for EDGAR document parsing: HTML normalization and
best-effort section extraction, including the traps found on real filings
(TOC hits, Unicode apostrophes, in-prose item cross-references)."""

from app.schemas.financials import DocumentType
from app.services.ingestion.edgar_documents import extract_section, html_to_text


def make_filing(mdna_body: str, risk_body: str) -> str:
    """Synthetic 10-Q-shaped text with a TOC, cross-references, and real sections."""
    return f"""
    <html><body>
    <p>TABLE OF CONTENTS</p>
    <p>Item 1. Financial Statements 3</p>
    <p>Item 1A. Risk Factors 5</p>
    <p>Item 2. Management&#8217;s Discussion and Analysis of Financial Condition 12</p>
    <p>Item 1A. Risk Factors</p>
    <p>{risk_body}</p>
    <p>Item 2. Management&#8217;s Discussion and Analysis of Financial Condition and Results of Operations</p>
    <p>{mdna_body} See Part II, Item 1A of this Form 10-Q for more information. {mdna_body}</p>
    <p>Item 3. Quantitative and Qualitative Disclosures About Market Risk</p>
    <p>Not applicable.</p>
    </body></html>
    """


LONG = ("The company generated revenue growth across segments while operating "
        "expenses reflected continued investment in the platform. ") * 25  # ~400 words


class TestHtmlToText:
    def test_strips_tags_and_normalizes_unicode(self):
        text = html_to_text("<p>Management’s &#8220;discussion&#8221;</p>")
        assert "Management's" in text
        assert '"discussion"' in text
        assert "<p>" not in text

    def test_drops_scripts(self):
        assert "alert" not in html_to_text("<script>alert(1)</script><p>body</p>")


class TestExtractSection:
    def test_mdna_extracted_past_toc_and_cross_references(self):
        """The in-prose 'Part II, Item 1A' cross-reference must NOT terminate
        the section (the bug found live on Apple's 10-Q)."""
        text = html_to_text(make_filing(LONG, LONG))
        section = extract_section(text, DocumentType.MDNA)
        assert section is not None
        # Both halves around the cross-reference survive:
        assert section.count("revenue growth across segments") >= 40

    def test_risk_factors_extracted(self):
        text = html_to_text(make_filing(LONG, LONG))
        section = extract_section(text, DocumentType.RISK_FACTORS)
        assert section is not None
        assert "revenue growth" in section

    def test_toc_only_returns_none(self):
        toc_only = html_to_text(
            "<p>Item 1A. Risk Factors 5</p><p>Item 2. Management&#8217;s Discussion and Analysis 12</p>"
        )
        assert extract_section(toc_only, DocumentType.MDNA) is None
        assert extract_section(toc_only, DocumentType.RISK_FACTORS) is None

    def test_missing_section_returns_none_not_fabrication(self):
        text = html_to_text(f"<p>Item 1. Financial Statements</p><p>{LONG}</p>")
        assert extract_section(text, DocumentType.MDNA) is None


class TestP0EQuarterLabeling:
    """P0-E: 8-K labels must not snap to a stale quarter when companyfacts
    hasn't seen the just-ended quarter yet."""

    def test_calendar_quarter_end_dec_fye(self):
        from datetime import date

        from app.services.ingestion.edgar_documents import _latest_calendar_quarter_end

        # Aug 1 earnings event, Dec FYE: latest fiscal quarter end is Jun 30.
        assert _latest_calendar_quarter_end(12, date(2026, 8, 1)) == date(2026, 6, 30)
        # Event the day after a quarter end snaps to that end, not the prior one.
        assert _latest_calendar_quarter_end(12, date(2026, 7, 1)) == date(2026, 6, 30)
        # On the quarter-end day itself: strictly-before -> prior quarter.
        assert _latest_calendar_quarter_end(12, date(2026, 6, 30)) == date(2026, 3, 31)

    def test_calendar_quarter_end_june_fye(self):
        from datetime import date

        from app.services.ingestion.edgar_documents import _latest_calendar_quarter_end

        # June FYE (MSFT-style): quarters end Sep/Dec/Mar/Jun.
        assert _latest_calendar_quarter_end(6, date(2026, 8, 1)) == date(2026, 6, 30)
        assert _latest_calendar_quarter_end(6, date(2026, 11, 15)) == date(2026, 9, 30)


class TestP0DEx99Selection:
    """P0-D: prefer the release (EX-99.1) over tables/slides exhibits."""

    def test_prefers_991_over_992(self):
        from app.services.ingestion.edgar_documents import _ex99_sort_key

        names = ["abc-ex99_2.htm", "abc-ex99_1.htm"]
        assert min(names, key=_ex99_sort_key) == "abc-ex99_1.htm"

    def test_prefers_named_release_when_no_number(self):
        from app.services.ingestion.edgar_documents import _ex99_sort_key

        names = ["q2supplement.htm", "pressrelease.htm"]
        assert min(names, key=_ex99_sort_key) == "pressrelease.htm"


class TestReviewFindings:
    """Adversarial regressions from the PR #2 review."""

    def test_ex99_release_semantics_outrank_exhibit_number(self):
        """Finding 3: 'ex99_1-tables.htm' + 'ex99_2-earnings-release.htm'
        must select the release."""
        from app.services.ingestion.edgar_documents import _ex99_sort_key

        names = ["issuer-ex99_1-tables.htm", "issuer-ex99_2-earnings-release.htm"]
        assert min(names, key=_ex99_sort_key) == "issuer-ex99_2-earnings-release.htm"

    def test_ex99_tables_penalized_even_unnamed_release(self):
        from app.services.ingestion.edgar_documents import _ex99_sort_key

        names = ["a-ex99_1-supplemental-tables.htm", "a-ex99_2.htm"]
        assert min(names, key=_ex99_sort_key) == "a-ex99_2.htm"

    def test_early_reporter_8k_labels_new_quarter(self):
        """Finding 4: known ends only through Mar 31, earnings 8-K on Jul 5
        (96-day gap — under the removed 100-day threshold) must label the
        June quarter, not March."""
        from datetime import date

        from app.services.ingestion.edgar_documents import _latest_calendar_quarter_end

        cal = _latest_calendar_quarter_end(12, date(2026, 7, 5))
        assert cal == date(2026, 6, 30)
        snapped = date(2026, 3, 31)  # freshest quarter end companyfacts knows
        assert cal > snapped  # therefore the label logic overrides the snap


class _FakeClient:
    """Offline SecClient stand-in for fetch_documents path tests."""

    def __init__(self, tmp_path, subs, archives):
        self.cache_dir = tmp_path
        self._subs = subs
        self._archives = archives  # url substring -> bytes

    def submissions_by_cik(self, cik):
        return self._subs

    def _get(self, url):
        for key, payload in self._archives.items():
            if key in url:
                return payload
        raise AssertionError(f"unexpected fetch: {url}")


def _one_802_subs():
    return {
        "cik": "1234",
        "filings": {
            "recent": {
                "form": ["8-K"],
                "accessionNumber": ["0001-26-000001"],
                "primaryDocument": ["main8k.htm"],
                "reportDate": ["2026-07-05"],
                "items": ["2.02,9.01"],
                "filingDate": ["2026-07-05"],
            }
        },
    }


def _release_archives():
    import json

    index = json.dumps(
        {"directory": {"item": [{"name": "ex99_1.htm"}, {"name": "main8k.htm"}]}}
    ).encode()
    body = ("<html><body><p>"
            + "Revenue increased and margins expanded across all segments this quarter. " * 30
            + "</p></body></html>").encode()
    return {"index.json": index, "ex99_1.htm": body}


class TestNoFyeMonth:
    """PR #2 review blocker: fiscal_year_end_month can be None; the calendar
    fallback must not raise, the known-quarter snap must be preserved, and
    the unlabelable case must degrade with an explicit diagnostic."""

    def test_calendar_helper_handles_none_fye(self):
        from datetime import date

        from app.services.ingestion.edgar_documents import _latest_calendar_quarter_end

        assert _latest_calendar_quarter_end(None, date(2026, 7, 5)) is None

    def test_802_without_fye_or_quarter_ends_degrades_explicitly(self, tmp_path):
        """The reviewer's reproduction: empty facts_json -> fye None AND no
        known quarter ends. Previously TypeError swallowed by the broad
        except; now: no exception diagnostic, one explicit cannot-label
        diagnostic, release omitted knowingly."""
        from app.services.ingestion.edgar_documents import fetch_documents

        client = _FakeClient(tmp_path, _one_802_subs(), _release_archives())
        result = fetch_documents(client, "FAKE", facts_json={}, cik=1234)
        assert result.documents == []
        assert not any("failed" in d for d in result.diagnostics), result.diagnostics
        assert any("cannot assign a fiscal quarter" in d for d in result.diagnostics)

    def test_802_without_fye_but_with_known_ends_uses_snap(self, tmp_path):
        """fye None but companyfacts has quarter-end instants: the snap is
        preserved and the release ingests under the P<date> fallback label."""
        from app.services.ingestion.edgar_documents import fetch_documents

        facts = {
            "facts": {
                "us-gaap": {
                    "Assets": {
                        "units": {
                            "USD": [
                                {"end": "2026-03-31", "val": 1000.0, "filed": "2026-05-01", "form": "10-Q"},
                                {"end": "2026-06-30", "val": 1100.0, "filed": "2026-07-04", "form": "8-K"},
                            ]
                        }
                    }
                }
            }
        }
        client = _FakeClient(tmp_path, _one_802_subs(), _release_archives())
        result = fetch_documents(client, "FAKE", facts_json=facts, cik=1234)
        assert len(result.documents) == 1
        assert result.documents[0].doc_type.value == "earnings_release"
        assert result.documents[0].fiscal_label == "P2026-06-30"


def _subs_with_event(event_date: str):
    return {
        "cik": "1234",
        "filings": {
            "recent": {
                "form": ["8-K"],
                "accessionNumber": ["0001-26-000001"],
                "primaryDocument": ["main8k.htm"],
                "reportDate": [event_date],
                "items": ["2.02,9.01"],
                "filingDate": [event_date],
            }
        },
    }


def _facts_with_assets_instant(end: str):
    return {
        "facts": {
            "us-gaap": {
                "Assets": {
                    "units": {
                        "USD": [
                            {"end": end, "val": 1000.0, "filed": end, "form": "10-Q"},
                        ]
                    }
                }
            }
        }
    }


class TestStaleSnapWithoutFye:
    """Final review blocker: with no derivable FYE, a stale known quarter end
    must not be accepted as the just-reported period — a Jul 5 release over a
    Mar 31 snap would silently attach current narrative to prior-quarter
    fundamentals. Bound: EVENT_SNAP_MAX_LAG_DAYS = 91 (one standard 13-week
    quarter; beyond it a newer quarter end has necessarily passed)."""

    def _run(self, tmp_path, event_date, instant_end):
        from app.services.ingestion.edgar_documents import fetch_documents

        client = _FakeClient(tmp_path, _subs_with_event(event_date), _release_archives())
        return fetch_documents(
            client, "FAKE", facts_json=_facts_with_assets_instant(instant_end), cik=1234
        )

    def test_council_case_jul5_over_mar31_refused(self, tmp_path):
        """Event 2026-07-05, latest known instant 2026-03-31 (96-day lag):
        NOT labeled P2026-03-31; explicit cannot-assign diagnostic instead."""
        result = self._run(tmp_path, "2026-07-05", "2026-03-31")
        assert result.documents == []
        assert not any(d.fiscal_label == "P2026-03-31" for d in result.documents)
        assert any("cannot assign a fiscal quarter" in d for d in result.diagnostics)
        assert not any("failed" in d for d in result.diagnostics)

    def test_boundary_91_days_accepted(self, tmp_path):
        """Lag of exactly 91 days (2026-03-31 -> 2026-06-30): a deadline-day
        annual straggler is plausible; accepted at the boundary."""
        result = self._run(tmp_path, "2026-06-30", "2026-03-31")
        assert len(result.documents) == 1
        assert result.documents[0].fiscal_label == "P2026-03-31"

    def test_boundary_92_days_refused(self, tmp_path):
        """Lag of 92 days (2026-03-31 -> 2026-07-01): past the bound, refused."""
        result = self._run(tmp_path, "2026-07-01", "2026-03-31")
        assert result.documents == []
        assert any("cannot assign a fiscal quarter" in d for d in result.diagnostics)
