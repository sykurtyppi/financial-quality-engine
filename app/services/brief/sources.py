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
from datetime import date, timedelta
from pathlib import Path

from app.services.ingestion.edgar_documents import (
    _ex99_sort_key,
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
_LABEL_CHARS_RE = re.compile(r"[^\w.\-]+")
_NAME_CHARS_RE = re.compile(r"[^\w .,&'\-]+")
LABEL_MAX = 60


def _safe_label(s: str, limit: int = LABEL_MAX) -> str:
    """A filer-supplied string (exhibit filename, document type) reduced to a
    token that can sit in the headless prompt. Anything the prompt says is
    instruction-level text to the model; only the file CONTENTS are framed as
    data. EDGAR accepts almost any filename, so a filer's naming must not be
    able to write a sentence into the instructions."""
    return _LABEL_CHARS_RE.sub("_", s).strip("_")[:limit] or "unnamed"


def _safe_name(s: str, limit: int = LABEL_MAX) -> str:
    """Company name for the prompt's first line: letters, digits and the
    punctuation a registrant name carries; one line, bounded."""
    return " ".join(_NAME_CHARS_RE.sub(" ", s).split())[:limit]


def _narrative_ex99(client: SecClient, cik: int, accession: str) -> tuple[list, list]:
    """(candidates, skipped): the filing's EX-99 html exhibits, release first.

    Ordering reuses `_ex99_sort_key`'s semantics (release-like names first,
    tables/slides last, exhibit number only as the tie-break): a filer can
    ship the tables as EX-99.1 and the release as EX-99.2, so EDGAR's
    numbering alone is not trusted — the same lesson `_find_ex99` carries.
    Tables/slides-NAMED exhibits are skipped only when a better-named one
    exists; a filer whose sole exhibit is `q2-supplement.htm` still gets its
    release read (by word count), rather than losing it to a filename.
    """
    ex99 = sorted(
        (d for d in filing_documents(client, cik, accession, strict=True)
         if d.type.startswith("EX-99") and d.filename.lower().endswith((".htm", ".html"))),
        key=lambda d: (_ex99_sort_key(d.filename)[0], d.exhibit_no, d.sequence),
    )
    narrative = [d for d in ex99 if not _NON_NARRATIVE_RE.search(d.filename)]
    if narrative:
        return narrative, [d for d in ex99 if d not in narrative]
    return ex99, []


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


MIN_PRIOR_GAP_DAYS = 45  # a "prior quarter" release is at least this much older than the current
TRANSCRIPT_MAX_LAG_DAYS = 7  # a transcript more than a week after the print is another call's


def earnings_8ks(submissions: dict) -> list[Filing]:
    """Every original 8-K with Item 2.02, newest first. Amendments (8-K/A)
    are excluded: an amended exhibit days after the print would otherwise
    become "the newest earnings 8-K", moving the print's identity — and the
    brief's filename — mid-window, so the print-night brief and the 10-Q
    rebuild would land on two different files."""
    hits = [
        f for f in recent_filings(submissions)
        if f.form.upper().startswith("8-K") and "/A" not in f.form.upper()
        and "2.02" in (f.items or "")
    ]
    return sorted(hits, key=lambda f: (f.filing_date, f.accepted or "", f.accession), reverse=True)


def prior_earnings_8k(submissions: dict, current: Filing) -> Filing | None:
    """The 2.02 8-K a quarter before `current` — its Outlook section is the
    company's own prior guide for the quarter `current` reports. Must be at
    least MIN_PRIOR_GAP_DAYS older: a second 2.02 8-K from the same print
    (preliminary then final results) is not last quarter's guide."""
    cutoff = current.filing_date - timedelta(days=MIN_PRIOR_GAP_DAYS)
    older = [f for f in earnings_8ks(submissions) if f.filing_date <= cutoff]
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
    candidates, _ = _narrative_ex99(client, cik, filing.accession)
    for d in candidates:
        text = _clean(html_to_text(_fetch_archive(client, cik, filing.accession, d.filename)))
        if len(text.split()) >= MIN_EXHIBIT_WORDS:
            return f"{_safe_label(d.type)} {_safe_label(d.filename)}", text
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
    company = _safe_name(str(submissions.get("name") or ticker)) or ticker
    workdir = (out_root or BRIEFS) / ticker / filing.filing_date.isoformat()
    workdir.mkdir(parents=True, exist_ok=True)
    src = BriefSources(ticker=ticker, filing=filing, company=company, workdir=workdir)

    # A header fetch failure raises here (strict): "EDGAR unreachable" must
    # never read as "no exhibits" and produce a release-less brief.
    ex99, skipped = _narrative_ex99(client, cik, filing.accession)
    if ex99 and all(_NON_NARRATIVE_RE.search(d.filename) for d in ex99):
        src.diagnostics.append(
            f"8-K {filing.accession}: every EX-99 exhibit is tables/slides-named "
            f"({', '.join(_safe_label(d.filename) for d in ex99)}) — reading them by "
            "word count; check the release role is right")
    for d in skipped:
        # A tables-named EX-99.1 never becomes the release when a better-named
        # exhibit exists — whatever EDGAR's numbering says.
        src.diagnostics.append(
            f"{_safe_label(d.type)} {_safe_label(d.filename)}: tables/slides by name — skipped")
    release_done = False
    for d in ex99:
        text = _clean(html_to_text(_fetch_archive(client, cik, filing.accession, d.filename)))
        words = len(text.split())
        dtype, dname = _safe_label(d.type), _safe_label(d.filename)
        if words < MIN_EXHIBIT_WORDS:
            src.diagnostics.append(f"{dtype} {dname}: {words} words — skipped as non-narrative")
            continue
        role = "release" if not release_done else "exhibit"
        out = workdir / (f"release_{dtype.replace('.', '_')}.txt" if role == "release"
                         else f"exhibit_{dtype.replace('.', '_')}.txt")
        out.write_text(text)
        src.files.append(SourceFile(role, out, f"{dtype} {dname} ({words} words)"))
        release_done = True
    if not release_done:
        # The release is what the brief IS. Without it the run would still
        # produce a plausible page (report + audit + "UNAVAILABLE"), which
        # unattended is worse than no page: fail, keep the marker, retry.
        why = ("no typed EX-99 html exhibit in the filing header" if not ex99
               else "every EX-99 exhibit is under the narrative word floor")
        raise BriefSourceError(f"8-K {filing.accession}: {why} — no release to brief")

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
        src.files.append(SourceFile(
            "transcript", out, f"call transcript ({_safe_label(transcript.name)})"))
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
    for the print's date, else the EARLIEST file in that folder dated within
    TRANSCRIPT_MAX_LAG_DAYS after the print (a transcript is posted after the
    call, never before — and one dated a quarter later is the NEXT call's,
    which must never be summarized as this one)."""
    folder = (root or TRANSCRIPTS) / safe_ticker(ticker)
    if not folder.is_dir():
        return None
    exact = folder / f"{event_day.isoformat()}.txt"
    if exact.is_file():
        return exact
    latest_ok = (event_day + timedelta(days=TRANSCRIPT_MAX_LAG_DAYS)).isoformat()
    later = sorted(
        p for p in folder.glob("*.txt")
        if re.match(r"\d{4}-\d{2}-\d{2}", p.stem)
        and event_day.isoformat() <= p.stem[:10] <= latest_ok
    )
    return later[0] if later else None
