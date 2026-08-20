import unittest

from app.engine.comparables import ComparablesResult, RentEstimate
from app.engine.financial import analyse_financials
from app.engine.scoring import score_property
from app.extractors.rightmove import ListingData
from app.schemas import Verdict


def _listing(**overrides) -> ListingData:
    defaults = dict(
        source_url="https://www.rightmove.co.uk/properties/1",
        address="1 Test St, Testville",
        price=200_000,
        beds=3,
        property_type="terraced",
        tenure="FREEHOLD",
        epc_rating="D",
        postcode_outcode="TE1",
        description="A lovely home",
        key_features=[],
        extraction_method="page_model",
        fields_missing=[],
    )
    defaults.update(overrides)
    return ListingData(**defaults)


class ScoringTests(unittest.TestCase):
    def test_strong_deal_scores_highly_across_the_board(self):
        listing = _listing(epc_rating="E", description="renovation potential, extend, no onward chain")
        comparables = ComparablesResult(
            sales=[("2 Test St", 240_000), ("3 Test St", 245_000), ("4 Test St", 238_000)],
            method="live_scrape",
        )
        rent = RentEstimate(monthly_rent=1_400, method="live_comparables", sample_size=8)
        fin = analyse_financials(200_000, monthly_rent=1_400)

        scores = score_property(listing, fin, comparables, rent)

        self.assertGreaterEqual(scores.growth, 70)     # priced well below comps
        self.assertGreaterEqual(scores.value_add, 60)   # poor EPC + renovation language
        self.assertGreaterEqual(scores.security, 70)     # 3 comps, tenure, epc, live rent all present
        self.assertIn(scores.verdict, (Verdict.strongbuy, Verdict.buy))

    def test_weak_deal_scores_poorly(self):
        listing = _listing(epc_rating="D", tenure=None, description="")
        comparables = ComparablesResult(sales=[], method="unavailable")
        rent = RentEstimate(monthly_rent=500, method="modelled")
        fin = analyse_financials(200_000, monthly_rent=500)  # thin rent -> negative cashflow

        scores = score_property(listing, fin, comparables, rent)

        self.assertLess(fin.cashflow_monthly, 0)
        self.assertLessEqual(scores.cashflow, 40)
        self.assertIn(scores.verdict, (Verdict.caution, Verdict.pass_, Verdict.invest))

    def test_missing_comparables_is_neutral_not_penalised_twice(self):
        listing = _listing()
        comparables = ComparablesResult(sales=[], method="unavailable")
        rent = RentEstimate(monthly_rent=1_000, method="modelled")
        fin = analyse_financials(200_000, monthly_rent=1_000)

        scores = score_property(listing, fin, comparables, rent)
        self.assertEqual(scores.growth, 50)  # neutral, evidence gap reflected in confidence instead
        self.assertLess(scores.confidence, 80)

    def test_all_scores_within_bounds(self):
        listing = _listing()
        comparables = ComparablesResult(sales=[("2 Test St", 50_000)], method="live_scrape")  # priced above comps
        rent = RentEstimate(monthly_rent=100, method="modelled")
        fin = analyse_financials(500_000, monthly_rent=100)

        scores = score_property(listing, fin, comparables, rent)
        for value in (scores.growth, scores.value_add, scores.security, scores.cashflow, scores.confidence,
                      scores.strategy_btl, scores.strategy_brrr, scores.strategy_flip):
            self.assertGreaterEqual(value, 0)
            self.assertLessEqual(value, 100)


if __name__ == "__main__":
    unittest.main()
