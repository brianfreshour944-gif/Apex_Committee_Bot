import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timezone

from data_feed import compute_indicators

def test_compute_indicators():
    # Create dummy dataframe with random walk
    np.random.seed(42)
    dates = pd.date_range(start="2026-07-01", periods=100, freq="15min")
    close_prices = 60000 + np.random.randn(100).cumsum() * 100
    df = pd.DataFrame({
        "open": close_prices - 10,
        "high": close_prices + 20,
        "low": close_prices - 20,
        "close": close_prices,
        "volume": np.random.rand(100) * 10,
        "vwap": close_prices
    }, index=dates)

    indicators = compute_indicators(df)

    # Check that required keys are present
    assert "price" in indicators
    assert "rsi" in indicators
    assert "macd" in indicators
    assert "bb_upper" in indicators
    assert "bb_mid" in indicators
    assert "bb_lower" in indicators
    assert "ema_fast" in indicators
    assert "ema_slow" in indicators
    assert "atr_pct" in indicators

    # Ensure no NaNs returned (since they should be filled or sliced)
    assert not np.isnan(indicators["rsi"])
    assert not np.isnan(indicators["atr_pct"])
    
    # Check bounds
    assert 0 <= indicators["rsi"] <= 100
    assert indicators["price"] > 0
