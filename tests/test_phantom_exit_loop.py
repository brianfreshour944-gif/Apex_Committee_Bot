# tests/test_phantom_exit_loop.py — Regression test: infinite EXIT loop on a
# position that holds no sellable quantity.
#
# Observed live 2026-10-06 (03:31-04:54 UTC): every 60s cycle logged
#   EXIT BTC/USD: Max hold 98.0h | PnL -0.0%
#   Close BTC/USD: sellable qty=0 (dust or locked, qty_total=0.00000000, ...)
#   Full close BTCUSD failed: APIError: {"code":40310000,"message":
#       "order qty must be >= minimal qty of order 0.000000002"}
# for both BTC/USD and ETH/USD, forever. `len(entry_times)` stayed 2/3.
#
# Root cause: the cycle's position fetch returned a truthy-but-stale qty
# (Alpaca crypto qty is a Decimal; sub-dust residues like 1.4e-9 are > 0 but
# print as 0.00000000), so main.py's `qty > 0` gate fired an EXIT. The exit
# re-fetched and got qty 0/dust, floored the sellable qty to 0, fell back to
# the full-position close API, which Alpaca rejected (code 40310000). The
# failure returned None, main.py never cleared local state, and the loop
# repeated indefinitely.
#
# Fix under test:
#   1. portfolio.is_effectively_flat(): qty 0 / < 1e-8 is "flat".
#   2. close_position() returns {"already_flat": True} (no order placed) for
#      a flat position instead of calling the doomed close API.
#   3. _close_position_full() maps the 40310000 "minimal qty" error (and a
#      zero-fill response) to {"already_flat": True} instead of None.
#   4. get_all_positions() omits flat positions so they look gone.
#   5. main.py's exit block clears local state on the already_flat sentinel
#      (main.clear_position_state), breaking the loop.

import asyncio
import os
import sys
from decimal import Decimal

os.environ.setdefault("APCA_API_KEY_ID", "PAPER_TEST_KEY")
os.environ.setdefault("APCA_API_SECRET_KEY", "PAPER_TEST_SECRET")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import AsyncMock, patch  # noqa: E402

import config  # noqa: E402
import portfolio  # noqa: E402
import data_feed  # noqa: E402
import main  # noqa: E402
from alpaca.common.exceptions import APIError  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}: {name}" + (f" | {detail}" if detail else ""))


class FakePosition:
    """Mirrors the attributes portfolio.py/data_feed.py read off an Alpaca
    Position. qty is passed as a Decimal/str to reproduce the real type."""

    def __init__(self, symbol, qty, avg_entry=100.0, qty_available=None):
        self.symbol = symbol
        self.qty = Decimal(str(qty))
        self.avg_entry_price = Decimal(str(avg_entry))
        self.market_value = Decimal(str(float(qty) * avg_entry))
        if qty_available is not None:
            self.qty_available = Decimal(str(qty_available))


class FakeOrder:
    def __init__(self, order_id, filled_qty, filled_avg_price):
        self.id = order_id
        self.filled_qty = Decimal(str(filled_qty))
        self.filled_avg_price = Decimal(str(filled_avg_price))


class FakeTradingClient:
    def __init__(self, positions, close_position_result=None, close_position_error=None):
        self._positions = positions
        self._close_position_result = close_position_result
        self._close_position_error = close_position_error
        self.close_position_calls = 0
        self.submit_order_calls = 0

    def get_all_positions(self):
        return list(self._positions)

    def submit_order(self, order_data=None):
        self.submit_order_calls += 1
        return FakeOrder("sell-1", float(order_data.qty), 100.0)

    def get_order_by_id(self, order_id):
        return FakeOrder(order_id, 1.0, 100.0)

    def close_position(self, symbol_or_asset_id=None, close_options=None):
        self.close_position_calls += 1
        if self._close_position_error is not None:
            raise self._close_position_error
        return self._close_position_result


async def _noop_sleep(*_a, **_k):
    return None


async def test_flat_position_returns_sentinel_without_ordering():
    print("\n--- Scenario A: cycle fetch stale (qty>0) but re-fetch is 0 ---")
    fake = FakeTradingClient(positions=[FakePosition("BTCUSD", "0")])
    with patch.object(portfolio, "trading_client", fake):
        result = await portfolio.close_position("BTC/USD", {"qty": 0.005}, current_price=85000.0)

    check("close_position signals already_flat", bool(result and result.get("already_flat")), f"result={result}")
    check("no order submitted for a flat position", fake.submit_order_calls == 0)
    check("no close-position API call for a flat position", fake.close_position_calls == 0)
    check("sentinel reports zero qty closed", result and result.get("qty") == 0.0)


async def test_subdust_residue_is_flat():
    print("\n--- Scenario B: sub-dust residue (1.4e-9) ---")
    fake = FakeTradingClient(positions=[FakePosition("ETHUSD", "0.0000000014")])
    with patch.object(portfolio, "trading_client", fake):
        result = await portfolio.close_position("ETH/USD", None, current_price=2700.0)

    check("sub-dust qty treated as already_flat", bool(result and result.get("already_flat")), f"result={result}")
    check("no order submitted for sub-dust qty", fake.submit_order_calls == 0)
    check("no close-position API call for sub-dust qty", fake.close_position_calls == 0)


