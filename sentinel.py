# sentinel.py — The 4th Brain. Does NOT vote. Only vetoes or caps positions.
# If the sentinel says DANGER, the committee is overridden and no trade fires.

import math

from config import (
    logger,
    SENTINEL_MAX_ATR_PCT, SENTINEL_MAX_VOL_MULT,
    MAX_CONSECUTIVE_LOSSES,
)
from models import MarketSnapshot, CommitteeResult, SentinelReport


class Sentinel:
    """
    Monitors danger conditions and vetoes committee decisions.
    Danger conditions:
      1. ATR% spike     — extreme volatility (flash crashes, pumps)
      2. Volume anomaly — abnormal volume (manipulation risk)
      3. Consecutive losses — a specific symbol's strategy may have broken;
         pause THAT symbol to protect equity (tracked per-symbol, see below)
      4. Insufficient buying power — don't over-leverage
      5. ATR > 3% but not vetoing — caps position size to 50%

    FIXED: consecutive-loss tracking was previously a single counter shared
    across ALL symbols (self._consecutive_losses = 0). With multiple
    concurrent positions across unrelated symbols, that meant e.g. one loss
    each on 3 different, unrelated symbols within a normal-variance stretch
    would trigger a full portfolio-wide trading pause -- even though no
    single symbol/setup showed genuine evidence of being broken. Conversely,
    a real losing streak concentrated on ONE symbol could be masked by an
    interleaved win on a different symbol resetting the shared counter.
    Now tracked per-symbol (dict), so the pause targets the specific symbol
    that's actually underperforming, not the whole portfolio.
    """

    def __init__(self):
        self._consecutive_losses: dict[str, int] = {}

    def register_loss(self, symbol: str):
        self._consecutive_losses[symbol] = self._consecutive_losses.get(symbol, 0) + 1
        logger.warning(
            f"🛑 Sentinel: {symbol} consecutive losses = {self._consecutive_losses[symbol]}"
        )

    def register_win(self, symbol: str):
        self._consecutive_losses[symbol] = 0

    def check(
        self,
        snapshot: MarketSnapshot,
        committee: CommitteeResult,
    ) -> SentinelReport:

        symbol     = snapshot.symbol
        losses     = self._consecutive_losses.get(symbol, 0)
        atr_pct    = snapshot.indicators["atr_pct"]
        vol_ratio  = snapshot.indicators["vol_ratio"]
        equity     = snapshot.equity
        bp         = snapshot.buying_power

        # ── Hard vetoes ────────────────────────────────────────────────────────

        if not math.isfinite(atr_pct) or atr_pct > SENTINEL_MAX_ATR_PCT:
            return SentinelReport(
                veto=True,
                reason=f"🌊 ATR spike {atr_pct:.2f}% > limit {SENTINEL_MAX_ATR_PCT}% — too volatile",
            )

        if not math.isfinite(vol_ratio) or vol_ratio > SENTINEL_MAX_VOL_MULT:
            return SentinelReport(
                veto=True,
                reason=f"📊 Volume anomaly {vol_ratio:.1f}x avg — possible manipulation",
            )

        if losses >= MAX_CONSECUTIVE_LOSSES:
            return SentinelReport(
                veto=True,
                reason=f"🔴 {symbol}: {losses} consecutive losses — pausing this symbol",
            )

        if committee.action == "BUY" and bp < 10.0:
            return SentinelReport(
                veto=True,
                reason=f"💸 Buying power ${bp:.2f} insufficient for minimum order",
            )

        # ── Soft warnings: allow trade but cap size ─────────────────────────────
        if atr_pct > 3.0:
            logger.warning(f"⚠️ Sentinel: elevated ATR {atr_pct:.2f}% — capping position at 50%")
            return SentinelReport(
                veto=False,
                reason=f"Elevated volatility ATR={atr_pct:.2f}% — position capped",
                cap_pct=0.50,
            )

        if losses >= 2:
            logger.warning(f"⚠️ Sentinel: {symbol} {losses} losses — reducing size to 60%")
            return SentinelReport(
                veto=False,
                reason=f"{symbol}: {losses} losses — size reduced",
                cap_pct=0.60,
            )

        return SentinelReport(veto=False, reason="✅ All clear")


sentinel = Sentinel()
