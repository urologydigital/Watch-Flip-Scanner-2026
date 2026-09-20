# Phase 5.2 — Candidate Cross-Check & Direct Listing Links

The problem this release solves: **without sold evidence, almost everything
became PASS.** A genuinely underpriced watch looked identical to a boring one,
which made the scanner least useful in exactly the situation you are in — live
on the eBay API, with no sold-data subscription.

Phase 5.2 does not fabricate the missing evidence. It separates *"not
interesting"* from *"interesting but unverified"*, and then makes it trivially
easy for you to do the verification yourself.

---

## 1. Two-stage workflow

```
eBay UK live listings (Browse API)
        ↓  deterministic filtering — free, no external calls
active-market benchmark per reference
        ↓  cross-check trigger — strict, capped
small shortlist for human verification
        ↓  one click each
[Open eBay Listing] [Chrono24] [WatchCharts] [eBay Sold]
        ↓  optional: record what you found
valuation / risk / economics
        ↓
BUY / CROSS-CHECK / WATCH / PASS
```

Stage 1 costs what it always did. Stage 2 costs nothing in API terms — it is a
set of links — but it costs **your attention**, which is why the shortlist is
capped at 25 by default.

---

## 2. The CROSS-CHECK status

| Status | Meaning |
|---|---|
| 🟢 **BUY** | Evidence-backed, within MAX BUY, all gates cleared |
| 🔎 **CROSS-CHECK** | Active market says it is unusually cheap; **no confirmed evidence**. Go and look. |
| 🟡 **WATCH** | Attractive but blocked, usually on price. Target offer supplied. |
| ⚪ **PASS** | Not interesting |

**CROSS-CHECK is not a BUY and never becomes one on its own.** Structurally:

- It is only ever promoted **from PASS**, never from BUY or WATCH.
- It carries no confirmed valuation and claims none.
- Market confidence with no sold evidence caps at 45 — the BUY gate needs 60.

So the status cannot leak into a buy recommendation. It is a shortlist for your
attention, nothing more.

### Trigger criteria

All must hold:

| Gate | Default | Env var |
|---|---|---|
| Discount to active median | ≥ 12% | `WFS52_MIN_DISCOUNT_PCT` |
| Comparable active listings | ≥ 3 | `WFS52_MIN_BENCHMARK_SAMPLE` |
| Risk score | ≤ 55 | `WFS52_MAX_RISK_SCORE` |
| Condition score | ≥ 35 | `WFS52_MIN_CONDITION_SCORE` |
| Discount **not** implausible | < 60% | `WFS52_IMPLAUSIBLE_DISCOUNT_PCT` |
| Shortlist cap | 25 per scan | `WFS52_MAX_CROSS_CHECKS` |

Plus: no damage, no aftermarket parts.

Note the **implausible-discount ceiling**. A watch at 63% below the active
median is not a bargain you found before anyone else — it is a red flag. The
scanner says so rather than putting it top of your list.

---

## 3. Active eBay market benchmark

Built from listings **already fetched** during the scan, so it costs zero extra
API calls.

Per reference: `active_listing_count`, `active_median_price`,
`active_mean_price`, `active_low_price`, `active_high_price`, and
`discount_to_active_median_pct`.

### Outlier handling, in two passes

1. **Accessory filter** — straps, bracelets, boxes, manuals, dials, bezels,
   parts, replicas and homages are removed by title before any maths, reusing
   the Phase 1 exclusion list.
2. **Ratio guard** — anything below 35% or above 250% of the provisional median
   is a different product, not a data point.
3. **Median absolute deviation** — remaining statistical outliers removed at
   3.5 MAD.

A worked example from the test suite: prices of £2,350–£2,500 plus a £60 parts
lot and a £25,000 listing produce a clean median of £2,410 from 6 listings, with
2 excluded and the reason recorded.

### Labelling

This is an **ACTIVE MARKET SIGNAL, not sold evidence**. The card labels it
*"eBay UK active asking market — not sold evidence"*, `as_dict()` carries
`is_sold_evidence: False`, and it cannot raise market confidence above the
asking-only ceiling.

---

## 4. Direct listing links

Every candidate carries the **canonical URL returned by the Browse API**
(`itemWebUrl`), used verbatim. A URL is only reconstructed from the item ID when
eBay supplied none, because hand-built URLs rot silently.

Each card offers, opening in a new tab:

- **Open eBay Listing** — the actual listing
- **Search eBay** — `BRAND + REFERENCE`, watches category
- **eBay Sold/Completed** — the public completed-listings view
- **Chrono24 Comparables** — reference search, GBP
- **Check WatchCharts** — reference search

