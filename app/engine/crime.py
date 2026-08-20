"""Local crime context via data.police.uk's official, free, unauthenticated
crime-data API.

Two things are surfaced:
  1. A plain descriptive snapshot (`CrimeStats`: count, month, top
     categories) — appended to the security clause as sourced, dated
     context, never phrased as a "safe"/"risky" judgement.
  2. A year-on-year trend for the SAME point and radius (`CrimeTrend`) —
     this one DOES feed a strength/risk line in narrative.py, but only
     because it compares an area against itself a year earlier (same
     ~1 mile radius, same calendar month — which also controls for
     seasonal patterns, e.g. summer anti-social-behaviour spikes), never
     against a different area or an invented "typical" threshold.

Why the raw snapshot alone still isn't scored: a crime COUNT within a
fixed ~1-mile radius isn't meaningfully comparable BETWEEN areas without
a population/density-normalised benchmark (a busy town-centre point
captures retail/nightlife crime that isn't representative of nearby
residential risk, for example) — there's no reliable, honestly-sourced
per-capita benchmark wired into this codebase for that comparison. The
year-on-year trend sidesteps this problem entirely: the only thing that
varies between the two numbers being compared is time, not location, so
"crime is up/down X% here versus a year ago" is a fair claim even though
"this area is safer than that one" from the same raw counts would not be.
A minimum baseline sample size (`_MIN_BASELINE_FOR_TREND`) guards against
a tiny base count making an ordinary swing look like a dramatic percentage
change.

Uses the SAME outcode -> lat/lng resolution as house_price_index.py, via
the shared geocoding.py helper — this is separate, newly-written code
rather than a refactor of the already-confirmed-working HPI module, to
avoid risking a regression there; the duplicate postcodes.io call this
causes per analysis is a minor, acceptable cost (postcodes.io is fast and
free), not a correctness concern.
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


def _month_offset(month: str, delta: int) -> str | None:
    """"2026-06" with delta=-12 -> "2025-06". Returns None on anything
    that doesn't parse as YYYY-MM rather than raising — callers treat that
    as "can't compute a baseline", not a crash."""
    try:
        year_str, month_str = month.split("-")
        year, mon = int(year_str), int(month_str)
    except (ValueError, AttributeError):
        return None
    total = year * 12 + (mon - 1) + delta
    new_year, new_mon = divmod(total, 12)
    return f"{new_year:04d}-{new_mon + 1:02d}"


@dataclass
class CrimeStats:
    total_count: int
    month: str | None          # the month the API actually returned data for
    top_categories: list[tuple[str, int]]   # [(label, count), ...] largest first
    radius_note: str = "within a fixed ~1 mile radius (data.police.uk)"


# Below this baseline count, a swing of just one or two crimes produces a
# wild-looking percentage change that isn't a meaningful signal — so no
# trend is reported at all rather than risk an overstated claim.
_MIN_BASELINE_FOR_TREND = 10


@dataclass
class CrimeTrend:
    """A same-point, same-radius, same-calendar-month comparison against a
    year earlier — see the module docstring for why this (unlike the raw
    CrimeStats snapshot) is fair to turn into a strength/risk line."""
    current_count: int
    current_month: str
    baseline_count: int
    baseline_month: str
    change_pct: float
    note: str = "year-on-year, same ~1 mile radius (police.uk)"


@dataclass
class CrimeDiagnostics:
    """Debug-only — see LandRegistryDiagnostics for why this exists as a
    separate, non-swallowing path from the production fetch function."""
    latitude: float | None = None
    longitude: float | None = None
    http_status: int | None = None
    error: str | None = None
    raw_record_count: int | None = None


async def _fetch_raw(geocode: OutcodeGeocode, date: str | None = None) -> tuple[list[dict], CrimeDiagnostics]:
    diag = CrimeDiagnostics(latitude=geocode.latitude, longitude=geocode.longitude)
    params = {"lat": geocode.latitude, "lng": geocode.longitude}
    if date:
        # Explicit YYYY-MM — used for the year-on-year baseline lookup.
        # Omitting this (the default) returns the latest month the API
        # has data for — confirmed in the API's own documentation — so
        # there's no need for an extra call to /crimes-street-dates first
        # to figure out which month is current.
        params["date"] = date
    try:
        async with httpx.AsyncClient(timeout=settings.police_uk_timeout_seconds) as client:
            resp = await client.get(_CRIMES_ENDPOINT, params=params)
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


async def fetch_crime_trend(geocode: OutcodeGeocode | None, current: CrimeStats | None) -> CrimeTrend | None:
    """Best-effort year-on-year comparison for the SAME point/radius as
    `current`. Returns None (never a fabricated or misleading figure) if
    geocoding/current data is missing, the baseline month can't be
    computed, the lookup fails, or the baseline sample is too small
    (`_MIN_BASELINE_FOR_TREND`) to be a meaningful comparison."""
    if geocode is None or current is None or not current.month:
        return None
    baseline_month = _month_offset(current.month, -12)
    if baseline_month is None:
        return None
    records, diag = await _fetch_raw(geocode, date=baseline_month)
    if diag.error:
        return None
    baseline_count = len(records)
    if baseline_count < _MIN_BASELINE_FOR_TREND:
        return None
    change_pct = round((current.total_count - baseline_count) / baseline_count * 100, 1)
    return CrimeTrend(
        current_count=current.total_count,
        current_month=current.month,
        baseline_count=baseline_count,
        baseline_month=baseline_month,
        change_pct=change_pct,
    )


async def fetch_crime_context(geocode: OutcodeGeocode | None) -> tuple[CrimeStats | None, CrimeTrend | None]:
    """Combines the current-month snapshot with the year-on-year trend —
    this is the function pipeline.py calls. The two police.uk requests
    happen sequentially inside this one coroutine (the trend needs the
    current month before it knows which baseline month to ask for), but
    the coroutine as a whole still runs concurrently with rent/comparables
    /area-trend in pipeline.py's asyncio.gather batch, so it doesn't add a
    second sequential wait to the overall request."""
    stats = await fetch_crime_stats(geocode)
    if stats is None:
        return None, None
    trend = await fetch_crime_trend(geocode, stats)
    return stats, trend
