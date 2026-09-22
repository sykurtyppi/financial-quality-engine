"""Narrative evidence names the filing it came from (PR 1.9).

Every ledger row used to claim `source="period documents"`, although each
DocumentRecord already carried "{form} {accession}[ {exhibit}]". A reader —
or the API's evidence JSON — could not tell which filing a quoted excerpt was
cut from. The ledger now locates each verbatim window in the documents of its
own period and names them; rows that are computed statements rather than
quotations name the period's documents they were derived from; anything it
cannot pin down says why instead of claiming a filing.
"""

from __future__ import annotations

from datetime import date

from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.pipeline import analyze
from app.schemas.financials import DocumentRecord, DocumentType
from app.services.narrative.evidence import (
    NO_SOURCE_RECORDED,
    NOT_LOCATED,
    EvidenceLedger,
    _fragments,
)
from app.services.narrative.narrative_metrics import compute_narrative_layer
from tests.fixtures.companies import stretch_dataset

MDNA, RISK, EX99 = DocumentType.MDNA, DocumentType.RISK_FACTORS, DocumentType.EARNINGS_RELEASE


def _doc(label, doc_type, text, source):
    return DocumentRecord(fiscal_label=label, doc_type=doc_type, text=text, source=source)


FILLER = "The company operates in several segments and reports results quarterly. " * 6


def _period_docs(label, mdna="", risk="", release="", tag=None):
    tag = tag or label
    return [
        _doc(label, MDNA, FILLER + mdna + " " + FILLER, f"10-Q MDNA-{tag}"),
        _doc(label, RISK, FILLER + risk + " " + FILLER, f"10-Q RISK-{tag}"),
        _doc(label, EX99, FILLER + release + " " + FILLER, f"8-K EX99-{tag} ex99_1.htm"),
    ]


# --- the ledger's attribution ------------------------------------------------------

def test_a_window_resolves_to_the_one_document_it_was_cut_from():
    docs = _period_docs("FY2025Q2", mdna="Demand remains strong across all segments.",
                        risk="We identified a material weakness in internal control.")
    ledger = EvidenceLedger(docs)
    assert ledger.attribute("…regments. We identified a material weakness in internal control. The…", "FY2025Q2") == NOT_LOCATED
    assert ledger.attribute("…We identified a material weakness in internal control.…", "FY2025Q2") == "10-Q RISK-FY2025Q2"
    assert ledger.attribute("…Demand remains strong across all segments.…", "FY2025Q2") == "10-Q MDNA-FY2025Q2"


def test_whitespace_in_the_document_does_not_hide_a_window():
    docs = [_doc("FY2025Q2", MDNA, "Demand   remains\n\nstrong  across", "10-Q A")]
    assert EvidenceLedger(docs).attribute("…Demand remains strong across…", "FY2025Q2") == "10-Q A"


def test_a_repeated_boilerplate_sentence_is_attributed_to_its_own_period():
    """A sentence found in every quarter's 10-Q was still cut from one."""
    sentence = "Results include restructuring charges we believe are one-time."
    docs = _period_docs("FY2025Q1", mdna=sentence) + _period_docs("FY2025Q2", mdna=sentence)
    ledger = EvidenceLedger(docs)
    assert ledger.attribute(f"…{sentence}…", "FY2025Q2") == "10-Q MDNA-FY2025Q2"


def test_multi_period_snippets_name_each_periods_filing_in_document_order():
    sentence = "Results include restructuring charges we believe are one-time."
    docs = _period_docs("FY2025Q1", mdna=sentence) + _period_docs("FY2025Q2", mdna=sentence)
    excerpt = f"[FY2025Q1] …{sentence}… | [FY2025Q2] …{sentence}…"
    assert _fragments(excerpt) == [("FY2025Q1", sentence), ("FY2025Q2", sentence)]
    assert EvidenceLedger(docs).attribute(excerpt) == "10-Q MDNA-FY2025Q1; 10-Q MDNA-FY2025Q2"