Search terms use **brand + reference only**. A title like *"Tudor Black Bay 58
79030N 39mm Mens Automatic Dive Watch Box Papers 2023 MINT RARE"* produces
useless comparables; `Tudor 79030N` does not.

The table view carries a `Link` column rendering as **Open eBay**.

---

## 5. WatchCharts and Chrono24 — no paid API

**No subscription is required and none is used.** Both are link-only in Phase 5.2.
There is a test asserting these modules import no HTTP client and reference no
credential identifier.

If you check a reference manually, record what you found:

| Source | Stored as | Evidence level |
|---|---|---|
| `MANUAL_EBAY_SOLD` | `CONFIRMED_SOLD` | A |
| `OTHER_CONFIRMED` | `CONFIRMED_SOLD` | A |
| `MANUAL_WATCHCHARTS` | `MARKET_BENCHMARK` | B |
| `MANUAL_CHRONO24` | `ACTIVE_ASKING` | C |
| `LOCAL_OBSERVATION` | `LOCAL_OBSERVATION` | D |

**The evidence kind is derived from the source and cannot be supplied by the
caller.** `record_benchmark()` has no `kind` parameter. A Chrono24 asking price
is therefore incapable of being stored as a sale, by construction rather than by
discipline.

Benchmarks older than 120 days are ignored — a stale benchmark is not market
context.

---

## 6. Evidence hierarchy

```
LEVEL A  CONFIRMED SOLD        manual eBay sold records, approved sold-data provider
LEVEL B  MARKET BENCHMARK      manually verified WatchCharts value
LEVEL C  ACTIVE ASKING         eBay UK active listings, Chrono24 listings
LEVEL D  LOCAL OBSERVATION     appeared / disappeared / relisted
```

Four statements this project treats as load-bearing:

- A disappeared listing is **not** a confirmed sale.
- A Chrono24 listing price is **not** a sold price.
- An eBay active price is **not** a sold price.
- A WatchCharts benchmark is **not** a confirmed sale.

Confidence ceilings enforce this: exact sold 100, variant 85, model family 65,
scanner observation 55, asking-only 45, seed 25. BUY needs 60. Levels B, C and D
therefore cannot produce a BUY on their own — not by policy, but because the
arithmetic will not reach the gate.

---

## 7. Local observation history

Unchanged from Phase 5.1, still accumulating. Per item ID: `first_seen`,
`last_seen`, price history, reference, status.

Statuses: `ACTIVE`, `LIKELY_SOLD`, `POSSIBLY_SOLD`, `RELISTED`,
`DELISTED_UNKNOWN`. Nothing is ever promoted to confirmed sold automatically.

---

## 8. Seller economics — unchanged

`EconomicsEngine` remains the single authoritative source. Default profile
`UK_PRIVATE`: 0% transaction fee, 0% payment processing, with postage,
insurance, packaging, promoted listings, international fees, service reserve and
negotiation allowance all configured separately.

`wfs/pricing.py` remains legacy-only. Phase 5.2 modules are asserted not to
import it, and a behavioural test confirms corrupting the legacy constants
changes no Phase 5.2 figure.

---

## 9. API and cost efficiency

| | Effect |
|---|---|
| Active benchmark | **0 extra API calls** — reuses fetched listings |
| Cross-check evaluation | **0 API calls** — deterministic, local |
| Market links | **0 API calls** — URLs, not requests |
| AI | unchanged: deterministic first, capped, optional |

Nothing in Phase 5.2 adds a network call. The only new cost is your time, and
the shortlist cap bounds that.

---

## 10. Security

No secret appears in the UI, links, logs, database or test output.
Credentials stay in environment variables. Tests assert that a planted
`EBAY_CLIENT_SECRET` never surfaces in analysis output or any generated URL.

---

## 11. Remaining limitations

- **Sold evidence is still the binding constraint.** CROSS-CHECK helps you find
  candidates worth verifying; it does not verify them. Recording what you find
  in the Market Evidence tab is what eventually produces BUY verdicts.
- **The active benchmark is asking prices.** In a slow market, sellers ask more
  than anyone pays, so a 15% "discount" to asking may be no discount at all.
  That is precisely why it cannot raise confidence past 45.
- **Reference matching drives the benchmark.** A reference with few live UK
  listings gets no benchmark and therefore no cross-check.
- **Links are searches, not deep links.** Chrono24 and WatchCharts URLs open a
  search for the reference; you still pick the right result.
- **Nothing here validates authenticity.** A cheap watch flagged for cross-check
  may be cheap because it is not genuine. The risk engine flags what it can see
  in the listing text; it cannot see the watch.
