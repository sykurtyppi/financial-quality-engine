"""Unit tests for the companyfacts mapper — one test class per real-data bug
class found during v0.2 validation (see docs/real_data_validation.md)."""

from datetime import date

import pytest

from app.services.ingestion.companyfacts_mapper import (
    _fiscal_label,
    build_dataset,
    fiscal_year_end_month,
    select_quarter_ends,
)

# --- synthetic companyfacts builders -----------------------------------------

Q_ENDS = ["2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31",
          "2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"]
FY_STARTS = {"2024": "2024-01-01", "2025": "2025-01-01"}


def fact(start: str | None, end: str, val: float, filed: str = "2026-01-01", form: str = "10-Q") -> dict:
    e = {"end": end, "val": val, "filed": filed, "form": form}
    if start:
        e["start"] = start
    return e


def facts_json(concepts: dict[str, list[dict]], taxonomy: str = "us-gaap") -> dict:
    return {
        "entityName": "Synthetic Test Co",
        "facts": {taxonomy: {tag: {"units": {"USD": entries}} for tag, entries in concepts.items()}},
    }


def q_start(end: str) -> str:
    """Approximate quarter start ~91 days before end."""
    from datetime import datetime, timedelta

    return (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=91)).strftime("%Y-%m-%d")


def assets_instants(values: list[float] | None = None) -> list[dict]:
    vals = values or [1000.0] * len(Q_ENDS)
    return [fact(None, end, v) for end, v in zip(Q_ENDS, vals)]


def quarterly_flows(tag_values: list[float]) -> list[dict]:
    return [fact(q_start(end), end, v) for end, v in zip(Q_ENDS, tag_values)]


class TestDirectAndDerivedFlows:
    def test_direct_quarterly_facts_used(self):
        fj = facts_json({
            "Assets": assets_instants(),
            "Revenues": quarterly_flows([100.0] * 8),
        })
        ds, diag = build_dataset(fj, "SYN", n_quarters=4)
        assert [p.revenue for p in ds.periods] == [100.0] * 4
        assert diag.field_by_name("revenue").methods == {"direct": 4}

    def test_q4_derived_from_fy_minus_3_quarters(self):
        # Q1-Q3 quarterly facts + FY annual fact; no Q4 QTD fact (the Q4 problem).
        entries = []
        for year in ("2024", "2025"):
            ends = [e for e in Q_ENDS if e.startswith(year)]
            for end in ends[:3]:
                entries.append(fact(q_start(end), end, 100.0))
            entries.append(fact(FY_STARTS[year], ends[3], 430.0, form="10-K"))
        fj = facts_json({"Assets": assets_instants(), "Revenues": entries})
        ds, diag = build_dataset(fj, "SYN", n_quarters=8)
        q4s = [p for p in ds.periods if p.fiscal_label.endswith("Q4")]
        assert all(p.revenue == pytest.approx(130.0) for p in q4s)  # 430 - 3*100
        assert diag.field_by_name("revenue").methods.get("fy_minus_3q") == 2

    def test_ytd_only_cashflow_differenced(self):
        # CFO reported only cumulatively (YTD) each quarter — the 10-Q pattern.
        entries = []
        for year in ("2024", "2025"):
            ends = [e for e in Q_ENDS if e.startswith(year)]
            cumulative = [50.0, 120.0, 200.0, 300.0]
            for end, cum in zip(ends, cumulative):
                entries.append(fact(FY_STARTS[year], end, cum))
        fj = facts_json({
            "Assets": assets_instants(),
            "Revenues": quarterly_flows([100.0] * 8),
            "NetCashProvidedByUsedInOperatingActivities": entries,
        })
        ds, diag = build_dataset(fj, "SYN", n_quarters=8)
        cfo = [p.cfo for p in ds.periods]
        # Q1 = 50 (YTD == QTD), then diffs: 70, 80, 100 — per year.
        assert cfo == [50.0, 70.0, 80.0, 100.0, 50.0, 70.0, 80.0, 100.0]
        methods = diag.field_by_name("cfo").methods
        assert methods.get("ytd_diff", 0) >= 6

    def test_window_edge_quarter_uses_buffer_history(self):
        """First window quarter must still derive via YTD-diff using the
        buffered quarter before the window (v0.2 bug #1)."""
        entries = []
        for year in ("2024", "2025"):
            ends = [e for e in Q_ENDS if e.startswith(year)]
            for end, cum in zip(ends, [50.0, 120.0, 200.0, 300.0]):
                entries.append(fact(FY_STARTS[year], end, cum))
        fj = facts_json({
            "Assets": assets_instants(),
            "Revenues": quarterly_flows([100.0] * 8),
            "NetCashProvidedByUsedInOperatingActivities": entries,
        })
        ds, _ = build_dataset(fj, "SYN", n_quarters=4)  # window = 2025 only
        assert ds.periods[0].fiscal_label == "FY2025Q1"
        assert [p.cfo for p in ds.periods] == [50.0, 70.0, 80.0, 100.0]


