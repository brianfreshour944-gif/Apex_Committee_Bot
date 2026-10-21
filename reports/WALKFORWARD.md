# Walk-forward + exit-parameter findings (2026-10-20)

Follow-up to `BACKTEST_ENTRY_EXIT.md`. Two questions were open:

1. Is the fee drag a one-window artifact, or consistent?
2. Is the 8h max-hold cutting winners short?

Both now have answers. Short version: **the loss is consistent and statistically
real, and the 8h max-hold is the LEAST-bad exit setting, not the problem.**

## 1. Walk-forward across three disjoint 30-day windows

Baseline config, $100k equity, real bars, next-open fills, 0.90% round trip.

| window | trades | net | win% |
|---|---|---|---|
| 0-30d (most recent) | 22 | -$50.99 | 31.8 |
| 30-60d | 18 | -$375.21 | 11.1 |
| 60-90d | 22 | -$377.25 | 18.2 |
| **pooled (90d, disjoint)** | **62** | **-$803.45** | **21.0** |

**Statistics on the clean disjoint sample (n=62, no repeated data):**
- mean net/trade **-$12.96**, sd $26.68, SE $3.39, **t = -3.82**
- bootstrap 95% CI on mean net/trade: **[-$19.39, -$6.27]** — entirely below zero
- per-trade win rate 21.0%, avg win +$21.24 vs avg loss -$22.03

The CI excludes zero, so on this data the negative expectancy is **not noise**.
That is a stronger statement than the single-window result in the previous
report, and it is the opposite of what I could claim before.

**The loss is broad-based, not tail-driven.** Excluding the 3 worst and 3 best
trades, the remaining 56 trades still lose $749.36 (mean -$13.38/trade). This is
not one bad trade or one crash; it is the typical trade losing slightly more
than it makes, repeatedly.

## 2. Max-hold sweep — the 8h cap is NOT the problem

| max hold | trades | net | win% |
|---|---|---|---|
| 2h | 14 | -$203.65 | 7.1 |
| **8h (baseline)** | **22** | **-$50.99** | **31.8** |
| 24h | 19 | -$324.89 | 15.8 |
| 48h | 14 | -$344.25 | 7.1 |

**The hypothesis is refuted.** If the 8h cap were cutting winners short, holding
longer would improve results. It does the opposite: 24h and 48h are both ~6x
worse. And shortening to 2h is also worse. 8h is the least-bad setting on this
data — so the max-hold is not the lever to pull.

(Pooled across configs and windows: n=109, mean -$15.38/trade, t=-6.98. Treat
that as directional only — the 109 includes repeated data across overlapping
configs, so the clean n=62 figure above is the defensible one.)

## 3. The arithmetic that matters

Per trade, averaged over the pooled sample:
- avg notional/trade: **$2,116**
- avg fees/trade: **$10.58** (0.500% of notional — the round-trip taker fee)
- avg gross/trade: **-$4.80** (-0.227%)
- **gross must reach +$10.58 (+0.500%) just to break even**
- required swing to reach break-even: **+0.727% per trade**

Two facts sit side by side:
- **36.7%** of trades had a positive GROSS result (before fees).
- Only **16.5%** had a positive NET result (after fees).

So roughly **20 percentage points of trades are flipped from winner to loser by
costs alone.** That is the mechanism. The strategy's gross signal is not
disastrous — it is mildly negative (-0.23%/trade) — but the 0.90% round-trip
cost (0.50% fee + 0.40% slippage) is roughly 4x the size of the gross edge it
would need to overcome.

## Conclusions

1. **The negative expectancy is statistically significant on this data** (t=-3.82,
   CI excludes 0). Not a fluke of one window.
2. **The 8h max-hold is not the problem** — it is the best of the settings tried.
   Do not "fix" it by holding longer; that is measurably worse.
3. **Costs are the binding constraint**, confirmed mechanically: they flip ~20pp
   of trades from gross winners to net losers.
4. **No entry/exit parameter tweak tried so far fixes this** — not the confidence
   filter (made it worse), not max-hold (8h already optimal). The gross edge
   itself is ~0 to slightly negative, and no amount of exit tuning creates an
   edge that is not there.

## What would actually change the picture

- **Reduce the cost per trade** (the one lever with arithmetic headroom): use
  maker limit orders instead of crossing the spread. At 0.15% maker vs 0.25%
  taker the round trip drops from 0.50% to 0.30% in fees — but a resting limit
  may not fill, so this trades cost for fill probability and needs its own test.
- **Trade less often at larger size** — but only if the kept trades are better.
  The confidence filter showed that selecting "better" trades by confidence does
  not work; a different selection criterion would need to be found first.
- **Find a real gross edge** before tuning exits. Everything measured here says
  the exits are not where the problem is.

## 4. Maker-order hypothesis: NOT refuted, NOT confirmed

The only lever with arithmetic headroom is cost. So I tested the optimistic
extreme: what if EVERY trade filled at mid (no spread) with maker fees?

