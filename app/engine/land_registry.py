"""HM Land Registry Price Paid Data — official, authoritative sold-price
records for England & Wales, queried live via the public SPARQL endpoint.
 
This is tried BEFORE the Rightmove-scraped sold-prices page (the existing
fallback in comparables.py) because it's the primary source Rightmove's own
"house prices" pages are themselves built from: official, dated,
government-published transaction records, rather than parsed HTML from
someone else's rendered page. Using it directly removes a layer of
scraping risk and gives genuine transaction dates, not just prices.
 
Two limitations, both handled the same way the rest of this codebase
handles best-effort external data — graceful degradation, never a hard
failure:
 
  * The endpoint (landregistry.data.gov.uk) is a public, best-effort
    triplestore with no documented SLA, rate limit, or performance
    guarantee — it can be slow, especially for a broad query, or briefly
    unavailable. A short, dedicated timeout means a slow response here
    doesn't drag down the whole analysis; comparables.py falls back to the
    Rightmove scrape if this returns too few results or errors out.
  * We usually only have an outcode (e.g. "CV6"), not a full postcode —
    Rightmove doesn't display full postcodes for privacy, so an exact
    single-postcode lookup (the simplest, best-documented query shape for
    this endpoint) isn't available for most listings. Instead this queries
    every postcode starting with the outcode via SPARQL's STRSTARTS, which
    is a standard, valid SPARQL pattern but a wider and less indexed query
    than an exact match — capped with LIMIT and a short timeout to bound
    the worst case.
 
IMPORTANT CAVEAT FOR WHOEVER DEPLOYS THIS: the outcode-prefix query
pattern here could not be live-verified from the development sandbox this
was built in (its web-fetch tooling gave inconsistent/stale results
against this specific endpoint when tested) — the exact-postcode query
shape it's based on IS confirmed correct, and the STRSTARTS modification
is standard SPARQL, but please sanity-check this against a live outcode
via GET /api/debug/land-registry?outcode=<youroutcode> once deployed. If
it's ever wrong or unreliably slow, the pipeline still works correctly
either way — comparables.py silently falls back to the previously-working
Rightmove scrape.
 
Licence: Open Government Licence v3.0. Required attribution is surfaced in
the API response's `data_quality.comparablesNote` when this source is
used: "Contains HM Land Registry data © Crown copyright and database
right. Licensed under the Open Government Licence v3.0." Also note: the
address components (from Royal Mail/OS data used by Land Registry) are
licensed for use in a residential property price information service —
which is exactly what this is — but not for unrelated commercial reuse;
worth keeping in mind if this data is ever repurposed elsewhere in the
product.
"""
from __future__ import annotations
 
import re
from dataclasses import dataclass
 
import httpx
 
from app.config import settings
 
_SPARQL_ENDPOINT = "http://landregistry.data.gov.uk/landregistry/query"
 
# STRSTARTS with a trailing space after the outcode avoids "CV6" also
# matching "CV61 1AA" or similar longer outcodes that merely share a
# prefix — full postcodes always have a space before the incode.
_OUTCODE_COMPARABLES_QUERY = """
PREFIX lrppi: <http://landregistry.data.gov.uk/def/ppi/>
PREFIX lrcommon: <http://landregistry.data.gov.uk/def/common/>
SELECT ?paon ?saon ?street ?town ?postcode ?amount ?date WHERE {{
  ?addr lrcommon:postcode ?postcode .
  FILTER(STRSTARTS(STR(?postcode), "{outcode} "))
  ?transx lrppi:propertyAddress ?addr ;
          lrppi:pricePaid ?amount ;
          lrppi:transactionDate ?date .
  OPTIONAL {{ ?addr lrcommon:paon ?paon }}
  OPTIONAL {{ ?addr lrcommon:saon ?saon }}
  OPTIONAL {{ ?addr lrcommon:street ?street }}
  OPTIONAL {{ ?addr lrcommon:town ?town }}
}}
ORDER BY DESC(?date)
LIMIT {limit}
"""
 
_OUTCODE_SHAPE_PATTERN = re.compile(r"[A-Z]{1,2}\d[A-Z\d]?")
 
 
def _sanitise_outcode(outcode: str) -> str | None:
    """Outcodes are interpolated directly into a SPARQL string literal
    (there's no parameterised-query support over this HTTP interface), so
    this is a strict allowlist rather than escaping — only the shape a
    real UK outcode can take is accepted, anything else returns None
    rather than risk SPARQL/query injection via a malformed address."""
    candidate = (outcode or "").strip().upper()
    if _OUTCODE_SHAPE_PATTERN.fullmatch(candidate):
        return candidate
    return None
 
 
@dataclass
class LandRegistrySale:
    address: str
    price: int
    date: str  # ISO-ish date string, e.g. "2025-11-14"
 
 
async def fetch_outcode_comparables(outcode: str, limit: int = 8) -> list[LandRegistrySale]:
    """Best-effort: recent sold-price transactions for postcodes starting
    with the given outcode, most recent first. Returns an EMPTY list
    (never fabricated data — same rule as every other comparables source
    in this codebase) on any failure: bad outcode, timeout, malformed
    response, or zero matches."""
    safe_outcode = _sanitise_outcode(outcode)
    if not safe_outcode:
        return []
 
    query = _OUTCODE_COMPARABLES_QUERY.format(outcode=safe_outcode, limit=limit)
    try:
        async with httpx.AsyncClient(timeout=settings.land_registry_timeout_seconds) as client:
            resp = await client.get(
                _SPARQL_ENDPOINT,
                params={"query": query, "output": "json"},
                headers={"Accept": "application/sparql-results+json"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return []
 
    sales: list[LandRegistrySale] = []
    for row in data.get("results", {}).get("bindings", []):
        amount_raw = row.get("amount", {}).get("value")
        try:
            amount = int(float(amount_raw))
        except (TypeError, ValueError):
            continue
        if not (10_000 <= amount <= 20_000_000):
            continue
 
        address_parts = [
            row[key]["value"]
            for key in ("saon", "paon", "street", "town")
            if row.get(key, {}).get("value")
        ]
        postcode = row.get("postcode", {}).get("value", "")
        if postcode:
            address_parts.append(postcode)
        if not address_parts:
            continue
        address = ", ".join(address_parts)
 
        date_raw = row.get("date", {}).get("value", "")
        date = date_raw[:10] if date_raw else ""
 
        sales.append(LandRegistrySale(address=address, price=amount, date=date))
 
    return sales[:limit]
 
