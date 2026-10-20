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

## Architecture (entry path, main.py:621-757, flat positions only)
1. Fail-closed: skip if the positions fetch failed this cycle
2. Cooldown (`cooldowns`, set on BUY and on EXIT)
3. Max positions (`max_open_positions`)
4. 3 brains → `run_committee` → `sentinel.check` (may veto / cap size)
5. `action != "BUY"` → skip
6. Orderbook "whale" gate (fail-closed — see bug below)
7. Sizing (`SIZING_TIERS`) → exposure cap (`max_total_exposure_pct`) → `place_order`

## Known bug fixed 2026-10-13 (entry logic)
Four entry-path defects found in review and fixed together; regression suite
`tests/test_entry_logic.py` (20 checks, fails on pre-fix code). All 7 suites exit 0 as of this fix.
1. **Falling-knife buy.** `committee.py` gave DUMP the SAME loosened 0.45
   threshold as ACCUMULATION, so a strong transformer + an oversold-RSI quant
   opened a long mid-dump (momentum's 20%-weight "not buying falling knife"
   SKIP cannot veto). Fix: DUMP uses the full `min_vote_score` (0.60), AND the
   sentinel hard-blocks BUY in `BLOCKED_ENTRY_REGIMES` (default DUMP). The
   sentinel block is ENTRY-only — SELL/exits in DUMP are unaffected.
2. **Whale gate failed OPEN.** `data_feed.get_orderbook_ratio` returned a
   passing `1.0` when the L2 book had no ask depth, and `None` on error; both
   PASSED in main.py, so the gate never blocked anything. Fix: empty/no-depth/
   error → `None`, and main.py vetoes on `None` when `orderbook_gate_fail_closed`
   (default True).
3. **No post-exit cooldown.** `clear_position_state()` removed state but never
   set `cooldowns` (only set on BUY), so a stop-out was immediately
   re-enterable → whipsaw. Fix: `clear_position_state()` arms
   `exit_cooldown_seconds` (default 300s), and `sync_state_with_alpaca()` no
   longer pops cooldowns so it survives a restart.
4. **momentum transition memory.** `_prev_regime` advanced on every `decide()`,
   including the exit-path committee call, so a DUMP→ACCUM transition could be
   consumed while still holding. Fix: advance only while flat.
Also documented (no behavior change): `CommitteeResult.confidence` is the
winning action's weighted score SUM, not a calibrated probability; it drives
sizing.
NOTE: the DUMP threshold change is symmetric, so a committee-driven SELL in
DUMP now also needs 0.60 (was 0.45). Acceptable — mechanical exits remain
primary and run before the committee SELL.

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

## Known bug fixed 2026-10-06 (phantom position — infinite EXIT loop)
Live 03:31–04:54 UTC: BTC/USD and ETH/USD each logged
`EXIT ... Max hold / Trailing stop` then
`Close ...: sellable qty=0 (dust or locked, qty_total=0.00000000, ...)` and
`Full close BTCUSD failed: APIError code 40310000 "order qty must be >=
minimal qty of order 0.000000002"` every 60s, forever; `Positions: 2/3` never
dropped. Root cause: the cycle's position fetch returned a truthy-but-stale
qty (Alpaca crypto qty is a `Decimal`; sub-dust residue like `1.4e-9` is
`> 0` yet prints as `0.00000000`), so main.py's `qty > 0` gate fired an EXIT.
The exit re-fetched, got qty 0/dust, floored the sellable qty to 0, fell back
to the full-position close API, and Alpaca rejected it. The failure returned
`None`, so main.py never cleared local state and retried forever.

Fix (dust is "flat" everywhere):
- `portfolio.is_effectively_flat(qty)` / `DUST_QTY = 1e-8` — qty 0 or sub-dust
  is treated as no position.
- `close_position()` returns `{"already_flat": True}` (no order placed) for a
  flat position instead of calling the doomed close API.
- `_close_position_full()` maps the `40310000` "minimal qty" error (and a
  zero-fill response) to `{"already_flat": True}` via
  `_is_nothing_to_close_error()`.
- `data_feed.get_all_positions()` omits flat positions so they look gone.
- `main.py` exit block clears local state on the `already_flat` sentinel via
  the new `clear_position_state()` helper (also reused by the normal exit
  path), breaking the loop.

Regression: `tests/test_phantom_exit_loop.py` (18 checks; fails on pre-fix
code). All 6 suites exit 0 as of this fix. NOTE: `close_position()` returning
a dict with `already_flat` is distinct from `None` (a real close failure) —
do not collapse the two.

## Known bug fixed 2026-10-20 (drawdown kill switch disarmed by failed fetch)
`main.py` armed the drawdown baseline with `if start_equity is None:
start_equity = equity`, but `get_account_state()` returns `0.0` on ANY failure
(auth/network) — indistinguishable from a genuinely empty account. So the FIRST
failed account fetch latched `start_equity = 0.0`, and the guard
`drawdown = ... if start_equity > 0 else 0.0` then reported drawdown 0.0
forever. `MAX_DRAWDOWN_STOP` (-10%) was permanently disarmed for the rest of
the process lifetime, INCLUDING after auth recovered. Observed live alongside
the `{"message": "unauthorized."}` outage (see below), which is exactly the
scenario that triggers it. Fix: arm the baseline only when `equity > 0`
(otherwise log a warning and leave it `None` to retry next cycle), and guard
the drawdown subtraction so an unarmed baseline yields `drawdown = 0.0`
instead of a `TypeError` (subtracting `None`). Baseline still arms once and is
not re-armed, so the session drawdown budget is unchanged. Regression:
`tests/test_drawdown_baseline.py` (9 checks; test 7 reproduces the pre-fix
disarm). All 8 suites exit 0 as of this fix.

## Added 2026-10-20 (alert on sustained account-fetch failure)
A revoked/bad key makes account + positions + orders all 401 while crypto market
data (which needs NO credentials) keeps flowing, so the bot fails closed on
every entry but the log still shows live prices/regimes. It previously sat there
silently (live: 25+ min of `{"message": "unauthorized."}`). `main.py` now calls
`track_account_fetch_outcome(equity > 0 and positions_ok)` each cycle: after
`account_failure_alert_threshold` (config, default 3) consecutive failures it
logs an ERROR and sends ONE Discord alert; the alert does not repeat while the
outage continues, and a successful fetch resets the streak and re-arms it.
Set the threshold to 0 to disable. The positions fetch is used as the stricter
signal because it gates ALL entries. Regression:
`tests/test_account_failure_alert.py` (14 checks).

## Diagnosed 2026-10-20 (credential outage signature — not a code bug)
`{"message": "unauthorized."}` (HTTP 401) on account/positions/order calls
while bars/indicators/regimes keep updating is a CREDENTIAL problem, not a data
or network outage. Reproduced with bogus keys on both paper and live endpoints
(rate limiting is 429, a different message). The asymmetry is by design:
`CryptoHistoricalDataClient()` (config.py) needs NO credentials and returns real
bars with zero env vars, while `trading_client` requires valid keys. Two causes:
(1) invalid/missing keys, or (2) a paper/live key-vs-`apca_api_paper` mismatch —
`config.py` never reads `APCA_API_BASE_URL` (it is only in `.env.example`), so
the endpoint is chosen solely by the flag. The bot does NOT stop on auth
failure; it fails closed on entries and prints `Equity: $0.00` indefinitely.

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