class TestAmendmentsAndTagSelection:
    def test_latest_filed_wins(self):
        end = Q_ENDS[0]
        fj = facts_json({
            "Assets": assets_instants(),
            "Revenues": [
                fact(q_start(end), end, 100.0, filed="2024-05-01"),
                fact(q_start(end), end, 105.0, filed="2024-08-15", form="10-Q/A"),
            ] + quarterly_flows([100.0] * 8)[1:],
        })
        ds, _ = build_dataset(fj, "SYN", n_quarters=8)
        assert ds.periods[0].revenue == 105.0

    def test_tag_with_best_coverage_wins_over_priority(self):
        """A higher-priority tag covering 2 quarters must lose to a lower-
        priority tag covering all 8 (v0.2 bug: XOM receivables tag switch)."""
        sparse = [fact(None, e, 1.0) for e in Q_ENDS[:2]]
        full = [fact(None, e, 2.0) for e in Q_ENDS]
        fj = facts_json({
            "Assets": assets_instants(),
            "Revenues": quarterly_flows([100.0] * 8),
            "AccountsReceivableNetCurrent": sparse,
            "ReceivablesNetCurrent": full,
        })
        ds, diag = build_dataset(fj, "SYN", n_quarters=8)
        assert all(p.receivables == 2.0 for p in ds.periods)
        assert diag.field_by_name("receivables").tag_used == "us-gaap:ReceivablesNetCurrent"

    def test_tags_never_mixed_within_series(self):
        """Even when mixing would fill more quarters, one tag is chosen."""
        first_half = [fact(None, e, 1.0) for e in Q_ENDS[:4]]
        second_half = [fact(None, e, 2.0) for e in Q_ENDS[4:]]
        fj = facts_json({
            "Assets": assets_instants(),
            "Revenues": quarterly_flows([100.0] * 8),
            "AccountsReceivableNetCurrent": first_half,
            "ReceivablesNetCurrent": second_half,
        })
        ds, diag = build_dataset(fj, "SYN", n_quarters=8)
        vals = {p.receivables for p in ds.periods if p.receivables is not None}
        assert len(vals) == 1  # one tag's values only
        assert diag.field_by_name("receivables").periods_filled == 4


