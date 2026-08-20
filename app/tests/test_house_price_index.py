import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

from app.engine.house_price_index import (
    AreaTrend,
    _build_trend,
    _label_candidates,
    _months_between,
    _parse_month,
    fetch_area_trend,
)
from app.engine.narrative import _area_trend_fragment, build_strengths_and_risks
from app.engine.scoring import _growth_score
from app.engine.comparables import ComparablesResult
from app.extractors.rightmove import ListingData


class LabelCandidateTests(unittest.TestCase):
    def test_strips_city_of_prefix(self):
        self.assertIn("Westminster", _label_candidates("City of Westminster"))

    def test_strips_county_of_suffix(self):
        self.assertIn("Herefordshire", _label_candidates("Herefordshire, County of"))

    def test_plain_name_has_itself_as_only_candidate(self):
        self.assertEqual(_label_candidates("Manchester"), ["Manchester"])

    def test_no_duplicate_candidates(self):
        candidates = _label_candidates("City of Westminster")
        self.assertEqual(len(candidates), len(set(candidates)))


class DateHelperTests(unittest.TestCase):
    def test_parse_month_handles_bare_year_month(self):
        self.assertEqual(_parse_month("2024-06"), date(2024, 6, 1))

    def test_parse_month_handles_full_iso_datetime(self):
        self.assertEqual(_parse_month("2024-06-01T00:00:00"), date(2024, 6, 1))

    def test_parse_month_returns_none_for_garbage(self):
        self.assertIsNone(_parse_month("not-a-date"))

    def test_months_between(self):
        self.assertEqual(_months_between(date(2023, 6, 1), date(2024, 6, 1)), 12)
        self.assertEqual(_months_between(date(2024, 1, 1), date(2024, 6, 1)), 5)


class BuildTrendTests(unittest.TestCase):
    def _monthly_series(self, months: int, start_index: float, monthly_growth_pct: float):
        """Synthetic descending-date series, most recent first, growing at a
        steady compounding rate — lets tests assert on a known % change."""
        points = []
        index = start_index
        indices = []
        d = date(2026, 8, 1)
        for i in range(months):
            indices.append((d, index))
            # step backwards in time, shrinking the index accordingly
            index = index / (1 + monthly_growth_pct / 100)
            month = d.month - 1 or 12
            year = d.year - (1 if d.month == 1 else 0)
            d = date(year, month, 1)
        return [(m, idx, None) for m, idx in indices]

    def test_computes_one_year_change_from_synthetic_series(self):
        # ~0.5%/month compounding ~= a bit over 6%/year
        points = self._monthly_series(65, 150.0, 0.5)
        trend = _build_trend("Coventry", points)
        self.assertIsNotNone(trend)
        self.assertAlmostEqual(trend.one_year_change_pct, 6.2, delta=0.5)

    def test_returns_none_for_empty_points(self):
        self.assertIsNone(_build_trend("Coventry", []))

    def test_omits_five_year_change_when_history_too_short(self):
        points = self._monthly_series(20, 150.0, 0.5)  # < 5 years of data
        trend = _build_trend("Coventry", points)
        self.assertIsNotNone(trend)
        self.assertIsNotNone(trend.one_year_change_pct)
        self.assertIsNone(trend.five_year_change_pct)

    def test_falling_index_gives_negative_change(self):
        points = self._monthly_series(65, 150.0, -0.3)
        trend = _build_trend("Coventry", points)
        self.assertLess(trend.one_year_change_pct, 0)


class FetchAreaTrendIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_none_without_outcode(self):
        self.assertIsNone(await fetch_area_trend(None))
        self.assertIsNone(await fetch_area_trend(""))

    async def test_returns_none_when_postcodes_io_has_no_district(self):
        with patch("app.engine.house_price_index._resolve_admin_districts", AsyncMock(return_value=[])):
            self.assertIsNone(await fetch_area_trend("CV6"))

    async def test_tries_second_district_if_first_yields_nothing(self):
        async def fake_query(district):
            from app.engine.house_price_index import HpiDiagnostics
            if district == "Nuneaton and Bedworth":
                return [(date(2026, 8, 1), 150.0, 200_000), (date(2025, 8, 1), 141.0, 190_000)], HpiDiagnostics(outcode="", districts_tried=[district])
            return [], HpiDiagnostics(outcode="", districts_tried=[district])

        with patch("app.engine.house_price_index._resolve_admin_districts",
                   AsyncMock(return_value=["Coventry", "Nuneaton and Bedworth"])), \
             patch("app.engine.house_price_index._query_region", side_effect=fake_query):
            trend = await fetch_area_trend("CV6")

        self.assertIsNotNone(trend)
        self.assertEqual(trend.region_label, "Nuneaton and Bedworth")


