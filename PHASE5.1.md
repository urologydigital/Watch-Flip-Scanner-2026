# Phase 5.1 — UK seller economics corrections

A corrective release. Phase 5's methodology is unchanged; its **economics were
wrong for the intended user** and are now fixed.

---

## The correction that matters

Phase 5 deducted roughly **14.8%** from every sale (12.8% selling fee + 2%
payment processing) regardless of who was selling. For a **UK private seller on
eBay, those fees do not apply.**

The effect was not cosmetic. Every downstream number was suppressed:

| Metric (worked example: buy £1,850, resell £2,748) | Phase 5 | Phase 5.1 |
|---|---|---|
| Exit costs | £438 | £32 |
| Net profit | £459 | £866 |
| Standard MAX BUY | £1,944 | £2,286 |
| Flip Score | 93 | 93 |

A suppressed MAX BUY means genuinely good watches were being marked WATCH or
PASS. The tool was systematically too pessimistic for its primary user.

---

## Seller profiles

| Profile | Transaction fee | Payment fee | Status |
|---|---|---|---|
| **UK_PRIVATE** (default) | 0% | 0% | Verified — no eBay selling fees for private sellers |
| **UK_BUSINESS** | 9.3% | 0% | **Estimate.** Plus 0.35% regulatory + £0.30 fixed |
| **CUSTOM** | user-set | user-set | Fully manual |

Real seller-side costs still apply to all profiles: postage £12, insurance £15,
packaging £5 by default, plus an optional service reserve.

Promoted listings and international fees are **separate, named, and off by
default**. Nothing is bundled into a generic percentage.

### The business profile is an estimate

eBay business fees vary by category, shop tier and account. The defaults are a
plausible watches-category figure, not a quote. The UI marks the profile as an
estimate and prompts you to replace the values from a real invoice. Do that
before trusting any number derived from it.

---

## Single source of truth

All financial calculation now lives in **`wfs/economics.py`**. The dashboard,
Flip Score, MAX BUY, ranking, capital velocity and AI payloads all call the same
`EconomicsEngine`. `CostConfig` survives only as a read-only view and computes
nothing.

If a number appears anywhere in the app, this module produced it.

---

## Double-counting audit

Every cost line was audited. One real bug found and fixed:

**Negotiation allowance was counted twice.** It reduced the acquisition price
*and* MAX BUY was divided by `(1 - negotiation)`, inflating the ceiling by the
same factor the price had already been cut. With a 10% allowance this overstated
MAX BUY by about 11%.

It is now applied **once**, to the acquisition price only. A regression test
asserts MAX BUY is identical with and without a negotiation allowance.

Also verified: payment fees are never folded into the transaction fee; shipping
appears once; the service reserve appears in exit costs only and never inside
the risk buffer (which is a separate multiplier on MAX BUY, not a cost line).

---

## Evidence architecture

Priority order, strongest first:

1. **Manually imported sold records** — you saw the sale. Strongest realistic source.
2. **Marketplace Insights** — optional. Access is restricted and most accounts never get it.
3. **Legacy `observed_sales.csv`** — the older manual format, still supported.
4. **Local observation history** — the scanner's own inference. Never confirmed sales.

### Marketplace Insights is optional, not architectural

Previous documentation implied the scanner was waiting on Insights access. It
is not, and it never should have read that way. The scanner is fully functional
without it. If access is granted it slots in as the strongest tier; if not,
nothing breaks.

### Local observation: disappearance is not a sale

The scanner classifies every tracked listing conservatively:

| Classification | Meaning |
|---|---|
| `LIKELY_SOLD` | Seen across several scans at a stable price, then vanished |
| `POSSIBLY_SOLD` | Disappeared after limited price movement |
| `DELISTED_UNKNOWN` | Repeatedly discounted before vanishing, or seen only once |
| `RELISTED` | Vanished then returned |
| `ACTIVE` | Still visible |

