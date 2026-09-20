# Phase 5 — Flip Intelligence: methodology

Phase 5 changes the question the scanner answers.

Phases 1–4 answered *"is this watch cheap?"*. Phase 5 answers:

> If I put my money into this watch today, how much am I realistically likely to
> make, how long will my capital be tied up, how confident are we in that
> estimate, and is this one of the best opportunities currently available?

A cheap watch is not automatically a good flip. The engine is built so that a
large discount cannot, on its own, produce an attractive score.

---

## 1. Pipeline

```
eBay UK listings (cached)
   ↓
match to active watchlist references
   ↓
market evidence            → evidence level + Market Confidence Score (0–100)
   ↓
liquidity                  → liquidity score (0–100), sell-through, absorption
   ↓
condition parsing          → completeness, condition score, value multiplier
   ↓
risk scoring               → structural risk + price scrutiny (0–100)
   ↓
days-to-sell               → Quick / Base / Patient ranges
   ↓
net profit                 → three strategies, fully costed
   ↓
MAX BUY                    → Conservative / Standard / Aggressive
   ↓
capital velocity           → profit per 30 days, turns, holding period
   ↓
FLIP SCORE                 → weighted composite (0–100)
   ↓
BUY / WATCH / PASS         → with explanation and, for WATCH, a target offer
   ↓
AI triage (only if useful) → may downgrade, never upgrade
```

---

## 2. Evidence hierarchy

Evidence levels are never silently mixed. A `MarketEvidence` object carries
exactly one level, and that level caps how confident the system may be.

| Level | Confidence ceiling | Scale factor |
|---|---|---|
| `REFERENCE_EXACT` | 100 | 1.00 |
| `REFERENCE_VARIANT` | 85 | 0.88 |
| `MODEL_FAMILY` | 65 | 0.70 |
| `ASKING_ONLY` | 45 | 0.45 |
| `SEED_PLACEHOLDER` | 25 | — |
| `NONE` | 0 | — |

Seed placeholders are marked `UNVERIFIED` in the UI, capped at 25 confidence,
and **cannot independently trigger a BUY**. Where nothing is available the
system displays *Insufficient Market Evidence* rather than substituting a
plausible-looking number.

Model-family evidence may be used when exact-reference data is thin, but it is
always labelled and always scaled down.

### Market Confidence Score (0–100)

Five components, summed then scaled and capped by evidence level:

| Component | Max | What it measures |
|---|---|---|
| Volume | 30 | Sales observed in 90 days |
| Recency | 20 | How recent those sales are |
| Consistency | 20 | Price dispersion (tight clustering scores higher) |
| Sample | 15 | Number of priced observations |
| Corroboration | 10 | Current listings confirming an active market |

Bands: 90+ Excellent, 75+ Strong, 60+ Moderate, 40+ Weak, below 40 Insufficient.

---

## 3. Valuation

The valuation band comes from the observed sold distribution — the 25th
percentile, median and 75th percentile — not from asking prices and not from a
fixed percentage of retail.

Where only asking prices exist, the engine takes the **lower half** of the
distribution and applies a further 7% haircut, because asking prices
systematically overstate what is achieved. Such a valuation is capped at
`ASKING_ONLY` confidence and cannot support a BUY.

The band is then multiplied by the **condition value multiplier** (see §5).

---

## 4. Net profit, not gross spread

Gross spread is reported but never described as profit.

> ### ⚠️ LEGACY PHASE 5 BEHAVIOUR — SUPERSEDED BY PHASE 5.1
>
> The percentages in the block immediately below are the **original Phase 5
> defaults and are no longer used**. They applied a flat 12.8% selling fee and
> 2.0% payment fee to every seller, which is wrong for a UK private seller.
>
> **Current behaviour:** costs come from the seller profile in
> `wfs/economics.py`. The default `UK_PRIVATE` profile charges **0% transaction
> fee and 0% payment processing fee**. See
> [PHASE5.1.md](PHASE5.1.md) and the README.
>
> This block is retained only to document what Phase 5 originally did.

```
LEGACY PHASE 5 MODEL — SUPERSEDED, DO NOT USE

NET PROFIT = sale price
           − selling fees          (12.8% default)   <- superseded
           − payment fees          (2.0% default)    <- superseded
           − postage               (£12)
           − insurance             (£15)
           − packaging             (£5)
           − authentication        (£0, configurable)
           − miscellaneous         (£0, configurable)
           − service reserve       (2% of resale, only when condition warrants)
           − acquisition cost      (listing price less negotiation allowance)
```

**The current Phase 5.1 model:**