class TestDebtComposition:
    def test_split_preferred_and_not_double_counted(self):
        fj = facts_json({
            "Assets": assets_instants(),
            "Revenues": quarterly_flows([100.0] * 8),
            "LongTermDebtNoncurrent": [fact(None, e, 800.0) for e in Q_ENDS],
            "LongTermDebtCurrent": [fact(None, e, 100.0) for e in Q_ENDS],
            "LongTermDebt": [fact(None, e, 900.0) for e in Q_ENDS],  # total; must NOT be added
            "CommercialPaper": [fact(None, e, 50.0) for e in Q_ENDS],
        })
        ds, diag = build_dataset(fj, "SYN", n_quarters=8)
        assert all(p.total_debt == 950.0 for p in ds.periods)  # 800+100+50, not +900

    def test_total_fallback_when_split_missing(self):
        fj = facts_json({
            "Assets": assets_instants(),
            "Revenues": quarterly_flows([100.0] * 8),
            "LongTermDebt": [fact(None, e, 900.0) for e in Q_ENDS],
        })
        ds, diag = build_dataset(fj, "SYN", n_quarters=8)
        assert all(p.total_debt == 900.0 for p in ds.periods)
        assert any("LongTermDebt total" in n for n in diag.field_by_name("total_debt").notes)

    def test_finance_leases_added_operating_leases_excluded(self):
        """P0-10: finance-lease liabilities belong in total debt; operating
        leases (a different economic obligation) do not."""
        fj = facts_json({
            "Assets": assets_instants(),
            "Revenues": quarterly_flows([100.0] * 8),
            "LongTermDebtNoncurrent": [fact(None, e, 800.0) for e in Q_ENDS],
            "LongTermDebtCurrent": [fact(None, e, 100.0) for e in Q_ENDS],
            "FinanceLeaseLiabilityNoncurrent": [fact(None, e, 60.0) for e in Q_ENDS],
            "FinanceLeaseLiabilityCurrent": [fact(None, e, 20.0) for e in Q_ENDS],
            # Operating leases must be ignored, even though they are larger.
            "OperatingLeaseLiabilityNoncurrent": [fact(None, e, 500.0) for e in Q_ENDS],
            "OperatingLeaseLiabilityCurrent": [fact(None, e, 200.0) for e in Q_ENDS],
        })
        ds, diag = build_dataset(fj, "SYN", n_quarters=8)
        assert all(p.total_debt == 980.0 for p in ds.periods)  # 800+100+60+20
        assert any("inance-lease" in n for n in diag.field_by_name("total_debt").notes)

    def test_finance_leases_not_double_counted_when_debt_tag_is_lease_inclusive(self):
        """When the chosen debt tag already embeds capital-lease obligations
        (LongTermDebtAndCapitalLeaseObligations), the separately reported
        finance-lease liability must NOT be added on top."""
        fj = facts_json({
            "Assets": assets_instants(),
            "Revenues": quarterly_flows([100.0] * 8),
            "LongTermDebtAndCapitalLeaseObligations": [fact(None, e, 800.0) for e in Q_ENDS],
            "LongTermDebtAndCapitalLeaseObligationsCurrent": [fact(None, e, 100.0) for e in Q_ENDS],
            "FinanceLeaseLiabilityNoncurrent": [fact(None, e, 60.0) for e in Q_ENDS],
            "FinanceLeaseLiabilityCurrent": [fact(None, e, 20.0) for e in Q_ENDS],
        })
        ds, diag = build_dataset(fj, "SYN", n_quarters=8)
        assert all(p.total_debt == 900.0 for p in ds.periods)  # leases already inside


class TestCoverDateTolerance:
    def test_shares_outstanding_matched_from_cover_dates(self):
        """dei share counts are stamped ~3 weeks after quarter end (v0.2 bug:
        WMT 0/8 coverage)."""
        cover_dates = ["2024-04-19", "2024-07-19", "2024-10-18", "2025-01-24",
                       "2025-04-18", "2025-07-18", "2025-10-17", "2026-01-23"]
        fj = facts_json({
            "Assets": assets_instants(),
            "Revenues": quarterly_flows([100.0] * 8),
        })
        fj["facts"]["dei"] = {
            "EntityCommonStockSharesOutstanding": {
                "units": {"shares": [fact(None, d, 500.0) for d in cover_dates]}
            }
        }
        ds, diag = build_dataset(fj, "SYN", n_quarters=8)
        assert all(p.shares_outstanding == 500.0 for p in ds.periods)
        fd = diag.field_by_name("shares_outstanding")
        assert fd.methods.get("nearest", 0) == 8
        assert any("cover-page" in n for n in fd.notes)


