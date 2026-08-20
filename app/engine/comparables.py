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
 
Both live paths scrape rendered Rightmove HTML pages — not Rightmove's
internal `/api/*` endpoints, which its robots.txt explicitly disallows
(`Disallow: /api/*`). `/house-prices/<outcode>.html` and
`/property-to-rent/find.html` are not in that disallow list, so this stays
within what Rightmove has told crawlers is acceptable, at the cost of
parsing rendered text rather than a clean JSON contract — the most likely
part of this codebase to need maintenance if Rightmove changes its page
layout. See README for the recommended upgrade path (a licensed
comparables/AVM API, or HM Land Registry Price Paid Data for sold prices).
"""
from __future__ import annotations
 
import re
from dataclasses import dataclass, field
 
import httpx
from bs4 import BeautifulSoup
 
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
 
_UK_POSTCODE = r"[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}"
 
 
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
    """Rightmove's autocomplete endpoint (a separate subdomain, not covered
    by rightmove.co.uk's robots.txt /api/* disallow) resolves a free-text
    area to the internal locationIdentifier its search pages require. It
    returns matches like {"id": "573", "type": "OUTCODE", ...} — the
    identifier search pages actually expect is "<type>^<id>", e.g.
    "OUTCODE^573", not a field returned directly by this endpoint."""
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
            m = matches[0]
            if m.get("id") and m.get("type"):
                return f"{m['type']}^{m['id']}"
    except Exception:
        return None
    return None
 
 
_RENT_PRICE_PATTERN = re.compile(r"£\s?([\d,]+)\s*pcm", re.I)
 
 
async def fetch_rent_estimate(
    price: int,
    property_type: str,
    beds: int | None,
    outcode: str | None,
) -> RentEstimate:
    """Best-effort: render Rightmove's to-rent search results for the same
    outcode/bed count and take the median advertised rent. Falls back to a
    modelled estimate on any failure — this function never raises."""
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
                "https://www.rightmove.co.uk/property-to-rent/find.html",
                params={
                    "locationIdentifier": location_id,
                    "minBedrooms": beds,
                    "maxBedrooms": beds,
                },
            )
            resp.raise_for_status()
            text = BeautifulSoup(resp.text, "html.parser").get_text(separator=" ", strip=True)
 
        rents = [
            int(m.replace(",", ""))
            for m in _RENT_PRICE_PATTERN.findall(text)
        ]
        # Sanity-bound: discard anything that isn't a plausible monthly rent
        # (guards against stray matches from unrelated page content).
        rents = [r for r in rents if 100 <= r <= 20_000]
 
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
 
 
# Sold-price pages render each result as an address ending in a UK
# postcode, followed shortly after (within the same result card — a sold
# date and property type typically sit in between) by a "£<price>" figure —
# e.g. "6, Ansell Drive, Coventry CV6 6PQ ... £250,000". Parsed from the
# page's flattened visible text (via get_text()) rather than assumed JSON
# key names, since — unlike the listing page — no embedded data blob could
# be confirmed for this page type. The bounded gap keeps a match from
# spanning into the next result's address.
_ADDRESS_LINE_PATTERN = re.compile(
    rf"(\d+,\s*[^£]{{3,80}}?{_UK_POSTCODE})[^£]{{0,80}}£\s?([\d,]+)", re.I
)
 
 
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
 
        text = BeautifulSoup(html, "html.parser").get_text(separator=" ", strip=True)
        matches = _ADDRESS_LINE_PATTERN.findall(text)
        sales = [
            (addr.strip(), int(price_str.replace(",", "")))
            for addr, price_str in matches
            if 10_000 <= int(price_str.replace(",", "")) <= 20_000_000
        ][:6]
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
 
