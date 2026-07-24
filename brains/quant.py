# brains/quant.py — Brain 2 (30% weight): Pure technical analysis brain.
# RSI + MACD + Bollinger Bands + EMA crossover. Regime-aware thresholds.

from models import MarketSnapshot, AIDecision
from config import logger


class QuantBrain:
    """
    Votes by counting how many TA indicators point the same direction.
    confidence = (agreeing indicators) / (total indicators checked)
    """

    def decide(self, snapshot: MarketSnapshot) -> AIDecision:
        ind    = snapshot.indicators
        regime = snapshot.regime

        rsi       = ind["rsi"]
        macd_hist = ind["macd_hist"]
        ema_fast  = ind["ema_fast"]
        ema_slow  = ind["ema_slow"]
        price     = ind["price"]
        bb_upper  = ind["bb_upper"]
        bb_lower  = ind["bb_lower"]
        bb_mid    = ind["bb_mid"]
        mom5      = ind["momentum_5"]

        # Regime-adaptive RSI thresholds
        if regime == "DUMP":
            rsi_buy, rsi_sell = 30, 55
        elif regime == "ACCUMULATION":
            rsi_buy, rsi_sell = 35, 60
        elif regime == "UPTREND":
            rsi_buy, rsi_sell = 45, 72
        else:   # DISTRIBUTION
            rsi_buy, rsi_sell = 55, 68

        buy_signals  = []
        sell_signals = []
        hold_signals = []

        # 1. RSI
        if rsi < rsi_buy:
            buy_signals.append(f"RSI={rsi:.1f}<{rsi_buy}")
        elif rsi > rsi_sell:
            sell_signals.append(f"RSI={rsi:.1f}>{rsi_sell}")
        else:
            hold_signals.append("RSI neutral")

        # 2. MACD histogram direction
        if macd_hist > 0:
            buy_signals.append(f"MACD hist bullish ({macd_hist:.4f})")
        else:
            sell_signals.append(f"MACD hist bearish ({macd_hist:.4f})")

        # 3. EMA crossover
        if ema_fast > ema_slow:
            buy_signals.append("EMA fast>slow")
        else:
            sell_signals.append("EMA fast<slow")

        # 4. Bollinger position
        bb_range = bb_upper - bb_lower
        if bb_range > 0:
            bb_pos = (price - bb_lower) / bb_range   # 0=lower band, 1=upper band
            if bb_pos < 0.2:
                buy_signals.append(f"Near BB lower (pos={bb_pos:.2f})")
            elif bb_pos > 0.8:
                sell_signals.append(f"Near BB upper (pos={bb_pos:.2f})")
            else:
                hold_signals.append(f"BB mid zone (pos={bb_pos:.2f})")

        # 5. Short-term momentum
        if mom5 > 1.5:
            buy_signals.append(f"Momentum bullish ({mom5:.1f}%)")
        elif mom5 < -1.5:
            sell_signals.append(f"Momentum bearish ({mom5:.1f}%)")
        else:
            hold_signals.append("Momentum neutral")

        total = len(buy_signals) + len(sell_signals) + len(hold_signals)

        if len(buy_signals) > len(sell_signals) and len(buy_signals) >= 3:
            confidence = len(buy_signals) / total
            return AIDecision(
                brain="quant", action="BUY",
                confidence=round(confidence, 4),
                regime=regime,
                reason=" | ".join(buy_signals),
            )
        elif len(sell_signals) > len(buy_signals) and len(sell_signals) >= 3:
            confidence = len(sell_signals) / total
            return AIDecision(
                brain="quant", action="SELL",
                confidence=round(confidence, 4),
                regime=regime,
                reason=" | ".join(sell_signals),
            )
        else:
            return AIDecision(
                brain="quant", action="HOLD",
                confidence=0.5,
                regime=regime,
                reason="Mixed signals — holding",
            )


quant_brain = QuantBrain()
