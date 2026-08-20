import unittest
from unittest.mock import AsyncMock, patch

from app.engine import land_registry
from app.engine.comparables import fetch_comparable_sales
from app.engine.land_registry import LandRegistrySale, _sanitise_outcode, fetch_outcode_comparables

# Realistic shape of a SPARQL JSON response from
# landregistry.data.gov.uk/landregistry/query for the outcode-prefix query
# — mirrors the confirmed-working exact-postcode query pattern researched
# during development, adapted with a STRSTARTS filter. bindings intentionally
# include: a normal result, a result missing optional fields (saon/street),
# and a malformed amount (to prove bad rows are skipped, not fatal).
SPARQL_RESPONSE = {
    "head": {"vars": ["paon", "saon", "street", "town", "postcode", "amount", "date"]},
    "results": {
        "bindings": [
            {
                "paon": {"type": "literal", "value": "6"},
                "street": {"type": "literal", "value": "ANSELL DRIVE"},
                "town": {"type": "literal", "value": "COVENTRY"},
                "postcode": {"type": "literal", "value": "CV6 6PQ"},
                "amount": {"type": "literal", "value": "250000"},
                "date": {"type": "literal", "value": "2026-04-16T00:00:00"},
            },
            {
                "paon": {"type": "literal", "value": "35"},
                "street": {"type": "literal", "value": "WOODCLOSE AVENUE"},
                "town": {"type": "literal", "value": "COVENTRY"},
                "postcode": {"type": "literal", "value": "CV6 1HA"},
                "amount": {"type": "literal", "value": "318950"},
                "date": {"type": "literal", "value": "2026-04-16T00:00:00"},
            },
            {
                # Flat with a SAON, no street — still a valid row.
                "paon": {"type": "literal", "value": "153"},
                "saon": {"type": "literal", "value": "FLAT 2"},
                "town": {"type": "literal", "value": "COVENTRY"},
                "postcode": {"type": "literal", "value": "CV6 2AF"},
                "amount": {"type": "literal", "value": "189000"},
                "date": {"type": "literal", "value": "2003-11-14T00:00:00"},
            },
            {
                # Malformed amount — should be skipped, not raise.
                "postcode": {"type": "literal", "value": "CV6 9ZZ"},
                "amount": {"type": "literal", "value": "not-a-number"},
                "date": {"type": "literal", "value": "2026-01-01T00:00:00"},
            },
            {
                # Implausible amount (outside sanity bounds) — should be skipped.
                "postcode": {"type": "literal", "value": "CV6 9ZZ"},
                "amount": {"type": "literal", "value": "5000"},
                "date": {"type": "literal", "value": "2026-01-01T00:00:00"},
            },
        ]
    },
}


class OutcodeSanitisationTests(unittest.TestCase):
    def test_accepts_valid_outcode_shapes(self):
        self.assertEqual(_sanitise_outcode("cv6"), "CV6")
        self.assertEqual(_sanitise_outcode(" SW1A "), "SW1A")
        self.assertEqual(_sanitise_outcode("M20"), "M20")

    def test_rejects_anything_that_is_not_a_bare_outcode(self):
        # Guards against SPARQL/query injection via a malformed address —
        # only a strict outcode shape is ever interpolated into the query.
        self.assertIsNone(_sanitise_outcode('CV6"} UNION { ?x ?y ?z'))
        self.assertIsNone(_sanitise_outcode("CV6 6PQ"))  # full postcode, not an outcode
        self.assertIsNone(_sanitise_outcode(""))
        self.assertIsNone(_sanitise_outcode(None))


