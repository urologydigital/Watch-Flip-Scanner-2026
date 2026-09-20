# Phase 5.1.1 — Documentation and Legacy Economics Cleanup

A maintenance release. **No methodology changed.** No formula, threshold, weight
or schema was touched. The purpose was to remove obsolete fee documentation and
make it structurally hard for the legacy Phase 2 economics to be reused.

---

## Test results

| | Result |
|---|---|
| Baseline (`pytest` and `python -m pytest`) | **244 passed** |
| Final (both commands) | **286 passed** |

42 new tests, all of them guards. No existing test was weakened or removed.

---

## One real bug found

The **Watchlist tab printed obsolete fee figures as current UI text**:

> "Resale economics in use: 13% selling fees, £25 fixed costs, 12% required margin."

That line read the legacy Phase 2 constants and contradicted the Phase 5.1
banner at the top of the same screen, which correctly said *Economics: UK Private
Seller*. A user could reasonably have believed a 13% fee was being applied.

It now reports the active profile from `EconomicsEngine`, and `app.py` no longer
imports the legacy constants at all — enforced by a test.

This was live UI, not documentation, so it is worth flagging separately.

---

## Obsolete fee references: found and classified

| Location | Classification | Action |
|---|---|---|
| `app.py` Watchlist caption | **OBSOLETE (live UI)** | Fixed — now reads the active profile |
| `app.py` import of `SELLING_FEE_PCT` etc. | **OBSOLETE** | Removed; comment explains why |
| `README.md` MAX BUY formula ("13% and £25") | **OBSOLETE** | Replaced with the profile-based formula |
| `PHASE5.md` §4 net profit block (12.8% / 2.0%) | **OBSOLETE as current** | Retained but wrapped in a `LEGACY PHASE 5 BEHAVIOUR — SUPERSEDED BY PHASE 5.1` callout, with the current model shown beside it |
| `PHASE5.md` §8 MAX BUY formula | **OBSOLETE** | Updated to the current model |
| `wfs/pricing.py` | **LEGACY / BACKWARD COMPATIBILITY** | Retained. Module banner added |
| `wfs/config.py` fee constants | **LEGACY / BACKWARD COMPATIBILITY** | Retained. Marked with a do-not-use block |
| `.env.example` `WFS_SELLING_FEE_PCT` | **LEGACY** | Retained with a comment scoping it to the classic scan |
| `wfs/economics.py` docstring "14.8%" | **CURRENT** | Kept — explains what was corrected |
| `PHASE5.1.md` "14.8% / 12.8%" | **CURRENT** | Kept — historical record of the correction |
| `tests/test_phase511.py` needles | **TEST FIXTURE** | Kept — the guard needs the strings |
| `tests/test_phase2.py` | **TEST FIXTURE** | Untouched — legitimate Phase 2 regression tests |

### Retained deliberately

`wfs/pricing.py` is still imported by `wfs/analysis.py`, which powers the
Phase 1–4 classic scan and is covered by 36 passing tests. Deleting it would
break working functionality. It is now marked instead:

```
LEGACY PHASE 2 PRICING MODULE — SUPERSEDED BY THE PHASE 5.1 ECONOMICS ENGINE.
  DO NOT USE THIS MODULE FOR NEW PHASE 5+ DEVELOPMENT.
```

No runtime warnings were added — documentation-level protection plus the
architecture tests are sufficient and keep the app output clean.

---

## Architecture guards added

Behavioural and structural, not string matching:

1. **AST import-graph check.** Every Phase 5+ module is parsed and asserted not
   to import `wfs.pricing` or the legacy fee constants. Runs per-module, so a
   failure names the offending file.
2. **Behavioural isolation test.** Legacy constants are monkeypatched to absurd
   values (95% fee, £5,000 fixed costs); net profit, MAX BUY, exit costs,
   velocity, Flip Score and verdict must all be unchanged. If anything moves,
   something is still reading the old model.
3. **Profile-tracking test.** Swapping `UK_PRIVATE` → `UK_BUSINESS` must move
   exit costs, net profit, ROI, MAX BUY, all three strategies, capital velocity
   and Flip Score together.
4. **Documentation regression.** Any mention of 12.8 / 0.128 / 13% / 14.8 in
   README or PHASE5.md must appear within a few lines of a legacy marker.
5. **UK private seller regression.** Zero transaction and payment fees by
   default, real costs still deducted, and the zero is explicitly overridable.

---

## Confirmations

- ✅ **`EconomicsEngine` remains authoritative** for net profit, ROI, MAX BUY, all
  three strategies, capital velocity, Flip Score and BUY/WATCH/PASS. No Phase 5+
  module imports the legacy pricing engine.
- ✅ **`UK_PRIVATE` remains the default profile**, asserted in three places.
- ✅ **No Flip Intelligence methodology was changed.** Confidence, velocity, Flip
  Score weights, MAX BUY method, thresholds, liquidity, condition and risk
  scoring, the watchlist, the eBay client, the provider architecture and the
  database schema are all untouched.
- ✅ **Marketplace Insights** is still documented as optional, restricted-access,
  not guaranteed and not required.
- ✅ Both `pytest` and `python -m pytest` pass from the repository root.

---

## Files modified

```
app.py               Fixed obsolete fee caption; dropped legacy imports
wfs/pricing.py       LEGACY module banner
wfs/config.py        Legacy fee constants marked do-not-use
wfs/economics.py     Seller-profile explanatory comments
README.md            MAX BUY formula, default economics table, evidence hierarchy
PHASE5.md            §4 and §8 fee blocks marked SUPERSEDED, current model added
.env.example         Legacy variable scoped to the classic scan
pyproject.toml       Version 5.1.0 -> 5.1.1
tests/test_phase511.py   New: 42 architecture and documentation guards
PHASE5.1.1.md            New: this file
```

No database migration is required. No user settings need updating.
