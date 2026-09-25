"""Draft corpus cases (`tests/corpus_drafts/`) stay drafts, and stay current.

A draft is real filer data whose expectations were copied from the engine and
not yet checked against a filing. It is never evidence: the corpus gate reads
`tests/corpus/` only. These tests keep the drafts honest until a person
reviews them — none may claim a review, and each must still describe what
the engine observes, so a review is done against the engine as it is.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.corpus import evaluate, load_case, observe

DRAFTS = Path(__file__).resolve().parents[1] / "corpus_drafts"
CASES = sorted(p for p in DRAFTS.iterdir() if (p / "case.json").exists())


def test_there_are_drafts_and_the_gate_does_not_read_them():
    from tests.integration import test_corpus

    assert CASES
    assert not {p.name for p in CASES} & {p.name for p in test_corpus.CASES}


@pytest.mark.parametrize("directory", CASES, ids=[p.name for p in CASES])
def test_a_draft_is_unreviewed_real_data_that_the_engine_still_matches(directory):
    case, facts, submissions = load_case(directory)
    assert case.name == directory.name
    assert case.reviewed is None, "a reviewed case belongs in tests/corpus/"
    assert not case.synthetic
    review = (directory / "REVIEW.md").read_text()
    assert "DRAFT" in review and "## What to check" in review
    result = evaluate(case, observe(facts, submissions, case.ticker, case.as_of, case.since))
    assert result.passed, (
        f"{directory.name}: the engine no longer observes what this draft recorded — "
        + "; ".join(result.problems)
        + ". Regenerate the draft (and its REVIEW.md) before anyone reviews it."
    )