class FetchOutcodeComparablesTests(unittest.IsolatedAsyncioTestCase):
    async def test_parses_a_realistic_sparql_response(self):
        mock_response = AsyncMock()
        mock_response.raise_for_status = lambda: None
        mock_response.json = lambda: SPARQL_RESPONSE

        with patch("httpx.AsyncClient.get", AsyncMock(return_value=mock_response)):
            sales = await fetch_outcode_comparables("CV6")

        # 5 bindings in, 2 dropped (bad amount, implausible amount) -> 3 sales.
        self.assertEqual(len(sales), 3)
        self.assertEqual(sales[0].price, 250_000)
        self.assertIn("ANSELL DRIVE", sales[0].address)
        self.assertIn("CV6 6PQ", sales[0].address)
        self.assertEqual(sales[0].date, "2026-04-16")

    async def test_includes_saon_when_present(self):
        mock_response = AsyncMock()
        mock_response.raise_for_status = lambda: None
        mock_response.json = lambda: SPARQL_RESPONSE

        with patch("httpx.AsyncClient.get", AsyncMock(return_value=mock_response)):
            sales = await fetch_outcode_comparables("CV6")

        flat_sale = next(s for s in sales if s.price == 189_000)
        self.assertIn("FLAT 2", flat_sale.address)

    async def test_returns_empty_list_on_invalid_outcode_without_making_a_request(self):
        with patch("httpx.AsyncClient.get", AsyncMock()) as mock_get:
            sales = await fetch_outcode_comparables("not an outcode!")
        self.assertEqual(sales, [])
        mock_get.assert_not_called()

    async def test_returns_empty_list_on_network_failure(self):
        with patch("httpx.AsyncClient.get", AsyncMock(side_effect=Exception("boom"))):
            sales = await fetch_outcode_comparables("CV6")
        self.assertEqual(sales, [])

    async def test_returns_empty_list_on_timeout(self):
        import httpx
        with patch("httpx.AsyncClient.get", AsyncMock(side_effect=httpx.TimeoutException("slow"))):
            sales = await fetch_outcode_comparables("CV6")
        self.assertEqual(sales, [])


class ComparablesFallsBackWhenLandRegistryIsThinTests(unittest.IsolatedAsyncioTestCase):
    """fetch_comparable_sales (comparables.py) should prefer Land Registry
    when it has enough results, and fall back to the existing Rightmove
    scrape otherwise — this is the integration point, not just the parser."""

    async def test_uses_land_registry_when_it_returns_enough_results(self):
        rich_sales = [
            LandRegistrySale(address="6, Ansell Drive, Coventry CV6 6PQ", price=250_000, date="2026-04-16"),
            LandRegistrySale(address="35, Woodclose Avenue, Coventry CV6 1HA", price=318_950, date="2026-04-16"),
            LandRegistrySale(address="153, Hollyfast Road, Coventry CV6 2AF", price=498_950, date="2026-04-15"),
        ]
        with patch("app.engine.land_registry.fetch_outcode_comparables", AsyncMock(return_value=rich_sales)):
            result = await fetch_comparable_sales("some address", "CV6", 2)

        self.assertEqual(result.method, "land_registry")
        self.assertEqual(len(result.sales), 3)
        self.assertIn("Open Government Licence", result.note)

    async def test_falls_back_to_rightmove_scrape_when_land_registry_is_thin(self):
        thin_sales = [
            LandRegistrySale(address="6, Ansell Drive, Coventry CV6 6PQ", price=250_000, date="2026-04-16"),
        ]

        async def fake_get(self, url, *args, **kwargs):
            class FakeResp:
                text = (
                    "6, Ansell Drive, Coventry CV6 6PQ 6, Ansell Drive, Coventry CV6 6PQ "
                    "2 bed Semi-Detached Freehold Today See what it's worth now "
                    "16 Apr 2026 £250,000 "
                    "35, Woodclose Avenue, Coventry CV6 1HA 35, Woodclose Avenue, Coventry CV6 1HA "
                    "3 bed Detached Freehold Today See what it's worth now "
                    "16 Apr 2026 £318,950"
                )

                def raise_for_status(self):
                    return None

            return FakeResp()

        with patch("app.engine.land_registry.fetch_outcode_comparables", AsyncMock(return_value=thin_sales)), \
             patch("httpx.AsyncClient.get", fake_get):
            result = await fetch_comparable_sales("some address", "CV6", 2)

        self.assertEqual(result.method, "live_scrape")
        self.assertGreaterEqual(len(result.sales), 1)

    async def test_falls_back_to_rightmove_scrape_when_land_registry_errors(self):
        async def fake_get(self, url, *args, **kwargs):
            class FakeResp:
                text = (
                    "30, Talland Avenue, Coventry CV6 7NX 30, Talland Avenue, Coventry CV6 7NX "
                    "2 bed Terraced Freehold Today See what it's worth now 15 Apr 2026 £135,000"
                )

                def raise_for_status(self):
                    return None

            return FakeResp()

        with patch("app.engine.land_registry.fetch_outcode_comparables", AsyncMock(side_effect=Exception("boom"))), \
             patch("httpx.AsyncClient.get", fake_get):
            result = await fetch_comparable_sales("some address", "CV6", 2)

        self.assertEqual(result.method, "live_scrape")


if __name__ == "__main__":
    unittest.main()
