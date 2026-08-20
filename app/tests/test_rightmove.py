import unittest

from bs4 import BeautifulSoup

from app.engine.renovation import estimate_renovation
from app.extractors.rightmove import (
    _detect_epc_rating,
    _detect_price_qualifier,
    _detect_tenure,
    _extract_full_description,
    _extract_key_features,
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


# Regression coverage for a real reported issue: listing 87541464
# (Stiffords Bridge, Cradley, Malvern WR13) genuinely needs substantial
# renovation — its own description explicitly says so — but the
# renovation-estimate module showed only the baseline cosmetic-refresh
# figure, because the jsonld/meta fallback path only ever captured
# Rightmove's tiny fixed-template SEO snippet ("4 bedroom house for sale
# in ... for £400,000"), never the page's own "Description"/"Key
# features" sections where the real condition language actually lives.
# This is a SYNTHETIC fixture (built from what a live fetch of that page
# reported, not a captured raw HTML file — the sandbox this was built in
# can't fetch rightmove.co.uk directly) — see rightmove.py's
# _extract_full_description docstring for that caveat, and check
# GET /api/debug/extraction?url=... on the live deployment to confirm
# against the real page structure.
WR13_STYLE_HTML = """
<html>
<head>
<meta property="og:description" content="4 bedroom house for sale in Stiffords Bridge, Cradley, Malvern, WR13 for £400,000. Marketed by Halls Estate Agents, Kidderminster">
<title>4 bedroom house for sale in Stiffords Bridge, Cradley, Malvern, WR13 - Rightmove</title>
</head>
<body>
<h1>Offers in Region of £400,000</h1>
<h2>Description</h2>
<p>A substantial and characterful four-bedroom detached property, formerly a public house,
with change of use to residential granted in July 2011.</p>
<p>The property is in need of renovation and modernisation, providing multiple reception
rooms with exposed oak beam and stone internal walls throughout the property.</p>
<p>The ground floor has ample scope for substantial renovation, subject to any necessary
consents, together with a cellar and conservatory.</p>
<h2>Key features</h2>
<ul>
<li>Substantial detached property</li>
<li>Four bedrooms</li>
<li>In need of renovation and modernisation</li>
<li>1.44 acre paddock</li>
</ul>
<h2>Council Tax</h2>
<p>This property is in Council Tax Band F — this paragraph must never be picked up as part
of the description, since it sits in an unrelated section.</p>
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


class FullDescriptionSectionExtractionTests(unittest.TestCase):
    def test_extracts_text_after_the_description_heading(self):
        soup = BeautifulSoup(WR13_STYLE_HTML, "html.parser")
        text = _extract_full_description(soup)
        self.assertIn("in need of renovation and modernisation", text)
        self.assertIn("ample scope for substantial renovation", text)

    def test_stops_before_the_next_section_heading(self):
        soup = BeautifulSoup(WR13_STYLE_HTML, "html.parser")
        text = _extract_full_description(soup)
        self.assertNotIn("Council Tax Band F", text)

    def test_returns_empty_string_when_no_description_heading_present(self):
        soup = BeautifulSoup("<html><body><p>No heading here.</p></body></html>", "html.parser")
        self.assertEqual(_extract_full_description(soup), "")


class KeyFeaturesSectionExtractionTests(unittest.TestCase):
    def test_extracts_list_items_after_the_key_features_heading(self):
        soup = BeautifulSoup(WR13_STYLE_HTML, "html.parser")
        features = _extract_key_features(soup)
        self.assertIn("Four bedrooms", features)
        self.assertIn("1.44 acre paddock", features)

    def test_returns_empty_list_when_no_heading_present(self):
        soup = BeautifulSoup("<html><body><p>No heading here.</p></body></html>", "html.parser")
        self.assertEqual(_extract_key_features(soup), [])


class FullDescriptionFeedsIntoParsedListingTests(unittest.TestCase):
    """End-to-end: reproduces the real reported bug (listing 87541464)
    and confirms the fix — the renovation module now sees a real
    modernisation signal instead of only the baseline cosmetic-refresh
    figure, because the description it receives is no longer just the
    tiny fixed SEO snippet."""

    def setUp(self):
        self.listing = parse_listing_html(
            WR13_STYLE_HTML, "https://www.rightmove.co.uk/properties/87541464"
        )

    def test_full_description_text_reaches_the_listing(self):
        self.assertIn("renovation and modernisation", self.listing.description)

    def test_key_features_are_populated_and_removed_from_missing(self):
        self.assertIn("Four bedrooms", self.listing.key_features)
        self.assertNotIn("key_features", self.listing.fields_missing)

    def test_renovation_estimate_now_includes_modernisation_not_just_baseline(self):
        estimate = estimate_renovation(self.listing)
        labels = [item.label for item in estimate.items]
        self.assertIn("Kitchen & bathroom modernisation", labels)
        # Confirms the actual regression: previously this would have been
        # ONLY the baseline cosmetic-refresh item.
        self.assertGreater(len(estimate.items), 1)


if __name__ == "__main__":
    unittest.main()
