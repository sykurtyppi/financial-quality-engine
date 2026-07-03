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
