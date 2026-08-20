import os
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from app.extractors.rightmove import ListingData, parse_listing_html
from app.pipeline import run_analysis_from_listing
from app.schemas import AnalyzeRequest

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "rightmove_sample.html")
NO_PAGE_MODEL_FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "rightmove_sample_no_page_model.html"
)


def _sample_listing() -> ListingData:
    return ListingData(
        source_url="https://www.rightmove.co.uk/properties/154829201",
        address="14 Ashworth Road, Manchester, M20",
        price=229_950,
        beds=3,
        baths=1,
        property_type="terraced",
        tenure="FREEHOLD",
        epc_rating="D",
        postcode_outcode="M20",
        description="A well-presented three bedroom terraced home with potential for a light refresh.",
        key_features=["No onward chain", "Potential to extend (STPP)"],
        extraction_method="page_model",
        fields_missing=[],
    )


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_analysis_from_listing_returns_full_contract(self):
        request = AnalyzeRequest(url="https://www.rightmove.co.uk/properties/154829201")
        result = await run_analysis_from_listing(_sample_listing(), request)

        # Matches analyse.html's PROPERTIES[...] shape field-for-field.
        self.assertEqual(result.address, "14 Ashworth Road, Manchester, M20")
        self.assertEqual(result.price, 229_950)
        self.assertEqual(result.beds, 3)
        self.assertIn(result.verdict.value, ("strongbuy", "buy", "invest", "caution", "pass"))
        self.assertEqual(len(result.verdictLabel) > 0, True)
        self.assertTrue(0 <= result.confidence <= 100)
        for score in (result.scores.growth, result.scores.valueAdd, result.scores.security, result.scores.cashflow):
            self.assertTrue(0 <= score <= 100)
        self.assertTrue(result.financials.yieldPct.endswith("%"))
        self.assertTrue(result.financials.roiPct.endswith("%"))
        self.assertGreater(len(result.strengths), 0)
        self.assertGreater(len(result.risks), 0)
        self.assertGreater(len(result.summary), 0)

    async def test_higher_ltv_reduces_deposit_and_raises_mortgage_payment(self):
        listing = _sample_listing()
        low_ltv = await run_analysis_from_listing(
            listing, AnalyzeRequest(url=listing.source_url, ltv_percent=60)
        )
        high_ltv = await run_analysis_from_listing(
            listing, AnalyzeRequest(url=listing.source_url, ltv_percent=85)
        )
        self.assertGreater(low_ltv.financials.deposit, high_ltv.financials.deposit)
        self.assertLess(low_ltv.financials.mortgage, high_ltv.financials.mortgage)


class RealWorldFallbackExtractionTests(unittest.IsolatedAsyncioTestCase):
    """Regression coverage for a real listing (an auction property) that
    Rightmove served without a window.PAGE_MODEL blob, which the original
    extractor couldn't handle — see rightmove_sample_no_page_model.html for
    the exact structure encountered. This is the extraction path that fixed
    a live "could not find a price" failure during development."""

    def setUp(self):
        with open(NO_PAGE_MODEL_FIXTURE_PATH) as f:
            self.html = f.read()

    def test_extracts_price_address_and_beds_from_seo_description(self):
        listing = parse_listing_html(self.html, "https://www.rightmove.co.uk/properties/90650022")
        self.assertEqual(listing.price, 29_000)
        self.assertEqual(listing.address, "32 Pembrook Road, Coventry, CV6 4FD, CV6")
        self.assertEqual(listing.beds, 2)
        self.assertEqual(listing.extraction_method, "jsonld")

    def test_flags_auction_guide_price_as_a_qualifier(self):
        listing = parse_listing_html(self.html, "https://www.rightmove.co.uk/properties/90650022")
        self.assertEqual(listing.price_qualifier, "guide_price")

    async def test_guide_price_produces_a_leading_risk_and_summary_caveat(self):
        listing = parse_listing_html(self.html, "https://www.rightmove.co.uk/properties/90650022")
        result = await run_analysis_from_listing(listing, AnalyzeRequest(url=listing.source_url))
        self.assertTrue(any("Guide Price" in r for r in result.risks))
        self.assertIn("not a firm asking price", result.summary)


class HttpEndpointTests(unittest.TestCase):
    def setUp(self):
        with open(FIXTURE_PATH) as f:
            self.fixture_html = f.read()

        async def fake_fetch_html(url):
            return self.fixture_html

        self.patcher = patch("app.extractors.rightmove._fetch_html", fake_fetch_html)
        self.patcher.start()
        from app.main import app  # imported after patching so the route module sees the stub
        self.client = TestClient(app)

    def tearDown(self):
        self.patcher.stop()

    def test_health(self):
        resp = self.client.get("/api/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok"})

    def test_analyze_happy_path_matches_frontend_contract(self):
        resp = self.client.post("/api/analyze", json={"url": "rightmove.co.uk/properties/154829201"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        expected_top_level_keys = {
            "address", "price", "beds", "type", "sourceUrl", "verdict", "verdictLabel",
            "confidence", "scores", "clauses", "financials", "strategy", "comparables",
            "strengths", "risks", "summary", "renovation", "assumptions", "data_quality",
        }
        self.assertEqual(expected_top_level_keys, set(body.keys()))
        self.assertEqual(set(body["scores"].keys()), {"growth", "valueAdd", "security", "cashflow"})
        self.assertEqual(
            set(body["financials"].keys()),
            {"purchase", "stampDuty", "deposit", "mortgage", "rent", "cashflow", "yieldPct", "roiPct"},
        )
        self.assertEqual(set(body["strategy"].keys()), {"btl", "brrr", "flip"})
        self.assertEqual(
            set(body["renovation"].keys()),
            {"items", "totalLow", "totalHigh", "asOf", "note"},
        )
        self.assertGreaterEqual(len(body["renovation"]["items"]), 1)
        self.assertEqual(
            set(body["renovation"]["items"][0].keys()),
            {"label", "low", "high", "rationale"},
        )

    def test_analyze_rejects_non_rightmove_url(self):
        resp = self.client.post("/api/analyze", json={"url": "https://www.zoopla.co.uk/for-sale/details/1"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "invalid_url")

    def test_analyze_rejects_missing_url(self):
        resp = self.client.post("/api/analyze", json={})
        self.assertEqual(resp.status_code, 422)

    def test_analyze_rejects_malformed_json(self):
        resp = self.client.post("/api/analyze", content=b"not json", headers={"content-type": "application/json"})
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
