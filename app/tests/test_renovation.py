import unittest

from app.engine.renovation import estimate_renovation
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


if __name__ == "__main__":
    unittest.main()
