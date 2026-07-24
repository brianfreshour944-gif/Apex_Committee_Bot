# regime.py — Classifies market state into DUMP / ACCUMULATION / UPTREND / DISTRIBUTION.
# Used both independently and by the committee to provide regime context to each brain.

import pandas as pd
from config import logger


def classify_regime(df: pd.DataFrame, indicators: dict) -> str:
    """
    Returns one of: DUMP | ACCUMULATION | UPTREND | DISTRIBUTION

    DUMP         — strong downtrend, capitulation
    ACCUMULATION — price stabilizing after dump, smart money buying
    UPTREND      — sustained rise, trend confirmed
    DISTRIBUTION — price at highs but momentum fading, smart money selling
    """
    try:
        close     = df["close"].astype(float)
        price     = float(close.iloc[-1])
        rsi       = indicators["rsi"]
        ema_fast  = indicators["ema_fast"]
        ema_slow  = indicators["ema_slow"]
        mom5      = indicators["momentum_5"]
        mom20     = indicators["momentum_20"]
        vol_ratio = indicators["vol_ratio"]
        macd_hist = indicators["macd_hist"]
        bb_upper  = indicators["bb_upper"]
        bb_lower  = indicators["bb_lower"]
        bb_mid    = indicators["bb_mid"]

        # ── Score each regime ──────────────────────────────────────────────────

        # DUMP signals
        dump_score = 0
        if price < ema_slow:                dump_score += 2
        if price < ema_fast:                dump_score += 1
        if rsi < 35:                        dump_score += 2
        if mom5 < -3.0:                     dump_score += 2
        if mom20 < -8.0:                    dump_score += 1
        if macd_hist < 0:                   dump_score += 1
        if price < bb_lower:                dump_score += 1

        # ACCUMULATION signals
        accum_score = 0
        last_5_close = close.iloc[-5:]
        price_range_5 = (last_5_close.max() - last_5_close.min()) / last_5_close.mean()
        if price_range_5 < 0.02:           accum_score += 2  # price stabilizing
        if 30 <= rsi <= 50:                 accum_score += 2
        if vol_ratio >= 1.5 and mom5 > 0:   accum_score += 2  # vol spike + green
        if price < ema_slow:                accum_score += 1  # still below slow ema
        if macd_hist > macd_hist - 0.001:   accum_score += 1  # macd improving (rough)
        if price > bb_lower:                accum_score += 1  # bounced off lower band

        # UPTREND signals
        up_score = 0
        if price > ema_fast > ema_slow:    up_score += 3
        if 50 <= rsi <= 70:                up_score += 2
        if mom5 > 1.0:                     up_score += 1
        if mom20 > 3.0:                    up_score += 1
        if macd_hist > 0:                  up_score += 1
        if price > bb_mid:                 up_score += 1

        # DISTRIBUTION signals
        dist_score = 0
        if price > ema_slow:               dist_score += 1
        if rsi > 65:                       dist_score += 2
        if vol_ratio < 0.8 and mom5 < 1.0: dist_score += 2  # vol drying up at highs
        if mom20 > 10.0 and mom5 < 0:      dist_score += 2  # big run, now fading
        if price > bb_upper:               dist_score += 1
        if macd_hist < 0 and price > ema_slow: dist_score += 2

        scores = {
            "DUMP":         dump_score,
            "ACCUMULATION": accum_score,
            "UPTREND":      up_score,
            "DISTRIBUTION": dist_score,
        }

        regime = max(scores, key=scores.get)
        logger.debug(f"📊 Regime scores: {scores} → {regime}")
        return regime

    except Exception as exc:
        logger.error(f"Regime classification failed: {exc} — defaulting to DUMP")
        return "DUMP"
