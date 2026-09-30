# AGENTS.md — Apex_Committee_Bot

## Environment
- Windows 11, Python 3.12 at `C:\Users\brian\AppData\Local\Programs\Python\Python312\python.exe`
- Run tests with `$env:PYTHONPATH='C:\Users\brian\Apex_Committee_Bot'; python tests\<test>.py`
- Tests print to stdout and use exit code 0/1; PowerShell stderr wrapping makes benign INFO logs look like errors — check `$LASTEXITCODE`, not the stderr noise.
- Avoid multi-line `python -c "..."` in this shell (PS continuation prompt hangs); write a temp .py file instead.
- Alpaca client initializes in test imports (paper=True); harmless.
- Repo has no pytest runner — tests are standalone scripts in `tests/`.

## Architecture (exit priority order, main.py ~line 390-410)
1. Stop loss: `stop_loss_pct` (4%) with time decay (4%→3% @1h→2% @2h) and ATR widening `min(atr_max_mult, atr_pct/threshold)`
2. Take profit: +6%
3. Trailing stop: 2% off `peak_price` — armed only once `peak_price > avg_entry` (do NOT gate on `pnl_pct > 0`, see bug below)
4. Max hold: 8h
5. Emergency: portfolio drawdown -10% liquidates all; buy cooldown 900s after crash exits

## Known bug fixed 2026-09-29 (trailing stop)
`main.py:401` gated the trailing stop on `pnl_pct > 0`, so a position that
rallied then dumped below entry deactivated its own trail and rode to the
hard stop. Live case: ETH trail $2683.78, price $2670.90 BELOW the trail,
logged HOLDING because PnL was -2.40%. Fix: arm the trail on
`peak_price > avg_entry` instead. Regression scenarios live in
`tests/test_stress_market_drops.py::test_trailing_stop_scenarios`
(fresh-never-green entry must NOT trail-exit; give-back below entry MUST).

`config.stop_loss_atr_max_mult` tightened 1.5 → 1.25 (worst stop -6% → -5%)
because the cap was reachable only when ATR% ≥ 4.5%, i.e. exactly during
flash crashes when widening is most harmful.

## Known bug fixed 2026-09-29 (symbol key mismatch — 7-day no-trade freeze)
Alpaca reports crypto positions slash-less (`BTCUSD`) while bot state used
`BTC/USD`. On restart reconciliation every lookup missed, so `entry_times`
drifted/re-added positions as fresh and the MAX_HOLD (8h) exit never fired —
BTC rode a position for 7+ days. Fix in `main.py` (`sync_state_with_alpaca()`)
+ `data_feed.py`: normalize every symbol with `normalize_symbol()` before any
state dict access; migration preserves the original entry time. Regression:
`tests/test_key_mismatch.py` (6 scenarios). All 5 suites exit 0 as of this fix.

## Test infra notes
- Tests import `config` → pydantic Settings REQUIRES `APCA_API_KEY_ID` /
  `APCA_API_SECRET_KEY` present (any placeholder works in tests; values are
  never used for network calls). Client init in imports is harmless.
- `test_committee.py` / `test_features.py` / `test_regime.py` now have
  `__main__` runner blocks (they were previously silent no-ops — exit 0
  without executing any assertions). Keep runners when editing these files.

## Gotchas
- `tests/test_stress_market_drops.py::print_summary()` is called per-suite; it assumes `drop_pct` may be None (trailing scenarios) — keep the None guard.
- Test-scenario labels must match actual price geometry (a "pullback to entry" scenario with price above its trail line correctly HOLDs).
- Bot version string: `2026-07-21-r2` / `Apex_Committee_v1`.