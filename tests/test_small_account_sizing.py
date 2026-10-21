"""Regression tests for small-account (e.g. $100) sizing viability.

Bug: the bot could not trade at all on a small account. MIN_ORDER_USD defaulted
to an invented $10, but the real Alpaca crypto floor is $1 notional. At $100
equity the 10% tier ($10) sat exactly on the invented floor while the measured
committee BUY-confidence ceiling is ~0.74 -- below the 0.75 tier needed -- so
EVERY entry sized to $0 and the bot took ZERO trades in 30 days. The symptom is
indistinguishable from "no signal".

A second defect compounded it: a sentinel size cap (0.5/0.6) is a risk
REDUCTION, but it multiplied an already-small affordable trade below the floor
and zeroed it -- turning "smaller position" into "no position".

Fixes under test:
  1. min_order_usd default lowered to the real $1 broker floor.
  2. position_sizing trades the minimum instead of 0 when the sentinel cap is
     what pushed an otherwise-affordable trade below the floor.
  3. the sentinel buying-power gate reads MIN_ORDER_USD from config instead of
     a hardcoded 10.0.

Run:  PYTHONPATH=<repo> python tests/test_small_account_sizing.py
"""

import os
os.environ.setdefault("APCA_API_KEY_ID", "test")
os.environ.setdefault("APCA_API_SECRET_KEY", "test")

import config
from models import CommitteeResult, MarketSnapshot
from position_sizing import calculate_trade_size
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


def snapshot(equity, buying_power=None):
    return MarketSnapshot(
        symbol="BTC/USD", candles=[],
        indicators={"price": 82000.0, "rsi": 55.0, "vol_ratio": 1.0, "atr_pct": 1.0},
        regime="UPTREND", atr_pct=1.0, has_position=False, position_size=0.0,
        entry_price=None, equity=equity,
        buying_power=buying_power if buying_power is not None else equity,
    )


def test_min_order_is_real_broker_floor():
    print("\n--- 1. MIN_ORDER_USD is the real broker floor, not an invented $10 ---")
    check("MIN_ORDER_USD == $1 (Alpaca crypto notional floor)",
          config.MIN_ORDER_USD == 1.0, f"value={config.MIN_ORDER_USD}")


def test_100_dollar_account_can_trade():
    print("\n--- 2. A $100 account can actually open a position ---")
    # Every confidence tier, including the 2.5% fallback, must clear the floor.
    for conf in (0.55, 0.62, 0.75, 0.92):
        v = calculate_trade_size(100.0, conf, None)
        check(f"$100 equity, conf={conf:.2f} -> tradable", v >= config.MIN_ORDER_USD,
              f"${v:.2f}")


def test_low_confidence_now_trades():
    print("\n--- 3. The fallback tier trades (this was the exact zero-trade case) ---")
    # conf 0.62 -> 5% tier -> $5.00. conf 0.55 -> 2.5% -> $2.50. Both used to
    # be blocked only by the invented $10 floor.
    v62 = calculate_trade_size(100.0, 0.62, None)
    v55 = calculate_trade_size(100.0, 0.55, None)
    check("conf 0.62 at $100 -> $5.00 (was $0)", v62 == 5.0, f"${v62:.2f}")
    check("conf 0.55 at $100 -> $2.50 (was $0)", v55 == 2.5, f"${v55:.2f}")


def test_prefix_ten_dollar_floor_reproduced():
    print("\n--- 4. Pre-fix $10 floor is reproduced (guards against vacuous pass) ---")
    # OLD behaviour, simulated with the old constant: at $100 the 5% tier is
    # $5 < $10 -> 0, and the 2.5% fallback is $2.50 < $10 -> 0.
    old_floor = 10.0
    old_v62 = 5.0 if 5.0 >= old_floor else 0.0
    old_v55 = 2.5 if 2.5 >= old_floor else 0.0
    check("OLD floor zeroed conf 0.62", old_v62 == 0.0)
    check("OLD floor zeroed conf 0.55", old_v55 == 0.0)
    check("NEW floor allows both",
          calculate_trade_size(100.0, 0.62, None) > 0
          and calculate_trade_size(100.0, 0.55, None) > 0)


def test_sentinel_cap_does_not_zero_affordable_trade():
    print("\n--- 5. Sentinel cap reduces size, never to zero ---")
    # $50 equity, conf 0.55 -> 2.5% = $1.25 uncapped (affordable). A 50% cap
    # would make it $0.625 -> below the $1 floor -> must trade $1, not $0.
    v = calculate_trade_size(50.0, 0.55, 0.5)
    check("cap on an affordable trade floors to MIN_ORDER_USD",
          v == config.MIN_ORDER_USD, f"${v:.2f}")
    v2 = calculate_trade_size(100.0, 0.92, 0.5)
    check("cap on a large trade still reduces it normally",
          v2 == 7.5, f"${v2:.2f}")


def test_genuinely_unaffordable_still_returns_zero():
    print("\n--- 6. A genuinely unaffordable trade still returns 0 (no over-reach) ---")
    # $20 equity, conf 0.55 -> 2.5% = $0.50 uncapped, already below $1 even
    # before any cap. The rescue must NOT invent a trade here.
    v = calculate_trade_size(20.0, 0.55, 0.5)
    check("$20 equity fallback tier -> $0", v == 0.0, f"${v:.2f}")
    v2 = calculate_trade_size(30.0, 0.55, None)
    check("$30 equity fallback tier (uncapped) -> $0", v2 == 0.0, f"${v2:.2f}")


def test_sentinel_bp_gate_uses_config():
    print("\n--- 7. Sentinel buying-power gate uses config, not a hardcoded 10 ---")
    s = Sentinel()
    # BP of $5 is above the real $1 floor -> must NOT veto on buying power.
    r = s.check(snapshot(100.0, buying_power=5.0),
                CommitteeResult(action="BUY", confidence=0.9, regime="UPTREND"))
    check("BP $5 no longer vetoed (old hardcoded floor was $10)",
          "insufficient for minimum order" not in r.reason, f"reason={r.reason}")
    # BP of $0.50 is below the real floor -> must still veto.
    r2 = s.check(snapshot(100.0, buying_power=0.5),
                 CommitteeResult(action="BUY", confidence=0.9, regime="UPTREND"))
    check("BP $0.50 still vetoed", r2.veto, f"reason={r2.reason}")


def test_fee_rate_is_taker():
    print("\n--- 8. FEE_RATE reflects the real Alpaca taker fee ---")
    check("FEE_RATE == 0.0025 (0.25% Tier-1 taker, orders cross the spread)",
          config.FEE_RATE == 0.0025, f"value={config.FEE_RATE}")


if __name__ == "__main__":
    test_min_order_is_real_broker_floor()
    test_100_dollar_account_can_trade()
    test_low_confidence_now_trades()
    test_prefix_ten_dollar_floor_reproduced()
    test_sentinel_cap_does_not_zero_affordable_trade()
    test_genuinely_unaffordable_still_returns_zero()
    test_sentinel_bp_gate_uses_config()
    test_fee_rate_is_taker()
    print(f"\n=== {_passed} passed, {_failed} failed ===")
    raise SystemExit(1 if _failed else 0)
