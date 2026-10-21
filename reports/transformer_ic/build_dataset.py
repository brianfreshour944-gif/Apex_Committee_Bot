"""Fetch + cache raw per-symbol features for the transformer IC experiment.

WHAT THIS DOES
  Downloads 4 years of real 15-minute crypto bars (BTC/ETH/SOL) from Alpaca's
  credential-free crypto endpoint and computes the SHIPPED 11-feature set with
  the REAL `feature_engineering.add_features` (imported, not reimplemented).

  It caches the RAW per-symbol arrays — feature matrix, bar opens, and close
  timestamps — not pre-windowed samples. Sample windows and forward-return
  labels are then derived cheaply in `dataset.py`, so changing the label
  horizon never requires refetching.

  Cache: reports/transformer_ic/cache/raw.npz
    {SYM}_feats : (n, 11) float32   shipped features, causal, row i uses bars<=i
    {SYM}_open  : (n,)    float64   bar open price (next-open fill reference)
    {SYM}_close : (n,)    float64   bar close price
    {SYM}_ts    : (n,)    int64     bar close time, epoch seconds
  `n` differs per symbol (BTC/ETH have ~140k bars, SOL ~100k).

CAUSALITY
  add_features uses trailing windows only. This script VERIFIES that by
  recomputing add_features on truncated prefixes and asserting the last row is
  unchanged — a centered window or full-series normalisation would fail it.

Run:  PYTHONPATH=<repo> python reports/transformer_ic/build_dataset.py
"""

import os
import sys
import time
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

os.environ.setdefault("APCA_API_KEY_ID", "test")
os.environ.setdefault("APCA_API_SECRET_KEY", "test")

from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from feature_engineering import FEATURE_COLS, add_features

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
RAW = os.path.join(CACHE, "raw.npz")

SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD"]
DAYS = 365 * 4
BAR_MINUTES = 15
CHUNK_DAYS = 180  # Alpaca caps one crypto-bars response; page backwards


def fetch_bars(client, symbol, days):
    """Fetch `days` of 15m bars for one symbol, paging backwards in chunks."""
    end = datetime.now(UTC) - timedelta(days=1)
    start = end - timedelta(days=days)
    frames = []
    cursor = end
    while cursor > start:
        chunk_start = max(start, cursor - timedelta(days=CHUNK_DAYS))
        r = client.get_crypto_bars(CryptoBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(BAR_MINUTES, TimeFrameUnit.Minute),
            start=chunk_start,
            end=cursor,
        ))
        bars = r.data.get(symbol)
        if bars:
            frames.append(pd.DataFrame([{
                "timestamp": b.timestamp,
                "open": float(b.open),
                "high": float(b.high),
                "low": float(b.low),
                "close": float(b.close),
                "volume": float(b.volume),
                "trade_count": float(getattr(b, "trade_count", 0) or 0),
                "vwap": float(getattr(b, "vwap", 0) or 0),
            } for b in bars]))
        cursor = chunk_start
    if not frames:
        raise RuntimeError(f"no bars returned for {symbol}")
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp")
    df = df[df["close"] > 0].reset_index(drop=True)
    # Alpaca returns datetime64[us, UTC]; normalise to ns before any int cast
    # (a bare .astype("int64") on a [us] column silently yields microseconds).
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).astype("datetime64[ns, UTC]")
    return df


def verify_causality(df, sample_ix):
    """Count feature rows that depend on the future (must be 0)."""
    full = add_features(df)[FEATURE_COLS].to_numpy(dtype=np.float64)
    bad = 0
    for i in sample_ix:
        prefix = add_features(df.iloc[:i + 1])[FEATURE_COLS].to_numpy(dtype=np.float64)
        if not np.allclose(prefix[-1], full[i], atol=1e-9, equal_nan=True):
            bad += 1
    return bad


def main():
    os.makedirs(CACHE, exist_ok=True)
    client = CryptoHistoricalDataClient()
    out = {}
    for symbol in SYMBOLS:
        t0 = time.time()
        df = fetch_bars(client, symbol, DAYS)
        t1 = time.time()
        feats = add_features(df)[FEATURE_COLS].to_numpy(dtype=np.float32)
        ts = (df["timestamp"].astype("int64").to_numpy() // 10**9).astype(np.int64)

        probe = np.linspace(32, min(5000, len(df) - 1), 12).astype(int)
        bad = verify_causality(df.iloc[:6000].reset_index(drop=True), probe)
        assert bad == 0, f"{symbol}: {bad}/12 feature rows are non-causal!"

        key = symbol.replace("/", "")
        out[f"{key}_feats"] = feats
        out[f"{key}_open"] = df["open"].to_numpy(dtype=np.float64)
        out[f"{key}_close"] = df["close"].to_numpy(dtype=np.float64)
        out[f"{key}_ts"] = ts
        print(f"{symbol}: {len(df)} bars | fetch {t1-t0:.1f}s features "
              f"{time.time()-t1:.1f}s | causality OK ({len(probe)} probes)", flush=True)

    out["symbols"] = np.array(SYMBOLS)
    np.savez_compressed(RAW, **out)
    print(f"\nSaved {RAW} ({os.path.getsize(RAW)/1e6:.1f} MB)")


if __name__ == "__main__":
    main()
