import unittest
 
from app.extractors.rightmove import (
    _detect_epc_rating,
    _detect_price_qualifier,
    _detect_tenure,
    parse_listing_html,
)
 
# Regression coverage for a real reported bug: listing 91464177 (Otterbrook
# Court, Coventry) is leasehold, NOT shared ownership, but was flagged as
# shared ownership — and its EPC rating ("C", stated in the description
# prose) wasn't picked up at all. Root cause for the false positive: the
# old _detect_price_qualifier searched the WHOLE page for phrases like
# "shared ownership", and real Rightmove pages carry unrelated glossary/
# explainer text mentioning it (e.g. a "types of ownership" blurb far from
# the price). Root cause for the missing EPC/tenure: the jsonld/meta
# fallback path (used by most real listings) never attempted to extract
# either field at all. This fixture reproduces both shapes.
OTTERBROOK_STYLE_HTML = """
<html>
<head>
<meta property="og:description" content="2 bedroom flat for sale in Otterbrook Court, Coventry, CV6 for £160,000.">
<title>2 bedroom flat for sale in Otterbrook Court, Coventry, CV6 - Rightmove</title>
</head>
<body>
<h1>£160,000</h1>
<p>A well presented two bedroom leasehold apartment in a popular Coventry location.</p>
<p>PROPERTY TYPE Flat SIZE Ask agent TENURE Leasehold</p>
<p>The EPC rating of C and falls within Council Tax Band B.</p>
<hr>
<p>Glossary: there are different types of ownership available when buying a home,
including outright ownership, shared ownership, and leasehold arrangements — speak
to your solicitor for advice on which applies to you.</p>
</body>
</html>
"""
 
 
class PriceQualifierWindowingTests(unittest.TestCase):
    def test_does_not_false_positive_on_unrelated_shared_ownership_glossary_text(self):
        # The glossary paragraph mentions "shared ownership" but is nowhere
        # near the price — this must NOT be flagged as a qualifier.
        qualifier = _detect_price_qualifier(OTTERBROOK_STYLE_HTML, 160_000)
        self.assertIsNone(qualifier)
 
    def test_still_detects_a_genuine_qualifier_immediately_before_the_price(self):
        html = "<h1>Guide Price £29,000</h1><p>unrelated shared ownership glossary text far below</p>"
        qualifier = _detect_price_qualifier(html, 29_000)
        self.assertEqual(qualifier, "guide_price")
 
    def test_checks_every_occurrence_of_the_price_not_just_the_first(self):
        # First occurrence (in a meta description) has no qualifier nearby;
        # second occurrence (the page heading) does. Must not stop at the
        # first, qualifier-less occurrence.
        html = (
            '<meta property="og:description" content="...for £29,000. Marketed by Under The Hammer">'
            "<h1>Guide Price £29,000</h1>"
        )
        qualifier = _detect_price_qualifier(html, 29_000)
        self.assertEqual(qualifier, "guide_price")
 
 
class TenureDetectionTests(unittest.TestCase):
    def test_extracts_leasehold_from_structured_tenure_field(self):
        self.assertEqual(_detect_tenure(OTTERBROOK_STYLE_HTML), "Leasehold")
 
    def test_does_not_match_lowercase_glossary_sentence_alone(self):
        html = (
            "<p>there are different types of tenure - freehold, leasehold, "
            "and commonhold - speak to your solicitor.</p>"
        )
        self.assertIsNone(_detect_tenure(html))
 
 
class EpcDetectionTests(unittest.TestCase):
    def test_extracts_rating_from_description_prose(self):
        self.assertEqual(_detect_epc_rating(OTTERBROOK_STYLE_HTML), "C")
 
    def test_returns_none_when_absent(self):
        self.assertIsNone(_detect_epc_rating("<p>No energy information here.</p>"))
 
 
class ParseListingWiresDetectionIntoResultTests(unittest.TestCase):
    def setUp(self):
        self.listing = parse_listing_html(
            OTTERBROOK_STYLE_HTML, "https://www.rightmove.co.uk/properties/91464177"
        )
 
    def test_tenure_is_leasehold_not_shared_ownership(self):
        self.assertEqual(self.listing.tenure, "Leasehold")
 
    def test_epc_rating_is_populated(self):
        self.assertEqual(self.listing.epc_rating, "C")
 
    def test_price_qualifier_is_not_flagged_as_shared_ownership(self):
        self.assertIsNone(self.listing.price_qualifier)
 
    def test_tenure_and_epc_removed_from_fields_missing(self):
        self.assertNotIn("tenure", self.listing.fields_missing)
        self.assertNotIn("epc_rating", self.listing.fields_missing)
 
 
if __name__ == "__main__":
    unittest.main()
 
