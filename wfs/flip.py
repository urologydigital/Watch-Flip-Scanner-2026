"""Flip intelligence engine (spec s.5-s.9, s.12).

This is where the project stops asking "is this watch cheap?" and starts asking
"if I put money into this today, what do I realistically make, how long is my
capital tied up, and how sure are we?".

Nothing here uses a fixed percentage where evidence is available. Where evidence
is absent, the engine says so rather than substituting a plausible-looking number.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .condition import ConditionAssessment
from .config5 import CONFIG, MaxBuyStance, Phase5Config
from .economics import EconomicsEngine, ExitCosts
from .evidence import ConfidenceScore, MarketEvidence
from .risk import RiskScore

QUICK, BASE, PATIENT = "QUICK", "BASE", "PATIENT"


# --- liquidity score --------------------------------------------------------

@dataclass
class LiquidityProfile:
    score: float                       # 0-100
    band: str                          # HIGH | MEDIUM | LOW | UNKNOWN
    monthly_sale_rate: float | None
    sell_through_rate: float | None
    competing_listings: int
    absorption_days: float | None
    basis: str

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


# Brand-level liquidity floor, used only to temper the reference-level estimate.
BRAND_LIQUIDITY_MODIFIER = {
    "TUDOR": 1.0, "OMEGA": 1.0, "SEIKO": 0.95, "BREITLING": 0.9,
    "LONGINES": 0.85, "TAG HEUER": 0.85, "ORIS": 0.8,
    "RADO": 0.7, "BAUME & MERCIER": 0.65,
}


def assess_liquidity(evidence: MarketEvidence, brand: str) -> LiquidityProfile:
    """Score 0-100 from observed sales rate, sell-through and competition."""
    if not evidence.has_sold_evidence:
        return LiquidityProfile(
            score=0.0, band="UNKNOWN", monthly_sale_rate=None,
            sell_through_rate=evidence.sell_through_rate,
            competing_listings=evidence.current_active_listing_count,
            absorption_days=None,
            basis=("No observed UK sales for this reference, so liquidity cannot "
                   "be estimated."),
        )

    monthly = round(evidence.sold_count_90d / 90 * 30, 2)
    competing = evidence.current_active_listing_count
    absorption = round((competing + 1) / max(monthly, 0.01) * 30, 1)

    # Rate contributes most; sell-through and absorption temper it.
    rate_points = min(monthly / 6.0, 1.0) * 55
    st = evidence.sell_through_rate
    st_points = (st or 0.0) * 25
    absorption_points = max(0.0, 1.0 - min(absorption / 180.0, 1.0)) * 20

    raw = rate_points + st_points + absorption_points
    raw *= BRAND_LIQUIDITY_MODIFIER.get(brand.upper(), 0.85)
    score = round(max(0.0, min(100.0, raw)), 1)

    band = "HIGH" if score >= 70 else ("MEDIUM" if score >= 40 else "LOW")
    return LiquidityProfile(
        score=score, band=band, monthly_sale_rate=monthly,
        sell_through_rate=st, competing_listings=competing,
        absorption_days=absorption,
        basis=(f"{evidence.sold_count_90d} observed sale(s) in 90 days "
               f"({monthly}/month) against {competing} competing UK listing(s)."),
    )


# --- days to sell (spec s.6) ------------------------------------------------

@dataclass
class DaysToSell:
    quick: tuple[int, int] | None
    base: tuple[int, int] | None
    patient: tuple[int, int] | None
    basis: str

    def band(self, strategy: str) -> tuple[int, int] | None:
        return {QUICK: self.quick, BASE: self.base, PATIENT: self.patient}[strategy]

    # Beyond this horizon the arithmetic is still valid but the precision is
    # meaningless: "1241-2836 days" implies a confidence nobody has. Anything
    # past a year is reported as such (spec s.6, no false precision).
    MAX_ESTIMABLE_DAYS = 365

    @staticmethod
    def format(band: tuple[int, int] | None) -> str:
        if band is None:
            return "cannot be estimated"
        lo, hi = band
        cap = DaysToSell.MAX_ESTIMABLE_DAYS
        if lo >= cap:
            return f"over {cap} days"
        if hi > cap:
            return f"{lo}\u2013{cap}+ days"
        return f"{lo}\u2013{hi} days"

    def as_dict(self) -> dict[str, Any]:
        return {"quick": self.quick, "base": self.base, "patient": self.patient,
                "basis": self.basis}


def estimate_days_to_sell(evidence: MarketEvidence, liquidity: LiquidityProfile,
                          condition: ConditionAssessment) -> DaysToSell:
    """Expected holding period per strategy, as a range. No false precision."""
    if liquidity.absorption_days is None:
        return DaysToSell(None, None, None,
                          "Insufficient sold evidence to estimate a holding period.")

    absorption = liquidity.absorption_days

    # Dispersion widens the range; an inconsistent market is less predictable.
    spread = 1.0 + min((evidence.price_dispersion or 0.0) * 2, 0.5)
    # Incomplete or compromised watches sit longer at any price.
    drag = 1.0 + (0.0 if condition.condition_score >= 60 else
                  (60 - condition.condition_score) / 100.0)

    def band(centre: float, lo_mult: float, hi_mult: float) -> tuple[int, int]:
        adjusted = centre * spread * drag
        lo = max(1, int(round(adjusted * lo_mult)))
        hi = max(lo + 2, int(round(adjusted * hi_mult)))
        return (lo, hi)

    return DaysToSell(
        quick=band(absorption * 0.25, 0.6, 1.5),
        base=band(absorption * 0.75, 0.7, 1.6),
        patient=band(absorption * 1.8, 0.8, 2.0),
        basis=(f"Derived from observed sales frequency and current competition "
               f"({liquidity.basis}) adjusted for price consistency and condition."),
    )


# --- net profit (spec s.9) --------------------------------------------------

# Phase 5.1: all fee arithmetic now lives in wfs/economics.py. The helpers below
# delegate to that single engine so the dashboard, scoring, MAX BUY, ranking and
# AI payloads can never disagree about what a sale costs.

def _engine(config: Phase5Config):
    return config.engine


def cost_breakdown(sale_price: float, condition: ConditionAssessment,
                   config: Phase5Config = CONFIG) -> ExitCosts:
    """Itemised exit costs for one sale price, from the active seller profile."""
    return _engine(config).exit_costs(sale_price, condition.needs_reserve)


def effective_acquisition(listing_price: float,
                          config: Phase5Config = CONFIG) -> float:
    """Price expected to actually be paid, after any negotiation allowance."""
    return _engine(config).effective_acquisition(listing_price)


# --- resale strategies (spec s.5) -------------------------------------------

@dataclass
class Strategy:
    name: str
    sale_price: float
    costs: ExitCosts
    acquisition: float
    days: tuple[int, int] | None

    @property
    def gross_spread(self) -> float:
        return round(self.sale_price - self.acquisition, 2)

    @property
    def net_profit(self) -> float:
        return round(self.costs.net_proceeds - self.acquisition, 2)

    @property
    def net_roi_pct(self) -> float | None:
        if self.acquisition <= 0:
            return None
        return round(self.net_profit / self.acquisition * 100, 2)

    @property
    def mid_days(self) -> float | None:
        return (self.days[0] + self.days[1]) / 2 if self.days else None

    @property
    def profit_per_30d(self) -> float | None:
        if not self.mid_days or self.mid_days <= 0:
            return None
        return round(self.net_profit / self.mid_days * 30, 2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sale_price": self.sale_price,
            "acquisition": self.acquisition,
            "gross_spread": self.gross_spread,
            "estimated_costs": self.costs.total,
            "cost_lines": self.costs.lines(),
            "net_profit": self.net_profit,
            "net_roi_pct": self.net_roi_pct,
            "days": self.days,
            "days_label": DaysToSell.format(self.days),
            "profit_per_30d": self.profit_per_30d,
        }


def build_strategies(evidence: MarketEvidence, condition: ConditionAssessment,
                     days: DaysToSell, acquisition: float,
                     config: Phase5Config = CONFIG) -> dict[str, Strategy]:
    """QUICK / BASE / PATIENT, priced from the evidence band, not fixed percentages."""
    low, mid, high = evidence.valuation_band()
    if mid is None:
        return {}

    m = condition.value_multiplier
    quick_price = round((low if low else mid * 0.93) * m, 2)
    base_price = round(mid * m, 2)
    patient_price = round((high if high else mid * 1.07) * m, 2)

    quick_price = min(quick_price, base_price)
    patient_price = max(patient_price, base_price)

    out: dict[str, Strategy] = {}
    for name, price in ((QUICK, quick_price), (BASE, base_price), (PATIENT, patient_price)):
        out[name] = Strategy(
            name=name,
            sale_price=price,
            costs=cost_breakdown(price, condition, config),
            acquisition=acquisition,
            days=days.band(name),
        )
    return out


# --- MAX BUY (spec s.8) -----------------------------------------------------

@dataclass
class MaxBuyResult:
    conservative: float | None
    standard: float | None
    aggressive: float | None
    aggressive_permitted: bool
    anchor_low: float | None
    anchor_mid: float | None
    explanation: str
    detail: dict[str, Any] = field(default_factory=dict)

    def for_stance(self, stance: str) -> float | None:
        return {"CONSERVATIVE": self.conservative, "STANDARD": self.standard,
                "AGGRESSIVE": self.aggressive}.get(stance.upper())

    def as_dict(self) -> dict[str, Any]:
        return {
            "conservative": self.conservative,
            "standard": self.standard,
            "aggressive": self.aggressive,
            "aggressive_permitted": self.aggressive_permitted,
            "explanation": self.explanation,
            **self.detail,
        }


def _max_buy_for(stance: MaxBuyStance, anchor: float, condition: ConditionAssessment,
                 risk: RiskScore, config: Phase5Config) -> float:
    """One stance, computed by the shared economics engine.

    Uses STRUCTURAL risk only, so a rising auction bid cannot raise MAX BUY.

    Phase 5.1 correction: the negotiation allowance is NOT applied here. It
    already reduces the acquisition price that MAX BUY is compared against, so
    applying it again inflated MAX BUY and double-counted the same benefit.
    """
    resale = anchor * condition.value_multiplier
    return _engine(config).max_buy(
        resale_anchor=resale,
        required_margin=stance.required_margin,
        uncertainty_buffer=stance.uncertainty_buffer,
        risk_buffer=risk.structural_buffer,
        needs_service_reserve=condition.needs_reserve,
    )


def calculate_max_buy(evidence: MarketEvidence, condition: ConditionAssessment,
                      risk: RiskScore, confidence: ConfidenceScore,
                      liquidity: LiquidityProfile,
                      config: Phase5Config = CONFIG) -> MaxBuyResult:
    """Three stances. Aggressive is gated on genuine confidence and liquidity."""
    low, mid, _ = evidence.valuation_band()
    if mid is None:
        return MaxBuyResult(None, None, None, False, None, None,
                            "Insufficient Market Evidence — MAX BUY cannot be calculated.")

    anchor_low = low or mid
    cfg = config.max_buy
    engine = _engine(config)

    conservative = _max_buy_for(cfg.conservative, anchor_low, condition, risk, config)
    standard = _max_buy_for(cfg.standard, anchor_low, condition, risk, config)

    permitted = (confidence.score >= cfg.aggressive_min_confidence
                 and liquidity.score >= cfg.aggressive_min_liquidity)
    aggressive = (_max_buy_for(cfg.aggressive, mid, condition, risk, config)
                  if permitted else None)

    return MaxBuyResult(
        conservative=conservative,
        standard=standard,
        aggressive=aggressive,
        aggressive_permitted=permitted,
        anchor_low=round(anchor_low, 2),
        anchor_mid=round(mid, 2),
        explanation=(
            f"Anchored on conservative resale £{anchor_low:,.0f} "
            f"(condition multiplier {condition.value_multiplier:.2f}), less "
            f"{engine.profile.total_percentage_fees:.2%} platform fees and "
            f"£{engine.profile.fixed_physical_costs:,.0f} seller costs "
            f"[{engine.mode_label}], less the stance margin and a "
            f"{risk.structural_buffer:.0%} structural risk buffer."
        ),
        detail={
            "risk_structural_buffer": risk.structural_buffer,
            "condition_multiplier": condition.value_multiplier,
            "seller_mode": engine.mode_label,
            "aggressive_gate": (
                f"Requires confidence ≥ {cfg.aggressive_min_confidence:.0f} "
                f"(actual {confidence.score:.0f}) and liquidity ≥ "
                f"{cfg.aggressive_min_liquidity:.0f} (actual {liquidity.score:.0f})."
            ),
        },
    )


# --- capital velocity (spec s.7) --------------------------------------------

@dataclass
class CapitalVelocity:
    score: float
    profit_per_30d: float | None
    expected_holding_days: float | None
    annual_turns: float | None
    capital_locked: float
    annualised_return_pct: float | None
    note: str

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def capital_velocity(base: Strategy | None,
                     config: Phase5Config = CONFIG) -> CapitalVelocity:
    """Reward money that comes back quickly.

    £180 in 20 days beats £300 in 120 days, and the score reflects that. The
    annualised figure is reported but deliberately not the decision metric —
    extrapolating one fast flip to a full year overstates what is achievable.
    """
    if base is None or base.mid_days is None:
        return CapitalVelocity(0.0, None, None, None,
                               base.acquisition if base else 0.0, None,
                               "Holding period unknown — capital velocity cannot be scored.")

    days = base.mid_days
    per30 = base.profit_per_30d or 0.0
    turns = round(365 / days, 2) if days > 0 else None
    annualised = (round(base.net_roi_pct * turns, 1)
                  if base.net_roi_pct is not None and turns else None)

    # Profit rate against the configured 'excellent' benchmark.
    rate_component = min(per30 / config.velocity.excellent_profit_per_30d, 1.0) * 70
    # Explicit penalty for long holds, independent of profit rate.
    speed_component = max(0.0, 1.0 - days / config.velocity.slow_holding_days) * 30
    score = round(max(0.0, min(100.0, rate_component + speed_component)), 1)

    return CapitalVelocity(
        score=score,
        profit_per_30d=base.profit_per_30d,
        expected_holding_days=round(days, 1),
        annual_turns=turns,
        capital_locked=base.acquisition,
        annualised_return_pct=annualised,
        note=(f"£{per30:,.0f} net profit per 30 days on £{base.acquisition:,.0f} "
              f"of capital, expected back in about {days:.0f} days. Annualised "
              "figures assume you immediately find an equally good flip, which "
              "is optimistic."),
    )


# --- flip score (spec s.12) -------------------------------------------------

@dataclass
class FlipScore:
    score: float
    components: dict[str, float] = field(default_factory=dict)
    weighted: dict[str, float] = field(default_factory=dict)
    formula: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"score": self.score, "components": self.components,
                "weighted": self.weighted, "formula": self.formula}


def _profit_quality(base: Strategy | None, config: Phase5Config) -> float:
    """Blend of absolute net profit and net ROI, both against configured minima."""
    if base is None or base.net_roi_pct is None:
        return 0.0
    d = config.decision
    profit_ratio = base.net_profit / max(d.min_net_profit_gbp, 1)
    roi_ratio = base.net_roi_pct / max(d.min_net_roi_pct, 1)
    if base.net_profit <= 0:
        return 0.0
    # Meeting the minimum scores 50; double the minimum approaches 100.
    absolute = min(profit_ratio, 2.0) / 2.0 * 100
    relative = min(roi_ratio, 2.0) / 2.0 * 100
    return round(absolute * 0.5 + relative * 0.5, 1)


def _discount_component(acquisition: float, evidence: MarketEvidence,
                        condition: ConditionAssessment) -> float:
    """How far below adjusted market the acquisition sits. Capped, not unbounded."""
    _, mid, _ = evidence.valuation_band()
    if not mid or acquisition <= 0:
        return 0.0
    adjusted = mid * condition.value_multiplier
    discount = (adjusted - acquisition) / adjusted
    if discount <= 0:
        return 0.0
    # 30% below adjusted market is a full score; beyond that adds nothing,
    # because an implausibly large discount is a risk signal, not a bonus.
    return round(min(discount / 0.30, 1.0) * 100, 1)


def calculate_flip_score(base: Strategy | None, liquidity: LiquidityProfile,
                         velocity: CapitalVelocity, confidence: ConfidenceScore,
                         condition: ConditionAssessment, risk: RiskScore,
                         evidence: MarketEvidence, acquisition: float,
                         config: Phase5Config = CONFIG) -> FlipScore:
    """Weighted 0-100 composite. Profit alone cannot carry it.

    FLIP = 0.25*profit + 0.20*liquidity + 0.15*velocity + 0.15*confidence
         + 0.10*discount + 0.10*condition + 0.05*(100 - risk)

    Then a hard gate: with insufficient evidence the score is capped, because a
    confident-looking number built on no evidence is worse than no number.
    """
    w = config.weights
    components = {
        "profit_quality": _profit_quality(base, config),
        "liquidity": liquidity.score,
        "capital_velocity": velocity.score,
        "market_confidence": confidence.score,
        "discount": _discount_component(acquisition, evidence, condition),
        "condition": condition.condition_score,
        "risk": round(100.0 - risk.total, 1),
    }
    weights = w.as_dict()
    weighted = {k: round(components[k] * weights[k], 2) for k in components}
    score = round(sum(weighted.values()) / max(w.total, 0.0001), 1)

    # Evidence gate: nothing built on seed or asking-only data may look strong.
    # Observed scanner activity is permitted a middling score — its own
    # confidence ceiling (55) already blocks a BUY, which needs 60.
    if not evidence.is_valuable:
        score = round(min(score, 35.0), 1)
    elif not evidence.has_sold_evidence:
        score = round(min(score, 55.0), 1)

    formula = " + ".join(f"{weights[k]:.2f}×{k}" for k in components)
    return FlipScore(round(max(0.0, min(100.0, score)), 1), components, weighted, formula)
