"""Deterministic, rules-based scoring engine.

Produces the four axis scores shown on the "Investment DNA" diamond
(growth / valueAdd / security / cashflow), an overall verdict + confidence,
and the three strategy scores (BTL / BRRR / Flip) — all 0-100, matching
analyse.html's `scores` / `strategy` objects exactly.

This is intentionally rules-based rather than a black-box model: every
score is traceable to a specific input (yield vs. benchmark, EPC rating,
comparable evidence, etc.), which matters for a product whose whole pitch
is "an explained recommendation, not just a number" — see the Business
Plan's "AI Reasoning" section. The narrative module (narrative.py) turns
these same inputs into the human-readable clauses/strengths/risks, so the
prose and the score always agree with each other.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.engine.comparables import ComparablesResult, RentEstimate
from app.engine.financial import FinancialAnalysis
from app.engine.house_price_index import AreaTrend
from app.extractors.rightmove import ListingData
from app.schemas import VERDICT_LABELS, Verdict

BENCHMARK_NET_YIELD_PCT = 5.5

_EPC_VALUE_ADD_POTENTIAL = {
    "A": 5, "B": 15, "C": 30, "D": 48, "E": 65, "F": 78, "G": 88,
}

_VALUE_ADD_KEYWORDS = [
    "potential", "extend", "extension", "renovat", "refurb", "modernis",
    "modernize", "no onward chain", "no chain", "development", "planning",
    "loft", "side return", "stpp",
]


def _clamp(n: float, lo: int = 0, hi: int = 100) -> int:
    return int(round(max(lo, min(hi, n))))


@dataclass
class ScoreBreakdown:
    growth: int
    value_add: int
    security: int
    cashflow: int
    confidence: int
    verdict: Verdict
    strategy_btl: int
    strategy_brrr: int
    strategy_flip: int


def _cashflow_score(financials: FinancialAnalysis) -> int:
    score = 50 + (financials.net_yield_pct - BENCHMARK_NET_YIELD_PCT) * 12
    if financials.cashflow_monthly < 0:
        score = min(score, 38)
    elif financials.cashflow_monthly > 300:
        score += 6
    return _clamp(score)


def _growth_score(comparables: ComparablesResult, price: int, listing: ListingData, area_trend: AreaTrend | None) -> int:
    if comparables.sales:
        avg_comp = sum(p for _, p in comparables.sales) / len(comparables.sales)
        discount_pct = ((avg_comp - price) / avg_comp) * 100 if avg_comp else 0
        score = 50 + discount_pct * 3
    else:
        score = 50  # no evidence either way — neutral, not penalised twice
                     # (lack of evidence already lowers `confidence` separately)
    if listing.price_reduced:
        score -= 8
    # Area-level momentum is a secondary signal alongside the (property-
    # specific) comparable-price discount above — modest weight, capped,
    # and only applied when real UK HPI data was found (never fabricated,
    # same rule as everywhere else: no data in, no adjustment out).
    if area_trend and area_trend.one_year_change_pct is not None:
        score += _clamp(area_trend.one_year_change_pct * 1.5, -8, 8)
    return _clamp(score)


def _value_add_score(listing: ListingData) -> int:
    epc_component = _EPC_VALUE_ADD_POTENTIAL.get((listing.epc_rating or "").upper(), 45)
    text = f"{listing.description} {' '.join(listing.key_features)}".lower()
    keyword_hits = sum(1 for kw in _VALUE_ADD_KEYWORDS if kw in text)
    keyword_boost = min(keyword_hits * 7, 28)
    score = epc_component * 0.55 + 35 + keyword_boost * 0.6
    return _clamp(score)


def _security_score(listing: ListingData, comparables: ComparablesResult, rent: RentEstimate) -> int:
    score = 40.0
    score += min(len(comparables.sales) * 8, 24)
    if listing.tenure:
        score += 10
    if listing.epc_rating:
        score += 8
    if listing.beds is not None:
        score += 5
    if len(listing.description) > 50:
        score += 5
    if rent.method == "live_comparables":
        score += 8
    return _clamp(score)


def _confidence(security: int, listing: ListingData, comparables: ComparablesResult, rent: RentEstimate) -> int:
    evidence_score = security
    extraction_quality = 100 if listing.extraction_method == "page_model" else 65
    extraction_quality -= min(len(listing.fields_missing) * 6, 30)
    if comparables.method == "land_registry":
        comps_quality = 100   # official government transaction records
    elif comparables.sales:
        comps_quality = 85    # scraped, real, but a less authoritative source
    else:
        comps_quality = 55
    rent_quality = 100 if rent.method == "live_comparables" else 60
    composite = evidence_score * 0.4 + extraction_quality * 0.25 + comps_quality * 0.2 + rent_quality * 0.15
    return _clamp(composite)


def _verdict_from_composite(composite: float) -> Verdict:
    if composite >= 80:
        return Verdict.strongbuy
    if composite >= 65:
        return Verdict.buy
    if composite >= 50:
        return Verdict.invest
    if composite >= 35:
        return Verdict.caution
    return Verdict.pass_


def score_property(
    listing: ListingData,
    financials: FinancialAnalysis,
    comparables: ComparablesResult,
    rent: RentEstimate,
    area_trend: AreaTrend | None = None,
) -> ScoreBreakdown:
    cashflow = _cashflow_score(financials)
    growth = _growth_score(comparables, financials.purchase, listing, area_trend)
    value_add = _value_add_score(listing)
    security = _security_score(listing, comparables, rent)
    confidence = _confidence(security, listing, comparables, rent)

    composite = cashflow * 0.30 + growth * 0.25 + value_add * 0.20 + security * 0.25
    verdict = _verdict_from_composite(composite)

    strategy_btl = _clamp(cashflow * 0.50 + security * 0.30 + growth * 0.20)
    strategy_brrr = _clamp(value_add * 0.45 + growth * 0.35 + cashflow * 0.20)
    strategy_flip = _clamp(value_add * 0.60 + growth * 0.40)

    return ScoreBreakdown(
        growth=growth,
        value_add=value_add,
        security=security,
        cashflow=cashflow,
        confidence=confidence,
        verdict=verdict,
        strategy_btl=strategy_btl,
        strategy_brrr=strategy_brrr,
        strategy_flip=strategy_flip,
    )


def verdict_label(verdict: Verdict) -> str:
    return VERDICT_LABELS[verdict]
