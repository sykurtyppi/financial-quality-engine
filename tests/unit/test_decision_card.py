"""Decision-card renderer tests (P1-D). The card leads with the thermometer and
tiered flags and carries NO composite grade (§7)."""

from app.schemas.financials import CompanyProfile
from app.schemas.report import AnalysisResult, Flag, NarrativeFinding
from app.services.reporting.decision_card import render_decision_card
from app.services.scoring.thermometer import ClusterReadout, DistressThermometer, RegimeFlag


def _flag(title: str, metrics: list[str], severity: str = "red") -> Flag:
    return Flag(
        severity=severity,
        title=title,
        detail="d",
        evidence_metrics=metrics,
        fiscal_label="FY2025Q4",
    )


def _result(**kw) -> AnalysisResult:
    base = dict(
        profile=CompanyProfile(ticker="TESTCO"),
        analyzed_periods=["FY2025Q4"],
        overall=None,
    )
    base.update(kw)
    return AnalysisResult(**base)


def _thermo(reading, hot=None, regime=None) -> DistressThermometer:
    return DistressThermometer(
        reading=reading,
        clusters=[ClusterReadout(hot, reading, ("m",))] if hot else [],
        regime_flags=regime or [],
    )


class TestCard:
    def test_leads_with_thermometer_and_no_composite(self):
        result = _result(overall=None)
        card = render_decision_card(
            result, _thermo(82.0, "Balance Sheet & Leverage"), generated_on="2026-08-09"
        )
        assert "Decision Card — TESTCO" in card
        # Review finding 4: NO raw 0-100 number and NO band thresholds on the card.
        assert "/100" not in card
        assert "82" not in card
        assert "experimental" in card.lower()
        # The concrete, descriptive facts are still surfaced.
        assert "Most-elevated dimension: Balance Sheet & Leverage" in card

    def test_flags_are_tiered(self):
        result = _result(
            red_flags=[
                _flag("Auditor non-reliance", ["non_reliance_8k_402"]),  # Tier 1
                _flag("Elevated leverage", ["net_debt_to_ebitda"]),  # Tier 2
                _flag("Recurring adjustment language", ["adjustment_recurrence_ratio"]),  # Tier 3
            ]
        )
        card = render_decision_card(result, _thermo(50.0), generated_on="2026-08-09")
        t1 = card.split("Tier 1")[1].split("Tier 2")[0]
        t2 = card.split("Tier 2")[1].split("Tier 3")[0]
        t3 = card.split("Tier 3")[1]
        assert "Auditor non-reliance" in t1
        assert "Elevated leverage" in t2
        assert "Recurring adjustment language" in t3

    def test_tier1_events_and_high_severity_populate_tier1(self):
        result = _result(
            narrative_findings=[
                NarrativeFinding(
                    kind="high_severity_disclosure",
                    detail="going concern language emerged",
                    fiscal_label="FY2025Q4",
                )
            ]
        )
        card = render_decision_card(
            result,
            _thermo(50.0),
            generated_on="2026-08-09",
            tier1_events=["8-K Item 4.02 non-reliance (restatement announced) filed 2026-06-01"],
        )
        t1 = card.split("Tier 1")[1].split("Tier 2")[0]
        assert "8-K Item 4.02 non-reliance" in t1
        assert "High-severity disclosure emergence" in t1

    def test_empty_state_is_graceful(self):
        card = render_decision_card(_result(), _thermo(None), generated_on="2026-08-09")
        assert "No material period-over-period changes surfaced." in card
        assert "Insufficient distress-relevant data" in card
        assert "none surfaced this run" in card

    def test_regime_and_events_rendered(self):
        result = _result(green_flags=[_flag("Strong FCF", ["fcf_margin"], "green")])
        card = render_decision_card(
            result,
            _thermo(95.0, "Cash Generation & Funding", [RegimeFlag("EBITDA_NEGATIVE", "EBITDA is negative", 25.0)]),
            generated_on="2026-08-09",
            coverage=0.82,
            event_lines=["2 equity takedowns in the last 18 months"],
        )
        assert "EBITDA is negative" in card
        assert "2 equity takedowns" in card
        assert "Strong FCF" in card
        assert "coverage 82%" in card

    def test_unavailable_tier1_sources_flagged(self):
        # A not-checked Tier-1 source must not read as clean (findings 4 + round 2).
        card = render_decision_card(
            _result(), _thermo(50.0), generated_on="2026-08-09",
            tier1_unavailable=["restatement footprints", "8-K 4.02 events"],
        )
        t1 = card.split("Tier 1")[1].split("Tier 2")[0]
        assert "not checked this run" in t1
        assert "restatement footprints" in t1
        assert "8-K 4.02 events" in t1
