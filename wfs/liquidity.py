"""Liquidity engine (spec s.11).

Estimates time-to-sale from observed sales frequency versus current competing UK
listings. Every output is an estimate, never a guarantee, and the engine returns
UNKNOWN rather than a fabricated day range when sold evidence is absent.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import ILLIQUID_BRANDS
from .sold_market import SoldEvidence

QUICK, BASE, PATIENT = "QUICK", "BASE", "PATIENT"
UNKNOWN = "UNKNOWN"


@dataclass
class Liquidity:
    reference: str
    exact_sales: int
    family_sales: int
    period_days: int
    competing_listings: int
    median_sale_price: float | None
    price_dispersion: float | None
    monthly_sale_rate: float | None
    absorption_days: float | None
    rating: str                     # HIGH | MEDIUM | LOW | UNKNOWN
    days_to_sale: dict[str, tuple[int, int] | None]
    basis: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "observed_exact_sales": self.exact_sales,
            "observed_family_sales": self.family_sales,
            "period_days": self.period_days,
            "competing_listings": self.competing_listings,
            "median_sale_price": self.median_sale_price,
            "price_dispersion": self.price_dispersion,
            "monthly_sale_rate": self.monthly_sale_rate,
            "absorption_days": self.absorption_days,
            "rating": self.rating,
            "days_to_sale": self.days_to_sale,
            "basis": self.basis,
        }


def _band(days: float, lo_mult: float, hi_mult: float) -> tuple[int, int]:
    return (max(1, int(round(days * lo_mult))), max(2, int(round(days * hi_mult))))


def assess(reference: str, brand: str, sold: SoldEvidence,
           competing_listings: int) -> Liquidity:
    """Estimate liquidity and time-to-sale for one reference."""
    exact = sold.exact_sale_count if sold.has_evidence else 0
    family = sold.family_sale_count if sold.has_evidence else 0
    period = sold.period_days

    if exact == 0:
        return Liquidity(
            reference=reference,
            exact_sales=0,
            family_sales=family,
            period_days=period,
            competing_listings=competing_listings,
            median_sale_price=sold.median_price,
            price_dispersion=sold.price_dispersion,
            monthly_sale_rate=None,
            absorption_days=None,
            rating=UNKNOWN,
            days_to_sale={QUICK: None, BASE: None, PATIENT: None},
            basis=("No observed UK sales for this reference, so time-to-sale cannot "
                   f"be estimated. {competing_listings} competing UK listing(s) "
                   "currently observed."),
        )

    monthly_rate = round(exact / period * 30, 2)
    # Absorption: how long the current shelf of competing listings takes to clear,
    # plus your own unit joining the back of the queue.
    queue = competing_listings + 1
    absorption_days = round(queue / max(monthly_rate, 0.01) * 30, 1)

    if monthly_rate >= 3 and absorption_days <= 45:
        rating = "HIGH"
    elif monthly_rate >= 1 and absorption_days <= 120:
        rating = "MEDIUM"
    else:
        rating = "LOW"

    if brand.upper() in ILLIQUID_BRANDS and rating == "HIGH":
        rating = "MEDIUM"  # thinner UK demand — do not overstate

    # Wide dispersion means the market is inconsistent: widen the estimates.
    spread_mult = 1.0 + min((sold.price_dispersion or 0.0) * 2, 0.5)

    days = {
        QUICK: _band(absorption_days * 0.25 * spread_mult, 0.6, 1.6),
        BASE: _band(absorption_days * 0.8 * spread_mult, 0.7, 1.8),
        PATIENT: _band(absorption_days * 2.0 * spread_mult, 0.8, 2.2),
    }

    return Liquidity(
        reference=reference,
        exact_sales=exact,
        family_sales=family,
        period_days=period,
        competing_listings=competing_listings,
        median_sale_price=sold.median_price,
        price_dispersion=sold.price_dispersion,
        monthly_sale_rate=monthly_rate,
        absorption_days=absorption_days,
        rating=rating,
        days_to_sale=days,
        basis=("Estimated based on observed UK sales frequency and current "
               f"competition: {exact} observed sale(s) in {period} days against "
               f"{competing_listings} competing listing(s)."),
    )
