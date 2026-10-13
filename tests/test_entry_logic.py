"""Regression tests for the entry-logic fixes.

Covers:
  1. DUMP no longer gets the loosened (0.45) committee threshold
  2. Sentinel hard-blocks NEW entries in a blocked regime (DUMP), but never
     blocks an exit (SELL)
  3. Orderbook (whale) gate fails CLOSED when the book is unavailable
  4. clear_position_state arms a post-exit cooldown
  5. momentum._prev_regime does not advance while holding a position
  6. CommitteeResult.confidence is the weighted score SUM (sizing input)

Run:  PYTHONPATH=<repo> python tests/test_entry_logic.py
"""

import os
os.environ.setdefault("APCA_API_KEY_ID", "test")
os.environ.setdefault("APCA_API_SECRET_KEY", "test")

import asyncio
import time
from unittest.mock import AsyncMock, patch

import config
import data_feed
import main
from brains.momentum import MomentumBrain
from committee import run_committee
from models import AIDecision, CommitteeResult, MarketSnapshot
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


def snapshot(regime, has_position=False):
    return MarketSnapshot(
        symbol="BTC/USD",
        candles=[],
        indicators={"price": 60000.0, "rsi": 40.0, "vol_ratio": 1.0, "atr_pct": 1.0},
        regime=regime,
        atr_pct=1.0,
        has_position=has_position,
        position_size=0.1 if has_position else 0.0,
        entry_price=60000.0 if has_position else None,
        equity=10000.0,
        buying_power=10000.0,
    )


def votes(regime, rows):
    return [AIDecision(brain=b, action=a, confidence=c, regime=regime, reason="test")
            for b, a, c in rows]


# ── 1. DUMP threshold no longer loosened ──────────────────────────────────────

def test_dump_not_loosened():
    print("\n--- 1. DUMP uses the full threshold (no falling-knife buy) ---")
    snap = snapshot("DUMP")
    # transformer-only buy that previously fired at the 0.45 DUMP threshold
    r = run_committee(snap, votes("DUMP", [
        ("transformer", "BUY", 0.95), ("quant", "HOLD", 0.50), ("momentum", "SKIP", 0.50),
    ]))
    check("single-brain DUMP buy (0.475) is now SKIP, not BUY", r.action == "SKIP",
          f"action={r.action} conf={r.confidence:.3f}")

    # a two-brain buy still clears the committee, but the sentinel must veto it
    r2 = run_committee(snap, votes("DUMP", [
        ("transformer", "BUY", 0.80), ("quant", "BUY", 0.70), ("momentum", "SKIP", 0.75),
    ]))
    check("two-brain DUMP buy clears committee at 0.61", r2.action == "BUY" and abs(r2.confidence - 0.61) < 1e-6,
          f"action={r2.action} conf={r2.confidence:.3f}")

    # ACCUMULATION still gets the loosened threshold
    r3 = run_committee(snapshot("ACCUMULATION"), votes("ACCUMULATION", [
        ("transformer", "BUY", 0.90), ("quant", "HOLD", 0.50), ("momentum", "SKIP", 0.50),
    ]))
    check("ACCUMULATION still loosened (single brain buys at 0.53)", r3.action == "BUY",
          f"action={r3.action} conf={r3.confidence:.3f}")


# ── 2. Sentinel hard block (entry only) ───────────────────────────────────────

def test_sentinel_blocked_regime():
    print("\n--- 2. Sentinel blocks BUY in DUMP, allows exits ---")
    s = Sentinel()
    buy = CommitteeResult(action="BUY", confidence=0.7, regime="DUMP")
    rep = s.check(snapshot("DUMP"), buy)
    check("BUY in DUMP is vetoed", rep.veto is True, f"reason={rep.reason}")

    sell = CommitteeResult(action="SELL", confidence=0.7, regime="DUMP")
    rep2 = s.check(snapshot("DUMP", has_position=True), sell)
    check("SELL in DUMP is NOT vetoed (exit still allowed)", rep2.veto is False,
          f"reason={rep2.reason}")

    rep3 = s.check(snapshot("ACCUMULATION"), CommitteeResult(action="BUY", confidence=0.7, regime="ACCUMULATION"))
    check("BUY in ACCUMULATION is NOT vetoed", rep3.veto is False, f"reason={rep3.reason}")

    check("BLOCKED_ENTRY_REGIMES contains DUMP", "DUMP" in config.BLOCKED_ENTRY_REGIMES,
          f"{sorted(config.BLOCKED_ENTRY_REGIMES)}")


# ── 3. Orderbook gate fails closed ────────────────────────────────────────────

class _FakeBook:
    def __init__(self, bids, asks):
        self.bids = bids
        self.asks = asks


class _FakeLevel:
    def __init__(self, size):
        self.s = str(size)


class _FakeDataClient:
    def __init__(self, books):
        self._books = books

    def get_crypto_latest_orderbook(self, req):
        return self._books


