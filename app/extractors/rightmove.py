"""Extracts structured listing data from a Rightmove property page.
 
Rightmove renders property detail pages with a large JSON blob embedded in
a <script> tag as `window.PAGE_MODEL = {...}`. This is the same approach
used by most Rightmove scraping tools. Because that structure is owned by
Rightmove and can change without notice, extraction is layered:
 
  1. Primary:   parse window.PAGE_MODEL JSON (richest data).
  2. Fallback:  parse embedded JSON-LD (schema.org) + OpenGraph meta tags
                (present on virtually every listings site for SEO, and far
                more stable than any one site's internal data model).
  3. Failure:   raise ExtractionError with a clear, specific reason — the
                API layer turns this into a structured error the caller can
                act on, rather than a silent/garbage result.
 
This module never crashes the pipeline on a partially-broken page: any
field it can't find comes back as None and is reflected in the returned
`data_quality` notes rather than raising, *except* for price and address,
which are load-bearing for every downstream calculation.
"""
from __future__ import annotations
 
import asyncio
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlunparse
 
import httpx
from bs4 import BeautifulSoup
 
from app.config import settings
 
 
class ExtractionError(Exception):
    """Raised when a listing URL cannot be turned into usable data."""
 
 
class InvalidListingUrlError(ExtractionError):
    pass
 
 
class ListingUnavailableError(ExtractionError):
    """The page loaded but the listing is gone (sold/let/removed)."""
 
 
@dataclass
class ListingData:
    source_url: str
    address: str
    price: int
    beds: int | None = None
    baths: int | None = None
    property_type: str = "property"
    tenure: str | None = None
    epc_rating: str | None = None
    postcode_outcode: str | None = None
    description: str = ""
    key_features: list[str] = field(default_factory=list)
    lat: float | None = None
    lng: float | None = None
    days_on_market: int | None = None
    price_reduced: bool = False
    price_qualifier: str | None = None   # "guide_price" | "offers_over" | "offers_in_region_of" | "fixed_price" | "shared_ownership" | None
    extraction_method: str = "unknown"   # "page_model" | "jsonld" | "partial"
    fields_missing: list[str] = field(default_factory=list)
 
 
def normalise_url(raw_url: str) -> str:
    """Accepts URLs with or without a scheme (the front end's placeholder
    input is bare, e.g. "rightmove.co.uk/properties/154829201")."""
    raw_url = raw_url.strip()
    if not re.match(r"^https?://", raw_url, re.I):
        raw_url = "https://" + raw_url
 
    parsed = urlparse(raw_url)
    host = parsed.netloc.lower()
    if not (host == "rightmove.co.uk" or host.endswith(".rightmove.co.uk")):
        raise InvalidListingUrlError(
            f"'{host or raw_url}' is not a rightmove.co.uk URL. "
            "Blackline currently supports Rightmove listings only."
        )
    if "/properties/" not in parsed.path:
        raise InvalidListingUrlError(
            "That looks like a Rightmove URL but not a single property listing "
            "page (expected a path containing '/properties/<id>')."
        )
    # Force https + canonical host, strip tracking query params.
    return urlunparse((("https"), "www.rightmove.co.uk", parsed.path, "", "", ""))
 
 
