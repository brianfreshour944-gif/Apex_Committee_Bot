# data_feed.py — OHLCV fetch + all indicator computation for the committee.

import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone

from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from config import logger, data_client, SEQUENCE_LEN
from models import MarketSnapshot


# ── OHLCV ─────────────────────────────────────────────────────────────────────

async def get_ohlcv(symbol: str, limit: int = 80) -> pd.DataFrame | None:
    """Fetch 15-minute bars. Returns finalized bars only (excludes current open bar)."""
    try:
        start_time = datetime.now(timezone.utc) - timedelta(days=5)
        req  = CryptoBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(15, TimeFrameUnit.Minute),
            limit=limit,
            start=start_time,
        )
        bars = data_client.get_crypto_bars(req).data.get(symbol, [])

        bar_duration = timedelta(minutes=15)
        now_utc      = datetime.now(timezone.utc)
        bars         = [b for b in bars if b.timestamp + bar_duration <= now_utc]

        if len(bars) < SEQUENCE_LEN:
            return None

        df = pd.DataFrame([{
            "timestamp":   b.timestamp,
            "open":        float(b.open   or 0),
            "high":        float(b.high   or 0),
            "low":         float(b.low    or 0),
            "close":       float(b.close  or 0),
            "volume":      float(b.volume or 0),
            "vwap":        float(b.vwap   or 0),
        } for b in bars])
        df.set_index("timestamp", inplace=True)
        df = df[df["close"] > 0]

        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)

        df["vwap"] = df["vwap"].where(df["vwap"] > 0, df["close"])
        return df

    except Exception as e:
        logger.error(f"OHLCV fetch failed for {symbol}: {e}")
        return None


# ── Indicators ────────────────────────────────────────────────────────────────

def _rsi(series: pd.Series, period: int = 14) -> float:
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    rsi   = 100 - (100 / (1 + rs))
    val   = rsi.iloc[-1]
    return float(val) if not np.isnan(val) else 50.0


def _macd(series: pd.Series) -> tuple[float, float]:
    """Returns (macd_line, signal_line)."""
    ema12  = series.ewm(span=12).mean()
    ema26  = series.ewm(span=26).mean()
    macd   = ema12 - ema26
    signal = macd.ewm(span=9).mean()
    return float(macd.iloc[-1]), float(signal.iloc[-1])


def _bollinger(series: pd.Series, period: int = 20) -> tuple[float, float, float]:
    """Returns (upper, mid, lower) bands."""
    mid   = series.rolling(period).mean()
    std   = series.rolling(period).std()
    upper = mid + 2 * std
    lower = mid - 2 * std
    return float(upper.iloc[-1]), float(mid.iloc[-1]), float(lower.iloc[-1])


def _atr_pct(df: pd.DataFrame, period: int = 14) -> float:
    high  = df["high"].astype(float)
    low   = df["low"].astype(float)
    close = df["close"].astype(float)
    prev  = close.shift(1)
    tr    = pd.concat([(high - low).abs(), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    atr   = tr.rolling(period).mean().iloc[-1]
    price = close.iloc[-1]
    return float((atr / price) * 100) if price > 0 else 0.0


def _volume_ratio(series: pd.Series, avg_period: int = 20) -> float:
    """Current volume vs N-bar average."""
    if len(series) < avg_period + 1:
        return 1.0
    avg = series.iloc[-(avg_period + 1):-1].mean()
    cur = series.iloc[-1]
    return float(cur / avg) if avg > 0 else 1.0


def _momentum_pct(close: pd.Series, lookback: int = 5) -> float:
    """% change over last N bars."""
    if len(close) < lookback + 1:
        return 0.0
    return float((close.iloc[-1] - close.iloc[-lookback - 1]) / close.iloc[-lookback - 1] * 100)


def compute_indicators(df: pd.DataFrame) -> dict:
    close = df["close"]
    rsi       = _rsi(close)
    macd, sig = _macd(close)
    bb_u, bb_m, bb_l = _bollinger(close)
    ema_fast  = float(close.ewm(span=9).mean().iloc[-1])
    ema_slow  = float(close.ewm(span=21).mean().iloc[-1])
    atr_pct   = _atr_pct(df)
    vol_ratio = _volume_ratio(df["volume"])
    mom5      = _momentum_pct(close, 5)
    mom20     = _momentum_pct(close, 20)
    price     = float(close.iloc[-1])

    return {
        "rsi":         rsi,
        "macd":        macd,
        "macd_signal": sig,
        "macd_hist":   macd - sig,
        "bb_upper":    bb_u,
        "bb_mid":      bb_m,
        "bb_lower":    bb_l,
        "ema_fast":    ema_fast,
        "ema_slow":    ema_slow,
        "atr_pct":     atr_pct,
        "vol_ratio":   vol_ratio,
        "momentum_5":  mom5,
        "momentum_20": mom20,
        "price":       price,
    }


# ── Account state ─────────────────────────────────────────────────────────────

def get_account_state() -> tuple[float, float]:
    """Returns (equity, buying_power)."""
    from config import trading_client
    try:
        acct = trading_client.get_account()
        return float(acct.equity), float(acct.buying_power)
    except Exception as e:
        logger.error(f"Account fetch failed: {e}")
        return 0.0, 0.0


def get_all_positions() -> dict:
    from config import trading_client
    try:
        return {
            p.symbol: {
                "qty":       float(p.qty),
                "avg_entry": float(p.avg_entry_price),
                "market_value": float(p.market_value),
            }
            for p in trading_client.get_all_positions()
        }
    except Exception as e:
        logger.error(f"Positions fetch failed: {e}")
        return {}


def get_orderbook_ratio(symbol: str) -> float | None:
    """
    Fetches real-time L2 orderbook and computes total Bid Size / total Ask Size.
    Safe fallbacks handle '.s' vs '.size' vs dictionary keys across API versions.
    """
    try:
        from alpaca.data.requests import CryptoOrderbookRequest
        req = CryptoOrderbookRequest(symbol_or_symbols=symbol)
        books = data_client.get_crypto_orderbook(req)
        book = books.get(symbol) if isinstance(books, dict) else getattr(books, "data", {}).get(symbol)
        
        if not book:
            return None

        bids = getattr(book, "bids", []) or []
        asks = getattr(book, "asks", []) or []

        def _get_size(entry):
            if hasattr(entry, "s"): return float(getattr(entry, "s") or 0)
            if hasattr(entry, "size"): return float(getattr(entry, "size") or 0)
            if isinstance(entry, dict): return float(entry.get("s") or entry.get("size") or 0)
            return 0.0

        bid_vol = sum(_get_size(b) for b in bids)
        ask_vol = sum(_get_size(a) for a in asks)

        if ask_vol <= 0:
            return 1.0
        return float(bid_vol / ask_vol)
    except Exception as e:
        logger.warning(f"Orderbook ratio fetch error for {symbol}: {e}")
        return None

