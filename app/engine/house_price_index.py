"""UK House Price Index (HPI) — official area-level price trend, used to
show whether a property's local area has been rising or falling in value,
which feeds the growth score and clause alongside the (property-specific)
comparable sold prices.

Two lookups are chained:

  1. Resolve the listing's outcode to a local authority district name via
     postcodes.io (`api.postcodes.io/outcodes/<outcode>`) — a free,
     unauthenticated, widely-used UK postcode-geography API. This is a
     geography lookup, not itself an HM Land Registry data source; UK HPI
     only publishes at local-authority-district level, never postcode or
     outcode level, so this resolution step is unavoidable.
  2. Query UK HPI for that district via the SAME public SPARQL endpoint
     already used for HM Land Registry sold-price comparables
     (land_registry.py) — deliberately NOT the REST-JSON convenience path
     at landregistry.data.gov.uk/data/ukhpi/..., because that path is
     explicitly disallowed by that site's robots.txt (`Disallow: /data`).
     This project has consistently avoided disallowed paths elsewhere
     (see comparables.py's docstring re: Rightmove's `/api/*`), so the
     same rule applies here even though it means writing SPARQL instead of
     a simpler REST call.

Matching a district NAME to a UK HPI region is done via an exact,
case-insensitive rdfs:label match (with a few common prefix variants tried,
e.g. "City of Westminster" vs "Westminster") rather than by constructing a
URL slug — there's no confirmed, documented slug rule (e.g. some sources
suggest simple lowercase-hyphenation, but names like "Herefordshire,
County of" break that pattern), so guessing a slug risks silently querying
the wrong area or getting an empty result with no way to tell why.
Deliberately no fuzzy/partial matching beyond that: a wrong area trend
would be actively misleading (the same "never fabricate" principle as
sold comparables), so this returns unavailable rather than guessing.

Unlike the sold-comparables SPARQL query (land_registry.py), this one is
expected to be fast: UK HPI has roughly 441 regions × ~30 years of monthly
observations (~160k rows total, not HM Land Registry's ~28 million
individual transactions), and the query is scoped to one specific,
exactly-matched region before it ever joins to observations.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import httpx

from app.config import settings

_POSTCODES_IO_ENDPOINT = "https://api.postcodes.io/outcodes/{outcode}"
_SPARQL_ENDPOINT = "http://landregistry.data.gov.uk/landregistry/query"

_HPI_QUERY = """
PREFIX ukhpi: <http://landregistry.data.gov.uk/def/ukhpi/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?refMonth ?housePriceIndex ?averagePrice ?label WHERE {{
  ?region rdfs:label ?label .
  FILTER(LCASE(STR(?label)) = LCASE("{district}"))
  ?obs ukhpi:refRegion ?region ;
       ukhpi:refMonth ?refMonth ;
       ukhpi:housePriceIndex ?housePriceIndex .
  OPTIONAL {{ ?obs ukhpi:averagePrice ?averagePrice }}
}}
ORDER BY DESC(?refMonth)
LIMIT 65
"""

# Common ways a Land Registry HPI region label differs from postcodes.io's
# admin_district name — tried in order, first match wins.
_PREFIX_VARIANTS = ("city of ", "royal borough of ", "london borough of ", "metropolitan borough of ", "borough of ")
_SUFFIX_VARIANTS = (", county of",)


def _label_candidates(admin_district: str) -> list[str]:
    candidates = [admin_district]
    lower = admin_district.lower()
    for prefix in _PREFIX_VARIANTS:
        if lower.startswith(prefix):
            candidates.append(admin_district[len(prefix):].strip())
    for suffix in _SUFFIX_VARIANTS:
        if lower.endswith(suffix):
            candidates.append(admin_district[: -len(suffix)].strip())
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


async def _resolve_admin_districts(outcode: str) -> list[str]:
    """Best-effort: returns [] on any failure — never raises."""
    try:
        async with httpx.AsyncClient(timeout=settings.postcodes_io_timeout_seconds) as client:
            resp = await client.get(_POSTCODES_IO_ENDPOINT.format(outcode=outcode.upper().strip()))
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return []
    result = data.get("result") or {}
    districts = result.get("admin_district") or []
    return [d for d in districts if isinstance(d, str) and d.strip()]


def _parse_month(value: str) -> date | None:
    """refMonth comes back as "YYYY-MM" or a full ISO datetime — only the
    year/month are meaningful for this dataset (it's monthly data)."""
    try:
        return date(int(value[0:4]), int(value[5:7]), 1)
    except (ValueError, IndexError, TypeError):
        return None


def _months_between(earlier: date, later: date) -> int:
    return (later.year - earlier.year) * 12 + (later.month - earlier.month)


@dataclass
class AreaTrend:
    region_label: str
    latest_month: str
    latest_index: float
    latest_average_price: int | None
    one_year_change_pct: float | None
    five_year_change_pct: float | None


@dataclass
class HpiDiagnostics:
    """Debug-only — see LandRegistryDiagnostics in land_registry.py for why
    this exists as a separate, non-swallowing path."""
    outcode: str
    districts_tried: list[str]
    http_status: int | None = None
    error: str | None = None
    raw_point_count: int | None = None


async def _query_region(district: str) -> tuple[list[tuple[date, float, int | None]], HpiDiagnostics]:
    diag = HpiDiagnostics(outcode="", districts_tried=[district])
    query = _HPI_QUERY.format(district=district.replace('"', ""))
    try:
        async with httpx.AsyncClient(timeout=settings.house_price_index_timeout_seconds) as client:
            resp = await client.get(
                _SPARQL_ENDPOINT,
                params={"query": query, "output": "json"},
                headers={"Accept": "application/sparql-results+json"},
            )
        diag.http_status = resp.status_code
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        diag.error = f"{type(exc).__name__}: {exc}"
        return [], diag

    points: list[tuple[date, float, int | None]] = []
    for row in data.get("results", {}).get("bindings", []):
        month = _parse_month(row.get("refMonth", {}).get("value", ""))
        if month is None:
            continue
        try:
            index = float(row["housePriceIndex"]["value"])
        except (KeyError, ValueError):
            continue
        avg_price_raw = row.get("averagePrice", {}).get("value")
        try:
            avg_price = int(float(avg_price_raw)) if avg_price_raw else None
        except ValueError:
            avg_price = None
        points.append((month, index, avg_price))

    points.sort(key=lambda p: p[0], reverse=True)
    diag.raw_point_count = len(points)
    return points, diag


def _build_trend(region_label: str, points: list[tuple[date, float, int | None]]) -> AreaTrend | None:
    if not points:
        return None
    latest_month, latest_index, latest_avg = points[0]

    def _change_at(target_months_back: int) -> float | None:
        # Nearest available observation to the target offset, tolerating
        # small gaps in the published series (a month or two either side).
        candidates = [
            (abs(_months_between(m, latest_month) - target_months_back), hpi_value)
            for m, hpi_value, _ in points
        ]
        if not candidates:
            return None
        best_gap, past_index = min(candidates)
        if best_gap > 3:  # nothing close enough to trust as "N years ago"
            return None
        if not past_index:  # zero/falsy — avoid a division by zero
            return None
        return round((latest_index - past_index) / past_index * 100, 1)

    return AreaTrend(
        region_label=region_label,
        latest_month=latest_month.strftime("%Y-%m"),
        latest_index=latest_index,
        latest_average_price=latest_avg,
        one_year_change_pct=_change_at(12),
        five_year_change_pct=_change_at(60),
    )


async def fetch_area_trend(outcode: str | None) -> AreaTrend | None:
    """Best-effort: returns None (never fabricated data) if the district
    can't be resolved, no HPI region matches it, or either lookup fails."""
    if not outcode:
        return None
    districts = await _resolve_admin_districts(outcode)
    for district in districts[:2]:  # try at most the first two districts an outcode spans
        for candidate in _label_candidates(district):
            points, _diag = await _query_region(candidate)
            trend = _build_trend(candidate, points)
            if trend is not None:
                return trend
    return None


async def fetch_area_trend_with_diagnostics(outcode: str) -> tuple[AreaTrend | None, HpiDiagnostics]:
    """Debug-only variant — surfaces which districts/labels were tried and
    the raw HTTP outcome for each, instead of silently returning None."""
    districts = await _resolve_admin_districts(outcode)
    if not districts:
        return None, HpiDiagnostics(outcode=outcode, districts_tried=[], error="postcodes.io returned no admin_district for this outcode")

    tried: list[str] = []
    last_diag = HpiDiagnostics(outcode=outcode, districts_tried=[])
    for district in districts[:2]:
        for candidate in _label_candidates(district):
            tried.append(candidate)
            points, diag = await _query_region(candidate)
            diag.outcode = outcode
            diag.districts_tried = list(tried)
            last_diag = diag
            trend = _build_trend(candidate, points)
            if trend is not None:
                return trend, diag
    last_diag.districts_tried = tried
    return None, last_diag