async def test_minimal_qty_error_maps_to_already_flat():
    print("\n--- Scenario C: full-close API raises 40310000 (exact live error) ---")
    api_error = APIError(
        '{"code":40310000,"message":"order qty must be >= minimal qty of order 0.000000002"}'
    )
    check("error classifier recognises the live message",
          portfolio._is_nothing_to_close_error(api_error))

    fake = FakeTradingClient(positions=[], close_position_error=api_error)
    with patch.object(portfolio, "trading_client", fake):
        result = await portfolio._close_position_full("BTCUSD", 0.0, 100.0, 85000.0)

    check("full-close error becomes already_flat (not None)",
          bool(result and result.get("already_flat")), f"result={result}")


async def test_locked_position_still_uses_full_close_fallback():
    print("\n--- Scenario D: real qty but qty_available=0 (locked) still closes ---")
    fake = FakeTradingClient(
        positions=[FakePosition("SOLUSD", "0.5", avg_entry=120.0, qty_available="0")],
        close_position_result=FakeOrder("close-1", 0.5, 120.0),
    )
    with patch.object(portfolio, "trading_client", fake), patch("asyncio.sleep", new=_noop_sleep):
        result = await portfolio.close_position("SOL/USD", None, current_price=120.0)

    check("locked position is NOT reported already_flat",
          bool(result) and not result.get("already_flat"), f"result={result}")
    check("locked position closed via full-position API", fake.close_position_calls == 1)
    check("locked close reports the filled qty", result and abs(result.get("qty", 0) - 0.5) < 1e-9)


async def test_normal_position_still_sells():
    print("\n--- Scenario E: normal position still places a SELL ---")
    fake = FakeTradingClient(positions=[FakePosition("BTCUSD", "0.005", avg_entry=80000.0)])
    with patch.object(portfolio, "trading_client", fake), patch("asyncio.sleep", new=_noop_sleep):
        result = await portfolio.close_position("BTC/USD", None, current_price=85000.0)

    check("normal position is not already_flat",
          bool(result) and not result.get("already_flat"), f"result={result}")
    check("normal position places exactly one SELL order", fake.submit_order_calls == 1)


async def test_get_all_positions_filters_flat():
    print("\n--- Scenario F: get_all_positions() omits flat positions ---")
    fake = FakeTradingClient(positions=[
        FakePosition("BTCUSD", "0"),           # phantom: must be omitted
        FakePosition("ETHUSD", "0.0000000014"),  # dust: must be omitted
        FakePosition("SOLUSD", "1.5", avg_entry=120.0),  # real: kept
    ])
    with patch.object(config, "trading_client", fake):
        positions = await data_feed.get_all_positions()

    check("flat positions omitted from get_all_positions",
          positions is not None and set(positions.keys()) == {"SOLUSD"}, f"keys={None if positions is None else sorted(positions)}")
    check("real position still reported", positions is not None and positions["SOLUSD"]["qty"] == 1.5)


async def test_main_cleanup_breaks_loop():
    print("\n--- Scenario G: main.clear_position_state clears the loop ---")
    main.entry_times.clear(); main.entry_prices.clear(); main.peak_prices.clear()
    main.entry_confidence.clear(); main.entry_regime.clear()
    main.entry_times["BTCUSD"] = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    main.entry_prices["BTCUSD"] = 85000.0
    main.peak_prices["BTCUSD"] = 86000.0
    main.entry_confidence["BTCUSD"] = 0.8
    main.entry_regime["BTCUSD"] = "ACCUMULATION"

    with patch.object(main, "save_state", new=AsyncMock()):
        await main.clear_position_state("BTCUSD")

    remaining = [
        "BTCUSD" in main.entry_times,
        "BTCUSD" in main.entry_prices,
        "BTCUSD" in main.peak_prices,
        "BTCUSD" in main.entry_confidence,
        "BTCUSD" in main.entry_regime,
    ]
    check("clear_position_state removes every state entry", not any(remaining), f"remaining={remaining}")
    check("position count gate drops to 0 (exit can no longer re-fire)",
          len(main.entry_times) == 0)


async def run_tests():
    await test_flat_position_returns_sentinel_without_ordering()
    await test_subdust_residue_is_flat()
    await test_minimal_qty_error_maps_to_already_flat()
    await test_locked_position_still_uses_full_close_fallback()
    await test_normal_position_still_sells()
    await test_get_all_positions_filters_flat()
    await test_main_cleanup_breaks_loop()


if __name__ == "__main__":
    print("=== PHANTOM EXIT LOOP REGRESSION TEST (unsellable dust position) ===")
    asyncio.run(run_tests())
    print(f"\n=== {len(PASS)} passed, {len(FAIL)} failed ===")
    sys.exit(1 if FAIL else 0)
