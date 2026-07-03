"""Unit tests for the backtesting framework: point-in-time filtering,
Spearman/rank machinery, analysis aggregations, and price-series math."""

from datetime import date

import pytest

from app.services.backtesting import analysis as an
from app.services.backtesting.pit import filter_as_of
from app.services.backtesting.prices import PriceSeries


class TestPointInTimeFilter:
    FACTS = {
        "entityName": "T",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {"start": "2024-01-01", "end": "2024-03-31", "val": 100, "filed": "2024-05-01"},
                            {"start": "2024-01-01", "end": "2024-03-31", "val": 105, "filed": "2025-02-15"},
                            {"start": "2024-04-01", "end": "2024-06-30", "val": 110, "filed": "2024-08-01"},
                            {"start": "2024-07-01", "end": "2024-09-30", "val": 120},  # no filed date
                        ]
                    }
                }
            }
        },
    }

    def test_future_filings_removed(self):
        pit = filter_as_of(self.FACTS, date(2024, 6, 1))
        entries = pit["facts"]["us-gaap"]["Revenues"]["units"]["USD"]
        assert len(entries) == 1
        assert entries[0]["val"] == 100  # the later amendment (105) is not yet knowable

    def test_amendment_visible_after_its_filing_date(self):
        pit = filter_as_of(self.FACTS, date(2025, 3, 1))
        vals = {e["val"] for e in pit["facts"]["us-gaap"]["Revenues"]["units"]["USD"]}
        assert 105 in vals and 110 in vals

    def test_undated_facts_dropped_in_pit_mode(self):
        pit = filter_as_of(self.FACTS, date(2026, 1, 1))
        vals = {e["val"] for e in pit["facts"]["us-gaap"]["Revenues"]["units"]["USD"]}
        assert 120 not in vals

    def test_concept_removed_when_nothing_knowable(self):
        pit = filter_as_of(self.FACTS, date(2024, 1, 1))
        assert "Revenues" not in pit["facts"].get("us-gaap", {})


class TestSpearman:
    def test_perfect_monotone(self):
        assert an.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
        assert an.spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)

    def test_ties_handled(self):
        ic = an.spearman([1, 1, 2, 3], [5, 5, 6, 7])
        assert ic == pytest.approx(1.0, abs=1e-9)

    def test_constant_series_returns_none(self):
        assert an.spearman([1, 1, 1], [1, 2, 3]) is None

    def test_too_few_points(self):
        assert an.spearman([1, 2], [2, 1]) is None


class TestAnalysisAggregations:
    def rows(self):
        out = []
        for i in range(50):
            score = float(i * 2)  # 0..98
            # construct a negative relationship: high concern -> low return
            ret = 0.20 - 0.004 * score
            out.append(
                {
                    "ticker": f"T{i}",
                    "archetype": "control",
                    "status": "ok",
                    "overall": str(score),
                    "rel_12m": str(ret),
                }
            )
        return out

    def test_quintiles_monotone_for_constructed_signal(self):
        qs = an.quintile_stats(self.rows(), "overall", "rel_12m")
        assert len(qs) == 5
        means = [q["mean_outcome"] for q in qs]
        assert means[0] > means[-1]

    def test_hit_rates_structure(self):
        hr = an.hit_rates(self.rows())
        assert hr["n"] == 50
        assert hr["n_flagged"] > 0
        assert hr["hit_rate"] is not None
        assert hr["hit_rate"] + hr["false_positive_rate"] == pytest.approx(1.0)

    def test_archetype_diagnostics_counts_statuses(self):
        rows = self.rows() + [
            {"ticker": "B1", "archetype": "bank_financial", "status": "excluded_financial"},
            {"ticker": "S1", "archetype": "stress_case", "status": "skip_stale"},
        ]
        diags = {d["archetype"]: d for d in an.archetype_diagnostics(rows)}
        assert diags["bank_financial"]["excluded"] == 1
        assert diags["stress_case"]["stale_skips"] == 1

    def test_stale_skip_signal(self):
        rows = [
            {"ticker": "SMCI", "status": "skip_stale"},
            {"ticker": "SMCI", "status": "skip_stale"},
            {"ticker": "AAPL", "status": "ok"},
        ]
        out = an.stale_skip_signal(rows)
        assert out == [{"ticker": "SMCI", "stale_asofs": 2}]


class TestPriceSeries:
    def test_forward_return(self):
        dates = [date(2024, 1, i + 1) for i in range(10)]
        closes = [100.0 * (1.01**i) for i in range(10)]
        ps = PriceSeries(dates, closes)
        r = ps.forward_return(date(2024, 1, 1), 5)
        assert r == pytest.approx(1.01**5 - 1)

    def test_forward_return_beyond_data_is_none(self):
        ps = PriceSeries([date(2024, 1, 1)], [100.0])
        assert ps.forward_return(date(2024, 1, 1), 5) is None

    def test_start_snaps_to_next_trading_day(self):
        dates = [date(2024, 1, 2), date(2024, 1, 5), date(2024, 1, 8)]
        ps = PriceSeries(dates, [100.0, 110.0, 121.0])
        # Jan 3 (non-trading) snaps to Jan 5
        assert ps.forward_return(date(2024, 1, 3), 1) == pytest.approx(0.10)
