"""HTTP layer. A single real endpoint — POST /api/analyze — plus a health
check. Written directly against Starlette + Pydantic (see requirements.txt
for why FastAPI isn't a hard dependency here); the request/response
validation, error handling, and route shape are the same either way.

Run with:
    uvicorn app.main:app --reload --port 8000
"""
from __future__ import annotations

import json
import logging

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.config import settings
from app.engine import crime as crime_mod
from app.engine import geocoding as geo_mod
from app.engine import house_price_index as hpi_mod
from app.engine import land_registry as lr_mod
from app.engine.comparables import fetch_comparable_sales, fetch_rent_estimate
from app.extractors.rightmove import ExtractionError, InvalidListingUrlError, ListingUnavailableError
from app.pipeline import run_analysis
from app.schemas import AnalyzeRequest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("blackline")


async def _run_and_respond(analyze_request: AnalyzeRequest) -> JSONResponse:
    try:
        result = await run_analysis(analyze_request)
    except InvalidListingUrlError as exc:
        return JSONResponse({"error": "invalid_url", "detail": str(exc)}, status_code=400)
    except ListingUnavailableError as exc:
        return JSONResponse({"error": "listing_unavailable", "detail": str(exc)}, status_code=404)
    except ExtractionError as exc:
        logger.warning("Extraction failed for %s: %s", analyze_request.url, exc)
        return JSONResponse({"error": "extraction_failed", "detail": str(exc)}, status_code=502)
    except Exception:
        logger.exception("Unexpected error analysing %s", analyze_request.url)
        return JSONResponse(
            {"error": "internal_error", "detail": "Something went wrong analysing this property. Please try again."},
            status_code=500,
        )

    return JSONResponse(json.loads(result.model_dump_json()))


async def analyze(request: Request) -> JSONResponse:
    try:
        raw_body = await request.body()
        payload = json.loads(raw_body or b"{}")
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid_json", "detail": "Request body must be valid JSON."}, status_code=400)

    try:
        analyze_request = AnalyzeRequest.model_validate(payload)
    except ValidationError as exc:
        return JSONResponse(
            {"error": "validation_error", "detail": exc.errors()},
            status_code=422,
        )

    return await _run_and_respond(analyze_request)


async def analyze_get(request: Request) -> JSONResponse:
    """GET equivalent of POST /api/analyze, taking ?url=... — purely a
    convenience for debugging from a browser or a tool that can only issue
    GET requests (e.g. checking a deployment quickly without curl)."""
    url = request.query_params.get("url")
    try:
        analyze_request = AnalyzeRequest(url=url or "")
    except ValidationError as exc:
        return JSONResponse({"error": "validation_error", "detail": exc.errors()}, status_code=422)

    return await _run_and_respond(analyze_request)


async def debug_comparables(request: Request) -> JSONResponse:
    """Diagnostic endpoint: exercises the live rent-comparables and
    sold-comparables lookups directly for a given outcode/beds, without
    needing a real listing URL. Returns the method actually used
    ("live_comparables"/"live_scrape" vs. the modelled/unavailable
    fallback) plus the raw result, so a failure in the live Rightmove
    lookups is visible immediately rather than being masked by the wider
    pipeline's graceful-degradation behaviour."""
    outcode = request.query_params.get("outcode")
    beds = int(request.query_params.get("beds", 2))
    property_type = request.query_params.get("type", "flat")
    price = int(request.query_params.get("price", 200_000))

    if not outcode:
        return JSONResponse({"error": "missing_outcode", "detail": "Pass ?outcode=CV6 (a Rightmove postcode outcode)."}, status_code=400)

    rent = await fetch_rent_estimate(price, property_type, beds, outcode)
    comparables = await fetch_comparable_sales("", outcode, beds)

    return JSONResponse({
        "outcode": outcode,
        "beds": beds,
        "rent": {
            "monthlyRent": rent.monthly_rent,
            "method": rent.method,
            "sampleSize": rent.sample_size,
            "note": rent.note,
        },
        "comparables": {
            "method": comparables.method,
            "note": comparables.note,
            "sales": [{"address": a, "price": p} for a, p in comparables.sales],
        },
    })


