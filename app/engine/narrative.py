"""Turns the scored, financial, and comparables data into the human-readable
text analyse.html displays: the four per-dimension `clauses`, the
`strengths` / `risks` lists, and the executive `summary`.

Every sentence here is built from a real computed value (yield, comparable
count, EPC rating, ...) via templates — nothing is invented. This is the
default and *only* path unless OPENAI_API_KEY is set, in which case the
deterministic text is handed to the model as grounding context and it is
asked only to rewrite/tighten the summary paragraph, not to introduce new
claims. If that call fails or times out for any reason, the deterministic
summary is used untouched — narrative generation never blocks or breaks
the pipeline.
"""
from __future__ import annotations

import httpx

from app.config import settings
from app.engine.comparables import ComparablesResult, RentEstimate
from app.engine.crime import CrimeStats, CrimeTrend
from app.engine.financial import FinancialAnalysis
from app.engine.house_price_index import AreaTrend
from app.engine.scoring import BENCHMARK_NET_YIELD_PCT, ScoreBreakdown
from app.extractors.rightmove import ListingData
from app.schemas import Verdict


def _cashflow_clause(financials: FinancialAnalysis) -> str:
    yld = financials.net_yield_pct
    if yld >= BENCHMARK_NET_YIELD_PCT + 1:
        comparison = "well above the local BTL average"
    elif yld >= BENCHMARK_NET_YIELD_PCT:
        comparison = "in line with the local BTL average"
    else:
        comparison = f"below the {BENCHMARK_NET_YIELD_PCT:.1f}% benchmark for this strategy"
    cf_note = "" if financials.cashflow_monthly >= 0 else " — currently cashflow negative at this rent"
    return f"{yld:.1f}% net yield, {comparison}{cf_note}"


def _area_trend_fragment(area_trend: AreaTrend | None) -> str:
    if not area_trend or area_trend.one_year_change_pct is None:
        return ""
    pct = area_trend.one_year_change_pct
    direction = "risen" if pct > 0 else "fallen" if pct < 0 else "held flat"
    return f"; {area_trend.region_label} area prices have {direction} {abs(pct):.1f}% over the past year (UK HPI)"


def _growth_clause(comparables: ComparablesResult, price: int, listing: ListingData, area_trend: AreaTrend | None = None) -> str:
    source_suffix = " (HM Land Registry)" if comparables.method == "land_registry" else ""
    if comparables.sales:
        avg_comp = sum(p for _, p in comparables.sales) / len(comparables.sales)
        discount_pct = ((avg_comp - price) / avg_comp) * 100 if avg_comp else 0
        n = len(comparables.sales)
        if discount_pct > 3:
            clause = f"priced {discount_pct:.0f}% below {n} comparable sold price{'s' if n != 1 else ''} nearby{source_suffix}"
        elif discount_pct >= -3:
            clause = f"broadly in line with {n} comparable sale{'s' if n != 1 else ''} nearby{source_suffix}"
        else:
            clause = f"priced {abs(discount_pct):.0f}% above {n} comparable sale{'s' if n != 1 else ''} nearby{source_suffix}"
    else:
        clause = "no verified comparable sales evidence yet for this area"
    if listing.price_reduced:
        clause += "; the asking price has already been reduced once"
    clause += _area_trend_fragment(area_trend)
    return clause


_EPC_LANGUAGE = {
    "A": "already at the top EPC band — limited upside from further improvement",
    "B": "already efficient (EPC B) — limited upside from further improvement",
    "C": "EPC C — modest scope for a cosmetic refresh",
    "D": "EPC D — some scope for a light refresh to lift rent or value",
    "E": "EPC E — meaningful scope to add value through improvement, though budget for it",
    "F": "EPC F — significant improvement required, factor this into any offer",
    "G": "EPC G — substantial works likely needed before letting or resale",
}


def _value_add_clause(listing: ListingData) -> str:
    epc = (listing.epc_rating or "").upper()
    base = _EPC_LANGUAGE.get(epc, "EPC rating not available — condition and improvement potential unverified")
    text = f"{listing.description} {' '.join(listing.key_features)}".lower()
    if any(kw in text for kw in ("extend", "extension", "loft", "side return", "stpp", "planning")):
        base += "; the listing signals extension/development potential, subject to planning"
    return base


