import unittest

from app.engine.renovation import _COSMETIC_REFRESH, estimate_renovation
from app.extractors.rightmove import ListingData


def _listing(**overrides):
    defaults = dict(
        source_url="https://www.rightmove.co.uk/properties/1",
        address="1 Test St",
        price=200_000,
        beds=3,
        property_type="terraced",
    )
    defaults.update(overrides)
    return ListingData(**defaults)


class BaselineTests(unittest.TestCase):
    def test_plain_listing_gets_only_the_cosmetic_baseline(self):
        estimate = estimate_renovation(_listing())
        self.assertEqual(len(estimate.items), 1)
        self.assertIn("Cosmetic refresh", estimate.items[0].label)

    def test_totals_are_the_sum_of_item_ranges(self):
        estimate = estimate_renovation(_listing(epc_rating="F", description="loft conversion potential"))
        self.assertEqual(estimate.total_low, sum(i.low for i in estimate.items))
        self.assertEqual(estimate.total_high, sum(i.high for i in estimate.items))
        self.assertGreater(len(estimate.items), 1)

    def test_note_and_as_of_are_populated(self):
        estimate = estimate_renovation(_listing())
        self.assertIn("not a", estimate.note.lower())
        self.assertTrue(estimate.as_of)


class EpcTriggerTests(unittest.TestCase):
    def test_poor_epc_adds_energy_efficiency_item(self):
        estimate = estimate_renovation(_listing(epc_rating="F"))
        self.assertTrue(any("Energy efficiency" in i.label for i in estimate.items))

    def test_good_epc_does_not_add_energy_efficiency_item(self):
        estimate = estimate_renovation(_listing(epc_rating="B"))
        self.assertFalse(any("Energy efficiency" in i.label for i in estimate.items))

    def test_missing_epc_does_not_add_energy_efficiency_item(self):
        estimate = estimate_renovation(_listing(epc_rating=None))
        self.assertFalse(any("Energy efficiency" in i.label for i in estimate.items))


class ModernisationKeywordTests(unittest.TestCase):
    def test_needs_renovation_triggers_modernisation_item(self):
        estimate = estimate_renovation(_listing(description="A great opportunity, in need of renovation throughout."))
        self.assertTrue(any("modernisation" in i.label.lower() for i in estimate.items))

    def test_recently_renovated_does_not_trigger_modernisation_item(self):
        estimate = estimate_renovation(_listing(description="A stunning home, recently renovated to a high standard."))
        self.assertFalse(any("modernisation" in i.label.lower() for i in estimate.items))

    def test_potential_keyword_alone_triggers_modernisation_item(self):
        estimate = estimate_renovation(_listing(description="Huge potential to add value throughout."))
        self.assertTrue(any("modernisation" in i.label.lower() for i in estimate.items))

    def test_key_features_are_also_scanned(self):
        estimate = estimate_renovation(_listing(key_features=["Requires modernisation", "Off-street parking"]))
        self.assertTrue(any("modernisation" in i.label.lower() for i in estimate.items))


class LoftKeywordTests(unittest.TestCase):
    def test_loft_potential_triggers_loft_item(self):
        estimate = estimate_renovation(_listing(description="Scope for a loft conversion, subject to planning."))
        self.assertTrue(any("Loft" in i.label for i in estimate.items))

    def test_recently_converted_loft_does_not_trigger_loft_item(self):
        estimate = estimate_renovation(_listing(description="Features a recently converted loft with en-suite."))
        self.assertFalse(any("Loft" in i.label for i in estimate.items))


class ExtensionKeywordTests(unittest.TestCase):
    def test_extension_potential_triggers_extension_item(self):
        estimate = estimate_renovation(_listing(description="Extension potential to the rear, STPP."))
        self.assertTrue(any("extension" in i.label.lower() for i in estimate.items))

    def test_already_extended_does_not_trigger_extension_item(self):
        estimate = estimate_renovation(_listing(description="This home has already been extended to the rear."))
        self.assertFalse(any("extension" in i.label.lower() for i in estimate.items))

    def test_planning_keyword_alone_triggers_extension_item(self):
        estimate = estimate_renovation(_listing(description="Full planning permission granted for a rear addition."))
        self.assertTrue(any("extension" in i.label.lower() for i in estimate.items))


class BedScalingTests(unittest.TestCase):
    def test_three_bed_matches_the_unscaled_baseline_band(self):
        estimate = estimate_renovation(_listing(beds=3))
        cosmetic = estimate.items[0]
        self.assertEqual((cosmetic.low, cosmetic.high), _COSMETIC_REFRESH)

    def test_missing_beds_also_matches_the_unscaled_baseline_band(self):
        estimate = estimate_renovation(_listing(beds=None))
        cosmetic = estimate.items[0]
        self.assertEqual((cosmetic.low, cosmetic.high), _COSMETIC_REFRESH)

    def test_one_bed_scales_down_from_baseline(self):
        estimate = estimate_renovation(_listing(beds=1))
        cosmetic = estimate.items[0]
        self.assertLess(cosmetic.low, _COSMETIC_REFRESH[0])
        self.assertLess(cosmetic.high, _COSMETIC_REFRESH[1])

    def test_five_bed_scales_up_from_baseline(self):
        estimate = estimate_renovation(_listing(beds=5))
        cosmetic = estimate.items[0]
        self.assertGreater(cosmetic.low, _COSMETIC_REFRESH[0])
        self.assertGreater(cosmetic.high, _COSMETIC_REFRESH[1])

    def test_very_large_bed_count_is_capped_not_unbounded(self):
        estimate_8 = estimate_renovation(_listing(beds=8))
        estimate_20 = estimate_renovation(_listing(beds=20))
        # Both are past the cap — should produce the SAME (capped) range,
        # not an ever-growing one that a typo'd bed count could blow up.
        self.assertEqual(
            (estimate_8.items[0].low, estimate_8.items[0].high),
            (estimate_20.items[0].low, estimate_20.items[0].high),
        )

    def test_two_listings_with_no_other_signals_still_differ_by_bed_count(self):
        # This is the real-world scenario a live user hit: two listings
        # with no EPC and no description keywords (the common SEO-fallback
        # extraction path) previously produced byte-identical renovation
        # totals regardless of how different the actual properties were.
        small = estimate_renovation(_listing(beds=1, epc_rating=None, description=""))
        large = estimate_renovation(_listing(beds=5, epc_rating=None, description=""))
        self.assertNotEqual(
            (small.total_low, small.total_high),
            (large.total_low, large.total_high),
        )

    def test_rationale_mentions_bed_count_when_known(self):
        estimate = estimate_renovation(_listing(beds=4))
        self.assertIn("4-bed", estimate.items[0].rationale)

    def test_rationale_omits_bed_mention_when_beds_unknown(self):
        estimate = estimate_renovation(_listing(beds=None))
        self.assertNotIn("-bed", estimate.items[0].rationale)


if __name__ == "__main__":
    unittest.main()
