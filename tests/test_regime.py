"""Regression tests for regime.py's classify_regime() scoring.

Added after the fact: the macd_hist tautology bug (fixed in commit
c981274/beda27c, see regime.py lines ~51-55) shipped and went unnoticed
for a while because nothing exercised classify_regime() with known inputs
and asserted the expected regime + score breakdown. These tests hand-pick
indicator values that clearly favor one regime under the current scoring
formula (see comments per test for the point tally) so a similar silent
scoring regression gets caught automatically instead of by observation.
"""

import pandas as pd

from regime import classify_regime


def _df(closes):
    return pd.DataFrame({"close": closes})


def test_classify_regime_dump():
    # dump_score: price<ema_slow(+2) price<ema_fast(+1) rsi<35(+2)
    #             mom5<-3.0(+2) mom20<-8.0(+1) macd_hist<0(+1) price<bb_lower(+1) = 10
    # accum_score: price<ema_slow(+1) = 1  |  up_score = 0  |  dist_score = 0
    df = _df([100, 98, 95, 92, 90])
    indicators = {
        "rsi": 20, "ema_fast": 100, "ema_slow": 105,
        "momentum_5": -5.0, "momentum_20": -10.0, "vol_ratio": 1.0,
        "macd_hist": -1.0, "bb_upper": 110, "bb_mid": 100, "bb_lower": 95,
    }
    assert classify_regime(df, indicators) == "DUMP"


def test_classify_regime_accumulation():
    # accum_score: price_range_5<0.02(+2) 30<=rsi<=50(+2)
    #              vol_ratio>=1.5 and mom5>0(+2) price<ema_slow(+1) price>bb_lower(+1) = 8
    # dump_score = 3  |  up_score = 1  |  dist_score = 0
    df = _df([100, 100.5, 99.8, 100.2, 100])
    indicators = {
        "rsi": 40, "ema_fast": 101, "ema_slow": 102,
        "momentum_5": 0.5, "momentum_20": 2.0, "vol_ratio": 2.0,
        "macd_hist": 0.1, "bb_upper": 105, "bb_mid": 100, "bb_lower": 95,
    }
    assert classify_regime(df, indicators) == "ACCUMULATION"


def test_classify_regime_uptrend():
    # up_score: price>ema_fast>ema_slow(+3) 50<=rsi<=70(+2) mom5>1.0(+1)
    #           mom20>3.0(+1) macd_hist>0(+1) price>bb_mid(+1) = 9
    # dump_score = 0  |  accum_score = 1  |  dist_score = 1
    df = _df([100, 103, 106, 108, 110])
    indicators = {
        "rsi": 60, "ema_fast": 105, "ema_slow": 100,
        "momentum_5": 2.0, "momentum_20": 5.0, "vol_ratio": 1.0,
        "macd_hist": 0.5, "bb_upper": 115, "bb_mid": 105, "bb_lower": 95,
    }
    assert classify_regime(df, indicators) == "UPTREND"


def test_classify_regime_distribution():
    # dist_score: price>ema_slow(+1) rsi>65(+2) vol_ratio<0.8 and mom5<1.0(+2)
    #             mom20>10 and mom5<0(+2) price>bb_upper(+1)
    #             macd_hist<0 and price>ema_slow(+2) = 10
    # up_score = 5 (fast>slow crossover still intact, a real topping pattern) |
    # dump_score = 1  |  accum_score = 1
    df = _df([125, 123, 122, 121, 120])
    indicators = {
        "rsi": 72, "ema_fast": 115, "ema_slow": 110,
        "momentum_5": -0.5, "momentum_20": 12.0, "vol_ratio": 0.5,
        "macd_hist": -0.3, "bb_upper": 118, "bb_mid": 112, "bb_lower": 105,
    }
    assert classify_regime(df, indicators) == "DISTRIBUTION"


def test_classify_regime_defaults_to_dump_on_missing_indicator():
    """classify_regime() must fail SAFE (default to DUMP, the no-buy regime)
    rather than raising or silently returning a buy-biased regime when
    indicators are malformed/missing."""
    df = _df([100, 101, 102, 103, 104])
    assert classify_regime(df, {}) == "DUMP"


if __name__ == "__main__":
    test_classify_regime_dump()
    test_classify_regime_accumulation()
    test_classify_regime_uptrend()
    test_classify_regime_distribution()
    test_classify_regime_defaults_to_dump_on_missing_indicator()
    print("test_regime: all tests passed")