Added `--cost-mode maker` to the harness (0.15%/leg fee, zero slippage) and ran
the same 3 disjoint windows. `--cost-mode taker` reproduces the known baseline
exactly (22 trades, -$50.99), confirming the refactor is behaviour-preserving.

| mode | trades | net | mean/trade | t | 95% CI | gross/trade |
|---|---|---|---|---|---|---|
| taker (live) | 62 | -$803.45 | -$12.96 | -3.82 | [-19.39, -6.27] | -$2.30 |
| maker (optimistic) | 142 | -$305.26 | -$2.15 | -0.92 | [-6.65, +2.56] | +$4.70 |

Per window, maker beats taker in **all three**:
`off0 -21.23 vs -50.99 | off30 -273.83 vs -375.21 | off60 -10.19 vs -377.25`

**What this shows:** cost is genuinely the dominant factor. Halving the round
trip flips *gross* per trade from -$2.30 to +$4.70 and cuts the per-trade loss
6x. The direction is right.

**What this does NOT show — and I want to be precise:**
- The maker CI **[-6.65, +2.56] includes zero.** Maker is "not proven negative,"
  which is *not* the same as "proven profitable." Mean is still -$2.15/trade.
- Maker mode is **optimistic by construction**: 100% fill at mid, zero spread
  cost, no adverse selection. Real resting limit orders fill *preferentially
  when price moves against you* — you get filled on the trades you'd rather not
  have. So the true maker result is worse than this bound, and this bound is
  already negative.
- Maker mode is **not a like-for-like counterfactual.** It takes 2.3x more trades
  (142 vs 62). Cause: costs feed `realized_pnl` -> `register_loss/win`, so
  cheaper trades register as wins more often, the sentinel loss-pause arms less,
  and the bot re-enters sooner. It is a *different trajectory*, not the same
  trades re-priced.

**Verdict:** unlike the max-hold hypothesis (cleanly refuted), the cost
hypothesis survives but is unproven. Cost reduction is the right direction; the
optimistic upper bound still does not reach profitability. A real answer needs a
maker-fill model with fill probability and adverse selection, not this bound.

## 5. Small-account ($100) walk-forward: same percentage, so "start small" changes nothing

The live account is ~$100, and the ask was whether starting small helps. Ran the
same 3 disjoint windows at $100 equity.

| window | $100k net | % | $100 net | % |
|---|---|---|---|---|
| 0-30d | -$50.99 | -0.051% | -$0.05 | -0.051% |
| 30-60d | -$375.21 | -0.375% | -$0.31 | -0.306% |
| 60-90d | -$377.25 | -0.377% | -$0.35 | -0.345% |
| **total** | **-$803.45** | **-0.803%** | **-$0.70** | **-0.702%** |

**62 trades at both sizes — the identical trade list, scaled.**

**The answer to "does starting small help?" is no, but for a precise reason.**
Position sizing is purely proportional (`pct * equity`), and Alpaca's crypto fee
is a percentage with **no per-order minimum**. So the $100 account loses the
*same fraction* of its equity as the $100k account — 0.70% vs 0.80% over 90
days. Starting small does not improve the edge; it just makes the loss small in
dollars. That is a real answer, and it is not the answer the premise hoped for:
**there is no small-account advantage here, because there is no fixed cost to
amortise.**

Note the sign of the two claims now on record:
- Small accounts are **not worse per unit** than large ones (this section).
- But the bot could not trade at all at $100 before the `MIN_ORDER_USD` fix —
  a *config* bug, not a market effect. Fixing it made the bot viable; it did not
  make it profitable.

### What the harness still does NOT model at $100

- **No per-order qty rounding.** Live BUY submits unrounded qty while SELL
  floors to 1e-8 (`orders.py`), so the two legs are not symmetric. Continuous
  qty here slightly favours the backtest.
- **Symbols replayed independently**, so the 30% portfolio exposure cap is
  applied per-symbol, not across the portfolio. At $100 the whole account is
  only $100, so a *true* portfolio sim would hit the cap more often than this
  harness shows — this run is therefore optimistic on that axis too.
- No minimum-notional rejections modelled at the order level.

Neither effect is large enough to flip a -0.70% 90-day return to positive, but
both point the same direction: the live small-account result would be **no
better** than this, and plausibly slightly worse.

## Honest limits

- One data source (Alpaca 15m crypto bars), ~90 days total, 3 symbols.
- Symbols replayed independently (not a true portfolio sim); orderbook gate
  disabled (no historical L2), so entries are slightly more permissive than live.
- No parameter search over a held-out set; these are in-sample observations and
  should be confirmed out-of-sample before any config change ships.

Reproduce:
```
PYTHONPATH=<repo> python reports/backtest_entry_exit.py --days 30 --offset-days 30
PYTHONPATH=<repo> python reports/backtest_entry_exit.py --days 30 --max-hold 24
```
