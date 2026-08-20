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
from app.extractors.rightmove import ExtractionError, InvalidListingUrlError, ListingUnavailableError
from app.pipeline import run_analysis
from app.schemas import AnalyzeRequest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("blackline")


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


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


routes = [
    Route("/api/analyze", analyze, methods=["POST"]),
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