def test_orderbook_fail_closed():
    print("\n--- 3. Orderbook gate returns None (fail-closed) when unverifiable ---")
    symbol = "BTC/USD"

    # empty book -> None (was 1.0 = pass before the fix)
    data_feed.data_client = _FakeDataClient({})
    r = asyncio.run(data_feed.get_orderbook_ratio(symbol))
    check("empty book -> None (was a passing 1.0)", r is None, f"ratio={r}")

    # book present but no ask depth -> None
    data_feed.data_client = _FakeDataClient({symbol: _FakeBook([_FakeLevel(5.0)], [])})
    r2 = asyncio.run(data_feed.get_orderbook_ratio(symbol))
    check("no ask depth -> None", r2 is None, f"ratio={r2}")

    # fetch error -> None
    class _Boom:
        def get_crypto_latest_orderbook(self, req):
            raise RuntimeError("network down")
    data_feed.data_client = _Boom()
    r3 = asyncio.run(data_feed.get_orderbook_ratio(symbol))
    check("fetch error -> None", r3 is None, f"ratio={r3}")

    # a real book -> a real ratio
    data_feed.data_client = _FakeDataClient({symbol: _FakeBook([_FakeLevel(30.0)], [_FakeLevel(10.0)])})
    r4 = asyncio.run(data_feed.get_orderbook_ratio(symbol))
    check("valid book -> bid/ask ratio 3.0", r4 is not None and abs(r4 - 3.0) < 1e-9, f"ratio={r4}")

    # main.py must treat None as a veto when fail-closed is on
    check("ORDERBOOK_GATE_FAIL_CLOSED default is True", config.ORDERBOOK_GATE_FAIL_CLOSED is True)


# ── 4. Post-exit cooldown ─────────────────────────────────────────────────────

def test_exit_cooldown():
    print("\n--- 4. clear_position_state arms a post-exit cooldown ---")
    main.cooldowns.clear()
    main.entry_times["BTCUSD"] = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    main.entry_prices["BTCUSD"] = 85000.0
    main.peak_prices["BTCUSD"] = 86000.0
    main.entry_confidence["BTCUSD"] = 0.8
    main.entry_regime["BTCUSD"] = "ACCUMULATION"

    with patch.object(main, "save_state", new=AsyncMock()):
        asyncio.run(main.clear_position_state("BTCUSD"))

    cd = main.cooldowns.get("BTCUSD")
    check("cooldown set after exit", cd is not None and cd > time.time(),
          f"cooldown in {None if cd is None else round(cd - time.time())}s")
    check("cooldown length matches EXIT_COOLDOWN_SECONDS",
          cd is not None and abs((cd - time.time()) - config.EXIT_COOLDOWN_SECONDS) < 5,
          f"EXIT_COOLDOWN_SECONDS={config.EXIT_COOLDOWN_SECONDS}")
    check("position state still fully removed",
          not any(k in main.entry_times for k in ["BTCUSD"]))

    # The exit cooldown must SURVIVE startup reconciliation when the position is
    # gone (closed while the bot was down). It must also expire on its own.
    async def _reconcile():
        with patch.object(main, "get_all_positions", new=AsyncMock(return_value={})), \
             patch.object(main, "save_state", new=AsyncMock()):
            await main.sync_state_with_alpaca()

    asyncio.run(_reconcile())
    cd2 = main.cooldowns.get("BTCUSD")
    check("exit cooldown survives startup reconciliation (position gone)",
          cd2 is not None and cd2 > time.time(),
          f"cooldown in {None if cd2 is None else round(cd2 - time.time())}s")
    check("stale entry state removed by reconciliation", "BTCUSD" not in main.entry_times)


# ── 5. momentum transition memory not consumed while holding ──────────────────

def test_momentum_transition_survives_hold():
    print("\n--- 5. momentum DUMP->ACCUM transition survives a hold ---")
    mb = MomentumBrain()
    ind = {"price": 60000.0, "vol_ratio": 1.8, "momentum_5": 0.5,
           "momentum_20": -1.0, "rsi": 34.0, "atr_pct": 1.0}

    def snap(regime, has_pos):
        return MarketSnapshot(symbol="BTC/USD", candles=[], indicators=ind, regime=regime,
                              atr_pct=1.0, has_position=has_pos, position_size=0.1 if has_pos else 0.0,
                              entry_price=60000.0 if has_pos else None, equity=10000.0, buying_power=10000.0)

    # Flat in DUMP first (this is what records the DUMP as the previous regime).
    mb.decide(snap("DUMP", has_pos=False))
    # Holding through the turn to ACCUMULATION: the exit path calls decide() too,
    # and that call must NOT consume the transition.
    mb.decide(snap("ACCUMULATION", has_pos=True))
    # Now flat again in ACCUMULATION -> the DUMP->ACCUM transition must still fire.
    d = mb.decide(snap("ACCUMULATION", has_pos=False))
    check("DUMP->ACCUM transition detected after holding through the dump",
          d.action == "BUY" and "DUMP->ACCUM" in d.reason, f"action={d.action} reason={d.reason}")

    # while flat, the memory DOES advance (a second flat ACCUM call is no longer a transition)
    d2 = mb.decide(snap("ACCUMULATION", has_pos=False))
    check("memory advances while flat (repeat ACCUM is not a new transition)",
          "DUMP->ACCUM" not in d2.reason, f"reason={d2.reason}")


# ── 6. confidence is a weighted sum ───────────────────────────────────────────

def test_confidence_is_weighted_sum():
    print("\n--- 6. CommitteeResult.confidence is the weighted score sum ---")
    r = run_committee(snapshot("ACCUMULATION"), votes("ACCUMULATION", [
        ("transformer", "BUY", 0.90), ("quant", "HOLD", 0.50), ("momentum", "SKIP", 0.50),
    ]))
    # BUY = transformer 0.5*0.90 + ACCUMULATION bias 0.08 = 0.53
    check("confidence == 0.5*0.90 + 0.08 = 0.53", abs(r.confidence - 0.53) < 1e-6,
          f"conf={r.confidence}")


if __name__ == "__main__":
    test_dump_not_loosened()
    test_sentinel_blocked_regime()
    test_orderbook_fail_closed()
    test_exit_cooldown()
    test_momentum_transition_survives_hold()
    test_confidence_is_weighted_sum()
    print(f"\n=== {_passed} passed, {_failed} failed ===")
    raise SystemExit(1 if _failed else 0)
