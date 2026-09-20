# Phase 5.2.1 — Small correction pass

Two defects in Phase 5.2, fixed. No redesign, no new APIs, no change to eBay
authentication or `EconomicsEngine`.

**Tests: 338 → 364 passing** on both `pytest -q` and `python -m pytest -q`.

---

## Fix 1 — Manual WatchCharts benchmark now participates

**Before:** a recorded benchmark was stored, loaded and displayed, but had no
effect on anything. It was decoration.

**Now** it is genuine LEVEL B context that shapes the cross-check shortlist.

### What it does

`discount_to_watchcharts_benchmark_pct` is calculated and exposed on
`FlipAnalysis`, in `as_dict()`, in the results table and in the detail view.

The two benchmarks are compared. Divergence is measured against
`WFS521_BENCHMARK_DISAGREEMENT_PCT` (20% default):

| Case | Effect on cross-check score |
|---|---|
| Benchmarks broadly agree | **+10** — an independent source corroborates the discount |
| Benchmarks disagree | **−10** and a warning: *"Market benchmarks disagree — verify manually."* |
| Benchmark is *above* the asking price | **−15** and a warning that the eBay discount may reflect optimistic asking prices rather than a bargain |

Worked example from the live demonstration, asking £1,950 against a £2,410
active median:

```
scenario                    score  vs active  vs bench  agree
no benchmark                 59.7      19.1%         —    -
benchmark £2,310             69.7      19.1%     15.6%   yes
benchmark £1,500             34.7      19.1%    -30.0%    NO
```

### What it still cannot do

Four hard limits, each with a test:

- **Cannot satisfy the sold-evidence gate.** `has_sold_evidence` stays `False`.
- **Cannot raise market confidence.** Confidence is byte-identical with and
  without a benchmark, and stays below the 60 a BUY requires.
- **Cannot produce a BUY.** A £2,800 benchmark on a £1,500 listing yields
  CROSS-CHECK, not BUY.
- **Cannot alter the valuation.** MAX BUY, net profit and the valuation band are
  unchanged even by a wildly optimistic benchmark.

The active eBay median and the manual benchmark are always reported **separately
and never averaged** into a single "market value".

A `CONFIRMED_SOLD` record is deliberately rejected by this path — sold evidence
must travel the sold-evidence route, not arrive as benchmark context.

Stale benchmarks (over 120 days) remain ignored, and no HTTP client was
introduced — asserted by AST inspection of `cross_check.py`.

---

## Fix 2 — Top Flip Opportunities ranking

**Two bugs, one visible and one latent.**

The table filtered `analyses` and sliced `[:30]` **without ranking**, so it
displayed candidates in discovery order — effectively the order eBay returned
them. A strong BUY could sit below a mediocre WATCH.

Separately, `VERDICT_RANK_52` had WATCH above CROSS-CHECK:

```python
# before
VERDICT_RANK_52 = {BUY: 0, WATCH: 1, CROSS_CHECK: 2, PASS: 3}
# after
VERDICT_RANK_52 = {BUY: 0, CROSS_CHECK: 1, WATCH: 2, PASS: 3}
```

CROSS-CHECK outranks WATCH deliberately. A WATCH is already understood and is
waiting on a price move; a CROSS-CHECK is an unexplained discount that will be
gone if nobody looks at it today. The time-sensitive item goes first.

A new `top_opportunities()` helper does the ranking explicitly: BUY, then
CROSS-CHECK, then WATCH, PASS excluded; within a tier by Flip Score, market
confidence, capital velocity, then net profit. The dashboard calls it, and a
test asserts the old slicing pattern has not returned.

```
discovery order: ['watch', 'dull', 'cross', 'buy']
ranked output:   ['buy(BUY)', 'cross(CROSS-CHECK)', 'watch(WATCH)']
```

---

## UI

Candidate detail now shows, when a fresh benchmark exists:

> **Manual market benchmark — NOT confirmed sold evidence**
> WatchCharts benchmark: £2,310 · Discount to benchmark: 15.6%
> Level B (MARKET_BENCHMARK). Market context only: cannot satisfy the
> sold-evidence gate and cannot on its own produce a BUY.

Agreement shows as a green confirmation; disagreement as an amber
*"Market benchmarks disagree — verify manually."* The active eBay median is
displayed independently alongside it.

The results table gains a **Discount vs benchmark** column beside
**Discount vs active**.

---

## Files modified

```
wfs/cross_check.py       Benchmark context, agreement detection, score adjustment
wfs/flip_analysis.py     Benchmark passed through; discount property; rank order fixed;
                         top_opportunities() helper added
app.py                   Ranked Top Opportunities; benchmark detail block; new column
pyproject.toml           Version 5.2.0 -> 5.2.1
tests/test_phase521.py   New — 26 tests
tests/test_phase52.py    Version assertion pinned by minimum rather than exact
PHASE5.2.1.md            This file
```

No database migration. No settings change.

## Configuration added

```
WFS521_BENCHMARK_DISAGREEMENT_PCT=20    # divergence before benchmarks "disagree"
WFS521_BENCHMARK_BONUS=10               # ranking adjustment either way
```

## Test results

```
pytest -q          364 passed
python -m pytest -q  364 passed
```

Per file: core 19, phase2 36, phase3 29, phase4 21, phase5 79, phase51 60,
phase511 42, phase52 52, **phase521 26**.
