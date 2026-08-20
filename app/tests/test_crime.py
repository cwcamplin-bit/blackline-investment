import unittest
from unittest.mock import AsyncMock, patch

from app.engine.crime import (
    CrimeStats,
    CrimeTrend,
    _label,
    _month_offset,
    _summarise,
    fetch_crime_context,
    fetch_crime_stats,
    fetch_crime_stats_with_diagnostics,
    fetch_crime_trend,
)
from app.engine.geocoding import OutcodeGeocode, resolve_outcode
from app.engine.narrative import _crime_fragment, _security_clause, build_strengths_and_risks
from app.engine.comparables import ComparablesResult, RentEstimate
from app.extractors.rightmove import ListingData
from app.engine.financial import analyse_financials
from app.engine.scoring import score_property


def _mock_response(records):
    resp = AsyncMock()
    resp.raise_for_status = lambda: None
    resp.json = lambda: records
    return resp

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


class MonthOffsetTests(unittest.TestCase):
    def test_one_year_back(self):
        self.assertEqual(_month_offset("2026-06", -12), "2025-06")

    def test_wraps_year_boundary(self):
        self.assertEqual(_month_offset("2026-01", -1), "2025-12")

    def test_returns_none_on_unparseable_input(self):
        self.assertIsNone(_month_offset("not-a-month", -12))
        self.assertIsNone(_month_offset(None, -12))


class FetchCrimeTrendTests(unittest.IsolatedAsyncioTestCase):
    def _geocode(self):
        return OutcodeGeocode(outcode="CV6", latitude=52.43, longitude=-1.5, admin_districts=["Coventry"])

    async def test_returns_none_without_geocode_or_current(self):
        current = CrimeStats(total_count=50, month="2026-06", top_categories=[])
        self.assertIsNone(await fetch_crime_trend(None, current))
        self.assertIsNone(await fetch_crime_trend(self._geocode(), None))

    async def test_returns_none_when_current_month_missing(self):
        current = CrimeStats(total_count=50, month=None, top_categories=[])
        self.assertIsNone(await fetch_crime_trend(self._geocode(), current))

    async def test_computes_change_pct_against_same_month_last_year(self):
        current = CrimeStats(total_count=50, month="2026-06", top_categories=[])
        baseline_records = [{"category": "burglary", "month": "2025-06"}] * 40

        async def get_side_effect(url, params=None, **kwargs):
            self.assertEqual(params.get("date"), "2025-06")
            return _mock_response(baseline_records)

        with patch("httpx.AsyncClient.get", AsyncMock(side_effect=get_side_effect)):
            trend = await fetch_crime_trend(self._geocode(), current)

        self.assertIsNotNone(trend)
        self.assertEqual(trend.baseline_month, "2025-06")
        self.assertEqual(trend.baseline_count, 40)
        self.assertEqual(trend.change_pct, 25.0)  # (50-40)/40 * 100

    async def test_returns_none_when_baseline_sample_too_small(self):
        current = CrimeStats(total_count=50, month="2026-06", top_categories=[])
        # Below _MIN_BASELINE_FOR_TREND (10) — a tiny base shouldn't
        # produce a headline-looking percentage swing.
        baseline_records = [{"category": "burglary", "month": "2025-06"}] * 5

        with patch("httpx.AsyncClient.get", AsyncMock(return_value=_mock_response(baseline_records))):
            trend = await fetch_crime_trend(self._geocode(), current)

        self.assertIsNone(trend)

    async def test_returns_none_when_baseline_lookup_fails(self):
        current = CrimeStats(total_count=50, month="2026-06", top_categories=[])
        with patch("httpx.AsyncClient.get", AsyncMock(side_effect=Exception("boom"))):
            trend = await fetch_crime_trend(self._geocode(), current)
        self.assertIsNone(trend)


class FetchCrimeContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_none_none_without_geocode(self):
        stats, trend = await fetch_crime_context(None)
        self.assertIsNone(stats)
        self.assertIsNone(trend)

    async def test_combines_current_stats_and_trend(self):
        geocode = OutcodeGeocode(outcode="CV6", latitude=52.43, longitude=-1.5, admin_districts=["Coventry"])
        current_records = [{"category": "burglary", "month": "2026-06"}] * 50
        baseline_records = [{"category": "burglary", "month": "2025-06"}] * 40

        async def get_side_effect(url, params=None, **kwargs):
            if params and params.get("date"):
                return _mock_response(baseline_records)
            return _mock_response(current_records)

        with patch("httpx.AsyncClient.get", AsyncMock(side_effect=get_side_effect)):
            stats, trend = await fetch_crime_context(geocode)

        self.assertEqual(stats.total_count, 50)
        self.assertIsNotNone(trend)
        self.assertEqual(trend.change_pct, 25.0)


class NarrativeCrimeTrendTests(unittest.TestCase):
    def _setup(self):
        listing = ListingData(
            source_url="https://www.rightmove.co.uk/properties/1",
            address="1 Test St",
            price=200_000,
            property_type="terraced",
            tenure="FREEHOLD",
            epc_rating="C",
        )
        comparables = ComparablesResult(sales=[], method="unavailable")
        rent = RentEstimate(monthly_rent=1000, method="modelled")
        fin = analyse_financials(200_000, monthly_rent=1000)
        scores = score_property(listing, fin, comparables, rent)
        return listing, fin, comparables, rent, scores

    def test_falling_crime_becomes_a_strength(self):
        listing, fin, comparables, rent, scores = self._setup()
        trend = CrimeTrend(
            current_count=40, current_month="2026-06",
            baseline_count=60, baseline_month="2025-06", change_pct=-33.3,
        )
        strengths, risks = build_strengths_and_risks(listing, fin, comparables, rent, scores, None, trend)
        self.assertTrue(any("crime" in s.lower() and "down" in s.lower() for s in strengths))

    def test_rising_crime_becomes_a_risk(self):
        listing, fin, comparables, rent, scores = self._setup()
        trend = CrimeTrend(
            current_count=80, current_month="2026-06",
            baseline_count=50, baseline_month="2025-06", change_pct=60.0,
        )
        strengths, risks = build_strengths_and_risks(listing, fin, comparables, rent, scores, None, trend)
        self.assertTrue(any("crime" in r.lower() and "up" in r.lower() for r in risks))

    def test_small_change_is_neither_a_strength_nor_a_risk(self):
        listing, fin, comparables, rent, scores = self._setup()
        trend = CrimeTrend(
            current_count=52, current_month="2026-06",
            baseline_count=50, baseline_month="2025-06", change_pct=4.0,
        )
        strengths, risks = build_strengths_and_risks(listing, fin, comparables, rent, scores, None, trend)
        self.assertFalse(any("crime" in s.lower() for s in strengths))
        self.assertFalse(any("crime" in r.lower() for r in risks))

    def test_none_trend_is_a_no_op(self):
        listing, fin, comparables, rent, scores = self._setup()
        strengths, risks = build_strengths_and_risks(listing, fin, comparables, rent, scores, None, None)
        self.assertFalse(any("crime" in s.lower() for s in strengths))
        self.assertFalse(any("crime" in r.lower() for r in risks))


if __name__ == "__main__":
    unittest.main()
