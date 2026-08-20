"""Rent estimation and comparable sales.

Two data needs, two different honesty rules:

  * Comparable SALES are presented to the customer as evidence ("12 Ashworth
    Rd sold for £241,000"). We only ever show real, sourced records here —
    if a live lookup fails, this returns an EMPTY list rather than invented
    addresses/prices. Fabricating comparables would make the report
    actively misleading for an investment decision.

  * Estimated RENT is presented as a model output, not a specific record —
    the UI already labels it "Estimated rent". So when a live comparable
    rental search can't be reached, falling back to a documented regional
    yield model is legitimate (the same thing Zoopla's Zed-Index or any
    AVM does), as long as it's flagged as modelled rather than verified.

Both live paths scrape Rightmove (per the "best-effort scraping" approach
agreed for v1) and are the most likely parts of this codebase to need
maintenance, since they depend on Rightmove's search/typeahead internals
rather than a stable public contract. See README for the recommended
upgrade path (a licensed comparables/AVM API or the free HM Land Registry
Price Paid Data for sold prices).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx

from app.config import settings

# Approximate gross rental yield by property type, used only as the
# last-resort fallback when no live rental comparables can be found.
# These are illustrative UK averages and should be reviewed periodically —
# they are intentionally conservative (mid-market) rather than tuned to any
# one city.
_FALLBACK_GROSS_YIELD_BY_TYPE = {
    "terraced": 0.062,
    "terrace": 0.062,
    "semi-detached": 0.055,
    "detached": 0.048,
    "flat": 0.068,
    "apartment": 0.068,
    "maisonette": 0.065,
    "bungalow": 0.052,
}
_DEFAULT_FALLBACK_YIELD = 0.058


@dataclass
class RentEstimate:
    monthly_rent: int
    method: str            # "live_comparables" | "modelled"
    sample_size: int = 0
    note: str = ""


@dataclass
class ComparablesResult:
    sales: list[tuple[str, int]] = field(default_factory=list)   # (address, price)
    method: str = "unavailable"
    note: str = ""


def _fallback_rent_estimate(price: int, property_type: str) -> RentEstimate:
    yield_rate = _FALLBACK_GROSS_YIELD_BY_TYPE.get(property_type.lower(), _DEFAULT_FALLBACK_YIELD)
    annual_rent = price * yield_rate
    return RentEstimate(
        monthly_rent=round(annual_rent / 12),
        method="modelled",
        note=(
            f"No live rental comparables were available, so this is a modelled "
            f"estimate using a {yield_rate*100:.1f}% gross yield assumption for "
            f"{property_type} properties — treat as indicative pending a verified "
            f"rental valuation."
        ),
    )


async def _resolve_location_identifier(client: httpx.AsyncClient, outcode: str) -> str | None:
    """Rightmove's autocomplete endpoint resolves a free-text area (e.g. a
    postcode outcode) to the internal locationIdentifier its search pages
    require. This is an unofficial, undocumented endpoint and the first
    thing likely to need updating if Rightmove changes it."""
    try:
        resp = await client.get(
            "https://los.rightmove.co.uk/typeahead",
            params={"query": outcode, "limit": 1},
            timeout=settings.extractor_timeout_seconds,
        )
        resp.raise_for_status()
        data = resp.json()
        matches = data.get("matches") or []
        if matches:
            return matches[0].get("locationIdentifier")
    except Exception:
        return None
    return None


async def fetch_rent_estimate(
    price: int,
    property_type: str,
    beds: int | None,
    outcode: str | None,
) -> RentEstimate:
    """Best-effort: search Rightmove's to-rent listings for comparable
    properties in the same outcode and take the median asking rent. Falls
    back to a modelled estimate on any failure — this function never
    raises."""
    if not outcode or not beds:
        return _fallback_rent_estimate(price, property_type)

    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": settings.extractor_user_agent},
            timeout=settings.extractor_timeout_seconds,
            follow_redirects=True,
        ) as client:
            location_id = await _resolve_location_identifier(client, outcode)
            if not location_id:
                return _fallback_rent_estimate(price, property_type)

            resp = await client.get(
                "https://www.rightmove.co.uk/api/_search",
                params={
                    "locationIdentifier": location_id,
                    "channel": "RENT",
                    "minBedrooms": beds,
                    "maxBedrooms": beds,
                    "numberOfPropertiesPerPage": 24,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            listings = data.get("properties") or []
            rents = []
            for item in listings:
                amount = (item.get("price") or {}).get("amount")
                if amount:
                    rents.append(int(amount))

            if len(rents) >= 3:
                rents.sort()
                median = rents[len(rents) // 2]
                return RentEstimate(
                    monthly_rent=median,
                    method="live_comparables",
                    sample_size=len(rents),
                    note=f"Median of {len(rents)} comparable {beds}-bed to-let listings in {outcode}.",
                )
    except Exception:
        pass

    return _fallback_rent_estimate(price, property_type)


async def fetch_comparable_sales(
    address: str,
    outcode: str | None,
    beds: int | None,
) -> ComparablesResult:
    """Best-effort: look up recently sold comparables near the listing.
    Returns an EMPTY result (never fabricated data) if nothing verifiable
    can be found — see module docstring for why."""
    if not outcode:
        return ComparablesResult(sales=[], method="unavailable", note="No postcode area available for comparables lookup.")

    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": settings.extractor_user_agent},
            timeout=settings.extractor_timeout_seconds,
            follow_redirects=True,
        ) as client:
            resp = await client.get(f"https://www.rightmove.co.uk/house-prices/{outcode.lower()}.html")
            resp.raise_for_status()
            html = resp.text

        # Sold-price pages embed a results array similarly to the listing
        # page's PAGE_MODEL; reuse the same tolerant "find + regex" approach
        # rather than assuming an exact key name, since this page type is
        # even less stable than the listing page.
        matches = re.findall(
            r'"address":"([^"]{5,80})"[^{}]*?"displayPrice":"£?([\d,]+)"',
            html,
        )
        sales = [(addr, int(price_str.replace(",", ""))) for addr, price_str in matches[:6]]
        if sales:
            return ComparablesResult(
                sales=sales,
                method="live_scrape",
                note=f"{len(sales)} recently sold properties in {outcode}.",
            )
    except Exception:
        pass

    return ComparablesResult(
        sales=[],
        method="unavailable",
        note="Live comparable sales data could not be retrieved for this area.",
    )
