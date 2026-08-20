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


def _parse_from_jsonld_and_meta(html: str, source_url: str) -> ListingData:
    """Fallback: schema.org JSON-LD + OpenGraph meta tags. Much less rich
    than PAGE_MODEL but present on almost every listings page and far less
    likely to change shape, since it's a public SEO contract, not an
    internal one."""
    soup = BeautifulSoup(html, "html.parser")
    missing = ["beds", "epc_rating", "tenure", "key_features", "description"]

    address = None
    price = None

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

    if price is None:
        og_price = soup.find("meta", {"property": "og:price:amount"})
        if og_price and og_price.get("content"):
            price = og_price["content"]

    if address is None:
        og_title = soup.find("meta", {"property": "og:title"})
        if og_title and og_title.get("content"):
            address = og_title["content"].split(" for sale")[0].strip()

    if price is None:
        raise ListingUnavailableError(
            "Could not find a price for this listing via either extraction "
            "method — it may have been sold, let, or removed."
        )
    if isinstance(price, str):
        price = int(re.sub(r"[^\d]", "", price) or 0)

    return ListingData(
        source_url=source_url,
        address=address or "Address unavailable",
        price=int(price),
        extraction_method="jsonld",
        fields_missing=missing + ([] if address else ["address"]),
    )


def parse_listing_html(html: str, source_url: str) -> ListingData:
    """Pure function (no network) — kept separate from fetch_listing() so it
    can be unit-tested against a saved HTML fixture."""
    model = _extract_page_model_json(html)
    if model is not None:
        try:
            return _parse_from_page_model(model, source_url)
        except ListingUnavailableError:
            raise
        except Exception:
            pass  # fall through to the jsonld/meta fallback

    return _parse_from_jsonld_and_meta(html, source_url)


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
