"""Offering-cadence reader tests. Prospectus fixtures are verbatim excerpts
from real 2026 EDGAR filings read during the Q2 season (FPS, KTOS)."""

from datetime import date

from app.services.ingestion.offerings import (
    OfferingFiling,
    OfferingsTimeline,
    _classify,
    _parse_prospectus,
    render_offerings_section,
)

# Kratos 424B5, Feb 26 2026 (accession 0001628280-26-012874) — cover page.
KTOS_424B5 = """
14,285,714 Shares Kratos Defense & Security Solutions, Inc. Common Stock
We are offering 14,285,714 shares of our common stock.
Per share Total Public Offering Price $ 84.00 $ 1,199,999,976.00 Underwriting
Discounts and Commissions $ 1.89 $ 26,999,999.46 Proceeds to Kratos Defense &
Security Solutions, Inc. before expenses $ 82.11 $ 1,172,999,976.54
"""

# Forgent 424B4, Jul 2 2026 (accession 0001193125-26-294982) — cover page.
FPS_424B4 = """
43,650,000 Shares Forgent Power Solutions, Inc. Class A Common Stock This
prospectus relates to the sale of (i) 29,094,075 shares of Class A common stock
of Forgent Power Solutions, Inc. by Forgent Parent I LP and Forgent Parent IV LP
(together, the "selling stockholders") and (ii) 14,555,925 shares of Class A
common stock by us. We intend to use the net proceeds we receive from this
offering to indirectly purchase 14,555,925 common units of Forgent Power
Solutions LLC from Opco, and Opco intends to use the net proceeds it receives
from the sale of Opco LLC Interests to us to redeem Opco LLC Interests from the
Existing Opco LLC Owners. We will not receive any of the proceeds from the sale
of shares of Class A common stock by the selling stockholders in this offering.
The public offering price of $49.00 per share was determined by negotiation.
"""


def _filing(form="424B5", kind="takedown"):
    return OfferingFiling(
        form=form,
        filing_date=date(2026, 2, 26),
        accession="0001628280-26-012874",
        primary_doc="doc.htm",
        kind=kind,
    )


class TestClassification:
    def test_takedown_shelf_registration_split(self):
        assert _classify("424B4") == "takedown"
        assert _classify("424B5") == "takedown"
        assert _classify("S-3ASR") == "shelf"
        assert _classify("S-1MEF") == "registration"
        assert _classify("10-Q") is None


class TestProspectusParsing:
    def test_ktos_primary_offering(self):
        f = _filing()
        diags: list[str] = []
        _parse_prospectus(KTOS_424B5, f, diags)
        assert f.price_per_share == 84.00
        assert f.primary_shares == 14_285_714
        assert f.secondary_shares is None
        assert not f.has_selling_stockholders
        assert not f.company_receives_no_secondary_proceeds
        assert diags == []

    def test_fps_mixed_offering_with_passthrough(self):
        f = _filing(form="424B4")
        diags: list[str] = []
        _parse_prospectus(FPS_424B4, f, diags)
        assert f.price_per_share == 49.00
        assert f.primary_shares == 14_555_925
        assert f.secondary_shares == 29_094_075
        assert f.has_selling_stockholders
        assert f.company_receives_no_secondary_proceeds
        assert "will not receive" in f.excerpt.lower()

    def test_missing_price_is_diagnosed_not_guessed(self):
        f = _filing()
        diags: list[str] = []
        _parse_prospectus("no cover page facts here at all", f, diags)
        assert f.price_per_share is None
        assert any("price not found" in d for d in diags)

    def test_total_amount_not_mistaken_for_per_share(self):
        f = _filing()
        _parse_prospectus(
            "Public Offering Price $ 1,199,999,976.00 total", f, []
        )
        assert f.price_per_share is None


class TestTimeline:
    def test_counts_and_recency(self):
        t = OfferingsTimeline(ticker="KTOS", cik=1069258, lookback_months=18)
        t.filings = [
            _filing(),
            OfferingFiling("S-3ASR", date(2026, 2, 26), "a", "b.htm", "shelf"),
        ]
        assert t.takedown_count == 1
        assert t.days_since_last_takedown(today=date(2026, 3, 28)) == 30

    def test_render_flags_secondary_only_supply(self):
        f = _filing(form="424B4")
        _parse_prospectus(FPS_424B4, f, [])
        t = OfferingsTimeline(ticker="FPS", cik=2080126, lookback_months=18, filings=[f])
        md = render_offerings_section(t, current_price=35.84)
        assert "Capital Markets Activity" in md
        assert "supply without balance-sheet benefit" in md
        assert "-26.9%" in md  # 35.84 vs 49.00 deal price

    def test_render_empty_window(self):
        t = OfferingsTimeline(ticker="AMPX", cik=1899287, lookback_months=18)
        assert "No offering-related filings" in render_offerings_section(t)
