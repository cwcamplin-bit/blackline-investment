"""UK residential financial calculations for a buy-to-let purchase.

These are the numbers behind analyse.html's "Financial analysis" panel:
purchase price, Stamp Duty, deposit, (interest-only BTL) mortgage payment,
estimated rent, net monthly cashflow, and net yield / ROI.

Everything here is deterministic arithmetic — no external data required —
so it is the most "reliable" part of the pipeline by construction. The one
thing that *does* drift over time is the SDLT bands themselves, which
Parliament changes at Budgets; see SDLT_BANDS_ENGLAND below and the README
note about keeping it current.

Disclaimer: this is a decision-support estimate, not tax or financial
advice. Blackline should tell users to confirm SDLT and lending terms with
a solicitor/broker before exchanging contracts.
"""
from __future__ import annotations

from dataclasses import dataclass

# England & Northern Ireland residential SDLT bands, effective from
# 1 April 2025 (Scotland uses LBTT and Wales uses LTT — out of scope for v1).
# Source: gov.uk Stamp Duty Land Tax rates. Verify against gov.uk before
# relying on this for a live product — thresholds move at most Budgets.
SDLT_BANDS_ENGLAND: list[tuple[int | None, float]] = [
    (125_000, 0.00),
    (250_000, 0.02),
    (925_000, 0.05),
    (1_500_000, 0.10),
    (None, 0.12),
]

# The additional-property surcharge (second homes / BTL) as of the
# 31 Oct 2024 Budget. Applied on top of the standard bands above.
ADDITIONAL_PROPERTY_SURCHARGE = 0.05

# First-time-buyer relief: 0% to £300k, 5% £300k-£500k, no relief above
# £500k (standard bands apply instead).
FTB_RELIEF_THRESHOLD = 300_000
FTB_RELIEF_CEILING = 500_000


def _banded_tax(price: int, bands: list[tuple[int | None, float]]) -> float:
    tax = 0.0
    lower = 0
    for upper, rate in bands:
        portion = max(0, price - lower) if upper is None else max(0, min(price, upper) - lower)
        tax += portion * rate
        if upper is not None:
            lower = upper
            if price <= upper:
                break
    return tax


def calculate_sdlt(price: int, is_additional_property: bool = True, is_first_time_buyer: bool = False) -> int:
    """Returns SDLT payable in whole pounds."""
    if is_first_time_buyer and not is_additional_property and price <= FTB_RELIEF_CEILING:
        bands = [(FTB_RELIEF_THRESHOLD, 0.00), (FTB_RELIEF_CEILING, 0.05)]
    else:
        bands = SDLT_BANDS_ENGLAND

    tax = _banded_tax(price, bands)

    if is_additional_property:
        # The surcharge applies at a flat +5 percentage points on every
        # band (including the 0% band), which is mathematically identical
        # to a flat 5% of the full price.
        tax += price * ADDITIONAL_PROPERTY_SURCHARGE

    return round(tax)


@dataclass
class FinancialAnalysis:
    purchase: int
    stamp_duty: int
    deposit: int
    loan_amount: int
    mortgage_monthly: int          # interest-only
    rent_monthly: int
    monthly_opex: int               # management + maintenance + voids + insurance
    cashflow_monthly: int            # rent - mortgage - opex
    gross_yield_pct: float
    net_yield_pct: float
    total_cash_invested: int
    annual_cashflow: int
    roi_pct: float                   # cash-on-cash

    def as_ui_dict(self) -> dict:
        """Matches analyse.html's `financials` object exactly."""
        return {
            "purchase": self.purchase,
            "stampDuty": self.stamp_duty,
            "deposit": self.deposit,
            "mortgage": self.mortgage_monthly,
            "rent": self.rent_monthly,
            "cashflow": self.cashflow_monthly,
            "yieldPct": f"{self.net_yield_pct:.1f}%",
            "roiPct": f"{self.roi_pct:.1f}%",
        }


def analyse_financials(
    price: int,
    monthly_rent: int,
    *,
    ltv_percent: float = 75.0,
    mortgage_rate_percent: float = 5.9,
    is_additional_property: bool = True,
    management_fee_percent: float = 10.0,
    maintenance_percent: float = 6.0,
    void_allowance_percent: float = 4.0,
    insurance_monthly: float = 18.0,
    acquisition_fees: int = 2_000,   # legal + survey + mortgage arrangement, illustrative flat estimate
) -> FinancialAnalysis:
    stamp_duty = calculate_sdlt(price, is_additional_property=is_additional_property)

    loan_amount = round(price * ltv_percent / 100)
    deposit = price - loan_amount
    mortgage_monthly = round(loan_amount * (mortgage_rate_percent / 100) / 12)

    opex_pct = (management_fee_percent + maintenance_percent + void_allowance_percent) / 100
    monthly_opex = round(monthly_rent * opex_pct + insurance_monthly)

    cashflow_monthly = monthly_rent - mortgage_monthly - monthly_opex

    annual_rent = monthly_rent * 12
    annual_opex = monthly_opex * 12
    annual_noi = annual_rent - annual_opex  # net operating income, pre-financing

    gross_yield_pct = (annual_rent / price * 100) if price else 0.0
    net_yield_pct = (annual_noi / price * 100) if price else 0.0

    total_cash_invested = deposit + stamp_duty + acquisition_fees
    annual_cashflow = cashflow_monthly * 12
    roi_pct = (annual_cashflow / total_cash_invested * 100) if total_cash_invested else 0.0

    return FinancialAnalysis(
        purchase=price,
        stamp_duty=stamp_duty,
        deposit=deposit,
        loan_amount=loan_amount,
        mortgage_monthly=mortgage_monthly,
        rent_monthly=monthly_rent,
        monthly_opex=monthly_opex,
        cashflow_monthly=cashflow_monthly,
        gross_yield_pct=gross_yield_pct,
        net_yield_pct=net_yield_pct,
        total_cash_invested=total_cash_invested,
        annual_cashflow=annual_cashflow,
        roi_pct=roi_pct,
    )
