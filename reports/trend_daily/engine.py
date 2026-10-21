"""Daily backtest engine: close-to-close returns, causal positions, explicit cost.

Timing model (no look-ahead)
----------------------------
`decide[i]` is the target position decided at the close of bar i. The position
actually held during bar i is `held[i] = decide[i-1]` — you cannot earn bar i's
return on a decision made at bar i's close. So every rule is lagged one bar.

Cost model
----------
Cost is charged per SIDE traded, and a round trip is two sides. With
`cost_per_round_trip` (e.g. 0.0006 = 6 bps) the per-side charge is half that.
A trade happens at bar i whenever `held[i] != held[i-1]`; the cost is subtracted
from bar i's return. Total cost therefore equals
`cost_per_round_trip * (number of round trips)`, exactly as intended, and a
long-only rule that never trades pays nothing.

Returns are close-to-close, matching the daily bars. Dividends are reinvested
(yfinance auto_adjust=True), so a buy-and-hold comparison is total return.
"""

import numpy as np

TRADING_DAYS = 252


def close_to_close(close):
    r = np.zeros(len(close), dtype=np.float64)
    r[1:] = close[1:] / close[:-1] - 1.0
    return r


def positions_held(decide):
    """Lag the decision by one bar: the position held during bar i was decided
    at bar i-1's close."""
    held = np.zeros(len(decide), dtype=np.float64)
    held[1:] = decide[:-1]
    return held


def backtest(close, decide, cost_per_round_trip=0.0006):
    """Run one long-only backtest.

    Returns a dict with the per-bar strategy returns, the held-position series,
    and summary metrics. `decide` is the causal target position in {0,1}.
    """
    close = np.asarray(close, dtype=np.float64)
    decide = np.asarray(decide, dtype=np.float64)
    ret = close_to_close(close)
    held = positions_held(decide)

    cost_per_side = cost_per_round_trip / 2.0
    # A position still open on the final bar is liquidated at the end, so every
    # strategy is charged for both sides of every position it takes. Without
    # this, a strategy that happens to end long is undercharged relative to one
    # that ends flat, and buy & hold would count as only half a round trip. The
    # terminal exit cost is charged on the last bar.
    change = np.zeros(len(held), dtype=np.float64)
    change[0] = held[0]
    change[1:] = np.abs(np.diff(held))
    change[-1] += held[-1]
    cost = cost_per_side * change

    strat = held * ret - cost

    entries = int(np.sum((held > 0) & (np.roll(held, 1) == 0)))
    n_sides = float(change.sum())
    n_round_trips = n_sides / 2.0

    m = metrics(strat, held, close)
    m.update({
        "n_sides": n_sides,
        "n_round_trips": n_round_trips,
        "n_entries": entries,
        "total_cost": float(cost.sum()),
    })
    return {"returns": strat, "held": held, "ret": ret, "cost": cost, **m}


def metrics(strat, held, close):
    """Summary statistics for a strategy return series."""
    n = len(strat)
    eq = np.cumprod(1.0 + strat)
    total = float(eq[-1] - 1.0)
    sd = float(strat.std(ddof=1)) if n > 1 else 0.0
    mean = float(strat.mean())
    sharpe = float(mean / sd * np.sqrt(TRADING_DAYS)) if sd > 0 else 0.0

    peak = np.maximum.accumulate(eq)
    max_dd = float((eq / peak - 1.0).min())

    # calendar span from bar count (252 trading days/year)
    years = n / TRADING_DAYS
    cagr = float((eq[-1]) ** (1.0 / years) - 1.0) if years > 0 and eq[-1] > 0 else -1.0

    return {
        "bars": int(n),
        "years": round(years, 2),
        "total_return": round(total, 4),
        "cagr": round(cagr, 4),
        "ann_vol": round(sd * np.sqrt(TRADING_DAYS), 4),
        "sharpe": round(sharpe, 3),
        "max_drawdown": round(max_dd, 4),
        "exposure": round(float(held.mean()), 3),
    }


def buy_and_hold(close, cost_per_round_trip=0.0006):
    """Long from the first bar to the last, paying one entry and one exit."""
    decide = np.ones(len(close), dtype=np.float64)
    return backtest(close, decide, cost_per_round_trip)
