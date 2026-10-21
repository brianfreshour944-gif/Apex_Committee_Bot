# Entry/Exit Backtest — results (2026-10-20, updated after the small-account fixes)

Harness: `reports/backtest_entry_exit.py` (replays the LIVE decision path).
Data: real Alpaca 15-minute crypto bars, 30 days, credential-free endpoint.
Fills: next bar's OPEN. Costs: **0.25%/leg fee (real Alpaca Tier-1 taker) +
0.20%/leg slippage = 0.90% round trip**.

Two config bugs found and fixed while doing this (see below): an invented $10
minimum order floor that made a $100 account untradeable, and an understated
fee rate (0.10% vs the real 0.25%).

## Headline: fee-bound, and filtering for higher confidence makes it WORSE

| run | trades | win% | net | fees |
|---|---|---|---|---|
| $100k, no conf filter | 22 | 31.8 | **-$50.99** | $246.00 |
| $100k, min-conf 0.55 | 6 | 33.3 | **-$145.72** | $69.94 |
| $100, no conf filter | 22 | 31.8 | **-$0.05** | $0.25 |

Same trades at $100k and $100 (sizing is proportional), so the small account is
not worse *per unit* — it is the same losing profile scaled down. At $100 the
loss is 5 cents, because the total notional traded is tiny.

**The important negative result:** raising the entry bar to confidence >= 0.55
*cuts* trades from 22 to 6 and makes the net result roughly **3x worse**. The
higher-confidence trades were not better. This refutes the intuitive "trade only
high-conviction signals" fix — on this sample, confidence is not a useful
quality filter, and cutting trades just concentrates the cost drag into fewer,
not-better bets.

## The two real bugs, and why the $100 account took zero trades

### Bug 1 — `MIN_ORDER_USD=$10` was invented, and it silently disabled small accounts

`$10` is not an Alpaca rule. Alpaca's crypto floor is a **$1 notional minimum**
for buy orders (`cost basis must be >= minimal amount of order 1`, code
40310000). At $100 equity:

| conf tier | % equity | size | vs old $10 floor |
|---|---|---|---|
| >= 0.90 | 15% | $15.00 | passes |
| >= 0.75 | 10% | $10.00 | exactly at floor |
| >= 0.60 | 5% | $5.00 | **blocked** |
| fallback | 2.5% | $2.50 | **blocked** |

The measured 30-day committee BUY-confidence ceiling is **BTC 0.688 / ETH 0.579
/ SOL 0.736** — none reach the 0.75 tier. So every entry sized below $10 and was
zeroed. **The bot took 0 trades in 30 days and the log said nothing was wrong.**
Fixed: `min_order_usd` default lowered to the real $1 floor.

### Bug 2 — the sentinel's risk cap could turn "smaller" into "nothing"

The sentinel caps size to 50%/60% after ATR spikes or consecutive losses. That
is a *risk reduction*, but on a small account it multiplied an already-small
affordable trade below the floor and zeroed it — e.g. $50 equity, fallback tier
$1.25, x 0.50 cap = $0.63 < $1 → no trade. Fixed: when the cap is what pushes an
otherwise-affordable trade under the floor, trade the minimum instead of
nothing. A genuinely unaffordable tier still returns 0.

### Bug 3 (accuracy) — fee rate understated 2.5x

`fee_rate` was 0.001 (0.10%). The bot submits limit orders that *cross the
spread* to fill immediately, so both legs are **takers** at Alpaca Tier 1 =
**0.25%**. Corrected to 0.0025. Not cosmetic: the fee feeds `realized_pnl`, which
decides `register_loss()` vs `register_win()`, so the old rate let sub-breakeven
trades count as wins.

## Result of the fixes

- **$100 account: 0 trades → 22 trades.** The bot is no longer silently inert.
- Every sizing tier now clears the floor at $100 (from $2.50 to $15.00).
- Sentinel cap floors to the minimum rather than skipping.

## What this does NOT mean

- **It does not make the bot profitable.** Net is still negative (-$50.99 at
  $100k on 22 trades). Fixing the floor made the bot *able to trade*; it did not
  give it an edge.
- **It is not an edge estimate.** One 30-day window, one fill model, no
  walk-forward, no Monte Carlo, no significance test. 22 trades is far too few
  to distinguish skill from noise.
- **The confidence-filter result is a single-sample finding.** It says confidence
  did not separate winners from losers *in this window*. Suggestive, not
  conclusive.

## Caveats

1. Symbols replayed **independently** — max-positions and the exposure cap are
   per-symbol, not portfolio-wide. The total is a sum of three runs.
2. **Orderbook gate disabled** (no historical L2). Entries are slightly *more*
   permissive than live, so live results would be no better.
3. **Zero take-profits** fired in 30 days; most exits were the 8h max hold. The
   +6% target is rarely reachable in an 8h window, so the cost side is certain
   while the profit side mostly is not. Unchanged by these fixes — worth revisiting.
4. 15m bars: when a bar spans both stop and target, the stop is assumed first.

## Honest bottom line for a small account

Making a $100 account tradeable is a genuine fix — it removes a config bug that
silently disabled the bot. But it does **not** make the bot win, and the
"be more selective" instinct made things worse on this data. The binding
constraint is the **0.90% round-trip cost against a ~0 gross edge**. Until there
is a measured gross edge that clears that cost, small-account profitability is
not something this backtest can demonstrate.

Reproduce:
```
PYTHONPATH=<repo> python reports/backtest_entry_exit.py --days 30 --equity 100
PYTHONPATH=<repo> python reports/backtest_entry_exit.py --days 30 --min-conf 0.55
```
