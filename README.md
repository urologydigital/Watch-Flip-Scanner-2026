# Watch Flip Scanner UK

A local-first, manually triggered decision-support tool for the UK secondary watch market.
It is not a continuously running scraper. Nothing happens until you press **RUN SCAN**.

**What this build is: Phases 1 and 2.**

*Phase 1* — eBay UK discovery, watchlist matching, SQLite persistence, deterministic
pre-filter, duplicate and price-drop tracking.

*Phase 2* — sold-market provider abstraction, market value engine with dynamic weighting
and confidence, liquidity engine, deterministic risk buffer, MAX BUY, the three resale
scenarios, and BUY / WATCH / PASS verdicts.

*Phase 3* — Chrono24 active-asking cross-check, UK dealer / index context, and AI analysis
of the shortlist with structured JSON in and out, capped per scan.

*Phase 4* — listing lifecycle tracking, delisting detection, asking-price drift, reference
performance, persistent-listing detection and scan trends. All of it runs on data the
earlier phases already store, so it costs no API calls and improves with use.

*Phase 5* — flip intelligence: evidence hierarchy, Market Confidence Score, net profit
(not gross spread), three MAX BUY stances, capital velocity, Flip Score, BUY/WATCH/PASS
with target offers and plain-English explanations, opportunity ranking, capital
allocation, and a substantial reduction in API usage.

*Phase 5.1* — corrected UK seller economics: seller profiles, a single economics
engine, transparent cost breakdowns, evidence-source labelling, manual sold-evidence
import and local observation classification.

*Phase 5.1.1* — documentation and legacy economics cleanup.

*Phase 5.2* — candidate cross-check and direct listing links. Current release.
Adds the **CROSS-CHECK** status for listings the live market says are cheap but
which lack confirmed evidence, an active eBay market benchmark, and one-click
links to the listing plus Chrono24 / WatchCharts comparables. **No paid market-data
subscription is required.** See **[PHASE5.2.md](PHASE5.2.md)**.

*Phase 5.2.1* — correction pass.

*Phase 5.3* — **automated market evidence.** Current release. Evidence is
collected automatically once per reference during a scan: stored sold records,
local UK listing history, and optionally Chrono24, WatchCharts and eBay
sold/completed. Adds Best Offer price-certainty handling, match-quality grading,
recency weighting, deduplication and per-source health. Playwright is optional.
See **[PHASE5.3.md](PHASE5.3.md)**. A manually recorded WatchCharts
benchmark now materially informs cross-check ranking (while still being unable to
produce a BUY), and Top Flip Opportunities is properly ranked BUY → CROSS-CHECK →
WATCH. See **[PHASE5.2.1.md](PHASE5.2.1.md)**.

All phases are complete. Methodology is documented in **[PHASE5.md](PHASE5.md)**
(formulas and reasoning), **[PHASE5.1.md](PHASE5.1.md)** (economics corrections
and migration notes) and **[PHASE5.1.1.md](PHASE5.1.1.md)** (cleanup release).

