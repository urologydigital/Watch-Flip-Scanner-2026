# Watch Flip Scanner UK — Final 6.0

The release that closes the evidence gap. Phases 1–5.3.3 could find candidates
but had no working route to **Level A confirmed sold evidence**, so almost
nothing could legitimately reach BUY.

## The evidence hierarchy

| Level | Meaning | Sources | Confidence ceiling |
|---|---|---|---|
| **A** | Confirmed transaction | eBay Product Research (assisted), Marketplace Insights, imported sold records | 100 |
| **B** | Independent valuation | WatchCharts API | context only |
| **C** | Active asking | eBay Browse active, Chrono24 | 45 |
| **D** | Local observation | our own scan history | 55 |

BUY requires confidence ≥ 60, so Levels B, C and D cannot produce one on their
own. That is arithmetic, not policy.

## eBay Product Research — the Level A route that works

Marketplace Insights is restricted and the public completed pages are
disallowed. The compliant route is the one you already have rights to: **Seller
Hub Research, in your own signed-in browser.**

The app prepares the exact brand+reference search, you open it, copy the sold
results, and paste them back. It then parses, match-checks, filters and stores
them as Level A.

By construction this module has **no HTTP client at all**. It cannot read your
cookies, store your password, log in for you, or bypass a CAPTCHA — there is
nothing in it that could.

Captured per sale: sold price, asking price, sale date, condition, box/papers,
shipping, seller, URL/item id, captured_at, match quality.

**Best Offer sales are never invented.** eBay shows the asking price on an
accepted offer, so those rows are stored with `BEST_OFFER_UNCERTAIN`: they count
toward liquidity and contribute nothing to price.

## Reference matching

`EXACT_REFERENCE` → `HIGH_CONFIDENCE_VARIANT` (a fuller factory code extending
ours) → `MODEL_FAMILY` → `WEAK_MATCH` → `REJECTED`.

79030N and 79030**B** are different watches: a conflicting reference is
**rejected**, not demoted to family. Only EXACT/VARIANT/NORMALIZED may move
MAX BUY.

## Comparable filtering with an audit trail

Every comparable is judged and the verdict recorded — strap only, box only,
parts, replica, aftermarket, head only, job lot, wrong reference, duplicate, no
price, price outlier. The candidate card shows which were used, which were not,
and why. No unexplained numbers.

## Adaptive valuation

Target ~70% confirmed sold / ~20% active asking / ~10% independent valuation,
with weights that adapt: a thin sold sample loses weight rather than pretending
to certainty, and a missing source has its weight redistributed.

**Hard guard:** when sold evidence exists, the blended mid is never allowed
above the sold mid. Asking prices are what sellers hope for; letting them lift a
resale estimate is precisely how a scanner talks you into overpaying.

## Source statuses

`OK` · `REUSED` · `PARTIAL` · `STALE` · `BLOCKED` · `UNAVAILABLE` ·
`NOT_CONFIGURED` · `USER_ACTION_REQUIRED` · `DISABLED` · `NOT_RUN` · `ERROR`

Enabled and operational are different states and are displayed as such.

## Database safety

A timestamped backup is taken **before** migration, into `db_backups/` (excluded
from the release archive). Row counts are compared before and after. Migration
is additive and idempotent.

## A real bug this release fixed

Price parsing truncated any amount without a thousands separator to its first
three digits: **£2650.00 was read as £265.00**. It affected three parsers. Every
valuation built on such a record was wrong by a factor of ten, silently. Fixed,
with parametrised regression tests.
