# tests/test_key_mismatch.py — Regression test: Alpaca position symbols are
# slash-less ("BTCUSD") while the bot's local state (entry_times, entry_prices,
# peak_prices, ...) is keyed by the config symbol ("BTC/USD"). After any bot
# restart, sync_state_with_alpaca() re-keys state by Alpaca's format while the
# exit logic still looks up the slash format -> entry_dt lookup misses ->
# held_h defaults to ~0 -> MAX_HOLD exit can NEVER fire -> zombie positions
# held forever (observed live: BTC/ETH held 7+ days at -2.5% with no exit).
#
# Fix under test:
#   1. get_all_positions() now returns keys normalized via normalize_symbol()
#      (slash-less) so they match main.py's local-state key format.
#   2. sync_state_with_alpaca() + per-cycle reconciliation MIGRATE legacy
#      "BTC/USD" keys instead of destroying them, PRESERVING entry time.
#
# Also verifies the exact held_h computation from main.py's exit block
# (entry_times.get(alpaca_sym, datetime.now(timezone.utc))).

import asyncio
import sys
import os
from datetime import datetime, timedelta, timezone

# config.py requires Alpaca credentials at import; dummy values are fine —
# the TradingClient(paper=True) constructor makes no network calls.
os.environ.setdefault("APCA_API_KEY_ID", "PAPER_TEST_KEY")
os.environ.setdefault("APCA_API_SECRET_KEY", "PAPER_TEST_SECRET")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402  (imports config -> Alpaca paper client; harmless per AGENTS.md)
from portfolio import normalize_symbol  # noqa: E402
from config import SYMBOLS  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}: {name}" + (f" | {detail}" if detail else ""))


async def run_tests():
    # ── Scenario: bot bought BTC pre-restart (state keyed "BTC/USD"),
    #    Alpaca reports positions WITHOUT the slash ("BTCUSD").
    #    ETH has no pre-restart state (fresh position example).
    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)

    main.entry_times.clear()
    main.entry_prices.clear()
    main.peak_prices.clear()
    main.entry_times["BTC/USD"] = seven_days_ago
    main.entry_prices["BTC/USD"] = 85491.14
    main.peak_prices["BTC/USD"] = 85491.14

    alpaca_positions = {  # exactly what Alpaca returns: no slash in p.symbol
        "BTCUSD": {"qty": 0.005, "avg_entry": 85491.14, "market_value": 416.44},
        "ETHUSD": {"qty": 0.15, "avg_entry": 2736.70, "market_value": 400.77},
    }

    async def fake_get_all_positions():
        return alpaca_positions

    original = main.get_all_positions
    main.get_all_positions = fake_get_all_positions
    try:
        await main.sync_state_with_alpaca()
    finally:
        main.get_all_positions = original

    print("\n--- After sync_state_with_alpaca() (restart reconciliation) ---")

    # Contract: state keys are canonical (slash-less), matching what
    # get_all_positions() now returns.
    check(
        "state keys are canonical (slash-less) after sync",
        "BTCUSD" in main.entry_times and "BTC/USD" not in main.entry_times,
        f"keys now: {sorted(main.entry_times)}",
    )

    # Contract: every held config symbol's lookup key finds restored state.
    for sym in ("BTC/USD", "ETH/USD"):
        key = normalize_symbol(sym)
        check(
            f"entry_times lookup via normalize_symbol('{sym}') finds state",
            key in main.entry_times,
            f"keys now: {sorted(main.entry_times)}",
        )

    # Contract: the original entry time is PRESERVED, so held_h reflects the
    # true hold duration (7 days -> max-hold exit fires next cycle).
    et = main.entry_times.get(normalize_symbol("BTC/USD"))
    preserved = et is not None and abs((et - seven_days_ago).total_seconds()) < 60
    check(
        "entry time preserved across key rename for BTC/USD (not reset to now)",
        preserved,
        f"entry_times['BTCUSD'] = {et}",
    )

    # The pre-fix failure mode, reproduced directly with the EXACT exit-block
    # computation: alpaca_sym = normalize_symbol(symbol), then
    # entry_times.get(alpaca_sym, datetime.now(timezone.utc)).
    print("\n--- Direct reproduction of the exit-logic lookup ---")
    symbol = "BTC/USD"  # what the per-symbol loop passes as symbol
    alpaca_sym = normalize_symbol(symbol)  # what the fixed exit block now uses
    entry_dt = main.entry_times.get(alpaca_sym, datetime.now(timezone.utc))
    held_h = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600
    check(
        "exit block computes real held_h (> 8h) for the held position",
        held_h > 8.0,
        f"held_h={held_h:.2f}h (a 7-day hold must exceed MAX_HOLD_HOURS=8)",
    )

    # Contract: fresh positions (no legacy state) still get state added.
    check(
        "fresh position (ETH) still added with entry state",
        "ETHUSD" in main.entry_times and "ETHUSD" in main.entry_prices,
        f"entry_prices['ETHUSD'] = {main.entry_prices.get('ETHUSD')}",
    )


if __name__ == "__main__":
    print("=== KEY MISMATCH REGRESSION TEST (max-hold exit vs Alpaca symbols) ===")
    asyncio.run(run_tests())
    print(f"\n=== {len(PASS)} passed, {len(FAIL)} failed ===")
    sys.exit(1 if FAIL else 0)