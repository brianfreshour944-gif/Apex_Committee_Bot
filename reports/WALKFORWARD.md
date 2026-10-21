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