def _crime_fragment(crime: CrimeStats | None) -> str:
    # Deliberately descriptive, not evaluative — see crime.py's docstring
    # for why a raw count within a fixed radius isn't turned into a
    # "safe"/"risky" judgement: there's no fair per-area benchmark wired
    # in to compare it against.
    if not crime:
        return ""
    when = f" in {crime.month}" if crime.month else ""
    top = f", most commonly {crime.top_categories[0][0]}" if crime.top_categories else ""
    return f"; {crime.total_count} recorded crimes{when} {crime.radius_note}{top} (police.uk)"


def _security_clause(listing: ListingData, comparables: ComparablesResult, rent: RentEstimate, crime: CrimeStats | None = None) -> str:
    parts = []
    if comparables.sales:
        parts.append(f"{len(comparables.sales)} comparable sale(s) as evidence")
    else:
        parts.append("no comparable sales evidence found")
    parts.append(f"{listing.tenure.lower()} tenure confirmed" if listing.tenure else "tenure unconfirmed")
    parts.append(
        "rent estimate based on live comparable listings"
        if rent.method == "live_comparables"
        else "rent estimate is modelled, not yet verified against live listings"
    )
    return ", ".join(parts) + _crime_fragment(crime)


def build_clauses(listing, financials, comparables, rent, area_trend: AreaTrend | None = None, crime: CrimeStats | None = None) -> dict:
    return {
        "cashflow": _cashflow_clause(financials),
        "growth": _growth_clause(comparables, financials.purchase, listing, area_trend),
        "valueAdd": _value_add_clause(listing),
        "security": _security_clause(listing, comparables, rent, crime),
    }


# Same-area, year-on-year crime trend — NOT a cross-area comparison (see
# crime.py's module docstring for why that distinction matters). Set wider
# than the HPI thresholds (±3%/±2%) because month-level crime counts are
# noisier than a price index; ±20% is a real, not marginal, swing.
_CRIME_TREND_RISK_PCT = 20.0
_CRIME_TREND_STRENGTH_PCT = -20.0


def build_strengths_and_risks(
    listing: ListingData,
    financials: FinancialAnalysis,
    comparables: ComparablesResult,
    rent: RentEstimate,
    scores: ScoreBreakdown,
    area_trend: AreaTrend | None = None,
    crime_trend: CrimeTrend | None = None,
) -> tuple[list[str], list[str]]:
    strengths: list[str] = []
    risks: list[str] = []

    _QUALIFIER_LANGUAGE = {
        "guide_price": "This is an auction Guide Price, not a firm asking price — the eventual sale price is very likely to exceed it, which affects every figure below",
        "offers_in_excess_of": "Listed as 'offers in excess of' this price — treat it as a floor, not the likely purchase price",
        "offers_in_region_of": "Listed as 'offers in the region of' — the agreed price could land either side of this figure",
        "offers_over": "Listed as 'offers over' this price — treat it as a floor, not the likely purchase price",
        "fixed_price": None,       # not a risk — informational only
        "shared_ownership": "Shared ownership property — financials below assume full ownership and will need adjusting for the actual share being purchased",
    }
    qualifier_note = _QUALIFIER_LANGUAGE.get(listing.price_qualifier or "")
    if qualifier_note:
        risks.append(qualifier_note)

    if comparables.sales:
        avg_comp = sum(p for _, p in comparables.sales) / len(comparables.sales)
        if avg_comp > financials.purchase:
            discount_pct = (avg_comp - financials.purchase) / avg_comp * 100
            source_suffix = " (HM Land Registry)" if comparables.method == "land_registry" else ""
            strengths.append(
                f"Priced ~{discount_pct:.0f}% below {len(comparables.sales)} comparable sold prices nearby{source_suffix}"
            )
    else:
        risks.append("No comparable sales evidence available yet — valuation confidence is limited")

    if area_trend and area_trend.one_year_change_pct is not None:
        pct = area_trend.one_year_change_pct
        if pct >= 3.0:
            strengths.append(f"{area_trend.region_label} area prices up {pct:.1f}% over the past year (UK HPI)")
        elif pct <= -2.0:
            risks.append(f"{area_trend.region_label} area prices down {abs(pct):.1f}% over the past year (UK HPI)")

    if crime_trend is not None:
        pct = crime_trend.change_pct
        if pct <= _CRIME_TREND_STRENGTH_PCT:
            strengths.append(
                f"Recorded crime down {abs(pct):.0f}% year-on-year in the immediate area (police.uk)"
            )
        elif pct >= _CRIME_TREND_RISK_PCT:
            risks.append(
                f"Recorded crime up {pct:.0f}% year-on-year in the immediate area (police.uk)"
            )

    if financials.cashflow_monthly > 0:
        strengths.append(f"Cashflow positive at ~£{financials.cashflow_monthly}/month under the modelled assumptions")
    else:
        risks.append(f"Cashflow negative at ~£{financials.cashflow_monthly}/month under the modelled assumptions")

    if financials.net_yield_pct >= BENCHMARK_NET_YIELD_PCT:
        strengths.append(f"Net yield of {financials.net_yield_pct:.1f}% exceeds the {BENCHMARK_NET_YIELD_PCT:.1f}% benchmark")

    if rent.method != "live_comparables":
        risks.append("Rental figure is modelled rather than verified against live comparable listings")

    epc = (listing.epc_rating or "").upper()
    if epc in ("E", "F", "G"):
        risks.append(f"EPC rated {epc} — likely to need improvement work, and possibly ahead of tightening MEES rules")
    if not listing.epc_rating:
        risks.append("No EPC on record — condition and running costs are unverified")

    if listing.tenure and listing.tenure.upper() == "LEASEHOLD":
        risks.append("Leasehold — check remaining lease term and service charges before offering")

    if listing.price_reduced:
        strengths.append("Asking price has already been reduced once, suggesting some room to negotiate further")

    if not strengths:
        strengths.append("No standout strengths identified from the available listing data")
    if not risks:
        risks.append("No material risks identified from the available listing data")

    return strengths[:4], risks[:4]