def _extract_page_model_json(html: str) -> dict | None:
    """Finds `window.PAGE_MODEL = {...};` and extracts the JSON object with
    brace-matching rather than a greedy regex, so it survives nested braces
    inside the payload (which is guaranteed, given it's a full property
    record)."""
    marker = "window.PAGE_MODEL"
    idx = html.find(marker)
    if idx == -1:
        return None
    brace_start = html.find("{", idx)
    if brace_start == -1:
        return None
 
    depth = 0
    in_string = False
    string_char = ""
    escape = False
    for i in range(brace_start, len(html)):
        ch = html[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == string_char:
                in_string = False
        else:
            if ch in ('"', "'"):
                in_string = True
                string_char = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = html[brace_start : i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        return None
    return None
 
 
def _parse_from_page_model(model: dict, source_url: str) -> ListingData:
    prop = model.get("propertyData") or {}
    missing: list[str] = []
 
    address_info = prop.get("address") or {}
    address = address_info.get("displayAddress") or prop.get("displayAddress")
    if not address:
        missing.append("address")
        address = "Address unavailable"
 
    price_info = prop.get("prices") or prop.get("price") or {}
    price = price_info.get("primaryPrice") or price_info.get("amount")
    if isinstance(price, str):
        price = int(re.sub(r"[^\d]", "", price) or 0)
    if not price:
        raise ListingUnavailableError(
            "Could not find a price on this listing — it may have been sold, "
            "let, or removed from Rightmove."
        )
 
    beds = prop.get("bedrooms")
    baths = prop.get("bathrooms")
    property_sub_type = (prop.get("propertySubType") or prop.get("propertyType") or "property")
 
    tenure = None
    tenure_info = prop.get("tenure") or {}
    if isinstance(tenure_info, dict):
        tenure = tenure_info.get("tenureType")
 
    epc_rating = None
    epc = prop.get("epcGraphs") or prop.get("epcChart")
    if isinstance(epc, list) and epc:
        epc_rating = epc[0].get("rating") if isinstance(epc[0], dict) else None
 
    postcode_outcode = None
    if isinstance(address_info, dict):
        postcode_outcode = address_info.get("outcode")
    if not postcode_outcode:
        postcode_outcode = _extract_outcode_from_address(address)
 
    description = ""
    text_block = prop.get("text") or {}
    if isinstance(text_block, dict):
        description = re.sub(r"<[^>]+>", " ", text_block.get("description") or "")
        description = re.sub(r"\s+", " ", description).strip()
 
    key_features = prop.get("keyFeatures") or []
 
    location = prop.get("location") or {}
    lat, lng = location.get("latitude"), location.get("longitude")
 
    listing_history = prop.get("listingHistory") or {}
    price_reduced = bool(listing_history.get("reducedDate") if isinstance(listing_history, dict) else False)
 
    for f_name, f_val in (("beds", beds), ("epc_rating", epc_rating), ("tenure", tenure)):
        if f_val is None:
            missing.append(f_name)
 
    return ListingData(
        source_url=source_url,
        address=address,
        price=int(price),
        beds=beds,
        baths=baths,
        property_type=str(property_sub_type).lower(),
        tenure=tenure,
        epc_rating=epc_rating,
        postcode_outcode=postcode_outcode,
        description=description,
        key_features=key_features,
        lat=lat,
        lng=lng,
        price_reduced=price_reduced,
        extraction_method="page_model",
        fields_missing=missing,
    )
 
 
# Rightmove's SEO description/OG/Twitter meta tags are consistently
# templated as "<N> bedroom <type> for sale in <address> for £<price>."
# This turned out to be far more reliable in practice than og:price:amount
# (frequently absent) or og:title (too generic — "Check out this 2 bedroom
# house for sale on Rightmove", no address at all). Verified against a live
# listing during development; see README for how this was found.
_DESCRIPTION_PATTERN = re.compile(
    r"(\d+)\s+bedroom\s+(.+?)\s+for sale in\s+(.+?)\s+for\s+£\s?([\d,]+)", re.I
)
_TITLE_ONLY_PATTERN = re.compile(
    r"(\d+)\s+bedroom\s+(.+?)\s+for sale in\s+(.+?)(?:\s*[-|]\s*Rightmove)?\s*$", re.I
)
 
_PRICE_QUALIFIER_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"guide\s+price", re.I), "guide_price"),
    (re.compile(r"offers?\s+in\s+excess\s+of", re.I), "offers_in_excess_of"),
    (re.compile(r"\boiro\b|offers?\s+in\s+the\s+region\s+of", re.I), "offers_in_region_of"),
    (re.compile(r"offers?\s+over", re.I), "offers_over"),
    (re.compile(r"shared\s+ownership", re.I), "shared_ownership"),
    (re.compile(r"fixed\s+price", re.I), "fixed_price"),
]
 
 
_BARE_OUTCODE_PATTERN = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?$")
 
 
def _extract_outcode_from_address(address: str | None) -> str | None:
    """Rightmove display addresses end with the postcode outcode — either
    bare ("...Coventry, CV6") or as the first token of a full postcode
    ("...Coventry, CV6 4FD, CV6"), for privacy (the full postcode is rarely
    shown). This is load-bearing: without an outcode, both live comparable
    lookups (comparables.py) have nothing to search on and silently no-op —
    that turned out to be the actual reason confidence looked capped
    regardless of which property was analysed, since the jsonld/meta
    fallback path (used by most real listings) never set this field."""
    if not address:
        return None
    for part in reversed([p.strip() for p in address.split(",")]):
        tokens = part.split()
        if tokens and _BARE_OUTCODE_PATTERN.match(tokens[0].upper()):
            return tokens[0].upper()
    return None
 
 
