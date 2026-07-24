# brains/momentum.py — Brain 3 (20% weight): Volume + Momentum + Regime Transition Brain.
# Specializes in detecting DUMP→ACCUMULATION (bottom) and UPTREND→DISTRIBUTION (top) transitions.

from models import MarketSnapshot, AIDecision
from config import logger


class MomentumBrain:
    """
    Focused on catching regime transitions:
    - DUMP → ACCUMULATION = strongest BUY signal
    - UPTREND → DISTRIBUTION = strongest SELL signal
    Uses volume, momentum, and candle structure.
    """

    def __init__(self):
        self._prev_regime: dict = {}   # symbol → last known regime

    def decide(self, snapshot: MarketSnapshot) -> AIDecision:
        ind    = snapshot.indicators
        regime = snapshot.regime
        symbol = snapshot.symbol
        price  = ind["price"]

        vol_ratio = ind["vol_ratio"]
        mom5      = ind["momentum_5"]
        mom20     = ind["momentum_20"]
        rsi       = ind["rsi"]
        atr_pct   = ind["atr_pct"]

        prev_regime = self._prev_regime.get(symbol, regime)
        self._prev_regime[symbol] = regime

        # ── Transition detection ────────────────────────────────────────────────

        # DUMP → ACCUMULATION: catching the bottom (highest conviction BUY)
        if prev_regime == "DUMP" and regime == "ACCUMULATION":
            confidence = min(0.95, 0.70 + vol_ratio * 0.05 + (35 - rsi) * 0.005)
            return AIDecision(
                brain="momentum", action="BUY",
                confidence=round(confidence, 4),
                regime=regime,
                reason=f"DUMP→ACCUM transition | vol={vol_ratio:.1f}x | RSI={rsi:.1f}",
            )

        # UPTREND → DISTRIBUTION: top signal (high conviction SELL)
        if prev_regime == "UPTREND" and regime == "DISTRIBUTION":
            confidence = min(0.92, 0.70 + (rsi - 65) * 0.01)
            return AIDecision(
                brain="momentum", action="SELL",
                confidence=round(confidence, 4),
                regime=regime,
                reason=f"UPTREND→DIST transition | RSI={rsi:.1f} | mom5={mom5:.1f}%",
            )

        # ── Regime-based actions ────────────────────────────────────────────────

        if regime == "ACCUMULATION":
            # Volume spike on green candles = institutional buying
            if vol_ratio >= 1.5 and mom5 > 0:
                conf = min(0.80, 0.55 + vol_ratio * 0.05)
                return AIDecision(
                    brain="momentum", action="BUY",
                    confidence=round(conf, 4),
                    regime=regime,
                    reason=f"Accumulation vol spike {vol_ratio:.1f}x | mom5={mom5:.1f}%",
                )

        if regime == "UPTREND":
            # Strong momentum with volume = trend continuation
            if mom5 > 1.0 and vol_ratio > 0.9:
                return AIDecision(
                    brain="momentum", action="HOLD",
                    confidence=0.70,
                    regime=regime,
                    reason=f"Uptrend intact | mom5={mom5:.1f}% | vol={vol_ratio:.1f}x",
                )

        if regime == "DUMP":
            # Active dump — do not buy
            return AIDecision(
                brain="momentum", action="SKIP",
                confidence=0.75,
                regime=regime,
                reason=f"Regime=DUMP — not buying falling knife | mom5={mom5:.1f}%",
            )

        if regime == "DISTRIBUTION":
            # Distribution top — exit or skip buys
            if snapshot.has_position:
                return AIDecision(
                    brain="momentum", action="SELL",
                    confidence=0.70,
                    regime=regime,
                    reason=f"Distribution detected | RSI={rsi:.1f} | vol drying up",
                )

        # Default: hold if already in a position, otherwise skip
        action = "HOLD" if snapshot.has_position else "SKIP"
        return AIDecision(
            brain="momentum", action=action,
            confidence=0.50,
            regime=regime,
            reason=f"No clear momentum signal | regime={regime}",
        )


momentum_brain = MomentumBrain()
