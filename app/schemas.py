"""Pydantic models.

The response shape of AnalysisResult is deliberately a 1:1 match for the
`PROPERTIES` records already consumed by analyse.html's renderReport()
(see /Blackline Frontend/analyse.html), so the front end needs no changes
beyond pointing its fetch() at this API instead of the mock dataset.
"""
from __future__ import annotations

from enum import Enum
from pydantic import BaseModel, Field


class AnalyzeRequest(BaseModel):
    url: str = Field(..., description="A Rightmove property listing URL, e.g. "
                                       "https://www.rightmove.co.uk/properties/154829201")
    # Optional per-request overrides of the default BTL assumptions.
    ltv_percent: float | None = None
    mortgage_rate_percent: float | None = None
    mortgage_term_years: int | None = None
    is_additional_property: bool = Field(
        True, description="Whether SDLT should include the additional-property "
                           "surcharge. Defaults to True since Blackline's users "
                           "are investors buying a second+ property."
    )


class Verdict(str, Enum):
    strongbuy = "strongbuy"
    buy = "buy"
    invest = "invest"
    caution = "caution"
    pass_ = "pass"


VERDICT_LABELS = {
    Verdict.strongbuy: "STRONG BUY",
    Verdict.buy: "BUY",
    Verdict.invest: "INVESTIGATE FURTHER",
    Verdict.caution: "PROCEED WITH CAUTION",
    Verdict.pass_: "PASS",
}


class Scores(BaseModel):
    growth: int
    valueAdd: int
    security: int
    cashflow: int


class Clauses(BaseModel):
    cashflow: str
    growth: str
    valueAdd: str
    security: str


class Financials(BaseModel):
    purchase: int
    stampDuty: int
    deposit: int
    mortgage: int          # monthly, interest-only
    rent: int               # monthly, estimated achievable rent
    cashflow: int            # monthly net cashflow after mortgage + opex
    yieldPct: str             # e.g. "6.4%" (net yield)
    roiPct: str                # e.g. "14.2%" (cash-on-cash ROI)


class Strategy(BaseModel):
    btl: int
    brrr: int
    flip: int


class AnalysisResult(BaseModel):
    address: str
    price: int
    beds: int | None
    type: str
    sourceUrl: str

    verdict: Verdict
    verdictLabel: str
    confidence: int

    scores: Scores
    clauses: Clauses
    financials: Financials
    strategy: Strategy

    comparables: list[list[str]]   # [["12 Ashworth Rd · 0.1mi", "£241,000"], ...]
    strengths: list[str]
    risks: list[str]
    summary: str

    # Transparency fields not shown in the current UI but useful for
    # debugging / future UI surfaces — safe to ignore client-side.
    assumptions: dict
    data_quality: dict


class ErrorResponse(BaseModel):
    error: str
    detail: str
