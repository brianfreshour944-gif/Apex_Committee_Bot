# sentinel.py — The 4th Brain. Does NOT vote. Only vetoes or caps positions.
# If the sentinel says DANGER, the committee is overridden and no trade fires.

import math
import time

from config import (
    logger,
    SENTINEL_MAX_ATR_PCT, SENTINEL_MAX_VOL_MULT,
    MAX_CONSECUTIVE_LOSSES, BLOCKED_ENTRY_REGIMES,
    SENTINEL_LOSS_COOLDOWN_SECONDS,
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
      5. Blocked entry regime — never open NEW exposure in DUMP (falling knife);
         ENTRY-only, exits are unaffected
      6. ATR > 3% but not vetoing — caps position size to 50%

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

    FIXED (this change): the per-symbol pause was a ONE-WAY LATCH. The only
    code that reset a streak was register_win(), which runs solely on the EXIT
    path -- so once a symbol hit MAX_CONSECUTIVE_LOSSES it was vetoed forever,
    and because the veto blocks every new entry the symbol could never exit
    again, so register_win() could never fire. The symbol was unrecoverable by
    construction (only a process restart cleared it, since the counter is not
    persisted). The pause is now TIME-LIMITED: it expires after
    SENTINEL_LOSS_COOLDOWN_SECONDS and the streak resets, so the symbol is
    re-evaluated on fresh market data. A symbol still genuinely broken will
    simply re-trip the pause; one caught in a bad patch recovers.
    """

    def __init__(self):
        self._consecutive_losses: dict[str, int] = {}
        self._paused_until: dict[str, float] = {}

    def register_loss(self, symbol: str):
        self._consecutive_losses[symbol] = self._consecutive_losses.get(symbol, 0) + 1
        if self._consecutive_losses[symbol] >= MAX_CONSECUTIVE_LOSSES:
            self._paused_until[symbol] = time.time() + SENTINEL_LOSS_COOLDOWN_SECONDS
            logger.warning(
                f"🛑 Sentinel: {symbol} hit {self._consecutive_losses[symbol]} consecutive "
                f"losses — pausing this symbol for {SENTINEL_LOSS_COOLDOWN_SECONDS / 3600:.1f}h"
            )
        else:
            logger.warning(
                f"🛑 Sentinel: {symbol} consecutive losses = {self._consecutive_losses[symbol]}"
            )

    def register_win(self, symbol: str):
        self._consecutive_losses[symbol] = 0
        self._paused_until.pop(symbol, None)

    def _losses(self, symbol: str) -> int:
        """Current consecutive losses, expiring a timed-out pause first."""
        paused_until = self._paused_until.get(symbol)
        if paused_until is not None and time.time() >= paused_until:
            logger.info(
                f"[REFRESH] Sentinel: {symbol} loss pause expired after "
                f"{SENTINEL_LOSS_COOLDOWN_SECONDS / 3600:.1f}h — resuming symbol"
            )
            self._paused_until.pop(symbol, None)
            self._consecutive_losses[symbol] = 0
        return self._consecutive_losses.get(symbol, 0)

    def check(
        self,
        snapshot: MarketSnapshot,
        committee: CommitteeResult,
    ) -> SentinelReport:

        symbol     = snapshot.symbol
        losses     = self._losses(symbol)
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
                reason=f"[CHART] Volume anomaly {vol_ratio:.1f}x avg — possible manipulation",
            )

        if losses >= MAX_CONSECUTIVE_LOSSES:
            return SentinelReport(
                veto=True,
                reason=f"[BULL] {symbol}: {losses} consecutive losses — pausing this symbol",
            )

        if committee.action == "BUY" and bp < 10.0:
            return SentinelReport(
                veto=True,
                reason=f"💸 Buying power ${bp:.2f} insufficient for minimum order",
            )

        # ── Falling-knife hard block (entry only) ──────────────────────────────
        # Even if the committee votes BUY, refuse NEW exposure in a regime we
        # have declared untradeable (default DUMP). ENTRY-only: a SELL/exit is
        # never blocked here, so a position opened before the dump can still be
        # closed by the exit logic. This is the last line of defense behind the
        # committee's threshold change.
        if committee.action == "BUY" and snapshot.regime.upper() in BLOCKED_ENTRY_REGIMES:
            return SentinelReport(
                veto=True,
                reason=f"🔪 Entry blocked in {snapshot.regime} regime — no buying into an active downtrend",
            )

        # ── Soft warnings: allow trade but cap size ─────────────────────────────
        if atr_pct > 3.0:
            logger.warning(f"[!]️ Sentinel: elevated ATR {atr_pct:.2f}% — capping position at 50%")
            return SentinelReport(
                veto=False,
                reason=f"Elevated volatility ATR={atr_pct:.2f}% — position capped",
                cap_pct=0.50,
            )

        if losses >= 2:
            logger.warning(f"[!]️ Sentinel: {symbol} {losses} losses — reducing size to 60%")
            return SentinelReport(
                veto=False,
                reason=f"{symbol}: {losses} losses — size reduced",
                cap_pct=0.60,
            )

        return SentinelReport(veto=False, reason="[OK] All clear")


sentinel = Sentinel()
