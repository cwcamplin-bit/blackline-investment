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

    # --- CORS: the Blackline front end origin(s) allowed to call this API ---
    allowed_origins: list[str] = ["*"]


settings = Settings()