```
NET PROFIT = sale price
           − transaction fee          (0% for UK_PRIVATE)
           − payment processing fee   (0% for UK_PRIVATE)
           − regulatory operating fee (0% for UK_PRIVATE)
           − promoted listing fee     (disabled by default)
           − international fee        (disabled by default)
           − fixed per-order fee      (£0 for UK_PRIVATE)
           − postage / insurance / packaging   (configurable)
           − authentication / miscellaneous    (configurable)
           − service reserve          (only when condition warrants)
           − acquisition cost         (listing price less negotiation allowance)
```

`NET ROI = net profit ÷ acquisition × 100`.

Every one of these is configurable, via the Settings tab (persisted to
`settings.json`) or the `WFS51_*` environment variables. There are no magic
numbers in the engine modules.

---

## 5. Condition and completeness

The parser reads the title, condition field and any description text. Anything
not positively stated is treated as absent, because in this market an
unmentioned box is usually a missing box.

Negation is handled explicitly: *"box only, no papers"* yields `BOX_ONLY`, not
`BOX_AND_PAPERS`. This was a real bug found during development — without it the
parser inflated completeness and therefore valuation.

| Completeness | Value multiplier |
|---|---|
| Full set / box and papers | 1.00 |
| Papers only | 0.96 |
| Box only | 0.95 |
| Completeness not stated | 0.92 |
| Watch only | 0.90 |

Further adjustments: damage −0.10, aftermarket parts −0.08, service needed
−0.05, polished −0.03, mint +0.03. The multiplier is clamped to 0.60–1.05.

Condition also produces a 0–100 condition score which feeds the Flip Score, and
sets whether a service reserve is deducted.

---

## 6. Risk (0–100, higher is worse)

Risk is deliberately **split in two**:

- **Structural risk** — seller quality, listing quality, condition, protections,
  location, evidence quality, liquidity. Price-independent.
- **Price scrutiny** — how implausibly cheap the watch is, and whether the price
  is settled (auction vs BIN). Price-dependent.

Only structural risk feeds MAX BUY. This is what guarantees **MAX BUY cannot
rise as an auction bid climbs** — a requirement of spec §8, and something that
was broken in an earlier phase before the split was introduced. There is a test
asserting MAX BUY is identical at a £600 bid and a £1,400 bid.

An extremely low price adds up to 25 points of scrutiny. It never improves the
opportunity score.

---

## 7. Liquidity and days-to-sell

```
monthly_sale_rate = sold_count_90d ÷ 90 × 30
absorption_days   = (competing_listings + 1) ÷ monthly_rate × 30
sell_through_rate = sold_90d ÷ (sold_90d + active_listings)
```

Liquidity score (0–100) = 55 × rate component + 25 × sell-through + 20 ×
absorption, then multiplied by a brand liquidity modifier (Tudor/Omega 1.0 down
to Baume & Mercier 0.65).

Days-to-sell ranges are derived from absorption, widened by price dispersion and
by poor condition:

- Quick ≈ 25% of absorption
- Base ≈ 75% of absorption
- Patient ≈ 180% of absorption

**With no sold evidence, all three are `None`** and reported as "cannot be
estimated". Nothing is invented.

Ranges beyond 365 days are reported as "over 365 days" rather than a spurious
figure like "1241–2836 days".

---

## 8. MAX BUY — three stances

```
MAX_BUY = conservative_resale × condition_multiplier
          − selling and payment fees
          − fixed costs
          − service reserve (if applicable)
        then × (1 − stance_margin)
             × (1 − uncertainty_buffer)
             × (1 − structural_risk_buffer)
             ÷ (1 − negotiation_allowance)
```

| Stance | Margin | Uncertainty buffer | Anchor |
|---|---|---|---|
| Conservative | 18% | 6% | market low |
| Standard | 12% | 3% | market low |
| Aggressive | 8% | 0% | market mid |

**Aggressive is gated**: it is only offered when Market Confidence ≥ 80 *and*
liquidity ≥ 70. Otherwise it is shown as "not permitted". The BUY decision
compares against Standard, never against Patient-sale pricing.

---

## 9. Capital velocity (0–100)

```
profit_per_30d = net_profit ÷ expected_holding_days × 30
score = 70 × min(profit_per_30d ÷ £400, 1) + 30 × max(0, 1 − days ÷ 120)
```

The explicit speed term means a long hold is penalised even if the profit rate
looks acceptable. £180 in 20 days scores higher than £300 in 120 days.

Annualised return is reported but deliberately **not** a decision metric, and
carries the caveat that it assumes you immediately find an equally good flip.