def suggested_offer_range(price: int, verdict: Verdict) -> tuple[int, int]:
    if verdict in (Verdict.strongbuy, Verdict.buy):
        low_pct, high_pct = 0.95, 0.99
    elif verdict == Verdict.invest:
        low_pct, high_pct = 0.92, 0.97
    else:
        low_pct, high_pct = 0.88, 0.94
    low = round(price * low_pct / 500) * 500
    high = round(price * high_pct / 500) * 500
    return low, high


def _deterministic_summary(
    listing: ListingData,
    financials: FinancialAnalysis,
    comparables: ComparablesResult,
    scores: ScoreBreakdown,
) -> str:
    low, high = suggested_offer_range(financials.purchase, scores.verdict)
    qualifier_prefix = ""
    if listing.price_qualifier in ("guide_price", "offers_over", "offers_in_excess_of", "offers_in_region_of"):
        qualifier_prefix = (
            "Note: the listed price is not a firm asking price (see risks below), so every "
            "figure in this report should be treated as indicative until a realistic likely "
            "sale price is established. "
        )
    comps_sentence = (
        f"Priced against {len(comparables.sales)} comparable sale(s) within the {listing.postcode_outcode or 'local'} area, "
        if comparables.sales
        else "No verified comparable sales were available for this area, so treat the valuation with extra caution. "
    )
    yield_sentence = (
        f"Net yield of {financials.net_yield_pct:.1f}% "
        f"{'exceeds' if financials.net_yield_pct >= BENCHMARK_NET_YIELD_PCT else 'sits below'} "
        f"the {BENCHMARK_NET_YIELD_PCT:.1f}% benchmark for this strategy. "
    )
    return (
        f"{qualifier_prefix}{comps_sentence}{yield_sentence}"
        f"Suggested offer range: £{low:,}–£{high:,}."
    )


async def _try_openai_polish(deterministic_summary: str, address: str) -> str | None:
    if not settings.openai_api_key:
        return None
    prompt = (
        "Rewrite the following UK property-investment summary in 2-3 tight "
        "sentences for a professional investor. Do not add any facts, "
        "figures, or claims that are not already present in the text — "
        "only rephrase for clarity and flow.\n\n"
        f"Property: {address}\n\nText:\n{deterministic_summary}"
    )
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={
                    "model": settings.openai_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                    "max_tokens": 200,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


async def build_summary(
    listing: ListingData,
    financials: FinancialAnalysis,
    comparables: ComparablesResult,
    scores: ScoreBreakdown,
) -> str:
    deterministic = _deterministic_summary(listing, financials, comparables, scores)
    polished = await _try_openai_polish(deterministic, listing.address)
    return polished or deterministic
