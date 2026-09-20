"""The single authoritative economics engine (spec 5.1 §5, §6, §26).

Every financial figure in the application comes from here: exit costs, net
profit, ROI and MAX BUY. Nothing else computes fees. If a number appears on the
dashboard, in the Flip Score, in ranking or in an AI payload, it was produced by
this module.

The headline correction in Phase 5.1: **a UK private seller on eBay does not pay
final value or payment processing fees.** The previous default deducted roughly
14.8% from every sale, which understated net profit and, in turn, suppressed
MAX BUY on every candidate. That default is gone.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, replace
from typing import Any

# --- Seller profiles --------------------------------------------------------
#
# UK_PRIVATE  Default profile for this project. Models a UK private seller on
#             eBay, who currently pays no transaction or payment processing fee.
#             Real seller-side costs (postage, insurance, packaging) still apply.
#             Every value is configurable — this is an application default, not a
#             guarantee about future platform policy.
#
# UK_BUSINESS Estimated business-seller economics. Business fees are NOT
#             universally fixed: they vary by category, shop subscription tier,
#             account history and promotions. The shipped values are a plausible
#             watches-category estimate only, flagged `is_estimate=True`, and
#             should be replaced with figures from a real eBay invoice.
#
# CUSTOM      Fully user-defined economics, for any other platform or
#             arrangement. Lets a future fee change be absorbed by configuration
#             rather than a code change.
#
UK_PRIVATE = "UK_PRIVATE"
UK_BUSINESS = "UK_BUSINESS"
CUSTOM = "CUSTOM"

PROFILE_LABEL = {
    UK_PRIVATE: "UK Private Seller",
    UK_BUSINESS: "UK Business Seller",
    CUSTOM: "Custom",
}


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _b(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class SellerProfile:
    """One seller's cost model. Percentages are fractions of the sale price.

    Every percentage is a SEPARATE, NAMED cost. Nothing is bundled into a
    generic 'selling fee', so a change in one platform charge never silently
    moves another.
    """

    name: str = UK_PRIVATE
    label: str = "UK Private Seller"

    # --- platform percentage costs ---------------------------------------
    transaction_fee_pct: float = 0.0        # eBay final value / transaction fee
    payment_processing_fee_pct: float = 0.0  # card / payment handling
    regulatory_operating_fee_pct: float = 0.0  # business-seller regulatory fee
    fixed_fee_gbp: float = 0.0              # per-order fixed charge

    # --- optional, off by default ----------------------------------------
    promoted_listing_enabled: bool = False
    promoted_listing_fee_pct: float = 0.0
    international_sale: bool = False
    international_fee_pct: float = 0.0

    # --- seller-side physical costs --------------------------------------
    shipping_cost_gbp: float = 12.0
    insurance_cost_gbp: float = 15.0
    packaging_cost_gbp: float = 5.0
    authentication_cost_gbp: float = 0.0
    miscellaneous_cost_gbp: float = 0.0

    # --- reserves and allowances -----------------------------------------
    # Held back against service/repair, only when the condition warrants it.
    service_reserve_pct: float = 0.02
    # Discount you expect to negotiate when BUYING. Applied to the acquisition
    # price only — never also to MAX BUY (see the double-counting note below).
    negotiation_allowance_pct: float = 0.0

    # --- provenance -------------------------------------------------------
    is_estimate: bool = False
    notes: str = ""

    @property
    def total_percentage_fees(self) -> float:
        """Every percentage charge that applies to a sale, summed once."""
        total = (self.transaction_fee_pct + self.payment_processing_fee_pct
                 + self.regulatory_operating_fee_pct)
        if self.promoted_listing_enabled:
            total += self.promoted_listing_fee_pct
        if self.international_sale:
            total += self.international_fee_pct
        return round(total, 6)

    @property
    def fixed_physical_costs(self) -> float:
        return round(self.shipping_cost_gbp + self.insurance_cost_gbp
                     + self.packaging_cost_gbp + self.authentication_cost_gbp
                     + self.miscellaneous_cost_gbp + self.fixed_fee_gbp, 2)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def with_overrides(self, **kwargs: Any) -> "SellerProfile":
        return replace(self, **kwargs)


def uk_private_profile() -> SellerProfile:
    """UK private seller: no eBay transaction or payment processing fee.

    Real seller-side costs (postage, insurance, packaging) still apply, and
    promoted listings or international sales can be switched on individually.
    """
    return SellerProfile(
        name=UK_PRIVATE,
        label=PROFILE_LABEL[UK_PRIVATE],
        transaction_fee_pct=_f("WFS51_PRIVATE_TRANSACTION_FEE_PCT", 0.0),
        payment_processing_fee_pct=_f("WFS51_PRIVATE_PAYMENT_FEE_PCT", 0.0),
        regulatory_operating_fee_pct=0.0,
        fixed_fee_gbp=_f("WFS51_PRIVATE_FIXED_FEE_GBP", 0.0),
        promoted_listing_enabled=_b("WFS51_PROMOTED_ENABLED", False),
        promoted_listing_fee_pct=_f("WFS51_PROMOTED_FEE_PCT", 0.0),
        international_sale=_b("WFS51_INTERNATIONAL_SALE", False),
        international_fee_pct=_f("WFS51_INTERNATIONAL_FEE_PCT", 0.0),
        shipping_cost_gbp=_f("WFS51_SHIPPING_GBP", 12.0),
        insurance_cost_gbp=_f("WFS51_INSURANCE_GBP", 15.0),
        packaging_cost_gbp=_f("WFS51_PACKAGING_GBP", 5.0),
        authentication_cost_gbp=_f("WFS51_AUTHENTICATION_GBP", 0.0),
        miscellaneous_cost_gbp=_f("WFS51_MISC_GBP", 0.0),
        service_reserve_pct=_f("WFS51_SERVICE_RESERVE_PCT", 0.02),
        negotiation_allowance_pct=_f("WFS51_NEGOTIATION_PCT", 0.0),
        is_estimate=False,
        notes=("UK private seller. No eBay transaction or payment processing fee "
               "is assumed. Postage, insurance and packaging still apply. "
               "Verify against your own recent sales."),
    )


def uk_business_profile() -> SellerProfile:
    """UK business seller — ESTIMATE ONLY.

    Business fees vary by category, shop subscription and account. The defaults
    here are a plausible watches-category estimate, not a quote. Replace them
    with your own figures from an actual eBay invoice.
    """
    return SellerProfile(
        name=UK_BUSINESS,
        label=PROFILE_LABEL[UK_BUSINESS],
        transaction_fee_pct=_f("WFS51_BUSINESS_TRANSACTION_FEE_PCT", 0.093),
        payment_processing_fee_pct=_f("WFS51_BUSINESS_PAYMENT_FEE_PCT", 0.0),
        regulatory_operating_fee_pct=_f("WFS51_BUSINESS_REGULATORY_FEE_PCT", 0.0035),
        fixed_fee_gbp=_f("WFS51_BUSINESS_FIXED_FEE_GBP", 0.30),
        promoted_listing_enabled=_b("WFS51_PROMOTED_ENABLED", False),
        promoted_listing_fee_pct=_f("WFS51_PROMOTED_FEE_PCT", 0.02),
        international_sale=_b("WFS51_INTERNATIONAL_SALE", False),
        international_fee_pct=_f("WFS51_INTERNATIONAL_FEE_PCT", 0.013),
        shipping_cost_gbp=_f("WFS51_SHIPPING_GBP", 12.0),
        insurance_cost_gbp=_f("WFS51_INSURANCE_GBP", 15.0),
        packaging_cost_gbp=_f("WFS51_PACKAGING_GBP", 5.0),
        authentication_cost_gbp=_f("WFS51_AUTHENTICATION_GBP", 0.0),
        miscellaneous_cost_gbp=_f("WFS51_MISC_GBP", 0.0),
        service_reserve_pct=_f("WFS51_SERVICE_RESERVE_PCT", 0.02),
        negotiation_allowance_pct=_f("WFS51_NEGOTIATION_PCT", 0.0),
        is_estimate=True,
        notes=("ESTIMATE ONLY. Business fees depend on category, shop tier and "
               "account. Replace these with figures from a real eBay invoice "
               "before relying on any number derived from them."),
    )


def custom_profile(**overrides: Any) -> SellerProfile:
    """Fully manual configuration, so a platform change needs no code change."""
    base = SellerProfile(
        name=CUSTOM,
        label=PROFILE_LABEL[CUSTOM],
        is_estimate=True,
        notes="Custom profile — all values supplied by the user.",
    )
    return base.with_overrides(**overrides) if overrides else base


PROFILE_BUILDERS = {
    UK_PRIVATE: uk_private_profile,
    UK_BUSINESS: uk_business_profile,
    CUSTOM: custom_profile,
}


def get_profile(name: str = UK_PRIVATE, **overrides: Any) -> SellerProfile:
    builder = PROFILE_BUILDERS.get((name or UK_PRIVATE).upper(), uk_private_profile)
    profile = builder()
    return profile.with_overrides(**overrides) if overrides else profile


# --- cost breakdown ---------------------------------------------------------

@dataclass
class ExitCosts:
    """Every deduction between the sale price and money in your pocket.

    Each line appears exactly ONCE. In particular:
      - payment processing is its own line, never folded into the transaction fee
      - shipping appears once, as a seller-side cost
      - the service reserve appears here only, never inside the risk buffer
      - the negotiation allowance is NOT here; it reduces the acquisition price
        instead, so the benefit is counted once (see effective_acquisition)
    """

    sale_price: float
    transaction_fee: float = 0.0
    payment_processing_fee: float = 0.0
    regulatory_operating_fee: float = 0.0
    promoted_listing_fee: float = 0.0
    international_fee: float = 0.0
    fixed_fee: float = 0.0
    shipping: float = 0.0
    insurance: float = 0.0
    packaging: float = 0.0
    authentication: float = 0.0
    miscellaneous: float = 0.0
    service_reserve: float = 0.0

    LINE_ITEMS = ("transaction_fee", "payment_processing_fee",
                  "regulatory_operating_fee", "promoted_listing_fee",
                  "international_fee", "fixed_fee", "shipping", "insurance",
                  "packaging", "authentication", "miscellaneous",
                  "service_reserve")

    LABELS = {
        "transaction_fee": "Platform transaction fee",
        "payment_processing_fee": "Payment processing fee",
        "regulatory_operating_fee": "Regulatory operating fee",
        "promoted_listing_fee": "Promoted listing fee",
        "international_fee": "International/cross-border fee",
        "fixed_fee": "Fixed per-order fee",
        "shipping": "Postage",
        "insurance": "Insurance",
        "packaging": "Packaging",
        "authentication": "Authentication",
        "miscellaneous": "Miscellaneous",
        "service_reserve": "Service reserve",
    }

    @property
    def total(self) -> float:
        return round(sum(getattr(self, k) for k in self.LINE_ITEMS), 2)

    @property
    def net_proceeds(self) -> float:
        return round(self.sale_price - self.total, 2)

    def lines(self, include_zero: bool = False) -> list[tuple[str, float]]:
        """Human-readable breakdown, zero-cost lines hidden by default."""
        out = []
        for key in self.LINE_ITEMS:
            value = getattr(self, key)
            if value or include_zero:
                out.append((self.LABELS[key], round(value, 2)))
        return out

    def as_dict(self) -> dict[str, Any]:
        d = {k: round(getattr(self, k), 2) for k in self.LINE_ITEMS}
        d.update({"sale_price": self.sale_price, "total_exit_costs": self.total,
                  "net_proceeds": self.net_proceeds})
        return d


@dataclass
class ProfitResult:
    acquisition_price: float
    listing_price: float
    negotiation_allowance: float
    costs: ExitCosts

    @property
    def gross_spread(self) -> float:
        return round(self.costs.sale_price - self.acquisition_price, 2)

    @property
    def net_profit(self) -> float:
        return round(self.costs.net_proceeds - self.acquisition_price, 2)

    @property
    def net_roi_pct(self) -> float | None:
        if self.acquisition_price <= 0:
            return None
        return round(self.net_profit / self.acquisition_price * 100, 2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "listing_price": self.listing_price,
            "negotiation_allowance": self.negotiation_allowance,
            "acquisition_price": self.acquisition_price,
            "gross_spread": self.gross_spread,
            "net_profit": self.net_profit,
            "net_roi_pct": self.net_roi_pct,
            **self.costs.as_dict(),
        }


class EconomicsEngine:
    """The one place fees are calculated. Construct with a SellerProfile."""

    def __init__(self, profile: SellerProfile | None = None):
        self.profile = profile or uk_private_profile()

    # -- naming ------------------------------------------------------------
    @property
    def mode_label(self) -> str:
        suffix = " (estimate)" if self.profile.is_estimate else ""
        return f"{self.profile.label}{suffix}"

    # -- costs -------------------------------------------------------------
    def exit_costs(self, sale_price: float, needs_service_reserve: bool = False
                   ) -> ExitCosts:
        p = self.profile
        return ExitCosts(
            sale_price=round(sale_price, 2),
            transaction_fee=round(sale_price * p.transaction_fee_pct, 2),
            payment_processing_fee=round(sale_price * p.payment_processing_fee_pct, 2),
            regulatory_operating_fee=round(
                sale_price * p.regulatory_operating_fee_pct, 2),
            promoted_listing_fee=round(
                sale_price * p.promoted_listing_fee_pct, 2)
            if p.promoted_listing_enabled else 0.0,
            international_fee=round(sale_price * p.international_fee_pct, 2)
            if p.international_sale else 0.0,
            fixed_fee=round(p.fixed_fee_gbp, 2),
            shipping=round(p.shipping_cost_gbp, 2),
            insurance=round(p.insurance_cost_gbp, 2),
            packaging=round(p.packaging_cost_gbp, 2),
            authentication=round(p.authentication_cost_gbp, 2),
            miscellaneous=round(p.miscellaneous_cost_gbp, 2),
            service_reserve=round(sale_price * p.service_reserve_pct, 2)
            if needs_service_reserve else 0.0,
        )

    def net_proceeds(self, sale_price: float,
                     needs_service_reserve: bool = False) -> float:
        return self.exit_costs(sale_price, needs_service_reserve).net_proceeds

    def effective_acquisition(self, listing_price: float) -> float:
        """Price you expect to actually pay.

        The negotiation allowance is applied HERE and nowhere else. Applying it
        again when computing MAX BUY would count the same benefit twice — a bug
        corrected in Phase 5.1.
        """
        return round(listing_price * (1 - self.profile.negotiation_allowance_pct), 2)

    def profit(self, sale_price: float, listing_price: float,
               needs_service_reserve: bool = False) -> ProfitResult:
        acquisition = self.effective_acquisition(listing_price)
        return ProfitResult(
            acquisition_price=acquisition,
            listing_price=round(listing_price, 2),
            negotiation_allowance=round(listing_price - acquisition, 2),
            costs=self.exit_costs(sale_price, needs_service_reserve),
        )

    # -- max buy -----------------------------------------------------------
    def max_buy(self, resale_anchor: float, required_margin: float,
                uncertainty_buffer: float = 0.0, risk_buffer: float = 0.0,
                needs_service_reserve: bool = False) -> float:
        """Highest price worth paying, given one resale anchor and one stance.

        Uses net proceeds from this profile, so a private seller (no platform
        fees) legitimately supports a higher MAX BUY than a business seller.

        The negotiation allowance is deliberately NOT applied here: it already
        reduced the acquisition price that MAX BUY is compared against.
        """
        net = self.net_proceeds(resale_anchor, needs_service_reserve)
        value = net * (1 - required_margin) * (1 - uncertainty_buffer) * (1 - risk_buffer)
        return round(max(value, 0.0), 2)

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode_label, "profile": self.profile.as_dict(),
                "total_percentage_fees": self.profile.total_percentage_fees,
                "fixed_physical_costs": self.profile.fixed_physical_costs}