def test_a_window_in_several_documents_of_its_period_names_all_once():
    sentence = "Demand remains strong across all segments."
    docs = _period_docs("FY2025Q2", mdna=sentence, release=sentence)
    assert EvidenceLedger(docs).attribute(f"…{sentence}…", "FY2025Q2") == (
        "10-Q MDNA-FY2025Q2; 8-K EX99-FY2025Q2 ex99_1.htm"
    )


def test_derived_rows_name_the_periods_documents():
    docs = _period_docs("FY2025Q1") + _period_docs("FY2025Q2")
    assert EvidenceLedger(docs).derived_from("FY2025Q2") == (
        "derived from FY2025Q2 documents: 10-Q MDNA-FY2025Q2; 10-Q RISK-FY2025Q2; "
        "8-K EX99-FY2025Q2 ex99_1.htm"
    )


def test_what_cannot_be_named_says_why():
    unsourced = [DocumentRecord(fiscal_label="FY2025Q2", doc_type=MDNA, text="Demand remains strong.")]
    ledger = EvidenceLedger(unsourced)
    assert ledger.attribute("…Demand remains strong.…", "FY2025Q2") == NO_SOURCE_RECORDED
    assert ledger.derived_from("FY2025Q2") == "derived from FY2025Q2 documents (no source recorded)"
    assert ledger.attribute("…never said…", "FY2025Q2") == NOT_LOCATED
    assert EvidenceLedger().derived_from("FY2025Q2") == "derived from FY2025Q2 documents (no source recorded)"


# --- the live layer ----------------------------------------------------------------

_BARE = "period documents"


def _sourced(dataset):
    for d in dataset.documents:
        d.source = f"10-Q acc-{d.fiscal_label}-{d.doc_type.value}"
    return dataset


def test_no_live_row_claims_bare_period_documents():
    ds = _sourced(stretch_dataset())
    rows = analyze(ds).narrative_evidence
    assert rows and all(r.source != _BARE for r in rows)
    assert all(r.source.startswith(("10-Q acc-", "derived from")) for r in rows)


def test_the_stretchco_rows_name_the_filings_their_snippets_came_from():
    ds = _sourced(stretch_dataset())
    rows = {r.evidence_id: r for r in analyze(ds).narrative_evidence}
    assert rows["NE-001"].source == (
        "10-Q acc-FY2025Q1-earnings_release; 10-Q acc-FY2025Q2-earnings_release"
    )
    assert rows["NE-008"].source == (
        "derived from FY2025Q4 documents: 10-Q acc-FY2025Q4-earnings_release"
    )
    assert rows["NE-010"].detector.startswith("mismatch:")
    assert rows["NE-010"].source == "10-Q acc-FY2025Q4-earnings_release"


def test_without_recorded_sources_the_rows_say_so():
    rows = analyze(stretch_dataset()).narrative_evidence
    assert rows and all(r.source != _BARE for r in rows)
    assert all("no source recorded" in r.source for r in rows)


SENTENCES = [
    "Results include restructuring charges and impairment we believe are one-time.",
    "Our integration and transformation program continues.",
    "We are lowering our full-year guidance given macroeconomic uncertainty.",
    "We are raising our full-year guidance and reaffirm our outlook.",
    "Headwinds and a challenging macro environment weighed on softness in demand.",
    "Management identified a material weakness in internal control.",
    "There is substantial doubt about our ability to continue as a going concern.",
    "Demand remains strong and record bookings continued.",
    "Revenue grew in every region and margins held steady.",
]
LABELS = ["FY2024Q3", "FY2024Q4", "FY2025Q1", "FY2025Q2"]


@st.composite
def sourced_documents(draw):
    docs = []
    for label in LABELS:
        for doc_type in (MDNA, RISK, EX99):
            if not draw(st.booleans()):
                continue
            picked = draw(st.lists(st.sampled_from(SENTENCES), min_size=1, max_size=6))
            accession = f"000{draw(st.integers(1000, 9999))}-{label}-{doc_type.value}"
            docs.append(_doc(label, doc_type, " ".join([FILLER, *picked, FILLER]), f"10-Q {accession}"))
    return docs


