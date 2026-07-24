# sentinel.py — The 4th Brain. Does NOT vote. Only vetoes or caps positions.
# If the sentinel says DANGER, the committee is overridden and no trade fires.

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
      3. Consecutive losses — strategy may have broken; pause to protect equity
      4. Insufficient buying power — don't over-leverage
      5. ATR > 3% but not vetoing — caps position size to 50%
    """

    def __init__(self):
        self._consecutive_losses = 0

    def register_loss(self):
        self._consecutive_losses += 1
        logger.warning(f"🛑 Sentinel: consecutive losses = {self._consecutive_losses}")

    def register_win(self):
        self._consecutive_losses = 0

    def check(
        self,
        snapshot: MarketSnapshot,
        committee: CommitteeResult,
    ) -> SentinelReport:

        atr_pct    = snapshot.indicators["atr_pct"]
        vol_ratio  = snapshot.indicators["vol_ratio"]
        equity     = snapshot.equity
        bp         = snapshot.buying_power

        # ── Hard vetoes ────────────────────────────────────────────────────────

        if atr_pct > SENTINEL_MAX_ATR_PCT:
            return SentinelReport(
                veto=True,
                reason=f"🌊 ATR spike {atr_pct:.2f}% > limit {SENTINEL_MAX_ATR_PCT}% — too volatile",
            )

        if vol_ratio > SENTINEL_MAX_VOL_MULT:
            return SentinelReport(
                veto=True,
                reason=f"📊 Volume anomaly {vol_ratio:.1f}x avg — possible manipulation",
            )

        if self._consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            return SentinelReport(
                veto=True,
                reason=f"🔴 {self._consecutive_losses} consecutive losses — strategy pause",
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

        if self._consecutive_losses >= 2:
            logger.warning(f"⚠️ Sentinel: {self._consecutive_losses} losses — reducing size to 60%")
            return SentinelReport(
                veto=False,
                reason=f"{self._consecutive_losses} losses — size reduced",
                cap_pct=0.60,
            )

        return SentinelReport(veto=False, reason="✅ All clear")


sentinel = Sentinel()
