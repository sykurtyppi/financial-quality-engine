"""Primary-source collection for an earnings brief.

What goes into a brief, and where each piece comes from:

- the earnings release — the 8-K Item 2.02 EX-99.1, from EDGAR, by exhibit
  TYPE (never by filename);
- any further narrative EX-99 exhibits the filer attached (CFO commentary,
  prepared remarks; NVDA files these as EX-99.2) — tables/slides excluded by
  name, short exhibits by length;
- the previous quarter's release, for the one comparison most often skipped:
  actual vs the company's OWN prior guide (its outlook section);
- the earnings-call transcript — NOT on EDGAR. Supplied by the operator as a
  text file (any source); absent, the call section of the brief is marked
  UNAVAILABLE rather than reconstructed from the release;
- the engine's own report and, when the headless audit ran, its audit.

Everything fetched is written to disk next to the brief so the brief's claims
can be checked against the exact text it was given.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from app.services.ingestion.edgar_documents import (
    _fetch_archive,
    filing_documents,
    html_to_text,
)
from app.services.ingestion.sec_client import SecClient
from app.services.journal.store import safe_ticker
from app.services.watch.poller import Filing, recent_filings

ROOT = Path(__file__).resolve().parents[3]
BRIEFS = ROOT / "reports" / "briefs"
TRANSCRIPTS = ROOT / "journal" / "transcripts"

MIN_EXHIBIT_WORDS = 100
_NON_NARRATIVE_RE = re.compile(r"table|supplement|slide|presentation|infographic|deck", re.I)


class BriefSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceFile:
    role: str  # release | exhibit | prior_release | transcript | report | audit | prior_brief
    path: Path
    label: str


@dataclass
class BriefSources:
    ticker: str
    filing: Filing
    company: str
    workdir: Path
    files: list[SourceFile] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)

    @property
    def has_transcript(self) -> bool:
        return any(f.role == "transcript" for f in self.files)

    @property
    def event_day(self) -> str:
        return self.filing.filing_date.isoformat()


def earnings_8ks(submissions: dict) -> list[Filing]:
    """Every 8-K with Item 2.02, newest first."""
    hits = [
        f for f in recent_filings(submissions)
        if f.form.upper().startswith("8-K") and "2.02" in (f.items or "")
    ]
    return sorted(hits, key=lambda f: (f.filing_date, f.accepted or "", f.accession), reverse=True)


def prior_earnings_8k(submissions: dict, current: Filing) -> Filing | None:
    """The 2.02 8-K before `current` — its Outlook section is the company's
    own prior guide for the quarter `current` reports."""
    older = [f for f in earnings_8ks(submissions)
             if (f.filing_date, f.accepted or "", f.accession)
             < (current.filing_date, current.accepted or "", current.accession)]
    return older[0] if older else None


def latest_earnings_8k(submissions: dict, accession: str | None = None) -> Filing:
    """The newest 8-K with Item 2.02, or the one named by `accession`."""
    hits = earnings_8ks(submissions)
    if accession:
        for f in hits:
            if f.accession == accession:
                return f
        raise BriefSourceError(f"{accession} is not an Item 2.02 8-K in the filing history")
    if not hits:
        raise BriefSourceError("no 8-K Item 2.02 (earnings release) in the filing history")
    return hits[0]


def _release_text(client: SecClient, cik: int, filing: Filing) -> tuple[str, str] | None:
    """(label, text) of a filing's EX-99.1-ranked release, or None."""
    ex99 = [
        d for d in filing_documents(client, cik, filing.accession)
        if d.type.startswith("EX-99") and d.filename.lower().endswith((".htm", ".html"))
    ]
    for d in sorted(ex99, key=lambda d: (d.exhibit_no, d.sequence)):
        text = _clean(html_to_text(_fetch_archive(client, cik, filing.accession, d.filename)))
        if len(text.split()) >= MIN_EXHIBIT_WORDS:
            return f"{d.type} {d.filename}", text
    return None


