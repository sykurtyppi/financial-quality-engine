"""EDGAR document ingestion: MD&A and Risk Factors from 10-K/10-Q filings,
and earnings releases from 8-K EX-99 exhibits.

Design constraints (stated, not hidden):
- Filing HTML varies enormously; section extraction is BEST-EFFORT regex over
  normalized text. Failures return diagnostics, never fabricated sections.
- Earnings-call transcripts have no free, redistribution-safe source; supply
  them via the canonical JSON path (DocumentType.TRANSCRIPT).
- All fetches go through SecClient (fair-access identity + rate limiting).

Fiscal labels are assigned structurally from the filing's report date using
the same fiscal-year-end logic as the fundamentals mapper, so documents line
up with mapped periods.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from app.schemas.financials import DocumentRecord, DocumentType
from app.services.ingestion.companyfacts_mapper import (
    _fiscal_label,
    fiscal_year_end_month,
    select_quarter_ends,
)
from app.services.ingestion.sec_client import SecClient

logger = logging.getLogger(__name__)

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{doc}"

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_ENTITY_MAP = {"&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">", "&#8217;": "'", "&#8220;": '"', "&#8221;": '"', "&rsquo;": "'", "&ldquo;": '"', "&rdquo;": '"'}


_UNICODE_MAP = {"’": "'", "‘": "'", "“": '"', "”": '"', " ": " ", "–": "-", "—": "-"}


def html_to_text(html: str) -> str:
    text = _SCRIPT_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", text)
    for ent, rep in _ENTITY_MAP.items():
        text = text.replace(ent, rep)
    # Filings use Unicode punctuation ("Management’s") — normalize so section
    # heading patterns with ASCII apostrophes match.
    for ch, rep in _UNICODE_MAP.items():
        text = text.replace(ch, rep)
    text = re.sub(r"&#?\w+;", " ", text)
    return re.sub(r"[ \t\r\f\v]+", " ", text)


# Section heading patterns. Items are matched on normalized text; the section
# runs to the next item heading.
_ITEM_HEADING = r"item\s+{num}\.?\s*[:\-–—.]?\s*"
SECTION_SPECS = {
    DocumentType.MDNA: (
        ("7", "management's discussion and analysis"),   # 10-K
        ("2", "management's discussion and analysis"),   # 10-Q
    ),
    DocumentType.RISK_FACTORS: (
        ("1a", "risk factors"),
    ),
}
# Section boundary: an item heading WITH its known title. A bare "Item 1A"
# also appears as a cross-reference inside prose ("see Part II, Item 1A of
# this Form 10-Q") and must not terminate the section.
_NEXT_ITEM_RE = re.compile(
    r"\bitem\s+\d{1,2}[a-c]?\.?\s*[:\-.]?\s*"
    r"(quantitative and qualitative|controls and procedures|legal proceedings|"
    r"financial statements|unresolved staff|properties|unregistered sales|"
    r"defaults upon|mine safety|other information|exhibits|"
    r"market for (the )?registrant|selected financial|cybersecurity)",
    re.IGNORECASE,
)
_MIN_SECTION_WORDS = 200  # anything shorter is a TOC hit, not the section


@dataclass
class ExtractionResult:
    documents: list[DocumentRecord] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)


def extract_section(text: str, doc_type: DocumentType) -> str | None:
    """Best-effort extraction: find the LAST occurrence of the item heading
    (first occurrences are usually the table of contents), then cut at the
    next item heading."""
    low = text.lower()
    for item_num, title in SECTION_SPECS[doc_type]:
        pattern = re.compile(_ITEM_HEADING.format(num=re.escape(item_num)) + re.escape(title[:20]), re.IGNORECASE)
        matches = list(pattern.finditer(low))
        if not matches:
            continue
        for m in reversed(matches):
            start = m.end()
            nxt = _NEXT_ITEM_RE.search(low, pos=start + 50)
            end = nxt.start() if nxt else len(text)
            section = text[start:end].strip()
            if len(section.split()) >= _MIN_SECTION_WORDS:
                return section
    return None


def _get_submissions(client: SecClient, ticker: str) -> dict:
    cik = client.resolve_cik(ticker)
    return client._cached_json(  # noqa: SLF001
        f"submissions_{ticker.upper()}.json", SUBMISSIONS_URL.format(cik=cik)
    )


def _fetch_archive(client: SecClient, cik: int, accession: str, doc: str) -> str:
    url = ARCHIVES_URL.format(cik=cik, accession=accession.replace("-", ""), doc=doc)
    return client._get(url).decode("utf-8", errors="replace")  # noqa: SLF001


def _find_ex99(client: SecClient, cik: int, accession: str) -> str | None:
    """Locate the EX-99 exhibit filename (earnings release) in a filing index."""
    import json as _json

    idx_raw = _fetch_archive(client, cik, accession, "index.json")
    try:
        idx = _json.loads(idx_raw)
        items = idx.get("directory", {}).get("item", [])
    except (ValueError, AttributeError):
        return None
    candidates = [
        it["name"]
        for it in items
        if re.search(r"ex[-_]?99|99[._-]?1|earnings|press|release", it.get("name", ""), re.IGNORECASE)
        and it["name"].lower().endswith((".htm", ".html"))
        and "index" not in it["name"].lower()
    ]
    return candidates[0] if candidates else None


def fetch_documents(
    client: SecClient,
    ticker: str,
    facts_json: dict,
    n_filings: int = 8,
    include_earnings_releases: bool = True,
) -> ExtractionResult:
    """Fetch the latest 10-K/10-Q MD&A + Risk Factors sections and 8-K
    (item 2.02) earnings releases, labeled with structural fiscal labels."""
    result = ExtractionResult()
    subs = _get_submissions(client, ticker)
    cik = int(subs["cik"]) if "cik" in subs else client.resolve_cik(ticker)
    fye_month = fiscal_year_end_month(facts_json)
    recent = subs.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    primaries = recent.get("primaryDocument", [])
    report_dates = recent.get("reportDate", [])
    items_list = recent.get("items", [])

    known_quarter_ends = select_quarter_ends(facts_json, 40)

    def label_for(report_date: str, is_event_dated: bool = False) -> str | None:
        """10-K/10-Q reportDate IS the period end. 8-K reportDate is the EVENT
        date (weeks after the covered quarter): snap to the latest known
        quarter end strictly before it."""
        try:
            d = datetime.strptime(report_date, "%Y-%m-%d").date()
        except ValueError:
            return None
        if is_event_dated:
            prior_ends = [q for q in known_quarter_ends if q < d]
            if not prior_ends:
                return None
            d = prior_ends[-1]
        return _fiscal_label(d, fye_month)

    statements_done = 0
    releases_done = 0
    for i in range(len(forms)):
        if statements_done >= n_filings and (not include_earnings_releases or releases_done >= n_filings):
            break
        form = forms[i]
        try:
            if form in ("10-K", "10-Q") and statements_done < n_filings:
                label = label_for(report_dates[i])
                if label is None:
                    result.diagnostics.append(f"{form} {accessions[i]}: unparseable report date")
                    continue
                text = html_to_text(_fetch_archive(client, cik, accessions[i], primaries[i]))
                found_any = False
                for doc_type in (DocumentType.MDNA, DocumentType.RISK_FACTORS):
                    section = extract_section(text, doc_type)
                    if section:
                        found_any = True
                        result.documents.append(
                            DocumentRecord(
                                fiscal_label=label,
                                doc_type=doc_type,
                                text=section,
                                source=f"{form} {accessions[i]}",
                            )
                        )
                    else:
                        result.diagnostics.append(
                            f"{form} {accessions[i]} ({label}): {doc_type.value} section not found"
                        )
                if found_any:
                    statements_done += 1
            elif (
                include_earnings_releases
                and form.startswith("8-K")
                and "2.02" in (items_list[i] or "")
                and releases_done < n_filings
            ):
                label = label_for(report_dates[i], is_event_dated=True)
                if label is None:
                    continue
                ex99 = _find_ex99(client, cik, accessions[i])
                if ex99 is None:
                    result.diagnostics.append(f"8-K {accessions[i]} ({label}): no EX-99 exhibit found")
                    continue
                text = html_to_text(_fetch_archive(client, cik, accessions[i], ex99)).strip()
                if len(text.split()) < 100:
                    result.diagnostics.append(f"8-K {accessions[i]} ({label}): exhibit too short")
                    continue
                result.documents.append(
                    DocumentRecord(
                        fiscal_label=label,
                        doc_type=DocumentType.EARNINGS_RELEASE,
                        text=text,
                        source=f"8-K {accessions[i]} {ex99}",
                    )
                )
                releases_done += 1
        except Exception as e:  # noqa: BLE001 - one bad filing must not kill ingestion
            result.diagnostics.append(f"{form} {accessions[i]}: fetch/extract failed: {e}")

    # Chronological order (oldest first) as the narrative layer expects.
    result.documents.sort(key=lambda d: d.fiscal_label)
    return result