def _detect_price_qualifier(html: str, price: int) -> str | None:
    """Auction listings in particular show a "Guide Price" rather than a
    firm asking price — the eventual sale price is very likely to exceed
    it. This matters financially (it's the input to every downstream
    calculation), so it's surfaced rather than silently treated as a normal
    asking price.
 
    Rightmove pages carry generic glossary/explainer blurbs for various
    fields (e.g. "there are different types of tenure — freehold,
    leasehold..."), and this function used to search the WHOLE page for
    these phrases, which caused a real false positive: "shared ownership"
    flagged on a property that has nothing to do with it, almost certainly
    picked up from unrelated boilerplate elsewhere on the page rather than
    anything about that specific listing. Now scoped to a small window
    immediately before the price's own display — where Rightmove actually
    renders a qualifier as a prefix, e.g. "Guide Price £29,000" — rather
    than the whole document.
 
    The price string can legitimately appear more than once (e.g. once in
    an SEO meta description with no qualifier nearby, again in the page's
    own visible heading right after "Guide Price") — so every occurrence is
    checked, not just the first, and the first one with a qualifier in its
    preceding window wins."""
    price_str = f"{price:,}"
    start = 0
    while True:
        idx = html.find(price_str, start)
        if idx == -1:
            return None
        window = html[max(0, idx - 80) : idx]
        for pattern, label in _PRICE_QUALIFIER_PATTERNS:
            if pattern.search(window):
                return label
        start = idx + 1
 
 
_TENURE_PATTERN = re.compile(r"\bTENURE\b\s{0,3}[:\-]?\s{0,3}(Leasehold|Freehold|Share of Freehold|Commonhold)\b")
_EPC_TEXT_PATTERN = re.compile(r"(?:EPC|energy)\s+rating\s+(?:of\s+|is\s+|[:\-]\s*)?([A-G])\b", re.I)
 
 
def _detect_tenure(html: str) -> str | None:
    """Rightmove renders tenure as a tight "TENURE Leasehold"-style label/
    value pair in a property-facts section — deliberately case-sensitive on
    the "TENURE" label and a short gap, so this doesn't also match the
    much longer, lower-case glossary sentence explaining what tenure means
    ("...types of tenure - freehold, leasehold, and commonhold"), which sits
    on the same page and would otherwise produce a false/arbitrary answer."""
    m = _TENURE_PATTERN.search(html)
    return m.group(1) if m else None
 
 
def _detect_epc_rating(html: str) -> str | None:
    """EPC rating is often stated in prose within the property's own
    description ("...EPC rating of C...") rather than a structured field —
    confirmed against a real listing during development, where the
    structured PAGE_MODEL path this originally relied on wasn't present."""
    m = _EPC_TEXT_PATTERN.search(html)
    return m.group(1).upper() if m else None
 
 
def _parse_from_jsonld_and_meta(html: str, source_url: str) -> ListingData:
    """Fallback: schema.org JSON-LD, then Rightmove's templated SEO
    description text, then OpenGraph meta tags. Much less rich than
    PAGE_MODEL but present on virtually every listing page and far more
    stable, since it's a public SEO contract rather than an internal one."""
    soup = BeautifulSoup(html, "html.parser")
    missing = ["epc_rating", "tenure", "key_features"]
 
    address = None
    price = None
    beds = None
    property_type = "property"
 
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "{}")
        except json.JSONDecodeError:
            continue
        candidates = data if isinstance(data, list) else [data]
        for c in candidates:
            if not isinstance(c, dict):
                continue
            offers = c.get("offers") or {}
            if isinstance(offers, dict) and offers.get("price"):
                price = offers.get("price")
            if c.get("name") and not address:
                address = c.get("name")
 
    description_text = ""
    for selector in ({"property": "og:description"}, {"name": "twitter:description"}, {"name": "description"}):
        tag = soup.find("meta", selector)
        if tag and tag.get("content"):
            description_text = tag["content"]
            break
 
    m = _DESCRIPTION_PATTERN.search(description_text)
    if m:
        beds = int(m.group(1))
        property_type = m.group(2).strip().lower()
        if address is None:
            address = m.group(3).strip().rstrip(".")
        if price is None:
            price = m.group(4)
    elif address is None and soup.title and soup.title.string:
        m2 = _TITLE_ONLY_PATTERN.search(soup.title.string.strip())
        if m2:
            beds = int(m2.group(1))
            property_type = m2.group(2).strip().lower()
            address = m2.group(3).strip().rstrip(".")
 
    if price is None:
        og_price = soup.find("meta", {"property": "og:price:amount"})
        if og_price and og_price.get("content"):
            price = og_price["content"]
 
    if price is None:
        raise ListingUnavailableError(
            "Could not find a price for this listing via either extraction "
            "method — it may have been sold, let, or removed."
        )
    if isinstance(price, str):
        price = int(re.sub(r"[^\d]", "", price) or 0)
 
    if beds is None:
        missing.append("beds")
    if not address:
        missing.append("address")
    if not description_text:
        missing.append("description")
 
    return ListingData(
        source_url=source_url,
        address=address or "Address unavailable",
        price=int(price),
        beds=beds,
        property_type=property_type,
        description=description_text,
        postcode_outcode=_extract_outcode_from_address(address),
        extraction_method="jsonld",
        fields_missing=missing,
    )
 
 
