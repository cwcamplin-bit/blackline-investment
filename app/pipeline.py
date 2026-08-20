"""Orchestrates one end-to-end analysis: URL in, AnalysisResult out.

This is deliberately the only place that calls every module — extractor,
financial engine, comparables/rent, scoring, narrative — so the HTTP layer
in main.py stays thin and this logic is independently testable without
spinning up a web server.
"""
from __future__ import annotations

import asyncio

from app.engine import comparables as comps_mod
from app.engine import financial as fin_mod
from app.engine import house_price_index as hpi_mod
from app.engine import narrative as narrative_mod
from app.engine import scoring as scoring_mod
from app.extractors import rightmove as rm
from app.schemas import (
    AnalysisResult,
    Clauses,
    Financials,
    Scores,
    Strategy,
    AnalyzeRequest,
)
from app.config import settings


async def run_analysis(request: AnalyzeRequest) -> AnalysisResult:
    listing = await rm.fetch_listing(request.url)
    return await _analyse_listing(listing, request)


async def run_analysis_from_listing(listing: rm.ListingData, request: AnalyzeRequest) -> AnalysisResult:
    """Same pipeline, skipping the network fetch — used by tests and by
    fetch_listing callers that already have a ListingData in hand."""
    return await _analyse_listing(listing, request)


async def _analyse_listing(listing: rm.ListingData, request: AnalyzeRequest) -> AnalysisResult:
    # These three external lookups are independent of each other (none
    # needs another's result), so they run concurrently rather than one
    # after another — meaningful latency saving now that comparables can
    # involve a slow Land Registry attempt before its Rightmove-scrape
    # fallback (see land_registry.py); previously this was three
    # sequential awaits, i.e. worst case roughly the SUM of all three
    # timeouts rather than the MAX of them.
    rent, comparables, area_trend = await asyncio.gather(
        comps_mod.fetch_rent_estimate(
            listing.price, listing.property_type, listing.beds, listing.postcode_outcode
        ),
        comps_mod.fetch_comparable_sales(
            listing.address, listing.postcode_outcode, listing.beds
        ),
        hpi_mod.fetch_area_trend(listing.postcode_outcode),
    )

    financials = fin_mod.analyse_financials(
        listing.price,
        rent.monthly_rent,
        ltv_percent=request.ltv_percent or settings.default_ltv_percent,
        mortgage_rate_percent=request.mortgage_rate_percent or settings.default_mortgage_rate_percent,
        is_additional_property=request.is_additional_property,
        management_fee_percent=settings.default_management_fee_percent,
        maintenance_percent=settings.default_maintenance_percent,
        void_allowance_percent=settings.default_void_allowance_percent,
        insurance_monthly=settings.default_insurance_monthly,
    )

    scores = scoring_mod.score_property(listing, financials, comparables, rent, area_trend)
    clauses = narrative_mod.build_clauses(listing, financials, comparables, rent, area_trend)
    strengths, risks = narrative_mod.build_strengths_and_risks(listing, financials, comparables, rent, scores, area_trend)
    summary = await narrative_mod.build_summary(listing, financials, comparables, scores)
    offer_low, offer_high = narrative_mod.suggested_offer_range(listing.price, scores.verdict)

    comparables_ui = [
        [addr, f"£{price:,}"] for addr, price in comparables.sales
    ]

    return AnalysisResult(
        address=listing.address,
        price=listing.price,
        beds=listing.beds,
        type=listing.property_type,
        sourceUrl=listing.source_url,
        verdict=scores.verdict,
        verdictLabel=scoring_mod.verdict_label(scores.verdict),
        confidence=scores.confidence,
        scores=Scores(
            growth=scores.growth,
            valueAdd=scores.value_add,
            security=scores.security,
            cashflow=scores.cashflow,
        ),
        clauses=Clauses(**clauses),
        financials=Financials(**financials.as_ui_dict()),
        strategy=Strategy(
            btl=scores.strategy_btl,
            brrr=scores.strategy_brrr,
            flip=scores.strategy_flip,
        ),
        comparables=comparables_ui,
        strengths=strengths,
        risks=risks,
        summary=summary,
        assumptions={
            "ltvPercent": request.ltv_percent or settings.default_ltv_percent,
            "mortgageRatePercent": request.mortgage_rate_percent or settings.default_mortgage_rate_percent,
            "isAdditionalProperty": request.is_additional_property,
            "managementFeePercent": settings.default_management_fee_percent,
            "maintenancePercent": settings.default_maintenance_percent,
            "voidAllowancePercent": settings.default_void_allowance_percent,
            "suggestedOfferLow": offer_low,
            "suggestedOfferHigh": offer_high,
            "rentEstimateMethod": rent.method,
            "rentEstimateNote": rent.note,
        },
        data_quality={
            "extractionMethod": listing.extraction_method,
            "fieldsMissing": listing.fields_missing,
            "comparablesMethod": comparables.method,
            "comparablesNote": comparables.note,
            "comparablesSource": (
                "HM Land Registry Price Paid Data (official)"
                if comparables.method == "land_registry"
                else "Rightmove sold-prices page (scraped)"
                if comparables.method == "live_scrape"
                else "unavailable"
            ),
            "areaTrendAvailable": area_trend is not None,
            "areaTrendRegion": area_trend.region_label if area_trend else None,
            "areaTrendOneYearChangePct": area_trend.one_year_change_pct if area_trend else None,
            "areaTrendFiveYearChangePct": area_trend.five_year_change_pct if area_trend else None,
            "areaTrendAsOf": area_trend.latest_month if area_trend else None,
            "areaTrendSource": "UK House Price Index (HM Land Registry / ONS)" if area_trend else "unavailable",
        },
    )
