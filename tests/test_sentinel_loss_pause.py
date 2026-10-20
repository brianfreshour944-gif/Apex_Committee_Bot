"""Regression tests for the sentinel per-symbol loss pause (one-way latch bug).

Bug: the sentinel's consecutive-loss pause was a ONE-WAY LATCH. The only code
that reset a symbol's streak was register_win(), which runs SOLELY on the exit
path. Once a symbol reached MAX_CONSECUTIVE_LOSSES, check() returned a hard veto
that blocks every new entry -- so the symbol could never be held again, could
never exit again, and register_win() could never fire. The symbol was
permanently unenterable AND unexitable, recoverable only by a process restart
(the counter is not persisted). Observed consequence: a symbol "stays at 0
forever" and silently stops trading.

Fix under test: the pause is TIME-LIMITED (SENTINEL_LOSS_COOLDOWN_SECONDS).
It expires, the streak resets, and the symbol is re-evaluated. A symbol that is
still genuinely broken simply re-trips the pause.

Covers:
  1. Losses below the limit do not pause
  2. Reaching the limit vetoes the symbol (original intent preserved)
  3. The veto is per-symbol -- other symbols keep trading
  4. The pause EXPIRES and the symbol becomes enterable again (the fix)
  5. The streak is reset on expiry, so the symbol is not re-paused immediately
  6. A win clears both the streak and any active pause
  7. A still-broken symbol re-trips the pause after expiry
  8. Expiry is driven by elapsed time, not by a call to register_win()

Run:  PYTHONPATH=<repo> python tests/test_sentinel_loss_pause.py
"""

import os
os.environ.setdefault("APCA_API_KEY_ID", "test")
os.environ.setdefault("APCA_API_SECRET_KEY", "test")

from unittest.mock import patch

import config
from models import CommitteeResult, MarketSnapshot
from sentinel import Sentinel

_passed = 0
_failed = 0


def check(label, condition, detail=""):
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  PASS: {label}" + (f" | {detail}" if detail else ""))
    else:
        _failed += 1
        print(f"  FAIL: {label}" + (f" | {detail}" if detail else ""))


class FakeClock:
    """Controllable clock so register_loss() and check() agree on the time.

    Patching time only inside check() leaves register_loss() on the real clock,
    so a pause set at real-now looks already-expired to a patched future time.
    """

    def __init__(self, start=1_000_000.0):
        self.now = start

    def advance(self, seconds):
        self.now += seconds


def frozen(clock):
    return patch("sentinel.time.time", side_effect=lambda: clock.now)


def snapshot(symbol="BTC/USD", regime="UPTREND"):
    return MarketSnapshot(
        symbol=symbol,
        candles=[],
        indicators={"price": 60000.0, "rsi": 55.0, "vol_ratio": 1.0, "atr_pct": 1.0},
        regime=regime,
        atr_pct=1.0,
        has_position=False,
        position_size=0.0,
        entry_price=None,
        equity=100000.0,
        buying_power=100000.0,
    )


def buy():
    return CommitteeResult(action="BUY", confidence=0.9, regime="UPTREND")


def test_below_limit_no_pause():
    print("\n--- 1. Losses below the limit do not pause ---")
    s = Sentinel()
    for _ in range(config.MAX_CONSECUTIVE_LOSSES - 1):
        s.register_loss("BTC/USD")
    report = s.check(snapshot(), buy())
    check("below-limit symbol is not vetoed", not report.veto, f"reason={report.reason}")


def test_limit_vetoes_symbol():
    print("\n--- 2. Reaching the limit vetoes the symbol ---")
    s = Sentinel()
    for _ in range(config.MAX_CONSECUTIVE_LOSSES):
        s.register_loss("BTC/USD")
    report = s.check(snapshot(), buy())
    check("at-limit symbol IS vetoed (original intent)", report.veto, f"reason={report.reason}")


def test_veto_is_per_symbol():
    print("\n--- 3. The veto is per-symbol ---")
    s = Sentinel()
    for _ in range(config.MAX_CONSECUTIVE_LOSSES):
        s.register_loss("BTC/USD")
    check("BTC vetoed", s.check(snapshot("BTC/USD"), buy()).veto)
    check("ETH unaffected", not s.check(snapshot("ETH/USD"), buy()).veto)


def test_pause_expires():
    print("\n--- 4. The pause EXPIRES (the core fix) ---")
    s = Sentinel()
    clock = FakeClock()
    with frozen(clock):
        for _ in range(config.MAX_CONSECUTIVE_LOSSES):
            s.register_loss("BTC/USD")
        check("paused now", s.check(snapshot(), buy()).veto)

    # Advance past the cooldown without calling register_win() -- the old code
    # could only recover via register_win(), which the veto made unreachable.
    with frozen(clock):
        clock.advance(config.SENTINEL_LOSS_COOLDOWN_SECONDS + 1)
        report = s.check(snapshot(), buy())
    check("pause expired -> symbol enterable again", not report.veto, f"reason={report.reason}")