---

## 10. Flip Score

```
FLIP = 0.25 × profit_quality
     + 0.20 × liquidity
     + 0.15 × capital_velocity
     + 0.15 × market_confidence
     + 0.10 × discount
     + 0.10 × condition
     + 0.05 × (100 − risk)
```

All weights are configurable and validated to sum to 1.0.

Two safeguards stop a big discount from carrying the score:

1. **The discount component is capped.** 30% below adjusted market scores the
   full 100; beyond that adds nothing, because an implausible discount is a risk
   signal rather than a bonus.
2. **An evidence gate.** Without sold evidence the entire Flip Score is capped at
   35, regardless of how attractive the arithmetic looks.

Profit quality itself blends absolute net profit and net ROI against the
configured minima, so neither a large absolute profit on huge capital nor a high
percentage on trivial capital dominates alone.

---

## 11. BUY / WATCH / PASS

**BUY** requires *all* of: price within Standard MAX BUY, net profit ≥ £150, net
ROI ≥ 10%, confidence ≥ 60, liquidity ≥ 40, holding period ≤ 90 days, risk ≤ 45,
no damage or aftermarket flags, and Flip Score ≥ 65.

**WATCH** where the problem is fixable — typically price. A **target offer** is
calculated, anchored between Conservative and Standard MAX BUY, so a successful
negotiation still leaves the required margin. No offer is suggested if the
listing is more than 25% above MAX BUY, because such an offer is not credible.

**PASS** where economics, evidence, liquidity, holding period or risk fail
fundamentally.

Every verdict carries a plain-English `why` string. The scanner explains its
reasoning rather than emitting an opaque score.

---

## 12. Ranking

Default ranking is **Best Flip Opportunities**: verdict, then Flip Score, then
confidence, then velocity, then liquidity, then net profit. Explicitly *not*
largest discount.

Alternative sorts: Highest NET Profit, Highest ROI, Fastest Sale, Highest
Liquidity, Highest Confidence, Largest Discount, Lowest Risk.

**Key regression test**: a watch with a large apparent discount but very poor
liquidity must not outrank a smaller-margin, highly liquid, well-evidenced
watch. This is asserted directly in `tests/test_phase5.py`.

---

## 13. Capital allocation

Given a budget, the engine suggests a set of opportunities using a greedy
selection on **profit per 30 days of capital**, which favours money that comes
back quickly over a single large slow position. It reports total capital, total
expected profit, blended ROI and remaining budget.

It never purchases anything and never instructs you to.

---

## 14. API efficiency

Phase 4 issued roughly 130 eBay calls per full scan. Four changes:

1. **Search result caching** — identical queries within the TTL (45 minutes for
   listings) are served from SQLite.
2. **Separated refresh cadence** — market evidence is cached for 24 hours, so
   three of the four daily scheduled scans reuse it. Listings stay fresh.
3. **Per-reference evidence lookup** — evidence is fetched once per reference and
   reused across every listing for that reference, rather than once per listing.
4. **Query pruning** — a query variant that has run at least six times and never
   found a match is dropped. The primary query for a reference is never dropped,
   so no reference is ever silently abandoned.

Plus the watchlist active/inactive split: the default Phase 5 focus (Tudor,
Longines, Oris, Breitling, Rado, Baume & Mercier) is 36 references rather than
51, a further 30% reduction. Omega, TAG Heuer and Seiko are retained and can be
re-enabled with one checkbox.

Every scan reports actual API calls made and calls avoided.

---

## 15. AI triage

AI runs only after deterministic filtering, and only where free text carries
decision-relevant information the code cannot read:

- BUY candidates (verify before committing capital)
- reference identification is unclear
- listing language raises authenticity questions
- completeness is ambiguous
- Flip Score is within 10 points of the BUY threshold
- a negotiated purchase could make it work
- unusually attractive economics with good confidence

Everything else is skipped. Existing Phase 3 limits still apply
(`MAX_AI_CANDIDATES_PER_SCAN`, default 10), and the Phase 3 constraints remain:
the AI cannot change MAX BUY, and it can only downgrade a verdict, never upgrade
one.

---

## 16. Data integrity

- Sold prices and sales volumes are never fabricated.
- Where evidence is unavailable, the UI shows *Insufficient Market Evidence*.
- Seed values are marked `UNVERIFIED`, capped at 25 confidence, and cannot
  trigger a BUY.
- Delisting is never treated as a sale (inherited from Phase 4).
- Chrono24 automated scraping remains disabled by default (inherited from
  Phase 3).
- Day ranges beyond a year are reported as such rather than as precise figures.