def _clean(text: str) -> str:
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def collect_sources(
    client: SecClient,
    ticker: str,
    *,
    accession: str | None = None,
    transcript: Path | None = None,
    transcript_root: Path | None = None,
    report: Path | None = None,
    audit: Path | None = None,
    prior_brief: Path | None = None,
    out_root: Path | None = None,
) -> BriefSources:
    ticker = safe_ticker(ticker)
    cik = client.resolve_cik(ticker)
    submissions = client.submissions_by_cik(cik)
    filing = latest_earnings_8k(submissions, accession)
    company = str(submissions.get("name") or ticker)
    workdir = (out_root or BRIEFS) / ticker / filing.filing_date.isoformat()
    workdir.mkdir(parents=True, exist_ok=True)
    src = BriefSources(ticker=ticker, filing=filing, company=company, workdir=workdir)

    ex99 = [
        d for d in filing_documents(client, cik, filing.accession)
        if d.type.startswith("EX-99") and d.filename.lower().endswith((".htm", ".html"))
    ]
    if not ex99:
        src.diagnostics.append(
            f"8-K {filing.accession}: no typed EX-99 exhibits found in the filing header"
        )
    release_done = False
    for d in sorted(ex99, key=lambda d: (d.exhibit_no, d.sequence)):
        text = _clean(html_to_text(_fetch_archive(client, cik, filing.accession, d.filename)))
        words = len(text.split())
        if words < MIN_EXHIBIT_WORDS:
            src.diagnostics.append(f"{d.type} {d.filename}: {words} words — skipped as non-narrative")
            continue
        if release_done and _NON_NARRATIVE_RE.search(d.filename):
            src.diagnostics.append(f"{d.type} {d.filename}: tables/slides by name — skipped")
            continue
        role = "release" if not release_done else "exhibit"
        out = workdir / (f"release_{d.type.replace('.', '_')}.txt" if role == "release"
                         else f"exhibit_{d.type.replace('.', '_')}.txt")
        out.write_text(text)
        src.files.append(SourceFile(role, out, f"{d.type} {d.filename} ({words} words)"))
        release_done = True

    prior = prior_earnings_8k(submissions, filing)
    if prior is not None:
        try:
            got = _release_text(client, cik, prior)
        except Exception as e:  # noqa: BLE001 — the prior guide is a bonus, not a requirement
            got, err = None, f"{type(e).__name__}: {e}"
        else:
            err = "no narrative EX-99 exhibit"
        if got is None:
            src.diagnostics.append(
                f"prior release 8-K {prior.accession} ({prior.filing_date}): {err} — "
                "the company's own prior guide is unavailable")
        else:
            label, text = got
            out = workdir / "prior_release.txt"
            out.write_text(text)
            src.files.append(SourceFile(
                "prior_release", out,
                f"PRIOR quarter's release, 8-K {prior.accession} filed {prior.filing_date} — "
                f"{label}; use ONLY its outlook/guidance as this quarter's prior guide"))
    else:
        src.diagnostics.append("no earlier 2.02 8-K in the filing history — prior guide unavailable")

    if transcript is None:
        # Operator drop folder, keyed by the print date the 8-K establishes.
        transcript = find_transcript(ticker, filing.filing_date, transcript_root)
    if transcript is not None:
        if not transcript.is_file():
            raise BriefSourceError(f"transcript not found: {transcript}")
        out = workdir / "transcript.txt"
        out.write_text(transcript.read_text(errors="replace"))
        src.files.append(SourceFile("transcript", out, f"call transcript ({transcript.name})"))
    else:
        src.diagnostics.append(
            "no call transcript supplied — call section will be UNAVAILABLE "
            f"(drop one at {TRANSCRIPTS / ticker}/ or pass --transcript and re-run)"
        )

    for role, p in (("report", report), ("audit", audit), ("prior_brief", prior_brief)):
        if p is not None and p.is_file():
            src.files.append(SourceFile(role, p, p.name))
    return src


def find_transcript(ticker: str, event_day: date, root: Path | None = None) -> Path | None:
    """Operator-dropped transcript: journal/transcripts/<TICKER>/<YYYY-MM-DD>.txt
    for the print's date, else the newest file in that folder dated on/after
    the print (a transcript is posted after the call, never before)."""
    folder = (root or TRANSCRIPTS) / safe_ticker(ticker)
    if not folder.is_dir():
        return None
    exact = folder / f"{event_day.isoformat()}.txt"
    if exact.is_file():
        return exact
    later = sorted(
        p for p in folder.glob("*.txt")
        if re.match(r"\d{4}-\d{2}-\d{2}", p.stem) and p.stem[:10] >= event_day.isoformat()
    )
    return later[0] if later else None