async def debug_land_registry(request: Request) -> JSONResponse:
    """Diagnostic endpoint: exercises the live HM Land Registry SPARQL
    lookup directly for a given outcode, without needing a full listing
    URL — the same pattern as /api/debug/comparables, added for the same
    reason (isolating a new live-data integration from the wider pipeline's
    graceful-degradation behaviour so it can be checked directly after
    deployment). This query shape (outcode-prefix, not exact-postcode)
    could not be live-verified from the development sandbox this was built
    in — please run this once deployed against a real outcode to confirm
    it returns real sold-price data before relying on it."""
    outcode = request.query_params.get("outcode")
    if not outcode:
        return JSONResponse({"error": "missing_outcode", "detail": "Pass ?outcode=CV6 (a Rightmove postcode outcode)."}, status_code=400)

    sales, diagnostics = await lr_mod.fetch_outcode_comparables_with_diagnostics(outcode)

    return JSONResponse({
        "outcode": outcode,
        "resultCount": len(sales),
        "sales": [{"address": s.address, "price": s.price, "date": s.date} for s in sales],
        "diagnostics": {
            "queryUrl": diagnostics.query_url,
            "httpStatus": diagnostics.http_status,
            "error": diagnostics.error,
            "rawBindingCount": diagnostics.raw_binding_count,
            "rawBodySnippet": diagnostics.raw_body_snippet,
        },
    })


async def debug_house_price_index(request: Request) -> JSONResponse:
    """Diagnostic endpoint: exercises the live postcodes.io -> UK HPI chain
    directly for a given outcode — same pattern as the other /api/debug/*
    routes. Surfaces which district name(s) were tried and the raw SPARQL
    outcome, since a silent None here could mean postcodes.io returned
    nothing, no label match was found, or the query itself failed."""
    outcode = request.query_params.get("outcode")
    if not outcode:
        return JSONResponse({"error": "missing_outcode", "detail": "Pass ?outcode=CV6 (a Rightmove postcode outcode)."}, status_code=400)

    trend, diagnostics = await hpi_mod.fetch_area_trend_with_diagnostics(outcode)

    return JSONResponse({
        "outcode": outcode,
        "found": trend is not None,
        "trend": {
            "regionLabel": trend.region_label,
            "latestMonth": trend.latest_month,
            "latestIndex": trend.latest_index,
            "latestAveragePrice": trend.latest_average_price,
            "oneYearChangePct": trend.one_year_change_pct,
            "fiveYearChangePct": trend.five_year_change_pct,
        } if trend else None,
        "diagnostics": {
            "districtsTried": diagnostics.districts_tried,
            "httpStatus": diagnostics.http_status,
            "error": diagnostics.error,
            "rawPointCount": diagnostics.raw_point_count,
        },
    })


async def debug_crime(request: Request) -> JSONResponse:
    """Diagnostic endpoint: exercises the live postcodes.io -> police.uk
    chain directly for a given outcode — same pattern as the other
    /api/debug/* routes."""
    outcode = request.query_params.get("outcode")
    if not outcode:
        return JSONResponse({"error": "missing_outcode", "detail": "Pass ?outcode=CV6 (a Rightmove postcode outcode)."}, status_code=400)

    geocode = await geo_mod.resolve_outcode(outcode)
    if geocode is None:
        return JSONResponse({"outcode": outcode, "found": False, "diagnostics": {"error": "postcodes.io could not resolve this outcode to a lat/lng"}})

    stats, diagnostics = await crime_mod.fetch_crime_stats_with_diagnostics(geocode)
    trend = await crime_mod.fetch_crime_trend(geocode, stats) if stats else None

    return JSONResponse({
        "outcode": outcode,
        "resolvedLatLng": {"lat": geocode.latitude, "lng": geocode.longitude},
        "found": stats is not None,
        "stats": {
            "totalCount": stats.total_count,
            "month": stats.month,
            "topCategories": [{"category": c, "count": n} for c, n in stats.top_categories],
            "radiusNote": stats.radius_note,
        } if stats else None,
        "trend": {
            "changePct": trend.change_pct,
            "currentMonth": trend.current_month,
            "currentCount": trend.current_count,
            "baselineMonth": trend.baseline_month,
            "baselineCount": trend.baseline_count,
            "note": trend.note,
        } if trend else None,
        "diagnostics": {
            "httpStatus": diagnostics.http_status,
            "error": diagnostics.error,
            "rawRecordCount": diagnostics.raw_record_count,
            "trendNote": (
                "No trend: either <10 baseline crimes a year ago, the "
                "baseline lookup failed, or there wasn't enough data to "
                "compute one — see fetch_crime_trend()."
                if stats and trend is None
                else None
            ),
        },
    })


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


routes = [
    Route("/api/analyze", analyze, methods=["POST"]),
    Route("/api/analyze", analyze_get, methods=["GET"]),
    Route("/api/debug/comparables", debug_comparables, methods=["GET"]),
    Route("/api/debug/land-registry", debug_land_registry, methods=["GET"]),
    Route("/api/debug/house-price-index", debug_house_price_index, methods=["GET"]),
    Route("/api/debug/crime", debug_crime, methods=["GET"]),
    Route("/api/health", health, methods=["GET"]),
]

middleware = [
    Middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_methods=["POST", "GET", "OPTIONS"],
        allow_headers=["*"],
    )
]

app = Starlette(routes=routes, middleware=middleware)
