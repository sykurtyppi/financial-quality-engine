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
from app.services.ingestion.sec_client import SecClient, SecClientError

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


_FILING_ARRAYS = ("form", "accessionNumber", "primaryDocument", "reportDate", "items", "filingDate")
_HISTORY_LOOKBACK_YEARS = 4  # enough for >12 quarters of filings before a cutoff


def _merged_filings(client: SecClient, subs: dict, before: date | None) -> dict:
    """Return the filing arrays, merging older submission pages when a `before`
    cutoff reaches past the 'recent' block (high-volume filers keep only their
    latest ~1000 filings in 'recent'; older ones live in filings.files)."""
    recent = subs.get("filings", {}).get("recent", {})
    arrays: dict[str, list] = {k: list(recent.get(k, [])) for k in _FILING_ARRAYS}
    if before is None:
        return arrays
    fds = arrays["filingDate"]
    oldest = min(fds) if fds else None
    if oldest is not None and oldest <= before.isoformat():
        return arrays  # recent already reaches past the cutoff

    lower = date(before.year - _HISTORY_LOOKBACK_YEARS, before.month, min(before.day, 28)).isoformat()
    cutoff = before.isoformat()
    for page in subs.get("filings", {}).get("files", []):
        # Load a page only if it overlaps the [cutoff - 4y, cutoff] window.
        if page.get("filingFrom", "9999") <= cutoff and page.get("filingTo", "0") >= lower:
            try:
                pdata = client.submissions_page(page["name"])
            except SecClientError:
                continue
            # Older pages may lack 'items' / 'primaryDocument'; pad to length.
            n = len(pdata.get("form", []))
            for k in _FILING_ARRAYS:
                vals = pdata.get(k, [])
                if len(vals) < n:
                    vals = list(vals) + [""] * (n - len(vals))
                arrays[k].extend(vals)
    return arrays


def _fetch_archive(client: SecClient, cik: int, accession: str, doc: str) -> str:
    cache = client.cache_dir / f"archive_{accession.replace('-', '')}_{doc.replace('/', '_')}"
    if cache.exists():
        return cache.read_text(errors="replace")
    url = ARCHIVES_URL.format(cik=cik, accession=accession.replace("-", ""), doc=doc)
    text = client._get(url).decode("utf-8", errors="replace")  # noqa: SLF001
    cache.write_text(text)
    return text


# Maximum accepted distance between an 8-K event and the quarter end it is
# assumed to report, WHEN no fiscal calendar exists to cross-check (fye_month
# is None). 91 days = the length of a standard 13-week fiscal quarter: if the
# nearest known quarter end is further back than one full quarter, a newer
# quarter end has necessarily passed in between, so the known end cannot be
# the just-reported period. Boundary semantics: a lag of exactly 91 days is
# accepted (a deadline-day annual straggler is plausible); 92+ is refused in
# favor of the explicit cannot-assign diagnostic. When fye_month IS known,
# this bound is unnecessary — the calendar fallback always supplies a quarter
# end within the trailing quarter of the event.
EVENT_SNAP_MAX_LAG_DAYS = 91


def _latest_calendar_quarter_end(fye_month: int | None, before_d: date) -> date | None:
    """Latest fiscal-quarter-end month boundary strictly before `before_d`,
    from calendar arithmetic (fye_month and each 3 months earlier). Used only
    as the P0-E fallback when companyfacts hasn't seen the quarter yet; a
    52/53-week filer's true end differs by a few days but maps to the same
    fiscal label. `fye_month` may be None (no annual-duration facts to derive
    it from) — no calendar can be built, so no fallback (PR #2 review: this
    previously raised TypeError, and the broad per-filing except silently
    dropped the earnings release)."""
    import calendar as _cal

    if fye_month is None:
        return None
    q_months = {(fye_month - 1 - 3 * k) % 12 + 1 for k in range(4)}
    y, m = before_d.year, before_d.month
    for _ in range(13):
        if m in q_months:
            end = date(y, m, _cal.monthrange(y, m)[1])
            if end < before_d:
                return end
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return None


def _ex99_sort_key(name: str) -> tuple[int, int, str]:
    """Prefer the release itself over supplementary exhibits — P0-D:
    candidates[0] of an unordered index could silently ingest the tables
    exhibit and distort every narrative density for the period.

    Semantics outrank exhibit number (review finding on PR #2: a filer can
    ship 'ex99_1-tables.htm' + 'ex99_2-earnings-release.htm'): release-like
    names first, table/slide/supplement-like names last, exhibit number only
    as the tie-break among equally-named candidates."""
    lower = name.lower()
    if re.search(r"earnings|press|release", lower):
        semantic = 0
    elif re.search(r"table|supplement|slide|presentation|infographic|deck", lower):
        semantic = 2
    else:
        semantic = 1
    m = re.search(r"99[._-]?(\d)", lower)
    exhibit_no = int(m.group(1)) if m else 5
    return (semantic, exhibit_no, lower)