class TestFiscalLabels:
    def test_calendar_fye(self):
        assert _fiscal_label(date(2025, 12, 31), 12) == "FY2025Q4"
        assert _fiscal_label(date(2025, 3, 31), 12) == "FY2025Q1"

    def test_september_fye_apple_style(self):
        assert _fiscal_label(date(2026, 3, 28), 9) == "FY2026Q2"
        assert _fiscal_label(date(2025, 12, 27), 9) == "FY2026Q1"
        assert _fiscal_label(date(2025, 9, 27), 9) == "FY2025Q4"

    def test_january_fye_53_week_boundary(self):
        # 52/53-week calendars can end a few days into the next month:
        # Feb 2 end is a January-family fiscal year end.
        assert _fiscal_label(date(2026, 2, 2), 1) == "FY2026Q4"
        assert _fiscal_label(date(2026, 1, 31), 1) == "FY2026Q4"
        assert _fiscal_label(date(2026, 4, 30), 1) == "FY2027Q1"

    def test_fye_month_derived_from_annual_facts_not_metadata(self):
        """fy/fp metadata is unreliable (observed wrong on CRM); the month must
        come from annual-duration end dates."""
        entries = [fact("2025-01-01", "2025-12-31", 400.0, form="10-K")]
        fj = facts_json({"Assets": assets_instants(), "Revenues": entries + quarterly_flows([100.0] * 8)})
        assert fiscal_year_end_month(fj) == 12

    def test_labels_are_unique(self):
        fj = facts_json({
            "Assets": assets_instants(),
            "Revenues": quarterly_flows([100.0] * 8)
            + [fact(FY_STARTS[y], [e for e in Q_ENDS if e.startswith(y)][3], 400.0, form="10-K") for y in ("2024", "2025")],
        })
        ds, _ = build_dataset(fj, "SYN", n_quarters=8)
        labels = [p.fiscal_label for p in ds.periods]
        assert len(labels) == len(set(labels))


class TestNonAdditiveShares:
    def test_diluted_shares_never_derived_for_q4(self):
        entries = []
        for year in ("2024", "2025"):
            ends = [e for e in Q_ENDS if e.startswith(year)]
            for end in ends[:3]:
                entries.append(fact(q_start(end), end, 100.0))
            entries.append(fact(FY_STARTS[year], ends[3], 100.0, form="10-K"))  # FY only
        fj = facts_json({
            "Assets": assets_instants(),
            "Revenues": quarterly_flows([100.0] * 8),
        })
        fj["facts"]["us-gaap"]["WeightedAverageNumberOfDilutedSharesOutstanding"] = {
            "units": {"shares": entries}
        }
        ds, diag = build_dataset(fj, "SYN", n_quarters=8)
        q4s = [p for p in ds.periods if p.fiscal_label.endswith("Q4")]
        assert all(p.shares_diluted is None for p in q4s)
        assert diag.field_by_name("shares_diluted").methods.get("fy_minus_3q") is None


class TestDiagnostics:
    def test_missing_critical_field_warned(self):
        fj = facts_json({"Assets": assets_instants(), "Revenues": quarterly_flows([100.0] * 8)})
        _, diag = build_dataset(fj, "SYN", n_quarters=8)
        assert any("'cfo'" in w for w in diag.warnings)
        assert any("'net_income'" in w for w in diag.warnings)

    def test_too_few_quarters_raises(self):
        fj = facts_json({"Assets": [fact(None, Q_ENDS[0], 1.0)]})
        with pytest.raises(ValueError, match="at least 2 quarter-end dates"):
            build_dataset(fj, "SYN")

    def test_quarter_ends_from_assets(self):
        fj = facts_json({"Assets": assets_instants()})
        assert select_quarter_ends(fj, 4) == [date.fromisoformat(e) for e in Q_ENDS[4:]]
