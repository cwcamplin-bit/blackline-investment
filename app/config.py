"""Central configuration. Every value has a sane default so the service runs
with zero setup; everything is overridable via environment variables (or a
.env file) for a real deployment.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Optional AI enhancement ---
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"

    # --- Default financial assumptions (all overridable per-request) ---
    default_ltv_percent: float = 75.0
    default_mortgage_rate_percent: float = 5.9
    default_mortgage_term_years: int = 25
    default_management_fee_percent: float = 10.0
    default_maintenance_percent: float = 6.0
    default_void_allowance_percent: float = 4.0
    default_insurance_monthly: float = 18.0

    # --- Networking ---
    extractor_timeout_seconds: float = 12.0
    extractor_user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
    extractor_max_retries: int = 2

    # HM Land Registry's public SPARQL endpoint has no documented SLA.
    # Confirmed via live diagnostics (GET /api/debug/land-registry) that the
    # outcode-prefix comparables query consistently times out against real
    # outcodes (CV6, M20) — tried raising this to 12s and restructuring the
    # query, still timed out, so it's very likely a structural limit of
    # this endpoint for broad searches (see land_registry.py), not
    # something a longer wait fixes. Deliberately shrunk back down: since
    # this attempt essentially never succeeds, every second here is a pure
    # latency tax on every single analysis before the Rightmove-scrape
    # fallback even starts — 12s made a real, user-visible difference to
    # response time for zero benefit. Kept non-zero (rather than disabled
    # outright) in case some quieter outcode ever IS fast enough to
    # succeed within this window — but this is now optimised for "fail
    # fast, use the fallback" rather than "wait and hope."
    land_registry_timeout_seconds: float = 3.0

    # UK House Price Index — same SPARQL endpoint as Land Registry sold
    # prices, but a much smaller, single-region query, so a short timeout
    # is appropriate (see house_price_index.py for why this should be
    # fast where the sold-comparables query wasn't).
    house_price_index_timeout_seconds: float = 8.0
    # postcodes.io — free, unauthenticated, no documented rate limit issue
    # in practice; short timeout since it's a simple lookup, not a search.
    postcodes_io_timeout_seconds: float = 5.0
    # data.police.uk — free, unauthenticated, purpose-built REST API for
    # exactly this per-request geo query (unlike Land Registry's SPARQL
    # endpoint), so a short timeout is appropriate; 15 req/s rate limit
    # documented but not a concern at this app's request volume.
    police_uk_timeout_seconds: float = 8.0

    # --- CORS: the Blackline front end origin(s) allowed to call this API ---
    allowed_origins: list[str] = ["*"]


settings = Settings()
