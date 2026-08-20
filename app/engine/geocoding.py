"""Shared UK postcode-geography resolution via postcodes.io.

Both house_price_index.py (needs a local authority district name) and
crime.py (needs a lat/lng point) need to go from "outcode" to "geography"
before they can query their respective official datasets — UK HPI only
publishes at local-authority level, and police.uk's crimes-street endpoint
takes a coordinate, not a postcode. Both need the SAME outcode resolved,
so this is done ONCE per analysis (in pipeline.py) rather than each module
hitting postcodes.io separately for an identical lookup — a real
efficiency point given the user explicitly asked for this to stay
efficient, not just accurate.

postcodes.io itself is free, unauthenticated, and widely used in the UK
dev community — it is NOT an HM Land Registry / government source, just
the standard way to bridge a UK postcode/outcode to the geography that
official datasets index by (there's no equivalently convenient official
postcode-to-geography API).
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config import settings

_ENDPOINT = "https://api.postcodes.io/outcodes/{outcode}"


@dataclass
class OutcodeGeocode:
    outcode: str
    latitude: float
    longitude: float
    admin_districts: list[str]   # an outcode can span more than one district


async def resolve_outcode(outcode: str | None) -> OutcodeGeocode | None:
    """Best-effort: returns None (never fabricated data, never raises) if
    the outcode is missing, unrecognised, or the lookup fails."""
    if not outcode:
        return None
    try:
        async with httpx.AsyncClient(timeout=settings.postcodes_io_timeout_seconds) as client:
            resp = await client.get(_ENDPOINT.format(outcode=outcode.upper().strip()))
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return None

    result = data.get("result") or {}
    lat, lng = result.get("latitude"), result.get("longitude")
    if lat is None or lng is None:
        return None
    districts = [d for d in (result.get("admin_district") or []) if isinstance(d, str) and d.strip()]
    return OutcodeGeocode(
        outcode=outcode.upper().strip(),
        latitude=float(lat),
        longitude=float(lng),
        admin_districts=districts,
    )
