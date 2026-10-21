"""Low-turnover long-only trend rules. Each returns a causal target-position series.

A rule maps a close series to `pos` where `pos[i]` is the position we want to
hold, decided using ONLY information available at the close of bar i. The engine
then earns the NEXT bar's return on that position, so there is no look-ahead.

Rules implemented (all long-only, all classic and parameter-free apart from the
lookback):
  ma_long(window)        long while close > rolling mean of `window` closes
  breakout(entry, exit)  long after close exceeds the prior `entry`-day high,
                         flat after close falls below the prior `exit`-day low
  donchian_channel(..)   alias for breakout with the classic 20/10

No shorting and no leverage: pos is in {0, 1}. This keeps the study a test of
*timing* (when to be long) rather than of leverage.
"""

import numpy as np
import pandas as pd


def rolling_mean(x, n):
    return pd.Series(x).rolling(n).mean().to_numpy()


def _prior_rolling_max(x, n):
    """max of x[i-n .. i-1] — strictly the PRIOR n bars, excluding i."""
    return pd.Series(x).rolling(n).max().shift(1).to_numpy()


def _prior_rolling_min(x, n):
    """min of x[i-n .. i-1] — strictly the PRIOR n bars, excluding i."""
    return pd.Series(x).rolling(n).min().shift(1).to_numpy()


def ma_long(close, window):
    """Long while the close is above its own trailing simple moving average."""
    ma = rolling_mean(close, window)
    pos = (close > ma).astype(np.float64)
    pos[np.isnan(ma)] = 0.0          # not enough history -> flat
    return pos


def breakout(close, entry=20, exit=10):
    """Donchian-style: enter above the prior `entry`-day high, exit below the
    prior `exit`-day low.

    Stateful, because the exit condition depends on being in the position. The
    entry level uses only prior closes; the exit level likewise. No bar sees its
    own value in either threshold.
    """
    n = len(close)
    up = _prior_rolling_max(close, entry)
    dn = _prior_rolling_min(close, exit)
    pos = np.zeros(n, dtype=np.float64)
    state = 0.0
    for i in range(n):
        if state == 0.0:
            if not np.isnan(up[i]) and close[i] > up[i]:
                state = 1.0
        else:
            if not np.isnan(dn[i]) and close[i] < dn[i]:
                state = 0.0
        pos[i] = state
    return pos


def donchian_channel(close):
    """The classic 20-day-high entry / 10-day-low exit channel."""
    return breakout(close, entry=20, exit=10)


RULES = {
    "ma100": lambda c: ma_long(c, 100),
    "ma200": lambda c: ma_long(c, 200),
    "breakout20": lambda c: breakout(c, 20, 10),
    "ma50": lambda c: ma_long(c, 50),
}


def get_rule(name):
    if name not in RULES:
        raise KeyError(f"unknown rule {name!r}; have {sorted(RULES)}")
    return RULES[name]