def parse_listing_html(html: str, source_url: str) -> ListingData:
    """Pure function (no network) — kept separate from fetch_listing() so it
    can be unit-tested against a saved HTML fixture."""
    listing: ListingData | None = None
 
    model = _extract_page_model_json(html)
    if model is not None:
        try:
            listing = _parse_from_page_model(model, source_url)
        except ListingUnavailableError:
            raise
        except Exception:
            listing = None  # fall through to the jsonld/meta fallback
 
    if listing is None:
        listing = _parse_from_jsonld_and_meta(html, source_url)
 
    listing.price_qualifier = _detect_price_qualifier(html, listing.price)
 
    # The page_model path already fills tenure/EPC when Rightmove's richer
    # data blob is present; the jsonld/meta fallback path (used by most real
    # listings, per README) never attempted either. Both are commonly stated
    # in the listing's own description prose or a structured facts strip
    # even when PAGE_MODEL is absent, so detect them here regardless of
    # which path produced `listing`, only overwriting genuinely missing
    # values (never second-guessing a value the richer path already found).
    if listing.tenure is None:
        detected_tenure = _detect_tenure(html)
        if detected_tenure:
            listing.tenure = detected_tenure
            if "tenure" in listing.fields_missing:
                listing.fields_missing.remove("tenure")
 
    if listing.epc_rating is None:
        detected_epc = _detect_epc_rating(html)
        if detected_epc:
            listing.epc_rating = detected_epc
            if "epc_rating" in listing.fields_missing:
                listing.fields_missing.remove("epc_rating")
 
    return listing
 
 
async def _fetch_html(url: str) -> str:
    """Fetches the listing page with a small manual retry/backoff loop for
    transient network errors (kept dependency-free rather than pulling in
    tenacity, since httpx + asyncio.sleep covers this narrow need)."""
    headers = {
        "User-Agent": settings.extractor_user_agent,
        "Accept-Language": "en-GB,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml",
    }
    attempts = settings.extractor_max_retries + 1
    last_transport_error: Exception | None = None
 
    for attempt in range(attempts):
        try:
            async with httpx.AsyncClient(timeout=settings.extractor_timeout_seconds, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
        except httpx.TransportError as exc:
            last_transport_error = exc
            if attempt < attempts - 1:
                await asyncio.sleep(min(0.5 * (2 ** attempt), 4))
                continue
            raise ExtractionError(
                f"Could not reach Rightmove after {attempts} attempt(s): {exc}"
            ) from exc
 
        if resp.status_code == 404:
            raise ListingUnavailableError("Rightmove returned 404 — this listing no longer exists.")
        if resp.status_code in (403, 429):
            raise ExtractionError(
                f"Rightmove blocked this request (HTTP {resp.status_code}). "
                "This usually means the requesting IP needs to look more like a "
                "real browser, or is being rate-limited — see README for mitigation."
            )
        resp.raise_for_status()
        return resp.text
 
    # Unreachable in practice, but keeps type-checkers happy.
    raise ExtractionError(f"Could not reach Rightmove: {last_transport_error}")
 
 
async def fetch_listing(raw_url: str) -> ListingData:
    """End-to-end: validate URL, fetch page, parse listing."""
    url = normalise_url(raw_url)
    html = await _fetch_html(url)
    return parse_listing_html(html, url)
 
