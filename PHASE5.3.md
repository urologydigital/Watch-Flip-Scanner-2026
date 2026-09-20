# Phase 5.3 — Automated Market Evidence

Evidence collection is now automatic. The normal workflow is:

```
RUN SCAN
 → eBay active discovery (Browse API, unchanged)
 → deterministic shortlist
 → automatic evidence collection   ← new, once per reference
 → valuation → liquidity → QUICK/BASE/PATIENT → MAX BUY → BUY/WATCH/PASS
```

Manual evidence remains available and unchanged, but it is no longer the only
route to a BUY verdict.

---

## Architecture

```
wfs/collectors/
    base.py            CollectedEvidence, certainty, match quality, robots, throttling
    ebay_sold.py       Insights API → stored records → optional public web
    chrono24.py        Wraps the existing Chrono24WebProvider, adds normalisation
    watchcharts.py     Public market context, no subscription
    local_history.py   UK listing lifecycle from our own scans
wfs/browser.py         Optional Playwright adapter, lazily imported
wfs/evidence_store.py  collected_evidence table, dedup, robust statistics
wfs/evidence_collection.py  Registry, settings, caching, source health
wfs/evidence_bridge.py Collected evidence → the existing MarketEvidence model
wfs/fx.py              GBP normalisation from configured rates
```

Nothing in Phase 5.2.1 was replaced. `EconomicsEngine` remains the single
economics authority, `flip.py` still owns MAX BUY, `decision.py` still owns the
verdict, and `evidence.py` still owns confidence scoring. Phase 5.3 feeds them
better inputs.

---

## Source hierarchy

| # | Source | Evidence level | Confidence ceiling |
|---|---|---|---|
| 1 | Verified exact-reference sold (manual import / Insights API) | `REFERENCE_EXACT` | 100 |
| 2 | **Collected exact-reference sold** *(new)* | `COLLECTED_SOLD` | **85** |
| 3 | Model-family sold | `MODEL_FAMILY` | 65 |
| 4 | Scanner observation | `OBSERVED_ACTIVITY` | 55 |
| 5 | Chrono24 / active asking | `ASKING_ONLY` | 45 |
| 6 | WatchCharts market context | context only | — |
| 7 | Seed placeholder | `SEED` | 25 |

BUY requires confidence ≥ 60. Levels 4–7 therefore cannot produce a BUY on their
own — not by policy, but because the arithmetic cannot reach the gate.

---

## Best Offer handling

eBay shows a struck-through asking price on accepted-offer sales without
revealing what was paid. Treating that number as the sale price systematically
overstates the market, so price certainty travels with every record:

| Certainty | Valuation weight | Counts for liquidity |
|---|---|---|
| `CONFIRMED_PRICE` | 1.00 | yes |
| `DISPLAYED_SOLD_PRICE` | 0.85 | yes |
| `BEST_OFFER_UNCERTAIN` | **0.00** | **yes** |
| `PRICE_UNKNOWN` | 0.00 | no |

A Best Offer sale proves the watch sold — useful for liquidity — and contributes
nothing to the price band. Ten Best Offer sales and no priced ones cannot
produce a BUY; there is a test asserting exactly that.

---

## Match quality

`EXACT_REFERENCE` (1.00) · `NORMALIZED_REFERENCE` (0.95) · `MODEL_FAMILY` (0.45)
· `AMBIGUOUS` (0) · `REJECTED` (0)

79030N and 79030B are different watches. Near-misses are **rejected, not
merged**, and model-family counts are always reported separately from exact ones.

---

## Recency

0–30 days 1.00 · 31–90 0.85 · 91–180 0.60 · 181–365 0.35 · >365 0.15 ·
undated 0.50

Valuation uses a weighted median, so recent sales dominate rather than being
averaged with stale ones.

---

## Local history

| Status | Meaning |
|---|---|
| `ACTIVE` | Still listed |
| `STALE` | Active over 90 days — demonstrably not selling at this price |
| `ENDED_UNKNOWN` | Vanished. Outcome unknown |
| `CONFIRMED_SOLD` | Corroborated by a real sold record |
| `RELISTED` | Vanished then returned |

**A listing disappearing does not mean it sold.** Phase 4's `LIKELY_SOLD`
inference maps to `ENDED_UNKNOWN` here. `CONFIRMED_SOLD` requires a matching
sold record within 8% on price **and** 45 days on date — either alone is
coincidence, not corroboration.

Tracked per listing: `first_seen`, `last_seen`, `initial_price`, `latest_price`,
`lowest_price_seen`, `price_change_count`, `status`, days on market.

