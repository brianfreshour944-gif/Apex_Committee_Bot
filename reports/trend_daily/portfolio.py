"""Equal-weight portfolio view: trend filters applied per asset, vs buy & hold.

A single-asset Sharpe table answers "does the filter time this asset?". A
portfolio view answers the question that matters for a real allocation: does an
equal-weight basket of trend-filtered assets beat an equal-weight basket held
throughout, after costs?

Construction
------------
- Assets are aligned on their COMMON daily dates, so every asset contributes on
  every day (no look-ahead: all series are truncated to the overlap).
- Each asset is either in (its rule is long) or in cash (0%) on each day; there
  is no leverage and no shorting.
- The portfolio return each day is the equal-weighted mean of the per-asset net
  returns, which implicitly rebalances to equal weight daily.
- Compared against an equal-weight buy & hold of the same basket over the same days.

Run:  PYTHONPATH=<repo> python reports/trend_daily/portfolio.py
"""

import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
for p in (REPO, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import engine
import rules as R

CACHE = os.path.join(HERE, "cache")
OUT = os.path.join(HERE, "results")

DIVERSIFIED = ["SPY", "QQQ", "IWM", "DIA", "EFA", "EEM", "GLD", "TLT"]
COST_RT = 0.0006


def load():
    return np.load(os.path.join(CACHE, "daily.npz"))


def common_grid(data, syms):
    """Timestamps present for every symbol."""
    ts = [set(np.asarray(data[f"{s}__ts"]).tolist()) for s in syms]
    return np.array(sorted(set.intersection(*ts)), dtype=np.int64)


def close_on(data, sym, grid):
    """Close series for `sym` sampled on `grid` (a subset of its own stamps)."""
    lookup = dict(zip(np.asarray(data[f"{sym}__ts"]).tolist(),
                      np.asarray(data[f"{sym}__close"], dtype=np.float64)))
    return np.array([lookup[t] for t in grid], dtype=np.float64)


def _date(t):
    return str(np.datetime64(int(t), "s"))


def portfolio(data, syms, rule, cost_rt=COST_RT, warmup=252):
    grid = common_grid(data, syms)
    n = len(grid)

    per_asset = {}
    strat_sum = np.zeros(n)
    bh_sum = np.zeros(n)
    for s in syms:
        c = close_on(data, s, grid)
        res = engine.backtest(c, R.get_rule(rule)(c), cost_rt)
        bh = engine.backtest(c, np.ones(n), cost_rt)
        per_asset[s] = {"sharpe": res["sharpe"], "cagr": res["cagr"],
                        "exposure": res["exposure"], "max_dd": res["max_drawdown"],
                        "n_rt": round(res["n_round_trips"], 1)}
        strat_sum += res["returns"]
        bh_sum += bh["returns"]

    port = strat_sum / len(syms)
    port_bh = bh_sum / len(syms)
    sl = slice(warmup, n)   # drop warmup from every asset
    return {
        "days": int(n - warmup),
        "start": _date(grid[warmup]), "end": _date(grid[-1]),
        "portfolio": engine.metrics(port[sl], np.ones(len(port[sl])), port[sl]),
        "buy_hold": engine.metrics(port_bh[sl], np.ones(len(port_bh[sl])), port_bh[sl]),
        "per_asset": per_asset,
    }


def main():
    os.makedirs(OUT, exist_ok=True)
    data = load()
    report = {}
    for rule in ("ma200", "breakout20", "ma100"):
        r = portfolio(data, DIVERSIFIED, rule)
        report[rule] = r
        p, b = r["portfolio"], r["buy_hold"]
        print(f"\n=== equal-weight basket ({','.join(DIVERSIFIED)}) — rule {rule} ===")
        print(f"  window {r['start']}..{r['end']} ({r['days']} days)")
        print(f"  rule : Sharpe {p['sharpe']:+.2f}  CAGR {p['cagr']:+.1%}  "
              f"maxDD {p['max_drawdown']:.1%}  vol {p['ann_vol']:.1%}")
        print(f"  B&H  : Sharpe {b['sharpe']:+.2f}  CAGR {b['cagr']:+.1%}  "
              f"maxDD {b['max_drawdown']:.1%}  vol {b['ann_vol']:.1%}")

    with open(os.path.join(OUT, "portfolio.json"), "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nSaved {os.path.join(OUT, 'portfolio.json')}")


if __name__ == "__main__":
    main()
