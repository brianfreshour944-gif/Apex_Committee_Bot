# Entry/Exit Backtest — results (2026-10-20)

Harness: `reports/backtest_entry_exit.py` (replays the LIVE decision path).
Data: real Alpaca 15-minute crypto bars, 30 days, credential-free endpoint.
Fills: next bar's OPEN. Costs: 0.10%/leg fee + 0.20%/leg slippage = **0.60% round trip**.

## Headline: the strategy is fee-bound, not signal-bound

| symbol | trades | win% | gross | fees | net | PF | buy&hold | excess |
|---|---|---|---|---|---|---|---|---|
| BTC/USD | 6 | 33.3 | +9.23 | 26.06 | **-16.84** | 0.63 | +0.78% | -0.79% |
| ETH/USD | 9 | 44.4 | -76.14 | 41.00 | **-117.14** | 0.15 | -9.60% | +9.48% |
| SOL/USD | 19 | 36.8 | +46.78 | 85.26 | **-38.48** | 0.84 | -5.64% | +5.61% |
| **total** | **34** | **38.2** | **-20.13** | **152.32** | **-172.46** | — | — | — |

Gross PnL is **-$20** on ~$152,000 of traded notional — statistically
indistinguishable from zero (-0.013%). Fees of **$152** turn that into a
**-$172** loss. **Fees are 88% of the net loss.** The entry/exit logic is not
losing money on direction; it is paying a toll on every round trip.

## Exit-reason mix (34 trades)

- `Max hold 8.0h` — 19 (56%)
- `Committee SELL override` — 2
- `Trailing stop` — 1
- `Stop loss` / `Take profit` — 0 (see caveat below)

The strategy is dominated by the 8-hour time stop. Most trades drift sideways
until the clock expires, then pay the round-trip cost. **Zero take-profits** in
30 days: the +6% target is rarely reached in an 8-hour window, so the profit
side of the strategy is effectively unreachable while the cost side is certain.

## The $100-account finding (matches the live symptom)

Running the same harness at the live **$100** equity: **0 trades in 30 days**,
on all three symbols. This is the "stays at 0 forever" symptom, and it is a
config/account-size problem, not a signal problem:

| conf tier | % equity | equity needed to clear MIN_ORDER_USD=$10 |
|---|---|---|
| >= 0.90 | 15.0% | $67 |
| >= 0.75 | 10.0% | $100 |
| >= 0.60 | 5.0% | $200 |
| >= 0.00 | 2.5% | $400 |

At $100 equity a BUY needs conf >= 0.75 to reach $10. The 30-day BUY-confidence
ceiling measured across symbols is **BTC 0.688 / ETH 0.579 / SOL 0.736** — none
reach 0.75. So **no entry can ever clear the minimum order size**, regardless of
signal quality. With a sentinel size cap active (2+ consecutive losses → 60%),
even conf 0.90 sizes to $0.

## Caveats (do not over-read these numbers)

1. **Symbols replayed independently.** MAX_OPEN_POSITIONS and the aggregate
   exposure cap are per-symbol here, not portfolio-wide. The total is the sum of
   three independent runs, not a true portfolio simulation.
2. **Orderbook gate disabled** — it needs live L2 depth, which has no historical
   equivalent, and it is a fail-closed VETO. Entries here are therefore slightly
   *more* permissive than live. Real results would be no better.
3. **No stop-loss or take-profit exits fired.** Because bars are 15m and fills
   are at the next open, the ladder is evaluated on bar closes only; the 4% stop
   and +6% target were not touched in this window (range was too narrow relative
   to position duration). A different window would exercise them.
4. **Single 30-day window, single fill model.** No walk-forward, no parameter
   search, no Monte Carlo. This is one sample, not an edge estimate.
5. Fill model has no partial fills, no market impact, no funding. Optimistic on
   size for a $100k book; the fee/slippage toll is the dominant, well-modelled
   term.

## What this implies

- **Do not size up to chase signals.** The measured gross edge is ~0. The fix is
  fewer, larger, longer-held trades (to amortise the 0.60% round trip), or lower
  costs — not more entries.
- **The 8h max-hold is doing most of the work and is likely too short** for a
  +6% target: it converts would-be winners into cost-paying round trips.
- **The $100 account cannot trade this configuration at all.** Either raise the
  paper balance (Alpaca paper normally starts at $100k) or lower `MIN_ORDER_USD`
  deliberately, accepting that every fill is then a large % of equity.

Reproduce:
```
PYTHONPATH=<repo> python reports/backtest_entry_exit.py --days 30
PYTHONPATH=<repo> python reports/backtest_entry_exit.py --days 30 --equity 100
```
