import unittest
from unittest.mock import AsyncMock, patch

from app.engine.crime import (
    CrimeStats,
    _label,
    _summarise,
    fetch_crime_stats,
    fetch_crime_stats_with_diagnostics,
)
from app.engine.geocoding import OutcodeGeocode, resolve_outcode
from app.engine.narrative import _crime_fragment, _security_clause
from app.engine.comparables import ComparablesResult, RentEstimate

# Realistic shape of data.police.uk's crimes-street response — a flat JSON
# array, each entry a crime record. Trimmed to the fields this codebase
# actually reads (category, month); real records carry more (location,
# outcome_status, ...) which are irrelevant here.
CRIME_RECORDS = (
    [{"category": "anti-social-behaviour", "month": "2026-06"}] * 40
    + [{"category": "violent-crime", "month": "2026-06"}] * 12
    + [{"category": "vehicle-crime", "month": "2026-06"}] * 8
    + [{"category": "bicycle-theft", "month": "2026-06"}] * 3
)


class GeocodingTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_none_without_outcode(self):
        self.assertIsNone(await resolve_outcode(None))
        self.assertIsNone(await resolve_outcode(""))

    async def test_parses_a_realistic_postcodes_io_response(self):
        mock_response = AsyncMock()
        mock_response.raise_for_status = lambda: None
        mock_response.json = lambda: {
            "status": 200,
            "result": {
                "outcode": "CV6",
                "longitude": -1.5073344717348947,
                "latitude": 52.43445488693967,
                "admin_district": ["Coventry", "Nuneaton and Bedworth"],
            },
        }
        with patch("httpx.AsyncClient.get", AsyncMock(return_value=mock_response)):
            geocode = await resolve_outcode("cv6")

        self.assertIsNotNone(geocode)
        self.assertEqual(geocode.outcode, "CV6")
        self.assertAlmostEqual(geocode.latitude, 52.434, places=2)
        self.assertEqual(geocode.admin_districts, ["Coventry", "Nuneaton and Bedworth"])

    async def test_returns_none_when_lat_lng_missing(self):
        mock_response = AsyncMock()
        mock_response.raise_for_status = lambda: None
        mock_response.json = lambda: {"result": {"admin_district": ["Coventry"]}}
        with patch("httpx.AsyncClient.get", AsyncMock(return_value=mock_response)):
            self.assertIsNone(await resolve_outcode("CV6"))

    async def test_returns_none_on_network_failure(self):
        with patch("httpx.AsyncClient.get", AsyncMock(side_effect=Exception("boom"))):
            self.assertIsNone(await resolve_outcode("CV6"))


class CategoryLabelTests(unittest.TestCase):
    def test_known_category_uses_friendly_label(self):
        self.assertEqual(_label("violent-crime"), "violence & sexual offences")

    def test_unknown_category_falls_back_to_hyphen_replacement(self):
        self.assertEqual(_label("some-new-category"), "some new category")


class SummariseTests(unittest.TestCase):
    def test_counts_and_ranks_categories(self):
        stats = _summarise(CRIME_RECORDS)
        self.assertEqual(stats.total_count, 63)
        self.assertEqual(stats.month, "2026-06")
        self.assertEqual(stats.top_categories[0], ("anti-social behaviour", 40))
        self.assertEqual(len(stats.top_categories), 3)

    def test_empty_records_gives_zero_count_not_an_error(self):
        stats = _summarise([])
        self.assertEqual(stats.total_count, 0)
        self.assertEqual(stats.top_categories, [])
        self.assertIsNone(stats.month)


class FetchCrimeStatsTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_none_without_geocode(self):
        self.assertIsNone(await fetch_crime_stats(None))

    async def test_parses_real_response_shape(self):
        mock_response = AsyncMock()
        mock_response.raise_for_status = lambda: None
        mock_response.json = lambda: CRIME_RECORDS
        geocode = OutcodeGeocode(outcode="CV6", latitude=52.43, longitude=-1.5, admin_districts=["Coventry"])

        with patch("httpx.AsyncClient.get", AsyncMock(return_value=mock_response)):
            stats = await fetch_crime_stats(geocode)

        self.assertIsNotNone(stats)
        self.assertEqual(stats.total_count, 63)

    async def test_returns_none_on_non_list_response(self):
        mock_response = AsyncMock()
        mock_response.raise_for_status = lambda: None
        mock_response.json = lambda: {"error": "not found"}
        geocode = OutcodeGeocode(outcode="CV6", latitude=52.43, longitude=-1.5, admin_districts=[])

        with patch("httpx.AsyncClient.get", AsyncMock(return_value=mock_response)):
            self.assertIsNone(await fetch_crime_stats(geocode))

    async def test_returns_none_on_network_failure(self):
        geocode = OutcodeGeocode(outcode="CV6", latitude=52.43, longitude=-1.5, admin_districts=[])
        with patch("httpx.AsyncClient.get", AsyncMock(side_effect=Exception("boom"))):
            self.assertIsNone(await fetch_crime_stats(geocode))

    async def test_zero_crimes_is_a_valid_result_not_a_failure(self):
        mock_response = AsyncMock()
        mock_response.raise_for_status = lambda: None
        mock_response.json = lambda: []
        geocode = OutcodeGeocode(outcode="ZZ1", latitude=0.0, longitude=0.0, admin_districts=[])

        with patch("httpx.AsyncClient.get", AsyncMock(return_value=mock_response)):
            stats = await fetch_crime_stats(geocode)

        self.assertIsNotNone(stats)
        self.assertEqual(stats.total_count, 0)

    async def test_diagnostics_variant_surfaces_the_raw_error(self):
        geocode = OutcodeGeocode(outcode="CV6", latitude=52.43, longitude=-1.5, admin_districts=[])
        with patch("httpx.AsyncClient.get", AsyncMock(side_effect=Exception("boom"))):
            stats, diag = await fetch_crime_stats_with_diagnostics(geocode)
        self.assertIsNone(stats)
        self.assertIn("boom", diag.error)


class NarrativeCrimeFragmentTests(unittest.TestCase):
    def test_fragment_is_empty_without_data(self):
        self.assertEqual(_crime_fragment(None), "")

    def test_fragment_is_descriptive_not_evaluative(self):
        stats = CrimeStats(total_count=63, month="2026-06", top_categories=[("anti-social behaviour", 40)])
        fragment = _crime_fragment(stats)
        self.assertIn("63", fragment)
        self.assertIn("2026-06", fragment)
        self.assertIn("anti-social behaviour", fragment)
        self.assertIn("police.uk", fragment)
        # Should not editorialise with words implying a judgement.
        for word in ("safe", "unsafe", "dangerous", "risky", "high crime", "low crime"):
            self.assertNotIn(word, fragment.lower())

    def test_security_clause_appends_crime_fragment_when_present(self):
        from app.extractors.rightmove import ListingData

        listing = ListingData(source_url="u", address="a", price=100_000, tenure="FREEHOLD")
        comparables = ComparablesResult(sales=[], method="unavailable")
        rent = RentEstimate(monthly_rent=1000, method="modelled")
        stats = CrimeStats(total_count=10, month="2026-06", top_categories=[("burglary", 5)])

        clause = _security_clause(listing, comparables, rent, stats)
        self.assertIn("10 recorded crimes", clause)

    def test_security_clause_unchanged_without_crime_data(self):
        from app.extractors.rightmove import ListingData

        listing = ListingData(source_url="u", address="a", price=100_000, tenure="FREEHOLD")
        comparables = ComparablesResult(sales=[], method="unavailable")
        rent = RentEstimate(monthly_rent=1000, method="modelled")

        clause = _security_clause(listing, comparables, rent, None)
        self.assertNotIn("crime", clause.lower())


if __name__ == "__main__":
    unittest.main()
