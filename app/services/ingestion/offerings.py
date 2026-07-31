"""Offering-cadence reader: securities-offering activity from the EDGAR
filing index, with best-effort parsing of 424 takedown prospectuses.

Motivation (docs/accuracy_improvement_plan.md B1, measured in the 2026Q2
season review): share-count metrics are blind to sponsor sell-downs — FPS
scored Capital Integrity 10/100 while its sponsor sold four times in five
months. The filing index itself carries that fact; this module surfaces it.

Design constraints (repo conventions):
- Evidence/context ONLY. Nothing here feeds a score. The output is a dated
  timeline rendered as a report section.
- Parsing prospectus HTML is BEST-EFFORT regex over normalized text; misses
  return diagnostics, never fabricated values.
- All fetches go through SecClient (fair-access identity + rate limiting).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from app.services.ingestion.edgar_documents import html_to_text
from app.services.ingestion.sec_client import SecClient, SecClientError

logger = logging.getLogger(__name__)

ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{doc}"

# Registration statements: intent/capacity, not sales.
SHELF_FORMS = {"S-3", "S-3/A", "S-3ASR", "F-3", "F-3/A", "F-3ASR"}
REGISTRATION_FORMS = {"S-1", "S-1/A", "S-1MEF", "F-1", "F-1/A", "F-1MEF"}
# 424(b) prospectuses: actual takedowns (424B3 is frequently a resale
# prospectus for previously issued shares — labeled, not excluded).
TAKEDOWN_FORMS = {"424B1", "424B2", "424B3", "424B4", "424B5", "424B7", "424B8"}

_ALL_FORMS = SHELF_FORMS | REGISTRATION_FORMS | TAKEDOWN_FORMS

# --- best-effort prospectus patterns (validated on FPS/KTOS/SKHY 2026 424s) ---
_PRICE_PATTERNS = [
    re.compile(r"[Pp]ublic\s+[Oo]ffering\s+[Pp]rice[^$%]{0,40}\$\s*([\d,]+\.\d{2})"),
    re.compile(r"offering\s+price\s+of\s+\$\s*([\d,]+\.\d{2})\s+per\s+share", re.I),
    re.compile(r"price\s+to\s+(?:the\s+)?public[^$]{0,40}\$\s*([\d,]+\.\d{2})", re.I),
]
_PRIMARY_SHARES_PATTERNS = [
    re.compile(r"[Ww]e\s+are\s+offering\s+([\d,]{4,})\s+shares"),
    # "(ii) 14,555,925 shares of Class A common stock by us" (FPS-style covers)
    re.compile(r"([\d,]{4,})\s+shares[^.]{0,80}?\bby\s+us\b"),
]
# Bounded any-char gap: cover pages name the sellers ("by Forgent Parent I LP
# ... (together, the 'selling stockholders')") and contain periods ("Inc.").
_SECONDARY_SHARES_RE = re.compile(
    r"([\d,]{4,})\s+shares[\s\S]{0,200}?selling\s+(?:stock|share)holders", re.I
)
_SELLING_HOLDER_RE = re.compile(r"selling\s+(?:stock|share)holders?", re.I)
_NO_PROCEEDS_RE = re.compile(
    r"will\s+not\s+receive\s+any\s+(?:of\s+the\s+)?(?:net\s+)?proceeds", re.I
)


@dataclass
class OfferingFiling:
    form: str
    filing_date: date
    accession: str
    primary_doc: str
    kind: str  # "takedown" | "shelf" | "registration"
    # Parsed detail (takedowns only; None = not found, see diagnostics)
    price_per_share: float | None = None
    primary_shares: int | None = None
    secondary_shares: int | None = None
    has_selling_stockholders: bool = False
    company_receives_no_secondary_proceeds: bool = False
    excerpt: str = ""


@dataclass
class OfferingsTimeline:
    ticker: str
    cik: int
    lookback_months: int
    filings: list[OfferingFiling] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)

    @property
    def takedowns(self) -> list[OfferingFiling]:
        return [f for f in self.filings if f.kind == "takedown"]

    @property
    def takedown_count(self) -> int:
        return len(self.takedowns)

    def days_since_last_takedown(self, today: date | None = None) -> int | None:
        if not self.takedowns:
            return None
        latest = max(f.filing_date for f in self.takedowns)
        return ((today or date.today()) - latest).days


def _classify(form: str) -> str | None:
    if form in TAKEDOWN_FORMS:
        return "takedown"
    if form in SHELF_FORMS:
        return "shelf"
    if form in REGISTRATION_FORMS:
        return "registration"
    return None


def _to_int(s: str) -> int:
    return int(s.replace(",", ""))


def _parse_prospectus(text: str, filing: OfferingFiling, diagnostics: list[str]) -> None:
    """Populate takedown detail from normalized prospectus text. Best-effort:
    every miss is recorded in diagnostics rather than guessed."""
    head = text[:20000]  # cover-page facts live up front

    for pat in _PRICE_PATTERNS:
        m = pat.search(head)
        if m:
            value = float(m.group(1).replace(",", ""))
            # Cover tables put per-share and total in adjacent cells; a
            # per-share price above $10,000 is a mis-grab of the total.
            if value < 10_000:
                filing.price_per_share = value
                break
    if filing.price_per_share is None:
        diagnostics.append(f"{filing.form} {filing.filing_date}: price not found")

    for pat in _PRIMARY_SHARES_PATTERNS:
        m = pat.search(head)
        if m:
            filing.primary_shares = _to_int(m.group(1))
            break
    m = _SECONDARY_SHARES_RE.search(head)
    if m:
        filing.secondary_shares = _to_int(m.group(1))

    filing.has_selling_stockholders = bool(_SELLING_HOLDER_RE.search(head))
    filing.company_receives_no_secondary_proceeds = bool(_NO_PROCEEDS_RE.search(head))

    m = _NO_PROCEEDS_RE.search(head)
    anchor = m or _SELLING_HOLDER_RE.search(head)
    if anchor:
        start = max(0, anchor.start() - 120)
        filing.excerpt = head[start : anchor.end() + 200].strip()


def fetch_offerings(
    client: SecClient,
    ticker: str,
    lookback_months: int = 18,
    parse_takedowns: bool = True,
    max_parsed: int = 8,
) -> OfferingsTimeline:
    """Build the offering timeline for a ticker from the EDGAR filing index."""
    cik = client.resolve_cik(ticker)
    timeline = OfferingsTimeline(ticker=ticker.upper(), cik=cik, lookback_months=lookback_months)

    try:
        subs = client.submissions_by_cik(cik)
    except SecClientError as e:
        timeline.diagnostics.append(f"submissions fetch failed: {e}")
        return timeline

    recent = subs.get("filings", {}).get("recent", {})
    cutoff = date.today() - timedelta(days=lookback_months * 30)

    rows = zip(
        recent.get("form", []),
        recent.get("filingDate", []),
        recent.get("accessionNumber", []),
        recent.get("primaryDocument", []),
    )
    for form, fdate, accession, doc in rows:
        kind = _classify(form)
        if kind is None:
            continue
        filed = datetime.strptime(fdate, "%Y-%m-%d").date()
        if filed < cutoff:
            continue
        timeline.filings.append(
            OfferingFiling(
                form=form,
                filing_date=filed,
                accession=accession,
                primary_doc=doc,
                kind=kind,
            )
        )

    timeline.filings.sort(key=lambda f: f.filing_date, reverse=True)

    if parse_takedowns:
        for filing in timeline.takedowns[:max_parsed]:
            if not filing.primary_doc or not filing.primary_doc.endswith(".htm"):
                timeline.diagnostics.append(
                    f"{filing.form} {filing.filing_date}: no parseable primary doc"
                )
                continue
            url = ARCHIVES_URL.format(
                cik=cik,
                accession=filing.accession.replace("-", ""),
                doc=filing.primary_doc,
            )
            try:
                html = client._get(url).decode("utf-8", errors="replace")
            except SecClientError as e:
                timeline.diagnostics.append(f"{filing.form} {filing.filing_date}: fetch failed ({e})")
                continue
            _parse_prospectus(html_to_text(html), filing, timeline.diagnostics)

    return timeline


def render_offerings_section(timeline: OfferingsTimeline, current_price: float | None = None) -> str:
    """Markdown section for the report. Evidence framing only — no scoring."""
    lines: list[str] = []
    lines.append("## Capital Markets Activity (evidence — not scored)")
    lines.append("")
    lines.append(
        f"Offering-related filings, last {timeline.lookback_months} months "
        f"(EDGAR filing index; prospectus detail is best-effort — misses are listed, not guessed)."
    )
    lines.append("")

    if not timeline.filings:
        lines.append("- No offering-related filings found in the window.")
        return "\n".join(lines)

    lines.append("| Filed | Form | Kind | Price/sh | Primary sh | Secondary sh | Selling holders | Co. gets no 2ndary proceeds |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for f in timeline.filings:
        lines.append(
            f"| {f.filing_date} | {f.form} | {f.kind} "
            f"| {f'${f.price_per_share:,.2f}' if f.price_per_share else '—'} "
            f"| {f'{f.primary_shares:,}' if f.primary_shares else '—'} "
            f"| {f'{f.secondary_shares:,}' if f.secondary_shares else '—'} "
            f"| {'yes' if f.has_selling_stockholders else '—'} "
            f"| {'yes' if f.company_receives_no_secondary_proceeds else '—'} |"
        )
    lines.append("")

    n = timeline.takedown_count
    days = timeline.days_since_last_takedown()
    summary = f"- **{n} takedown(s)** in the window"
    if days is not None:
        summary += f"; last was {days} days ago"
    lines.append(summary + ".")

    priced = [f for f in timeline.takedowns if f.price_per_share]
    if priced and current_price:
        last = max(priced, key=lambda f: f.filing_date)
        delta = (current_price / last.price_per_share - 1.0) * 100.0
        lines.append(
            f"- Last priced takedown: **${last.price_per_share:,.2f}** ({last.filing_date}); "
            f"current price ${current_price:,.2f} is **{delta:+.1f}%** vs deal price."
        )

    secondary_only = [
        f for f in timeline.takedowns
        if f.has_selling_stockholders and f.company_receives_no_secondary_proceeds
    ]
    if secondary_only:
        lines.append(
            f"- {len(secondary_only)} takedown(s) include selling stockholders where the "
            f"company states it receives none of those proceeds — supply without balance-sheet benefit."
        )
    if timeline.diagnostics:
        lines.append("")
        lines.append("Parse diagnostics: " + "; ".join(timeline.diagnostics))
    return "\n".join(lines)
