# Low-turnover trend filters on daily bars — do they beat buy & hold?

**Question.** The intraday transformer study found no tradeable signal (a ~+0.03
IC against a ~90 bps round-trip cost). Before accepting that, test the opposite
design: a *low-turnover* long-only rule on **daily** bars — price above a
100/200-day average, or a 20-day breakout. Daily bars go back decades, so the
statistical power the intraday study lacked is available, and there are only a
few round trips per month.

**Short answer.** Trend filters are **not** an alpha source — in raw return they
lose to buy & hold, and not one of the 48 rule-asset combinations beats buy &
hold at conventional significance (20 of 48 significantly *underperform*). But
they are a real **risk-management** tool: a 200-day filter cuts the equal-weight
drawdown from 41% to 14% and lifts the Sharpe ratio slightly (0.74 vs 0.69).
Net: it changes the shape of the return, not the rate. Whether that is worth it
depends on the objective, not on the data.

---

## Method

- **Assets (12):** SPY, QQQ, IWM, DIA, EFA, EEM, GLD, TLT, AAPL, MSFT, NVDA,
  BTC-USD. Daily bars, **fixed end date 2026-10-19**, up to 8,001 bars / 31 years
  (SPY, AAPL, MSFT from 1995). Equity history from yfinance (survivorship caveat:
  the single names are *today's* survivors — this biases toward trend working).
- **Rules:** `ma50`, `ma100`, `ma200` (long while close > trailing SMA) and
  `breakout20` (enter above the prior 20-day high, exit below the prior 10-day
  low). Long-only, no leverage, position in {0,1}.
- **Engine (no look-ahead):** the decision made at bar *i*'s close is held during
  bar *i+1*; return is close-to-close; the strategy earns bar *i*'s return on the
  position decided at bar *i-1*. Verified by mutation test (scrambling future
  bars does not change past decisions — all 4 rules).
- **Costs:** charged **per side**, so total cost = `cost_per_round_trip` x round
  trips (default 6 bps). Every strategy, including buy & hold, is charged a
  terminal liquidation, so a strategy that ends long is not undercharged.
- **Inference:** block-bootstrap CIs (21-day blocks) on Sharpe and total return;
  a **paired** block-bootstrap test of each rule's daily return *minus* buy &
  hold (common beta cancels, so this is a powerful test of *timing*); a
  matched-exposure random-entry control (same exposure, same turnover); per-window
  walk-forward stability; and walk-forward **rule selection** (pick the best rule
  in-sample, apply out-of-sample).

All evaluation windows begin after 252 bars of warmup, shared by every rule and
by buy & hold, so the comparison is like-for-like.

---

## Result 1 — Raw return: no edge, and often a significant negative

Per-asset comparison of each rule against buy & hold over the identical window:

| asset | ma100 | ma200 | breakout20 | ma50 | buy & hold (Sharpe) |
|---|---|---|---|---|---|
| SPY | +0.56 | **+0.69\*** | +0.47 | +0.40 | +0.61 |
| QQQ | +0.36 | **+0.50\*** | +0.34 | +0.46\* | +0.42 |
| IWM | +0.38 | +0.37 | +0.20 | +0.30 | +0.46 |
| DIA | +0.37 | +0.31 | +0.41 | +0.39 | +0.54 |
| EFA | +0.51\* | **+0.54\*** | +0.15 | +0.41 | +0.46 |
| EEM | +0.44\* | +0.32 | +0.28 | +0.22 | +0.40 |
| GLD | +0.47 | +0.56 | +0.38 | +0.47 | +0.63 |
| TLT | +0.17 | +0.07 | +0.26 | +0.06 | +0.29 |
| AAPL | +0.71 | **+0.93\*** | +0.75 | +0.70 | +0.78 |
| MSFT | +0.43 | +0.59 | +0.60 | +0.46 | +0.69 |
| NVDA | +0.79\* | **+0.94\*** | +0.72 | +0.85\* | +0.79 |
| BTC-USD | +1.08\* | +0.98 | +1.15\* | +1.23\* | +0.93 |

(\* beats buy & hold on Sharpe.) Trend filters raise Sharpe on 7/12 assets with
`ma200`, and it is the most consistent rule. But Sharpe is not the whole story —
the paired test below is the decisive one.

**Paired vs buy & hold — annualised excess return (95% block-bootstrap CI):**

| asset | ma100 | ma200 | breakout20 | ma50 |
|---|---|---|---|---|
| SPY | -5.1% [-10,-1] | -3.2% [-7,+1] | -7.3% [-12,-2] | -7.2% [-11,-3] |
| DIA | -5.9% [-10,-1] | -6.4% [-11,-2] | -6.3% [-11,-1] | -5.8% [-10,-1] |
| AAPL | -10.1% [-19,-1] | -3.6% [-13,+6] | -13.8% [-24,-2] | -12.2% [-22,-2] |
| MSFT | -11.7% [-18,-5] | -7.9% [-15,-2] | -10.4% [-18,-3] | -11.6% [-18,-6] |
| NVDA | -12.9% [-27,+2] | -5.2% [-20,+9] | -21.1% [-38,-5] | -10.9% [-25,+3] |
| BTC-USD | -4.9% [-21,+10] | -7.8% [-23,+7] | -10.5% [-28,+7] | -1.4% [-17,+14] |

Across all 48 rule-asset pairs: **0 beat buy & hold at 95%; 20 significantly
underperform; 28 inconclusive.** The point estimates are negative almost
everywhere: filtering out of the market costs more return than it saves in risk
on a pure-return basis. This is the mirror image of the intraday result — here the
signal exists (trend is real) but timing *out* of a rising asset forfeits
compounding.

## Result 2 — Risk: the genuine finding

The one place trend filters deliver is drawdown. Equal-weight portfolio of
`SPY,QQQ,IWM,DIA,EFA,EEM,GLD,TLT` (common window 2005-11-17 → 2026-10-16):

| | Sharpe | CAGR | max drawdown | ann vol |
|---|---|---|---|---|
| **ma200 filter** | **+0.74** | +6.5% | **-14.2%** | 9.0% |
| buy & hold | +0.69 | **+9.9%** | -41.4% | 15.5% |
| breakout20 | +0.54 | +3.6% | -14.3% | 7.0% |

The 200-day filter roughly **halves the volatility and cuts the max drawdown by
about two-thirds**, at the price of ~3.4 percentage points of annual return. Per
asset the pattern is stark (SPY: -55% -> -29% drawdown; QQQ: -83% -> -59%; EFA:
-61% -> -28%; NVDA: -90% -> -55%). A leverage-scaled version of the filtered
portfolio could match the buy-and-hold return at lower risk — but that is a
*portfolio-construction* decision, and any such scaling must be evaluated out of
sample (see the walk-forward below, which is mixed).

## Result 3 — Controls and stability

**Matched-exposure random-entry control.** For each rule we build random
long-only schedules with the *same* exposure and the *same* number of round trips
(verified: exposure and turnover match to <1%), score them on the same returns,
and ask whether the rule's Sharpe is in the upper tail.

- Only **7 of 48** pairs are significant at p < 0.05, and they cluster on BTC
  (ma50/ma100/breakout20) and the highest-momentum names (AAPL ma200, NVDA
  ma200/ma50) — i.e. where trend was strongest. For SPY/QQQ/IWM/DIA/GLD/TLT/EEM,
  random entry with matched exposure does just as well as the rule. The timing
  edge, where present, is thin and concentrated.

**Walk-forward rule selection.** On each expanding in-sample window we pick the
best-Sharpe rule and apply it to the next, unseen window:

- The selected rule beats buy & hold out-of-sample in **5 of 12** assets
  (SPY, EFA, AAPL, NVDA, BTC-USD) — better than a coin flip, not compelling.
- `ma200` is chosen in 28 of 72 windows — the most robust single choice, but the
  selection is unstable (QQQ flips ma50 → ma200; MSFT flips ma200 → breakout20).

**Cost sensitivity** (mean Sharpe across 12 assets):

| cost/round trip | ma200 | breakout20 | ma100 | buy & hold |
|---|---|---|---|---|
| 0 bps | +0.58 | +0.50 | +0.55 | +0.58 |
| 6 bps | +0.57 | +0.48 | +0.52 | +0.58 |
| 20 bps | +0.53 | +0.41 | +0.47 | +0.58 |
| 50 bps | +0.46 | +0.28 | +0.36 | +0.58 |

Unlike the intraday study — where cost was the whole game — the low-turnover
design keeps cost small (few round trips: e.g. ma200 on BTC does 38 over 16
years). Buying and holding pays *zero* ongoing cost, so cost alone already puts
the rule behind on Sharpe; higher costs only widen the gap.

---

## Honest interpretation

1. **As a return strategy, no.** Trend filters do not beat buy & hold on raw or
   risk-adjusted *return* in a majority of cases, and the paired test finds no
   significant outperformance anywhere (20/48 significantly negative). The
   classic result that timing-filter backtests look good in isolation but lose to
   buy & hold once you compare on the same days is reproduced here with 30 years
   of data.

2. **As risk management, yes.** The 200-day filter's value is drawdown, not
   return: -41% → -14% on the portfolio. That is a real, large, and robust effect
   (it appears in every sub-period except the strongest bull runs, where it
   simply lags). It is not alpha; it is a variance/DD reducer.

3. **The timing edge is thin and concentrated.** Only 7/48 matched-exposure tests
   are significant, clustered on the strongest-trending assets (BTC, NVDA, AAPL).
   A defensible reading: trend-following pays where trends are strong and
   persistent; on broad indices it is close to "be long a random 70% of days."

4. **What would change the verdict.** (a) A rule whose paired CI excludes zero
   *positively* — none here. (b) A vol-targeted or leverage-scaled version of the
   filtered portfolio that matches buy-and-hold return at materially lower risk
   *out of sample* — plausible but not demonstrated (selection generalised 5/12).
   (c) Multi-asset breadth with cross-sectional selection, which this per-asset
   study does not test.

## Limitations

- **Survivorship bias** in the single names (AAPL/MSFT/NVDA are survivors) and
  in the ETF list; this *favours* trend and buy & hold alike, and is a known
  caveat of yfinance daily data.
- **No dividends-withholding or financing** modelling beyond adjusted closes;
  no market impact (irrelevant at daily frequency).
- Assets are analysed **independently**; the equal-weight portfolio is the only
  joint view and it uses implicit daily rebalancing (no rebalancing cost modelled
  beyond the underlying position changes).
- The walk-forward uses expanding (anchored) training with a fixed 6 windows; a
  rolling window or more windows might change the selection stability picture.

## Files

| file | purpose |
|---|---|
| `fetch_daily.py` | fetch + cache deep daily history with a fixed end date |
| `rules.py` | ma50/ma100/ma200 and the 20-day breakout, all causal |
| `engine.py` | close-to-close backtest, 1-bar lag, per-side cost, terminal liquidation |
| `stats.py` | block bootstrap, paired-vs-benchmark test, matched-exposure random control |
| `portfolio.py` | equal-weight basket view (the decision-relevant form) |
| `experiment.py` | the full study (per-asset, paired, control, walk-forward, cost) |
| `results/*.json` | all raw numbers behind the tables |
| `tests/test_trend_rules.py` | 31 checks: no look-ahead, cost/lag accounting, stat calibration |

Reproduce:
```
pip install yfinance numpy pandas scipy
PYTHONPATH=<repo> python reports/trend_daily/fetch_daily.py
PYTHONPATH=<repo> python reports/trend_daily/experiment.py
PYTHONPATH=<repo> python reports/trend_daily/portfolio.py
```