class GrowthScoreWithAreaTrendTests(unittest.TestCase):
    def _listing(self, **overrides):
        defaults = dict(
            source_url="https://www.rightmove.co.uk/properties/1",
            address="1 Test St",
            price=200_000,
            property_type="terraced",
            price_reduced=False,
        )
        defaults.update(overrides)
        return ListingData(**defaults)

    def test_rising_area_nudges_growth_score_up(self):
        listing = self._listing()
        comparables = ComparablesResult(sales=[], method="unavailable")
        no_trend = _growth_score(comparables, 200_000, listing, None)
        rising = AreaTrend("Coventry", "2026-08", 150.0, None, one_year_change_pct=6.0, five_year_change_pct=None)
        with_trend = _growth_score(comparables, 200_000, listing, rising)
        self.assertGreater(with_trend, no_trend)

    def test_falling_area_nudges_growth_score_down(self):
        listing = self._listing()
        comparables = ComparablesResult(sales=[], method="unavailable")
        no_trend = _growth_score(comparables, 200_000, listing, None)
        falling = AreaTrend("Coventry", "2026-08", 150.0, None, one_year_change_pct=-6.0, five_year_change_pct=None)
        with_trend = _growth_score(comparables, 200_000, listing, falling)
        self.assertLess(with_trend, no_trend)

    def test_adjustment_is_capped(self):
        listing = self._listing()
        comparables = ComparablesResult(sales=[], method="unavailable")
        extreme = AreaTrend("Coventry", "2026-08", 150.0, None, one_year_change_pct=500.0, five_year_change_pct=None)
        no_trend = _growth_score(comparables, 200_000, listing, None)
        with_trend = _growth_score(comparables, 200_000, listing, extreme)
        self.assertLessEqual(with_trend - no_trend, 8)


class NarrativeAreaTrendTests(unittest.TestCase):
    def _listing(self, **overrides):
        defaults = dict(
            source_url="https://www.rightmove.co.uk/properties/1",
            address="1 Test St",
            price=200_000,
            property_type="terraced",
            tenure="FREEHOLD",
            epc_rating="C",
        )
        defaults.update(overrides)
        return ListingData(**defaults)

    def test_fragment_mentions_direction_and_region(self):
        rising = AreaTrend("Coventry", "2026-08", 150.0, None, one_year_change_pct=4.2, five_year_change_pct=None)
        fragment = _area_trend_fragment(rising)
        self.assertIn("Coventry", fragment)
        self.assertIn("risen", fragment)
        self.assertIn("4.2%", fragment)

    def test_fragment_empty_when_no_trend(self):
        self.assertEqual(_area_trend_fragment(None), "")

    def test_strong_rise_becomes_a_strength(self):
        from app.engine.financial import analyse_financials
        from app.engine.scoring import score_property
        from app.engine.comparables import RentEstimate

        listing = self._listing()
        comparables = ComparablesResult(sales=[], method="unavailable")
        rent = RentEstimate(monthly_rent=1000, method="modelled")
        fin = analyse_financials(200_000, monthly_rent=1000)
        scores = score_property(listing, fin, comparables, rent)
        rising = AreaTrend("Coventry", "2026-08", 150.0, None, one_year_change_pct=5.0, five_year_change_pct=None)

        strengths, risks = build_strengths_and_risks(listing, fin, comparables, rent, scores, rising)
        self.assertTrue(any("Coventry" in s and "up" in s for s in strengths))

    def test_notable_decline_becomes_a_risk(self):
        from app.engine.financial import analyse_financials
        from app.engine.scoring import score_property
        from app.engine.comparables import RentEstimate

        listing = self._listing()
        comparables = ComparablesResult(sales=[], method="unavailable")
        rent = RentEstimate(monthly_rent=1000, method="modelled")
        fin = analyse_financials(200_000, monthly_rent=1000)
        scores = score_property(listing, fin, comparables, rent)
        falling = AreaTrend("Coventry", "2026-08", 150.0, None, one_year_change_pct=-4.0, five_year_change_pct=None)

        strengths, risks = build_strengths_and_risks(listing, fin, comparables, rent, scores, falling)
        self.assertTrue(any("Coventry" in r and "down" in r for r in risks))


if __name__ == "__main__":
    unittest.main()
