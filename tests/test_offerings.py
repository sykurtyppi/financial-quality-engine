"""Offering-cadence reader tests. Prospectus fixtures are verbatim excerpts
from real 2026 EDGAR filings read during the Q2 season (FPS, KTOS)."""

from datetime import date

from app.services.ingestion.offerings import (
    OfferingFiling,
    OfferingsTimeline,
    _classify,
    _parse_prospectus,
    fetch_offerings,
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


# Amazon-style 424B5 bond cover (2026-07-08 filing was a note offering).
AMZN_DEBT_424B5 = """
$1,000,000,000 3.850% Notes due 2029 $1,250,000,000 4.100% Notes due 2031
Amazon.com, Inc. is offering the notes described in this prospectus supplement.
Interest on the notes is payable semi-annually in arrears.
"""

# Pathological cover where the secondary pattern would re-match the primary
# tranche's number (observed on the FPS IPO cover).
DUP_SPAN_COVER = """
We are offering 16,586,427 shares of Class A common stock by us and the
selling stockholders named herein at a public offering price of $27.00 per share.
"""


class TestSecurityType:
    def test_debt_prospectus_classified_and_skipped(self):
        f = _filing(form="424B5")
        diags: list[str] = []
        _parse_prospectus(AMZN_DEBT_424B5, f, diags)
        assert f.security_type == "debt"
        assert f.price_per_share is None
        assert diags == []  # no misleading "price not found" for bonds

    def test_equity_prospectus_classified(self):
        f = _filing()
        _parse_prospectus(FPS_424B4, f, [])
        assert f.security_type == "equity"

    def test_convertible_cover_left_unknown_not_equity(self):
        # Review finding: a convertible-notes cover matches BOTH the debt
        # regex ("notes due") and the equity regex (conversion "shares of
        # common stock"). Defaulting that to "equity" let debt paper read as
        # a sponsor equity sale downstream; both-match must classify unknown
        # with a diagnostic.
        cover = (
            "We are offering $500,000,000 aggregate principal amount of "
            "1.25% convertible senior notes due 2031. The notes will be "
            "convertible into shares of common stock at an initial "
            "conversion rate described herein. Selling stockholders may "
            "also offer shares issuable upon conversion."
        )
        diags: list = []
        f = _filing(form="424B5")
        _parse_prospectus(cover, f, diags)
        assert f.security_type == "unknown"
        assert any("convertible?" in d for d in diags)

    def test_equity_secondary_with_later_debt_mention_stays_equity(self):
        # Audit finding (final pre-merge review): a genuine selling-stockholder
        # secondary whose Use-of-Proceeds boilerplate mentions unrelated
        # existing debt matched both regexes and was blanket-classified
        # "unknown" — with an early return that skipped the selling-stockholder
        # parse. That silently blinded the Capital Integrity caveat to the
        # exact FPS-class sponsor sell-down it exists to catch. The cover names
        # the offered security first, so equity-before-debt must stay equity
        # and keep parsing.
        cover = (
            "14,555,925 shares of Class A common stock offered by the "
            "selling stockholders named herein at a public offering price "
            "of $18.00 per share. We will not receive any of the proceeds "
            "from the sale of shares by the selling stockholders. "
            "Prospectus Summary: we intend to use available cash to redeem "
            "our outstanding 5.00% Senior Notes due 2027."
        )
        diags: list = []
        f = _filing(form="424B7")
        _parse_prospectus(cover, f, diags)
        assert f.security_type == "equity"
        assert f.has_selling_stockholders is True
        assert f.company_receives_no_secondary_proceeds is True
        assert f.secondary_shares == 14_555_925
        assert any("classified equity by cover order" in d for d in diags)

    def test_convertible_resale_with_shares_named_first_stays_unknown(self):
        # Re-review counterexample: convertible-note RESALE prospectuses
        # (424B3) are routinely titled "N Shares of Common Stock Issuable
        # Upon Conversion of $X Y% Convertible Senior Notes due YYYY" — the
        # equity phrase precedes the notes phrase, so a position tie-break
        # classified this "equity" and the selling-stockholder caveat would
        # falsely attribute a notes resale as a sponsor equity sell-down.
        # Convertible language must veto positive equity classification
        # regardless of cover order — while parsing still runs so the
        # evidence stays visible under the honest "unknown" label.
        cover = (
            "6,432,749 Shares of Common Stock Issuable Upon Conversion of "
            "$300,000,000 4.00% Convertible Senior Notes due 2030. This "
            "prospectus relates to the resale, from time to time, by the "
            "selling stockholders identified herein of up to 6,432,749 "
            "shares of our common stock issuable upon conversion of the "
            "notes. We will not receive any proceeds from the sale of the "
            "shares by the selling stockholders."
        )
        diags: list = []
        f = _filing(form="424B3")
        _parse_prospectus(cover, f, diags)
        assert f.security_type == "unknown"
        assert any("convertible" in d for d in diags)
        # Evidence is parsed and visible, but the positive classification —
        # the caveat's gate — is withheld.
        assert f.has_selling_stockholders is True

    def test_convertible_notes_first_still_unknown(self):
        # Debt-named-first covers (the real convertible shape) must NOT be
        # rescued to equity by the position tie-break.
        cover = (
            "$500,000,000 1.25% convertible senior notes due 2031, "
            "convertible into shares of common stock as described herein."
        )
        diags: list = []
        f = _filing(form="424B5")
        _parse_prospectus(cover, f, diags)
        assert f.security_type == "unknown"
        assert any("convertible?" in d for d in diags)

    def test_debt_takedowns_excluded_from_supply_summary(self):
        f = _filing(form="424B5")
        _parse_prospectus(AMZN_DEBT_424B5, f, [])
        t = OfferingsTimeline(ticker="AMZN", cik=1018724, lookback_months=18, filings=[f])
        md = render_offerings_section(t, current_price=232.0)
        assert "0 equity takedown(s)" in md
        assert "1 debt takedown(s)" in md
        assert "supply without balance-sheet benefit" not in md


class TestDoubleGrabGuard:
    def test_overlapping_span_drops_secondary_with_diagnostic(self):
        f = _filing()
        diags: list[str] = []
        _parse_prospectus(DUP_SPAN_COVER, f, diags)
        assert f.primary_shares == 16_586_427
        assert f.secondary_shares is None
        assert any("same span" in d for d in diags)


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


class _FakeClient:
    """Network-free SecClient stand-in for PIT tests (P0-12)."""

    def __init__(self, cik: int, submissions: dict):
        self._cik = cik
        self._subs = submissions

    def resolve_cik(self, ticker: str) -> int:
        return self._cik

    def submissions_by_cik(self, cik: int) -> dict:
        return self._subs


def _subs(rows: list[tuple[str, str, str, str]]) -> dict:
    """Build a companyfacts-style submissions blob from (form, filingDate,
    accession, primaryDoc) rows."""
    return {
        "filings": {
            "recent": {
                "form": [r[0] for r in rows],
                "filingDate": [r[1] for r in rows],
                "accessionNumber": [r[2] for r in rows],
                "primaryDocument": [r[3] for r in rows],
            }
        }
    }


class TestPointInTime:
    """P0-12: the offerings window must be anchored to an explicit as-of date,
    not the wall clock, so a report (or backtest) generated for a past date
    cannot see filings that did not yet exist."""

    def test_as_of_excludes_future_filings(self):
        subs = _subs(
            [
                ("424B5", "2026-01-10", "acc-jan", "jan.htm"),  # in-window
                ("424B5", "2026-07-01", "acc-jul", "jul.htm"),  # after as_of
            ]
        )
        client = _FakeClient(1069258, subs)
        timeline = fetch_offerings(
            client, "KTOS", as_of=date(2026, 3, 1), parse_takedowns=False
        )
        filed = {f.filing_date for f in timeline.filings}
        assert date(2026, 1, 10) in filed
        assert date(2026, 7, 1) not in filed  # future filing must not leak
        assert timeline.as_of == date(2026, 3, 1)

    def test_recency_uses_timeline_as_of_not_wall_clock(self):
        t = OfferingsTimeline(
            ticker="KTOS",
            cik=1069258,
            lookback_months=18,
            filings=[_filing()],  # filed 2026-02-26
            as_of=date(2026, 3, 28),
        )
        # No explicit `today=` — must fall back to the timeline's as_of.
        assert t.days_since_last_takedown() == 30

    def test_render_recency_reflects_as_of(self):
        t = OfferingsTimeline(
            ticker="KTOS",
            cik=1069258,
            lookback_months=18,
            filings=[_filing()],
            as_of=date(2026, 3, 28),
        )
        md = render_offerings_section(t)
        assert "30 days ago" in md
