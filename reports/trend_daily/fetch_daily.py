"""Fetch and cache deep daily-bar history for the low-turnover trend study.

Data sources
------------
- Equities/ETFs/single names: yfinance (survivorship caveat — see README). ~31
  years for SPY/AAPL, which is what gives this study real statistical power.
- Crypto: yfinance too (BTC-USD back to 2014) so every asset shares one clean
  daily schema. The live bot trades crypto, so crypto is the like-for-like case.

A FIXED end date is used so the sample does not drift between runs. A fixed end
date is also required for an honest walk-forward: every window ends on or before
END_DATE and nothing peeks past it.

Run:  PYTHONPATH=<repo> python reports/trend_daily/fetch_daily.py
"""

import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

CACHE = os.path.join(HERE, "cache")
OUT = os.path.join(HERE, "results")

# Fixed end date — do not use "today", or results move between runs.
END_DATE = "2026-10-19"
START_DATE = "1995-01-01"

# ETFs (diversified asset classes) + single names + crypto. Mixing them lets the
# study separate "trend works on equities" from "trend works everywhere".
ASSETS = {
    "SPY": "equity_us_large",
    "QQQ": "equity_us_nasdaq",
    "IWM": "equity_us_small",
    "DIA": "equity_us_dow",
    "EFA": "equity_intl_dev",
    "EEM": "equity_em",
    "GLD": "commodity_gold",
    "TLT": "bond_long_treasury",
    "AAPL": "equity_single",
    "MSFT": "equity_single",
    "NVDA": "equity_single",
    "BTC-USD": "crypto",
}


def fetch(symbol, start=START_DATE, end=END_DATE):
    import yfinance as yf
    df = yf.download(symbol, start=start, end=end, interval="1d",
                     progress=False, auto_adjust=True, actions=False)
    if df is None or len(df) == 0:
        return None
    # yfinance returns a MultiIndex (field, ticker) for a single symbol too
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    df = df.dropna(subset=["close"])
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df[~df.index.duplicated(keep="last")].sort_index()


def main():
    os.makedirs(CACHE, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)
    store = {}
    meta = {"end_date": END_DATE, "start_date": START_DATE, "assets": {}}
    for sym, kind in ASSETS.items():
        df = fetch(sym)
        if df is None or len(df) < 300:
            print(f"  {sym:<8} SKIP (only {0 if df is None else len(df)} bars)")
            continue
        store[f"{sym}__ts"] = np.asarray(df.index.view("int64"), dtype=np.int64)
        for col in ("open", "high", "low", "close", "volume"):
            store[f"{sym}__{col}"] = df[col].to_numpy(np.float64)
        meta["assets"][sym] = {
            "kind": kind, "bars": len(df),
            "start": str(df.index.min().date()), "end": str(df.index.max().date()),
        }
        print(f"  {sym:<8} {kind:<20} {len(df):>5} bars  "
              f"{df.index.min().date()} -> {df.index.max().date()}")

    np.savez_compressed(os.path.join(CACHE, "daily.npz"), **store)
    with open(os.path.join(CACHE, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)
    print(f"\nSaved {len(store)} assets to {CACHE}/daily.npz")


if __name__ == "__main__":
    main()