> **Default economics: UK Private Seller.** eBay charges private sellers no selling
> or payment processing fee, so none is deducted. Postage, insurance and packaging
> still are. If you sell as a business, switch profile in the Settings tab — see
> [Seller profiles](#seller-profiles) below.

---

## 1. Install Python

- **Windows:** download Python 3.12+ from python.org, tick "Add Python to PATH" during install.
  Verify in PowerShell: `python --version`
- **macOS:** `brew install python@3.12`, or download from python.org.
  Verify in Terminal: `python3 --version`

## 2. Create a virtual environment

Windows (PowerShell):
```powershell
cd path\to\watch_flip_scanner
python -m venv .venv
.venv\Scripts\Activate.ps1
```

macOS / Linux:
```bash
cd path/to/watch_flip_scanner
python3 -m venv .venv
source .venv/bin/activate
```

## 3. Install dependencies

```bash
pip install -r requirements.txt
```

## 4. Get eBay developer credentials

1. Create a free account at https://developer.ebay.com
2. Go to **Application Keys** and create a **Production** keyset.
3. Copy the **App ID (Client ID)** and **Cert ID (Client Secret)**.
4. The app uses the client-credentials OAuth flow and the Browse API only — no user
   login and no HTML scraping.

Note on quotas: the Browse API has a daily call ceiling on the default tier. The scan
issues 2–3 queries per reference, so a full 51-reference watchlist is roughly 130 calls
per scan. Reduce the watchlist or `WFS_RESULTS_PER_QUERY` if you hit limits.

## 5. Configure `.env`

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

Fill in `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET`. Never commit `.env` — it is gitignored.

## 6. Seed the watchlist

```bash
python seed_watchlist.py
```

This writes `watchlist.json` with the 51 specified references.

> **Important.** The `market_low/mid/high` figures in the seed file are *unverified
> placeholders*, put there only so the pre-filter has a comparator on first run. They are
> not market evidence and they will be wrong. Replace them with your own figures via the
> **Watchlist settings** tab or by editing `watchlist.json` directly. The app shows a
> warning banner until `value_source` is changed from `SEED_PLACEHOLDER_UNVERIFIED`.

## 7. Run

```bash
python -m streamlit run app.py
```

`streamlit run app.py` also works; the `python -m` form is more reliable when
several Python installations are present.

Run the tests any time with either of:

```bash
pytest
python -m pytest
```

Both work from the repository root — `pyproject.toml` sets the package path, so
no `PYTHONPATH` juggling is needed.

---

## Seller profiles

The economics are configured for a **UK private seller** by default: eBay charges
private sellers no selling or payment processing fee, so none is deducted.

| Profile | Transaction fee | Payment fee | Notes |
|---|---|---|---|
| **UK Private** (default) | 0% | 0% | Postage, insurance, packaging still apply |
| **UK Business** | 9.3% + 0.35% regulatory + £0.30 | 0% | **Estimate** — replace from a real invoice |
| **Custom** | your values | your values | For any other arrangement |

Change it in the **Settings tab**. The choice persists to `settings.json`, and the
current mode is shown at the top of every screen as *Economics: UK Private Seller*.

Promoted listing and international fees are separate optional costs, off by default.

Advanced values — postage, insurance, packaging, service reserve, negotiation
allowance — sit under **Advanced Economics** in the same tab, with a live worked
example showing what your settings do to a £1,000 → £1,300 flip.

Credentials are never written to `settings.json`; they stay in `.env`.

---

## Sold evidence

Evidence sources, strongest first:

1. **Manual sold records** — import a CSV in the **Market Evidence** tab. The
   strongest source realistically available. A template is downloadable in-app or
   at `manual_sold_evidence_template.csv`:

   ```csv
   brand,reference,model,sold_price_gbp,sold_date,condition,full_set,source,notes
   Longines,L3.781.4.56.6,HydroConquest,1125,2026-08-20,excellent,yes,eBay UK,
   ```

   Rows are validated for price, date, brand and reference, and duplicates are
   rejected. The importer reports exactly what it accepted and what it did not.

2. **Marketplace Insights** — optional. **Access is restricted and most developer
   accounts will never receive it.** The scanner does not depend on it and works
   fully without it.

3. **Local observation history** — built automatically from listings the scanner
   has watched. A disappearance is *not* treated as a sale: outcomes are classified
   as likely sold, possibly sold, delisted-unknown or relisted, and this tier is
   capped below the confidence a BUY requires.

3b. **Active eBay market benchmark** — median asking price across live UK listings
   for the reference, with accessories and outliers removed. Costs no extra API
   calls. **Asking prices, not sales** — it drives the CROSS-CHECK shortlist but
   cannot raise confidence past 45.

4. **Model-family evidence** — used only when exact-reference data is too thin,
   and always labelled as such.
5. **Observed active-market activity** — current competing listings. Corroborates
   that a market exists; does not value it.
6. **Active asking prices only** — weak. Asking is not achieving.
7. **Seed / unverified values** — placeholders shipped for development. Marked
   `UNVERIFIED` in the UI.

**Seed and unverified data cannot independently produce a high-confidence BUY.**
Their confidence is capped at 25, well below the 60 a BUY requires. The same
applies to asking-only evidence (capped at 45) and scanner observation (capped
at 55). Only genuine sold records clear the gate.

Every estimate states its source without ambiguity, e.g. *Exact reference sold
evidence — Manual Sold Evidence*, or *Observed listing activity — Historical
Scanner Evidence*.

---

## Default economics

These are the **application defaults for a UK private seller**. They are
configurable assumptions, not a promise that a future eBay sale will carry no
seller-side cost. Verify them against your own recent invoices and adjust in the
Settings tab.

| Setting | Default | Configurable |
|---|---|---|
| Seller profile | `UK_PRIVATE` | ✅ |
| eBay transaction / final value fee | **0%** | ✅ |
| Payment processing fee | **0%** | ✅ |
| Regulatory operating fee | 0% | ✅ |
| Promoted listing | **disabled** | ✅ |
| International selling fee | **disabled** | ✅ |
| Postage | £12 | ✅ |
| Insurance | £15 | ✅ |
| Packaging | £5 | ✅ |
| Authentication | £0 | ✅ |
| Miscellaneous | £0 | ✅ |
| Service reserve | 2% of resale, only when condition warrants | ✅ |
| Negotiation allowance | 0% | ✅ |

The zero platform fees reflect eBay's current treatment of UK private sellers.
Policy can change, and other platforms differ — that is precisely why every
value above is configurable rather than hard-coded.

## How the pre-filter works (spec §9)

---

## Phase 2: how a verdict is reached

```
shortlisted listing
  -> sold evidence      (SoldMarketProvider — null / your CSV / Marketplace Insights)
  -> market value       (dynamic 70/20/10 weighting, confidence HIGH/MEDIUM/LOW)
  -> liquidity          (observed sales rate vs competing listings -> days-to-sale)
  -> risk buffer        (deterministic, split structural vs price-scrutiny)
  -> MAX BUY            (conservative resale, net of fees, margin and risk)
  -> QUICK / BASE / PATIENT resale scenarios
  -> verdict
```

**Sold evidence.** `wfs/sold_market.py` ships three providers. `NullSoldProvider` is the
default and reports `UNAVAILABLE` — it never invents a count. `ManualSoldProvider` reads
sales *you* have observed and recorded in `observed_sales.csv` (copy
`observed_sales.csv.example` to get started); this is the practical route today.
`MarketplaceInsightsProvider` is wired for eBay's restricted Marketplace Insights API and
activates the moment your keyset is approved.

**Market value.** Weights are 70% sold / 20% Chrono24 asking / 10% dealer index, but a
source contributing nothing has its weight redistributed rather than assumed. Asking
prices are drawn from the *lower half* of the distribution and haircut a further 5% when
they are the only evidence, because asking prices systematically overstate what you can
actually get. Confidence is HIGH at 8+ observed exact-reference sales, MEDIUM at 4–7,
LOW below that or when no sold data exists.

**Liquidity.** Monthly sale rate is observed sales ÷ period × 30. Absorption is
(competing listings + your unit) ÷ monthly rate. With zero observed sales the rating is
`UNKNOWN` and every day-range is `None` — no invented ranges. Rado and Baume & Mercier are
never rated HIGH.

**MAX BUY.**

```
MAX_BUY = market_low × condition_multiplier
          − platform fees        # 0% under the default UK_PRIVATE profile
          − seller costs         # postage + insurance + packaging, £32 by default
          × (1 - stance_margin)          # 18% / 12% / 8%
          × (1 - uncertainty_buffer)     # 6% / 3% / 0%
          × (1 - structural_risk_buffer) # capped at 30%
```

All fees come from the active seller profile via `EconomicsEngine`. Earlier
releases used a flat percentage for every seller; that model was superseded in
Phase 5.1.

The anchor is `market_low`, never the high. Note **structural** risk buffer: the risk
engine deliberately separates price-independent risk (documents, condition, seller,
location, evidence quality, liquidity) from price-dependent scrutiny ("this is suspiciously
cheap"). Only the structural half feeds MAX BUY. That separation is what makes the spec's
requirement hold — MAX BUY cannot creep upward as an auction bid climbs. There is a test
asserting exactly this.

**Verdicts.** `BUY` requires acquisition within MAX BUY *and* non-LOW market confidence
*and* known liquidity *and* non-HIGH risk *and* a settled (non-auction) price. Fail any
one and it is downgraded to `WATCH`. Above MAX BUY is always `PASS`. With no sold data
configured, nothing can reach `BUY` — which is the correct behaviour, not a bug.

## Phase 3: asking-market cross-check and AI

### Chrono24 — read this before enabling anything

Chrono24 publishes no marketplace API, and automated collection from their site may
breach their terms of use. `wfs/asking_market.py` therefore ships three providers and
**the automated one is off by default**:

| Provider | Default | What it does |
|---|---|---|
| `NullAskingProvider` | ✅ on | Returns UNAVAILABLE. The scan continues normally. |
| `ManualAskingProvider` | on if CSV present | Reads asking prices *you* recorded in `chrono24_asking.csv`. Unambiguously fine. **This is the recommended route.** |
| `Chrono24WebProvider` | ❌ off | Opt-in via `WFS_CHRONO24_ENABLED=1`. Checks robots.txt before every fetch, enforces a 6-second gap between requests, one request per reference, and fails open on any error. |

Enabling the web provider is your decision and your responsibility; a robots.txt check
is not the same thing as permission under their terms. The manual CSV gets you the same
20% weighting with none of that exposure.

Everything from these providers is labelled **CHRONO24 ACTIVE ASKING DATA**. It is never
described as sold, and sales volume is never derived from it — there are tests asserting
both. If Chrono24 is unavailable for any reason, the scan carries on without it.

### Dealer and WatchCharts context

WatchCharts has no public API either. `ManualDealerProvider` reads `dealer_prices.csv`
with figures you look up yourself. Dealer asking price is not transaction value, so it
carries only the 10% weight and is used purely as context.

### AI analysis

Runs on the shortlist only, after all deterministic work is done. Structured JSON goes
in, structured JSON comes back, validated against the required schema before use. Three
hard constraints, each with a test behind it:

1. **The AI cannot change MAX BUY.** Any attempt to return a price field is stripped in
   validation, and `apply_ai()` asserts MAX BUY is unchanged afterwards.
2. **The AI can only downgrade a verdict.** An AI `BUY` against a deterministic `WATCH`
   is recorded for your attention and *not applied* — the deterministic downgrades exist
   for reasons the model cannot verify, like absent sold evidence or an unsettled auction.
3. **An AI failure is never fatal.** A network error, malformed JSON or missing field
   leaves the deterministic verdict standing, with a note explaining why.

Unreadable risk values are coerced to `HIGH` rather than ignored, and a low price is
framed in the system prompt as grounds for more scrutiny, never less.

### Cost control (spec §21)

```
326 listings scanned
 -> 17 pre-filter candidates   (deterministic, free)
 ->  6 deep analysed           (evidence lookups)
 ->  6 AI calls                (capped by MAX_AI_CANDIDATES_PER_SCAN, default 10)
```

The AI budget is spent on the strongest deterministic candidates first, so if the cap
binds you lose the weakest ones, not a random selection. The scan summary reports actual
usage every run.

## Phase 4: historical analytics

Every scan already stored listings, prices and timestamps. Phase 4 reads that history
back. There are no new API calls — the Analytics tab is free to use and gets better the
longer you run the tool.

### The one thing to keep straight

**Delisted does not mean sold.** A listing vanishing from eBay may mean it sold, or that
the seller ended it, or that it simply expired. Phase 4 therefore records listings as
`DELISTED`, never as sold, and none of it feeds the sold-evidence engine — there is a test
asserting that specifically. If you want evidence you can value a watch on, it still has
to go in `observed_sales.csv`.

Absence is also interpreted carefully: a listing missing from a scan that never searched
its reference tells you nothing, so each scan records which references it covered
(`scan_references`) and only those are eligible to be marked delisted.

### What you get

| View | What it tells you |
|---|---|
| **Live price drops** | Active listings that have fallen since you first saw them, ranked by percentage. A seller who has already dropped twice will usually drop again. |
| **Reference performance** | Per reference: how many listings tracked, median days on your radar before delisting, median discount from opening ask, how many needed a price cut. |
| **Asking-price drift** | How far asking prices move before delisting, split against still-active listings. Large typical drift means sellers of that reference start high — a reason to wait rather than pay the opening ask. |
| **Listings that keep coming back** | Watches seen in three or more scans and still unsold. Usually priced above what the market will pay, or the reference is thinner than asking prices suggest. Useful context before you bid. |
| **Scan trends** | Listings scanned, candidates found and candidate rate over time. A falling candidate rate means the market has tightened or your seed values have drifted. |

Statistics are suppressed below three observations and show a sample warning instead of a
number, because a median of one data point is not a median.

### Database migration

Phase 4 adds three columns (`times_seen`, `first_price`, `disappeared_at`) and one table.
An existing Phase 1–3 database is migrated in place on startup and keeps all its history;
`first_price` is backfilled from your stored price observations. Nothing is rebuilt and
nothing is lost.

## Phase 5: what changed

The scanner no longer asks "is this watch cheap?". It asks what you will
realistically net, how long your capital is tied up, and how confident anyone can be
in that estimate.

**Headline changes**

- **Net profit, never gross spread.** Selling fees, payment fees, postage, insurance,
  packaging and a conditional service reserve are all deducted before anything is called
  profit.
- **Three MAX BUY stances** — Conservative, Standard, Aggressive. Aggressive is gated on
  confidence ≥ 80 and liquidity ≥ 70, so it is unavailable on exactly the watches where
  it would be dangerous.
- **Capital velocity.** £180 in 20 days beats £300 in 120 days, and the scoring says so.
- **Flip Score (0–100)** combining profit, liquidity, velocity, confidence, discount,
  condition and risk. A large discount alone cannot produce a good score.
- **Evidence hierarchy.** Exact reference → variant → model family → asking only → seed.
  Never silently mixed, and each level caps how confident the system may be.
- **Plain-English explanations.** Every verdict says why, in a sentence you can check.
- **Target offers** on WATCH candidates, anchored below Standard MAX BUY.
- **Capital allocation** across a budget you specify.

**API usage cut substantially** — caching, per-reference evidence lookup, query pruning
and the active/inactive brand split. See PHASE5.md §14.

Run `python sample_output.py` to see a worked BUY, WATCH and PASS using clearly marked
mock data.

## How the pre-filter works (spec §9)

Stage one is plain code, never AI:

1. Total acquisition = price + cheapest shipping.
2. Reject anything whose title matches parts/accessory exclusion terms.
3. Require **both**: acquisition ≤ 80% of market mid **and** gross spread ≥ the band
   minimum (£200 for £500–1,000; £325 for £1,000–2,000; £400 for £2,000–3,500).
4. Rado and Baume & Mercier get an extra 7 percentage-point discount requirement for
   thinner liquidity.

Both conditions must hold. The tool prefers false negatives, and
`NO SUITABLE FLIP CANDIDATES FOUND` is an acceptable and often correct result.

## Known limitations, stated honestly

- **Sold data is still the binding constraint.** The Browse API does not return
  completed-transaction history. Until you record your own observations or get
  Marketplace Insights access, market values fall back to your seed figures, liquidity is
  `UNKNOWN`, and no listing can reach a `BUY` verdict. The plumbing is finished; the
  evidence is not.
- **No Chrono24.** They publish no marketplace API. Phase 3 will add a cautious
  public-web cross-check, clearly labelled as *active asking data*, never as sold data,
  and the scan will continue without it if unavailable.
- **Returns policy** is not in Browse API search summaries; it needs a per-item detail
  call. Left null rather than guessed.
- **Auction listings** show the current bid, not a settled acquisition price. They are
  flagged in the reasons column and should be treated with more caution than BIN.
- **Box/papers** are only inferable from title text at this stage; no reliable field exists
  in search results.

## Project layout

```
app.py                  Streamlit UI
seed_watchlist.py       Generates watchlist.json
watchlist.json          Editable watchlist + your market values
wfs/config.py           Settings, spread bands, thresholds
wfs/db.py               SQLite schema and persistence
wfs/watchlist.py        Query generation, exclusions, reference matching
wfs/ebay.py             Browse API client + OAuth
wfs/prefilter.py        Deterministic first-stage filter
wfs/sold_market.py      SoldMarketProvider + null / CSV / Insights implementations
wfs/market.py           Market value engine, dynamic weights, confidence
wfs/liquidity.py        Sales frequency, absorption, time-to-sale
wfs/pricing.py          Risk buffer, MAX BUY, three resale scenarios
wfs/asking_market.py    Chrono24 + dealer providers (null / CSV / opt-in web)
wfs/ai.py               AI client, schema validation, verdict reconciliation
wfs/analysis.py         Orchestration and verdicts
wfs/analytics.py        Historical analytics over stored scans
wfs/pipeline.py         Scan orchestration (Phases 1-4)
wfs/config5.py          Phase 5 configuration — every threshold and cost
wfs/evidence.py         Evidence hierarchy + Market Confidence Score
wfs/condition.py        Condition and completeness parsing
wfs/risk.py             0-100 risk scoring
wfs/flip.py             Days-to-sell, net profit, MAX BUY, velocity, Flip Score
wfs/decision.py         BUY/WATCH/PASS, target offers, explanations
wfs/flip_analysis.py    Phase 5 orchestration, ranking, capital allocation
wfs/cache.py            API caching and query pruning
wfs/pipeline5.py        Phase 5 scan pipeline
wfs/theme.py            Premium Ethereum visual system
sample_output.py        Worked BUY/WATCH/PASS example (mock data)
PHASE5.md               Phase 5 methodology
observed_sales.csv      Your own recorded sale observations (optional)
chrono24_asking.csv     Your own recorded asking observations (optional)
dealer_prices.csv       Your own recorded dealer figures (optional)
tests/test_core.py      Phase 1 tests
tests/test_phase2.py    Phase 2 tests
tests/test_phase3.py    Phase 3 tests
tests/test_phase4.py    Phase 4 tests
tests/test_phase5.py    Phase 5 tests — 184 in total, all external calls mocked
```

## Recording your own sold observations

```bash
cp observed_sales.csv.example observed_sales.csv
```

Columns: `sold_date,reference,model_family,price_gbp,source,notes`. Add a row each time
you see a watch on your list actually sell. Around 8 observations per reference in a
90-day window lifts that reference to HIGH confidence. Twenty minutes a week of
record-keeping is what turns this from a filter into a valuation tool.

## Recording asking-market observations

```bash
cp chrono24_asking.csv.example chrono24_asking.csv
cp dealer_prices.csv.example dealer_prices.csv
```

Two or more observations per reference within 60 days activates the source. Older rows
are ignored, because a stale asking price is not market context.

## Where this stands

All four phases of the specification are implemented and tested. The tool does what it was
asked to do: it finds listings where the achievable purchase price appears sufficiently
below a conservative UK resale value, and it says `NO SUITABLE FLIP CANDIDATES FOUND` when
that is the truthful answer.

The remaining constraint is not code. Without sold evidence, market confidence stays LOW,
liquidity stays UNKNOWN, and nothing can reach a `BUY` verdict — by design, not by
oversight. Two things fix that, in order of effort:

1. **Record observations in `observed_sales.csv`.** Roughly eight per reference in a
   90-day window lifts that reference to HIGH confidence. This is the practical route and
   you can start today.
2. **Apply to eBay for Marketplace Insights API access.** `MarketplaceInsightsProvider` is
   already written and activates the moment your keyset is approved.

Run scans regularly even before that. The Analytics tab needs history to be worth
anything, and history only accumulates if you are scanning.
