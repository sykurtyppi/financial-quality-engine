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
