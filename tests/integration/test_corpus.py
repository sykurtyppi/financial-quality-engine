"""The validation corpus gate (`app/services/corpus.py`, `docs/corpus.md`).

Each directory under `tests/corpus/` is one case: a trimmed companyfacts
payload, the filing index, and `case.json` with expectations a person pinned
after reading the filings (`reviewed`). Every case must pass every check, and
the corpus as a whole must meet the gates: false-clean rate 0, restatement
recall 1.0, amended precision 1.0.

A case marked `synthetic` is a harness self-test, not evidence; the summary
counts real cases separately so a synthetic-only corpus can never be read as
validation.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.services.corpus import (
    CorpusCase,
    Expected,
    ExpectedFootprint,
    Observation,
    ObservedFootprint,
    evaluate,
    load_case,
    metrics,
    observe,
)

CORPUS = Path(__file__).resolve().parents[1] / "corpus"
CASES = sorted(p for p in CORPUS.iterdir() if (p / "case.json").exists())


def _run(directory: Path):
    case, facts, submissions = load_case(directory)
    return case, evaluate(case, observe(facts, submissions, case.ticker, case.as_of, case.since))


@pytest.mark.parametrize("directory", CASES, ids=[p.name for p in CASES])
def test_every_case_is_reviewed_and_passes(directory):
    case, result = _run(directory)
    assert case.name == directory.name
    assert case.reviewed is not None, (
        f"{directory.name} is a DRAFT: read the filings its observation names, correct "
        "`expected`, and fill in `reviewed` before committing it"
    )
    assert result.passed, f"{directory.name}: " + "; ".join(result.problems)


def test_the_corpus_meets_its_gates(capsys):
    results = [_run(d)[1] for d in CASES]
    m = metrics(results)
    with capsys.disabled():
        print("\n" + m.summary())
    assert m.cases == len(CASES) >= 2
    assert m.false_clean_rate == 0.0
    assert m.restatement_recall == 1.0
    assert m.amended_precision == 1.0
    assert m.gates_pass


def test_the_self_tests_cover_both_directions():
    """One self-test must fire (a scored 10-Q/A and an in-window 4.02) and
    one must not (an amendment re-filing the same figure) — so a harness that
    always or never fires cannot pass."""
    by_name = {d.name: _run(d)[1] for d in CASES}
    fires = by_name["_selftest_amended_10qa"].observation
    quiet = by_name["_selftest_amendment_unchanged"].observation
    assert fires.tier1 and any(f.amended for f in fires.footprints)
    assert fires.non_reliance == ("0000000001-25-000700",)  # not the 2022 one
    assert "8-K Item 4.02 non-reliance (restatement announced) filed 2025-01-15" in fires.tier1
    assert not quiet.tier1 and not any(f.amended for f in quiet.footprints)


# --- the arithmetic, on hand-built observations -------------------------------------


def _case(*, tier1=False, footprints=(), non_reliance=(), coverage_min=0.0, uninspected=(),
          selections=None, synthetic=True) -> CorpusCase:
    return CorpusCase(
        name="t", ticker="T", as_of=date(2025, 3, 31), since=date(2023, 1, 1), why="unit",
        synthetic=synthetic,
        expected=Expected(tier1=tier1, footprints=[ExpectedFootprint(**f) for f in footprints],
                          non_reliance=list(non_reliance), coverage_min=coverage_min,
                          uninspected=list(uninspected), selections=selections or {}),
    )


def _obs(*, tier1=(), footprints=(), non_reliance=(), inspected=("revenue",), uninspected=None,
         selections=None) -> Observation:
    return Observation(
        selections=selections or {}, footprints=tuple(ObservedFootprint(*f) for f in footprints),
        tier1=tuple(tier1), non_reliance=tuple(non_reliance), inspected=tuple(inspected),
        uninspected=uninspected or {},
    )


REV = {"field": "revenue", "period_end": date(2024, 9, 30), "amended": True}
REV_OBS = ("revenue", date(2024, 9, 30), True, "acc-a")


def test_a_pinned_signal_that_reads_clean_is_a_false_clean():
    r = evaluate(_case(tier1=True, footprints=[REV]), _obs())
    assert r.false_clean and not r.passed and r.expected_found == 0
    m = metrics([r])
    assert m.false_clean_rate == 1.0 and m.restatement_recall == 0.0 and not m.gates_pass


def test_a_found_amendment_scores_full_recall_and_precision():
    r = evaluate(_case(tier1=True, footprints=[REV]), _obs(tier1=["x"], footprints=[REV_OBS]))
    assert r.passed and not r.false_clean
    m = metrics([r])
    assert (m.false_clean_rate, m.restatement_recall, m.amended_precision) == (0.0, 1.0, 1.0)


def test_an_unpinned_amendment_costs_precision_and_raises_a_false_alarm():
    stray = ("net_income", date(2024, 6, 30), True, "acc-b")
    r = evaluate(_case(), _obs(tier1=["x"], footprints=[stray]))
    assert r.false_alarm and not r.passed
    assert any("unpinned amended footprint: net_income" in p for p in r.problems)
    assert metrics([r]).amended_precision == 0.0


def test_a_revised_but_unamended_footprint_does_not_satisfy_a_pinned_amendment():
    revised = ("revenue", date(2024, 9, 30), False, "acc-c")
    r = evaluate(_case(tier1=True, footprints=[REV]), _obs(tier1=["x"], footprints=[revised]))
    assert r.expected_found == 0 and "missed footprint" in "; ".join(r.problems)


def test_coverage_uninspected_selection_and_402_checks():
    case = _case(tier1=True, non_reliance=["acc-402"], coverage_min=0.75,
                 uninspected=["goodwill"], selections={"revenue": "us-gaap:Revenues"})
    obs = _obs(tier1=["x"], inspected=("revenue",), uninspected={"cfo": "no series"},
               selections={"revenue": "us-gaap:SalesRevenueNet"})
    problems = "; ".join(evaluate(case, obs).problems)
    assert "missed 8-K 4.02 acc-402" in problems
    assert "evidence coverage 50% below the pinned 75%" in problems
    assert "goodwill is pinned as not inspectable" in problems
    assert "selection revenue: pinned us-gaap:Revenues" in problems


def test_empty_denominators_and_the_real_case_count():
    m = metrics([evaluate(_case(), _obs()), evaluate(_case(synthetic=False), _obs())])
    assert (m.false_clean_rate, m.restatement_recall, m.amended_precision) == (0.0, 1.0, 1.0)
    assert m.real_cases == 1 and "real cases: 1" in m.summary()
    only_synthetic = metrics([evaluate(_case(), _obs())])
    assert "NOT validation" in only_synthetic.summary()


def test_a_draft_case_is_refused(tmp_path):
    import shutil

    draft = tmp_path / "_draft"
    shutil.copytree(CASES[0], draft)
    text = (draft / "case.json").read_text()
    import json

    data = json.loads(text)
    data["reviewed"], data["name"] = None, "_draft"
    (draft / "case.json").write_text(json.dumps(data))
    with pytest.raises(AssertionError, match="is a DRAFT"):
        test_every_case_is_reviewed_and_passes(draft)


def test_the_case_builder_writes_a_draft_from_what_it_observed(tmp_path):
    """Offline mode on a committed case's payloads: the draft's expectations
    are exactly the observation, it is unreviewed, and the trims keep only
    engine concepts and filings made by the case date."""
    import json

    from scripts import make_corpus_case as mk

    src = CASES[0]
    case, facts, submissions = load_case(src)
    facts["facts"]["us-gaap"]["NotAnEngineConcept"] = {"units": {"USD": [
        {"end": "2024-06-30", "val": 1.0, "filed": "2024-08-01", "form": "10-Q", "accn": "x"}]}}
    late = submissions["filings"]["recent"]
    for column, value in (("form", "8-K"), ("filingDate", "2030-01-01"),
                          ("accessionNumber", "late-1"), ("primaryDocument", "d.htm"),
                          ("items", "4.02")):
        late[column].append(value)
    (tmp_path / "f.json").write_text(json.dumps(facts))
    (tmp_path / "s.json").write_text(json.dumps(submissions))
    assert mk.main([
        "draft", case.ticker, "--as-of", case.as_of.isoformat(), "--since", case.since.isoformat(),
        "--why", "test", "--from-facts", str(tmp_path / "f.json"),
        "--from-submissions", str(tmp_path / "s.json"), "--synthetic", "--corpus", str(tmp_path),
    ]) == 0
    draft, dfacts, dsubs = load_case(tmp_path / "draft")
    assert draft.reviewed is None and draft.synthetic
    assert draft.expected == case.expected.model_copy(update={"coverage_min": draft.expected.coverage_min})
    assert "NotAnEngineConcept" not in dfacts["facts"]["us-gaap"]
    assert "late-1" not in dsubs["filings"]["recent"]["accessionNumber"]


def test_an_observation_sees_nothing_filed_after_the_case_date():
    """A real case's payload is fetched today and holds everything filed
    since. Nothing filed after `as_of` may reach the observation: neither an
    amendment (the scan's cut) nor a better-covered tag that would change
    which concept the mapper reads (the mapper's cut)."""
    case, facts, submissions = load_case(CORPUS / "_selftest_amended_10qa")
    later = "2025-06-01"
    concepts = facts["facts"]["us-gaap"]
    rows = concepts["NetIncomeLoss"]["units"]["USD"]
    rows.append(dict(rows[-1], val=rows[-1]["val"] * 2, filed=later, form="10-Q/A", accn="late-a"))
    assets = concepts["Assets"]["units"]["USD"]
    concepts["Goodwill"] = {"units": {"USD": [
        dict(r, val=100.0, filed=later, accn="late-g") for r in assets
    ]}}
    obs = observe(facts, submissions, case.ticker, case.as_of, case.since)
    assert "late-a" not in {f.accession for f in obs.footprints}
    assert "goodwill" not in obs.selections and "goodwill" in obs.uninspected
    assert evaluate(case, obs).passed
    # The same payload read as of the later day sees both.
    after = observe(facts, submissions, case.ticker, date(2025, 6, 30), case.since)
    assert "late-a" in {f.accession for f in after.footprints}
    assert after.selections["goodwill"] == "us-gaap:Goodwill"


def test_the_corpus_holds_real_cases():
    """The `corpus-real` gate (its own CI job): the evidence layer is not
    validated until at least one reviewed case built from real filings is
    pinned. Skipped in the main suite; FQE_REQUIRE_REAL_CORPUS=1 enforces it,
    and that job stays red until the operator pins a case with
    `scripts/make_corpus_case.py` (SEC access needed — docs/corpus.md)."""
    import os

    if os.environ.get("FQE_REQUIRE_REAL_CORPUS") != "1":
        pytest.skip("real-corpus gate runs in the corpus-real CI job")
    real = [d.name for d in CASES if not load_case(d)[0].synthetic]
    assert real, (
        "no real corpus case: only synthetic self-tests are pinned, so the evidence "
        "layer is NOT validated. Build one with scripts/make_corpus_case.py "
        "(see docs/corpus.md) and review it before committing."
    )


def test_the_case_builder_refuses_a_misaligned_index():
    """Aligned or refused, like every reader of the filing index."""
    import importlib.util

    from app.services.ingestion.payloads import ExternalPayloadError

    spec = importlib.util.spec_from_file_location(
        "_mcc", Path(__file__).resolve().parents[2] / "scripts" / "make_corpus_case.py")
    mcc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mcc)
    subs = {"filings": {"recent": {"form": ["10-Q", "8-K"], "filingDate": ["2026-01-02"]}}}
    with pytest.raises(ExternalPayloadError, match="unequal lengths"):
        mcc.trim_submissions(subs, date(2026, 6, 30))


def test_a_derived_quarter_the_report_promotes_is_observed_like_a_footprint():
    """Hermes audit round 4: an amended H1 moving 101 -> 101.9 is below
    materiality as a filed figure but moves the derived Q2 from 1 to 1.9.
    The report promotes it; the corpus must see what the report says, so a
    case pinned without it fails on precision and one pinned with it on
    recall."""
    from tests.unit.test_restatement_scan_truth import AS_OF, Q2, SINCE, _ytd_filer

    obs = observe(_ytd_filer(101.9), None, "T", AS_OF, SINCE)
    (fp,) = [f for f in obs.footprints if f.field == "operating_income"]
    assert (fp.period_end, fp.amended, fp.derived) == (Q2, True, True) and fp.accession
    assert any("moved derived operating_income" in t for t in obs.tier1)
    assert not obs.clean
    pinned = {"field": "operating_income", "period_end": Q2, "amended": True}
    assert evaluate(_case(tier1=True, footprints=[pinned]), obs).passed
    unpinned = evaluate(_case(), obs)
    assert unpinned.false_alarm and not unpinned.passed
    assert any("unpinned amended derived footprint: operating_income" in p
               for p in unpinned.problems)