@settings(max_examples=60)
@given(docs=sourced_documents())
def test_every_quoted_row_names_only_documents_that_contain_its_windows(docs):
    """The invariant, over generated filings: no row claims bare "period
    documents"; a quoted row names exactly documents of the right period that
    contain one of its windows, and every window is in one of them."""
    result = compute_narrative_layer(docs)
    by_source = {d.source: d for d in docs}
    for row in result.evidence:
        assert row.source != _BARE
        if row.source.startswith("derived from") or row.source in (NOT_LOCATED, NO_SOURCE_RECORDED):
            continue
        named = [by_source[s] for s in row.source.split("; ")]
        fragments = _fragments(row.excerpt)
        if len(row.excerpt) == 400:  # the ledger truncates; the cut window cannot be checked
            fragments = fragments[:-1]
        for label, text in fragments:
            period = label or row.fiscal_label
            assert any(
                text in " ".join(d.text.split()) and d.fiscal_label == period for d in named
            ), (row.detector, text)
        for d in named:
            assert any(text in " ".join(d.text.split()) for _, text in fragments) or not fragments


def test_edgar_documents_record_the_filings_structured_provenance(tmp_path):
    from app.services.ingestion.edgar_documents import fetch_documents
    from tests.unit.test_edgar_documents import (
        _FakeClient,
        _one_802_subs,
        _release_archives,
    )

    facts = {"facts": {"us-gaap": {"Assets": {"units": {"USD": [
        {"end": "2026-03-31", "val": 1000.0, "filed": "2026-05-01", "form": "10-Q"},
        {"end": "2026-06-30", "val": 1100.0, "filed": "2026-07-04", "form": "8-K"},
    ]}}}}}
    client = _FakeClient(tmp_path, _one_802_subs(), _release_archives())
    (doc,) = fetch_documents(client, "FAKE", facts_json=facts, cik=1234).documents
    assert (doc.accession, doc.form, doc.filed) == ("0001-26-000001", "8-K", date(2026, 7, 5))
    assert doc.source == "8-K 0001-26-000001 ex99_1.htm"  # unchanged; backtests parse it


def test_a_guidance_shift_row_names_the_current_periods_filing():
    docs = [
        _doc("FY2025Q1", EX99, FILLER + "We are raising our full-year guidance and reaffirm our outlook. "
             "We raise guidance again and reaffirm." + FILLER, "8-K EX99-Q1"),
        _doc("FY2025Q2", EX99, FILLER + "We are lowering our full-year guidance. We withdraw our "
             "outlook and are no longer providing guidance." + FILLER, "8-K EX99-Q2"),
    ]
    rows = [r for r in compute_narrative_layer(docs).evidence if r.detector == "guidance_shift"]
    assert len(rows) == 1 and rows[0].source == "8-K EX99-Q2"


def test_two_sections_of_one_filing_name_it_once():
    """MD&A and Risk Factors cut from the same 10-Q share one source."""
    sentence = "Demand remains strong across all segments."
    docs = [
        _doc("FY2025Q2", MDNA, FILLER + sentence + FILLER, "10-Q 0001-25-000002"),
        _doc("FY2025Q2", RISK, FILLER + sentence + FILLER, "10-Q 0001-25-000002"),
    ]
    assert EvidenceLedger(docs).attribute(f"…{sentence}…", "FY2025Q2") == "10-Q 0001-25-000002"


def test_a_guidance_sentence_repeated_from_last_quarter_names_this_quarters_filing():
    """The same lowering sentence sits in both quarters (last quarter it was
    outweighed by raises); the row quotes the current quarter, so it must
    name the current filing only."""
    lowering = "We are lowering our full-year guidance."
    raising = " We are raising guidance and reaffirm our outlook." * 4
    docs = [
        _doc("FY2025Q1", EX99, FILLER + lowering + " " + FILLER + raising, "8-K EX99-Q1"),
        _doc("FY2025Q2", EX99, FILLER + lowering + " " + FILLER, "8-K EX99-Q2"),
    ]
    rows = [r for r in compute_narrative_layer(docs).evidence if r.detector == "guidance_shift"]
    assert len(rows) == 1 and rows[0].source == "8-K EX99-Q2"
    assert _fragments(rows[0].excerpt)[0][1] in " ".join(docs[0].text.split())  # in Q1 too
