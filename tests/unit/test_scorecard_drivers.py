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
        assert "computed (40)" in drivers

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
        assert "| Block | Score | Top drivers |" in card
        # and it is populated, not an empty column
        assert "beneish_m_score (" in card

    def test_the_retirement_is_explained_in_the_report(self):
        assert "No Direction word is shown per block" in self._scorecard()