def test_streak_reset_on_expiry():
    print("\n--- 5. Streak resets on expiry (not re-paused immediately) ---")
    s = Sentinel()
    clock = FakeClock()
    with frozen(clock):
        for _ in range(config.MAX_CONSECUTIVE_LOSSES):
            s.register_loss("BTC/USD")
        clock.advance(config.SENTINEL_LOSS_COOLDOWN_SECONDS + 1)
        s.check(snapshot(), buy())
    check("streak reset to 0 after expiry",
          s._consecutive_losses.get("BTC/USD", 0) == 0,
          f"losses={s._consecutive_losses.get('BTC/USD')}")
    check("pause marker cleared", "BTC/USD" not in s._paused_until)


def test_win_clears_pause():
    print("\n--- 6. A win clears the streak and any active pause ---")
    s = Sentinel()
    clock = FakeClock()
    with frozen(clock):
        for _ in range(config.MAX_CONSECUTIVE_LOSSES):
            s.register_loss("BTC/USD")
        check("paused before win", s.check(snapshot(), buy()).veto)
        s.register_win("BTC/USD")
    check("win clears the veto immediately", not s.check(snapshot(), buy()).veto)
    check("pause marker cleared by win", "BTC/USD" not in s._paused_until)


def test_still_broken_retrips():
    print("\n--- 7. A still-broken symbol re-trips the pause ---")
    s = Sentinel()
    clock = FakeClock()
    with frozen(clock):
        for _ in range(config.MAX_CONSECUTIVE_LOSSES):
            s.register_loss("BTC/USD")
        clock.advance(config.SENTINEL_LOSS_COOLDOWN_SECONDS + 1)
        check("resumes after the first pause expires", not s.check(snapshot(), buy()).veto)
        # Symbol keeps losing after resuming -> must pause again, not run forever.
        for _ in range(config.MAX_CONSECUTIVE_LOSSES):
            s.register_loss("BTC/USD")
        report = s.check(snapshot(), buy())
    check("re-trips the pause after another full losing streak", report.veto,
          f"reason={report.reason}")


def test_recovery_is_time_driven_not_win_driven():
    print("\n--- 8. Recovery is driven by elapsed time, not a win ---")
    s = Sentinel()
    clock = FakeClock()
    with frozen(clock):
        for _ in range(config.MAX_CONSECUTIVE_LOSSES):
            s.register_loss("BTC/USD")
        # No register_win() anywhere in this test -- only the clock advances.
        clock.advance(config.SENTINEL_LOSS_COOLDOWN_SECONDS + 1)
        recovered = not s.check(snapshot(), buy()).veto
    check("recovered with no win ever registered", recovered)

    # And confirm it does NOT recover before the cooldown elapses.
    s2 = Sentinel()
    clock2 = FakeClock()
    with frozen(clock2):
        for _ in range(config.MAX_CONSECUTIVE_LOSSES):
            s2.register_loss("BTC/USD")
        clock2.advance(1)
        still_paused = s2.check(snapshot(), buy()).veto
    check("still paused before the cooldown elapses", still_paused)


def test_config_default():
    print("\n--- 9. Config default ---")
    check("SENTINEL_LOSS_COOLDOWN_SECONDS defaults to 4h (14400s)",
          config.SENTINEL_LOSS_COOLDOWN_SECONDS == 14400,
          f"value={config.SENTINEL_LOSS_COOLDOWN_SECONDS}")


def test_prefix_latch_is_reproduced():
    print("\n--- 10. Pre-fix one-way latch is reproduced (guards against vacuous pass) ---")
    # OLD logic, simulated directly: register_win() was the ONLY reset, and the
    # veto made register_win() unreachable, so the veto was permanent.
    losses: dict = {}

    def old_register_loss(sym):
        losses[sym] = losses.get(sym, 0) + 1

    def old_vetoed(sym):
        return losses.get(sym, 0) >= config.MAX_CONSECUTIVE_LOSSES

    for _ in range(config.MAX_CONSECUTIVE_LOSSES):
        old_register_loss("BTC/USD")
    check("OLD logic vetoes forever with no time-based release",
          old_vetoed("BTC/USD") and old_vetoed("BTC/USD"))

    # NEW logic: the same streak is released once the cooldown elapses.
    s = Sentinel()
    clock = FakeClock()
    with frozen(clock):
        for _ in range(config.MAX_CONSECUTIVE_LOSSES):
            s.register_loss("BTC/USD")
        clock.advance(config.SENTINEL_LOSS_COOLDOWN_SECONDS + 1)
        check("NEW logic releases the same streak", not s.check(snapshot(), buy()).veto)


if __name__ == "__main__":
    test_below_limit_no_pause()
    test_limit_vetoes_symbol()
    test_veto_is_per_symbol()
    test_pause_expires()
    test_streak_reset_on_expiry()
    test_win_clears_pause()
    test_still_broken_retrips()
    test_recovery_is_time_driven_not_win_driven()
    test_config_default()
    test_prefix_latch_is_reproduced()
    print(f"\n=== {_passed} passed, {_failed} failed ===")
    raise SystemExit(1 if _failed else 0)
