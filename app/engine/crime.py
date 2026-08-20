"""Local crime context via data.police.uk's official, free, unauthenticated
crime-data API — surfaced as transparent context in the security clause,
not as a scored strength/risk (see below for why).

Uses the SAME outcode -> lat/lng resolution as house_price_index.py, via
the shared geocoding.py helper — this is separate, newly-written code
rather than a refactor of the already-confirmed-working HPI module, to
avoid risking a regression there; the duplicate postcodes.io call this
causes per analysis is a minor, acceptable cost (postcodes.io is fast and
free), not a correctness concern.

Deliberately NOT turned into a security-score adjustment or an automatic
strength/risk line, unlike the UK HPI area trend: a raw crime COUNT within
a fixed ~1-mile radius isn't meaningfully comparable between areas without
a population/density-normalised benchmark (a busy town-centre point
captures retail/nightlife crime that isn't representative of nearby
residential risk, for example) — there's no reliable, honestly-sourced
per-capita benchmark wired into this codebase to compare against. Rather
than invent a "typical" threshold and risk mischaracterising an area
either way, this is surfaced as plain, dated, sourced information for a
human to interpret — the same "never fabricate, never mislead" principle
applied throughout this codebase, just manifesting here as "don't
editorialise a number that isn't fairly comparable" rather than "don't
show fake data."
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import httpx

from app.config import settings
from app.engine.geocoding import OutcodeGeocode

_CRIMES_ENDPOINT = "https://data.police.uk/api/crimes-street/all-crime"

# A handful of raw category slugs (as returned by the API) mapped to
# reader-friendly labels for the ones most likely to actually appear near
# residential UK addresses — anything not in this map falls back to a
# simple slug-to-words conversion, so this list doesn't need to be
# exhaustive to stay correct.
_CATEGORY_LABELS = {
    "anti-social-behaviour": "anti-social behaviour",
    "bicycle-theft": "bicycle theft",
    "burglary": "burglary",
    "criminal-damage-arson": "criminal damage & arson",
    "drugs": "drugs",
    "other-theft": "other theft",
    "possession-of-weapons": "possession of weapons",
    "public-order": "public order",
    "robbery": "robbery",
    "shoplifting": "shoplifting",
    "theft-from-the-person": "theft from the person",
    "vehicle-crime": "vehicle crime",
    "violent-crime": "violence & sexual offences",
    "other-crime": "other crime",
}


def _label(category: str) -> str:
    return _CATEGORY_LABELS.get(category, category.replace("-", " "))


@dataclass
class CrimeStats:
    total_count: int
    month: str | None          # the month the API actually returned data for
    top_categories: list[tuple[str, int]]   # [(label, count), ...] largest first
    radius_note: str = "within a fixed ~1 mile radius (data.police.uk)"


@dataclass
class CrimeDiagnostics:
    """Debug-only — see LandRegistryDiagnostics for why this exists as a
    separate, non-swallowing path from the production fetch function."""
    latitude: float | None = None
    longitude: float | None = None
    http_status: int | None = None
    error: str | None = None
    raw_record_count: int | None = None


async def _fetch_raw(geocode: OutcodeGeocode) -> tuple[list[dict], CrimeDiagnostics]:
    diag = CrimeDiagnostics(latitude=geocode.latitude, longitude=geocode.longitude)
    try:
        async with httpx.AsyncClient(timeout=settings.police_uk_timeout_seconds) as client:
            # Omitting `date` returns the latest month the API has data
            # for — confirmed in the API's own documentation — so there's
            # no need for an extra call to /crimes-street-dates first to
            # figure out which month is current.
            resp = await client.get(
                _CRIMES_ENDPOINT,
                params={"lat": geocode.latitude, "lng": geocode.longitude},
            )
        diag.http_status = resp.status_code
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        diag.error = f"{type(exc).__name__}: {exc}"
        return [], diag

    if not isinstance(data, list):
        diag.error = "Response wasn't a JSON array as expected"
        return [], diag
    diag.raw_record_count = len(data)
    return data, diag


def _summarise(records: list[dict]) -> CrimeStats:
    month = records[0].get("month") if records else None
    counts = Counter(r.get("category", "other-crime") for r in records)
    top = [(_label(cat), n) for cat, n in counts.most_common(3)]
    return CrimeStats(total_count=len(records), month=month, top_categories=top)


async def fetch_crime_stats(geocode: OutcodeGeocode | None) -> CrimeStats | None:
    """Best-effort: returns None (never fabricated data) if geocoding
    failed or the API call fails — never raises."""
    if geocode is None:
        return None
    records, _diag = await _fetch_raw(geocode)
    if not records and _diag.error:
        return None
    return _summarise(records)


async def fetch_crime_stats_with_diagnostics(geocode: OutcodeGeocode) -> tuple[CrimeStats | None, CrimeDiagnostics]:
    """Debug-only variant — surfaces the raw HTTP outcome instead of
    silently returning None."""
    records, diag = await _fetch_raw(geocode)
    if diag.error:
        return None, diag
    return _summarise(records), diag