---

## Deduplication

Fingerprint precedence: `item_id` → canonical URL (query string stripped) →
content hash of reference/title/price/date. Re-collecting the same sale
**refreshes one row**; it never creates a second apparent transaction. A
duplicated sale would inflate both the price sample and the liquidity estimate.

---

## Cache

| Source | TTL |
|---|---|
| eBay sold (Insights / web) | 24 h |
| Chrono24 | 8 h |
| WatchCharts | 24 h |
| Stored records | 1 h |
| Local history | recomputed (no network) |

A second scan shortly after the first reuses cached evidence. **Refresh
evidence** on the Flip Intelligence tab forces re-collection for the shortlist.

Collection runs **once per reference**, capped at 15 by default
(`WFS53_EVIDENCE_SHORTLIST_CAP`). A 189-listing scan performs a handful of
collections, not 189.

---

## Source controls

Enabled by default (no network or permission needed):
Local History · Stored sold records · Marketplace Insights (if eBay granted access)

Off by default, toggled in **Settings → Automatic Market Evidence**, persisted to
`settings.json`:
Chrono24 · WatchCharts · eBay Sold (public web)

A master **AUTOMATIC MARKET EVIDENCE** switch disables everything at once.

---

## Failure behaviour

Source health: `OK` · `PARTIAL` · `BLOCKED` · `UNAVAILABLE` · `DISABLED` ·
`NOT_RUN`, with last success, record count, cache status and error, shown in the
Market Evidence tab.

Three layers of isolation:

1. `BaseCollector.collect()` catches everything a collector can throw.
2. The registry records failures as health and moves on.
3. The pipeline wraps the whole collection stage, so even a catastrophic
   registry failure leaves the scan intact.

Robots.txt is checked before every public-web fetch, and a robots.txt that
cannot be READ returns False — not being able to check permission is not the
same as having it. HTTP 401/403/429 is recorded as `BLOCKED` and never
circumvented.

---

## Playwright

**Optional.** Not in `dependencies`. Imported lazily, headless, one session per
run, disabled unless `WFS53_BROWSER_ENABLED=1`.

```
pip install playwright && playwright install chromium
```

Without it the app launches normally and browser-backed collectors report
`UNAVAILABLE`.

---

## Currency

Conversion uses **configured rates only** (`WFS53_FX_<CCY>`). There is no live FX
feed. An unknown currency returns `None` and the record is skipped rather than
converted at a guessed rate. Review the defaults in `wfs/fx.py`; a stale rate
quietly distorts every converted comparable.

---

## Verdict gating

Structured codes accompany the prose explanation:
`INSUFFICIENT_SOLD_EVIDENCE` · `PRICE_UNCERTAIN` · `LOW_LIQUIDITY` ·
`REFERENCE_AMBIGUOUS` · `AUTHENTICITY_RISK` · `CONDITION_RISK` ·
`MARGIN_TOO_SMALL` · `SOURCE_UNAVAILABLE`

Shown under **Why not BUY** in candidate detail.

---

## Security

No secret is printed, logged, stored in the database or placed in a URL.
Credentials stay in environment variables; `.env` was not modified. Tests assert
a planted `EBAY_CLIENT_SECRET` never appears in collected evidence or source
health, and that no collector module references a credential identifier.

Optional future variables, names only: `WFS53_BROWSER_ENABLED`,
`WFS53_EBAY_SOLD_WEB`, `WFS53_WATCHCHARTS_ENABLED`, `WFS53_FX_<CCY>`,
`WFS53_EVIDENCE_SHORTLIST_CAP`, `WFS53_EVIDENCE_LOOKBACK_DAYS`.

---

## Database migration

Two additive tables, created automatically on first launch:

- `collected_evidence` — normalised records, 4 indexes
- `source_health` — per-source status

No existing table, row or index is altered. Verified against a populated
Phase 5.2.1 database: listings, price history, scan runs, manual sold evidence,
benchmarks and watch references all unchanged. Re-running the migration is a
no-op.

---

## Troubleshooting

**Everything still says PASS.** Check Market Evidence → Source health. With only
Local History enabled and no sold records, confidence stays below 60 by design.
Import sold records or enable a sold source.

**A source shows BLOCKED.** robots.txt disallows it or the site refused. This is
the intended outcome, not a bug. The scan continues without it.

**Evidence looks stale.** Tick *Refresh evidence* before scanning.

**Browser collectors show UNAVAILABLE.** Playwright is not installed or
`WFS53_BROWSER_ENABLED` is not set. Both are optional.