def _find_ex99(client: SecClient, cik: int, accession: str) -> list[str]:
    """EX-99 exhibit filenames in a filing index, best candidate first.
    Callers take [0] and should surface the alternatives diagnostically."""
    import json as _json

    idx_raw = _fetch_archive(client, cik, accession, "index.json")
    try:
        idx = _json.loads(idx_raw)
        items = idx.get("directory", {}).get("item", [])
    except (ValueError, AttributeError):
        return []
    candidates = [
        it["name"]
        for it in items
        if re.search(r"ex[-_]?99|99[._-]?1|earnings|press|release", it.get("name", ""), re.IGNORECASE)
        and it["name"].lower().endswith((".htm", ".html"))
        and "index" not in it["name"].lower()
    ]
    return sorted(candidates, key=_ex99_sort_key)


def fetch_documents(
    client: SecClient,
    ticker: str,
    facts_json: dict,
    n_filings: int = 8,
    include_earnings_releases: bool = True,
    cik: int | None = None,
    before: date | None = None,
) -> ExtractionResult:
    """Fetch the latest 10-K/10-Q MD&A + Risk Factors sections and 8-K
    (item 2.02) earnings releases, labeled with structural fiscal labels.

    `cik` bypasses ticker resolution (required for delisted companies absent
    from the current registry). `before` restricts to filings filed on or
    before that date — point-in-time document discipline, so a pre-event view
    cannot see filings that did not yet exist.
    """
    result = ExtractionResult()
    if cik is not None:
        subs = client.submissions_by_cik(cik)
    else:
        subs = _get_submissions(client, ticker)
        cik = int(subs["cik"]) if "cik" in subs else client.resolve_cik(ticker)
    fye_month = fiscal_year_end_month(facts_json)
    merged = _merged_filings(client, subs, before)
    forms = merged["form"]
    accessions = merged["accessionNumber"]
    primaries = merged["primaryDocument"]
    report_dates = merged["reportDate"]
    items_list = merged["items"]
    filing_dates = merged["filingDate"]
    cutoff = before.isoformat() if before else None

    known_quarter_ends = select_quarter_ends(facts_json, 40)

    def label_for(report_date: str, is_event_dated: bool = False) -> str | None:
        """10-K/10-Q reportDate IS the period end. 8-K reportDate is the EVENT
        date (weeks after the covered quarter): snap to the latest known
        quarter end strictly before it.

        P0-E: between the earnings 8-K and the subsequent 10-Q, the just-ended
        quarter's balance-sheet date is usually absent from companyfacts, so
        the known-quarter-end snap lands one quarter too early. Whenever the
        fiscal-calendar quarter end before the event is NEWER than the snap
        target, the snap target is stale and the calendar end wins — no
        staleness threshold, so early reporters (8-K a few days after quarter
        end) are labeled correctly too (review finding on PR #2). Both dates
        in the same fiscal quarter yield the same label, so the override is
        harmless when companyfacts is current."""
        try:
            d = datetime.strptime(report_date, "%Y-%m-%d").date()
        except ValueError:
            return None
        if is_event_dated:
            prior_ends = [q for q in known_quarter_ends if q < d]
            snapped = prior_ends[-1] if prior_ends else None
            cal = _latest_calendar_quarter_end(fye_month, d)
            if cal is not None and (snapped is None or cal > snapped):
                snapped = cal
            if (
                snapped is not None
                and fye_month is None
                and (d - snapped).days > EVENT_SNAP_MAX_LAG_DAYS
            ):
                # Final review blocker: with no fiscal calendar to fill the
                # gap, a stale known quarter end must not be accepted as the
                # just-reported period — a Jul 5 release over a Mar 31 snap
                # would silently attach current narrative to prior-quarter
                # fundamentals. Prefer explicit omission.
                return None
            if snapped is None:
                return None
            d = snapped
        return _fiscal_label(d, fye_month)

    statements_done = 0
    releases_done = 0
    for i in range(len(forms)):
        if statements_done >= n_filings and (not include_earnings_releases or releases_done >= n_filings):
            break
        if cutoff is not None and i < len(filing_dates) and filing_dates[i] > cutoff:
            continue  # point-in-time: this filing did not exist yet at `before`
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
                    result.diagnostics.append(
                        f"8-K {accessions[i]}: earnings release NOT ingested — cannot "
                        "assign a fiscal quarter (no derivable fiscal year-end month, and "
                        "no known quarter end recent enough to be the just-reported period)"
                    )
                    continue
                ex99_candidates = _find_ex99(client, cik, accessions[i])
                if not ex99_candidates:
                    result.diagnostics.append(f"8-K {accessions[i]} ({label}): no EX-99 exhibit found")
                    continue
                ex99 = ex99_candidates[0]
                if len(ex99_candidates) > 1:
                    result.diagnostics.append(
                        f"8-K {accessions[i]} ({label}): chose {ex99} among "
                        f"{len(ex99_candidates)} EX-99 candidates "
                        f"({', '.join(ex99_candidates[1:])})"
                    )
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
