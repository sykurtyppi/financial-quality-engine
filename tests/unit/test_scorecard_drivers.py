"""The §2 Scorecard names drivers instead of labelling blocks.

The per-block Direction label was retired because its bands are percentiles of
the COMPOSITE distribution: one band set transplanted onto eight
differently-distributed series, where the same word carried a different prior
in each row and — because low coverage suppresses a score downward — an
unmeasurable block rendered as *positive*. These pin the replacement.
"""

from __future__ import annotations

from app.schemas.scoring import BlockScore, ComponentContribution, Confidence, Direction
from app.services.reporting.markdown_report import _top_drivers


def _component(name: str, concern: float | None, weight: float) -> ComponentContribution:
    return ComponentContribution(
        metric_name=name, metric_value=1.0, concern_score=concern,
        weight=weight, anchors=[], status="ok",
    )


def _block(components: list[ComponentContribution], score: float | None = 50.0) -> BlockScore:
    return BlockScore(
        name="Test Block", score=score, direction=Direction.MIXED,
        confidence=Confidence.HIGH, rationale="", components=components,
        data_coverage=1.0,
    )


class TestDriverRanking:
    def test_ranks_by_weight_times_concern_not_concern_alone(self):
        # The block score is a weighted mean, so a heavily weighted middling
        # metric moved it more than a lightly weighted extreme one. Ranking on
        # concern alone would put the wrong metric first.
        drivers = _top_drivers(_block([
            _component("lightly_weighted_extreme", 100.0, 0.05),
            _component("heavily_weighted_middling", 50.0, 0.90),
        ]))
        assert drivers.startswith("heavily_weighted_middling")

    def test_caps_at_three(self):
        drivers = _top_drivers(_block([
            _component(f"m{i}", 90.0 - i, 0.2) for i in range(6)
        ]))
        assert drivers.count(",") == 2

    def test_skips_metrics_that_did_not_compute(self):
        drivers = _top_drivers(_block([
            _component("computed", 40.0, 0.5),
            _component("not_meaningful", None, 0.5),
        ]))
        assert "not_meaningful" not in drivers
        assert drivers == "computed"  # names only: no 0-100 number

    def test_a_block_with_nothing_usable_says_so(self):
        # A blank cell and a genuinely clean block must not look alike — that
        # conflation is exactly what the retired label did.
        assert _top_drivers(_block([_component("nothing", None, 0.5)], score=None)) == (
            "— insufficient coverage"
        )


class TestScorecardRendering:
    def _scorecard(self) -> str:
        import json

        from app.core.pipeline import analyze
        from app.schemas.financials import CompanyDataset
        from app.services.reporting.markdown_report import render

        ds = CompanyDataset(**json.loads(open("data/example_company.json").read()))
        out = render(analyze(ds), generated_on="2026-09-21")
        return out.split("## 2. Scorecard")[1].split("## 3.")[0]

    def test_no_per_block_direction_word_is_rendered(self):
        rows = [ln for ln in self._scorecard().splitlines() if ln.startswith("| ") and "---" not in ln]
        body = "\n".join(rows[1:])  # skip the header row
        for word in ("Positive", "Mixed", "Negative"):
            assert word not in body, f"{word!r} still labels a block row"

    def test_the_drivers_column_replaced_it(self):
        card = self._scorecard()
        assert "| Block | Top drivers | Confidence | Coverage | Weight |" in card
        # and it is populated, not an empty column — by names, not numbers
        assert "| Earnings Quality | beneish_m_score, " in card

    def test_the_retirement_is_explained_in_the_report(self):
        assert "No Direction word is shown per block" in self._scorecard()


class TestNoZeroToHundredNumberRemains:
    """P1-C's done-when: no 0-100 number on any rendered surface. The scores
    are still computed — the calibration snapshot pins them — they are just
    not shown, because they measured non-discriminating."""

    ZERO_TO_HUNDRED = r"(?:/100\b|concern \d+|\(\d{1,3}\)|0[–-]100 concern)"

    def test_the_golden_report_carries_none(self):
        import re
        from pathlib import Path

        golden = (Path(__file__).resolve().parents[1] / "golden_reports"
                  / "stretchco_report.md").read_text()
        assert not re.findall(self.ZERO_TO_HUNDRED, golden)
        for line in golden.split("## 2. Scorecard")[1].split("## 3.")[0].splitlines():
            if line.startswith("| ") and "---" not in line and "Block" not in line:
                name, drivers = line.split(" | ")[:2]
                assert not drivers.strip().isdigit(), line  # no score cell

    def test_a_real_full_report_carries_none(self):
        import json
        import re
        from pathlib import Path

        from app.core.pipeline import analyze
        from app.services.ingestion.companyfacts_mapper import build_dataset
        from app.services.reporting.report_builder import build_report

        real = Path(__file__).resolve().parents[1] / "fixtures" / "real"
        for ticker in ("AAPL", "KO", "CRM"):
            ds, diag = build_dataset(json.loads((real / f"companyfacts_{ticker}_trimmed.json")
                                                .read_text()), ticker)
            report, _ = build_report(analyze(ds), ds, generated_on="2026-09-24",
                                     coverage=diag.coverage(), fetched_at="2026-09-24 09:00 UTC")
            assert not re.findall(self.ZERO_TO_HUNDRED, report), ticker

    def test_the_scores_are_still_computed(self):
        import json

        from app.core.pipeline import analyze
        from app.schemas.financials import CompanyDataset

        ds = CompanyDataset(**json.loads(open("data/example_company.json").read()))
        result = analyze(ds)
        assert result.overall is not None and result.overall.score is not None
        assert all(b.score is not None for b in result.block_scores if b.data_coverage)