Only `LIKELY_SOLD` contributes a price, and observation evidence sits in its own
tier with a **confidence ceiling of 55** — below the 60 a BUY requires. It can
therefore inform a WATCH but can never, on its own, produce a BUY.

The language throughout is "observed market activity", never "confirmed sales".

### Evidence source labels

Every estimate states its provenance without ambiguity:

- `Exact reference sold evidence — Manual Sold Evidence`
- `Exact reference sold evidence — Marketplace Insights`
- `Observed listing activity — Historical Scanner Evidence`
- `Model-family sold evidence — …`
- `Active Asking Prices`
- `Seed / Unverified`

---

## BUY safety, strengthened

Unchanged in principle, tightened in practice. A BUY still requires genuine sold
evidence. Asking prices, seed values and scanner observation each fail the
confidence gate on their own.

New in 5.1: a WATCH backed by unconfirmed evidence is labelled
**"WATCH — Needs Market Confirmation"**, so the reason is visible rather than
buried in the blocker list.

---

## Migration note

**No database migration is required.** Phase 5.1 adds one new table
(`manual_sold_evidence`) which is created automatically on first run. All
existing scan history, listings, price observations and analytics are preserved.

**Existing settings:** there are none to migrate — Phase 5 had no settings file.
On first launch you get `UK_PRIVATE` defaults and `settings.json` is created when
you first save.

**If you customised Phase 5 economics via `.env`:** the old `WFS5_SELLING_FEE_PCT`
and `WFS5_PAYMENT_FEE_PCT` variables are **no longer read**. Their replacements
are `WFS51_*` (see `.env.example`), or preferably use the Settings tab, which
persists to `settings.json`.

**Recalculate anything you saved.** Verdicts computed under Phase 5 used the old
fee model and were too pessimistic. Re-run the scan.

---

## Files added

```
wfs/economics.py                    EconomicsEngine, seller profiles, ExitCosts
wfs/settings_store.py               Settings persistence (never stores secrets)
wfs/observation.py                  Local observation classification
wfs/manual_evidence.py              CSV import, validation, DB provider
tests/test_phase51.py               60 new tests
pyproject.toml                      Packaging + pytest path config
manual_sold_evidence_template.csv   Import template
PHASE5.1.md                         This file
```

## Files modified

```
wfs/config5.py         CostConfig -> read-only view; evidence tiers; source labels
wfs/flip.py            All fee arithmetic delegated to EconomicsEngine
wfs/evidence.py        source_kind, source_label, observed-activity tier
wfs/decision.py        "Needs Market Confirmation" qualifier
wfs/flip_analysis.py   seller_mode recorded; cost breakdown on the card
wfs/pipeline5.py       Uses the composite provider chain
wfs/sold_market.py     CompositeSoldProvider, build_provider, Insights wording
app.py                 Settings tab, Market Evidence tab, seller mode banner
tests/test_phase5.py   Two tests updated to the new economics API
.env.example, README.md
```

Two Phase 5 tests changed: they asserted that a selling fee and payment fee were
both greater than zero, which is exactly the assumption this release corrects.

---

## Test results

```
Baseline (Phase 5):   184 passed
Phase 5.1:            244 passed
```

`pytest` and `python -m pytest` both work from the repository root.

---

## Remaining limitations

- **The business fee defaults are estimates.** Replace them from a real invoice.
- **Private-seller fees can change.** eBay policy is not permanent; the profile
  is configurable precisely so a policy change needs no code change.
- **Local observation is inference.** It improves with scan history but will
  never equal a real sold record. It cannot produce a BUY by design.
- **Marketplace Insights access is unlikely** for most accounts. Manual import
  remains the realistic route to high-confidence evidence.
- **Postage/insurance defaults are generic.** £12/£15 suits a mid-value watch;
  adjust for a £3,000 piece.
- **No VAT handling.** A VAT-registered business seller has obligations this tool
  does not model. Speak to an accountant rather than relying on these figures.
