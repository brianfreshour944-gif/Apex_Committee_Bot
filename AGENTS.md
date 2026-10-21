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

## Fixed 2026-10-20 (sentinel loss pause was a one-way latch — "symbol dies forever")
The per-symbol consecutive-loss pause was unrecoverable BY CONSTRUCTION.
`register_win()` was the ONLY code that reset a streak, and it runs solely on
the EXIT path (main.py exit block). Once a symbol hit `MAX_CONSECUTIVE_LOSSES`
(default 4), `sentinel.check()` returned a hard veto that blocks every new
entry — so the symbol could never be held again, could never exit again, and
`register_win()` could never fire. The counter is not persisted by
`save_state()`, so only a PROCESS RESTART cleared it: for a continuously
running bot, effectively permanent. Symptom: the symbol silently stops trading
forever, which is indistinguishable from "no signal".
Fix: the pause is now TIME-LIMITED via `sentinel_loss_cooldown_seconds`
(config, default 14400 = 4h). `register_loss()` stamps `_paused_until[symbol]`
when the limit is hit; `check()` calls `_losses()`, which expires a timed-out
pause (resets the streak, clears the marker) before evaluating. A symbol that
is still genuinely broken simply re-trips the pause on its next losing streak;
one caught in a bad patch recovers. `register_win()` still clears both.
Also made the skip VISIBLE: `position_sizing.py` logged the below-MIN_ORDER_USD
case at `debug` (never printed — config level is INFO) and main.py's
`if trade_value <= 0: continue` logged nothing at all. Both now log a WARNING,
so a BUY signal that never reaches an order is always traceable.
Regression: `tests/test_sentinel_loss_pause.py` (18 checks, incl. a direct
reproduction of the pre-fix latch; uses a controllable fake clock so
`register_loss` and `check` agree on time). All 10 suites exit 0.
NOTE: at the live $100 paper equity the symbol can still be capped to $0 by
MIN_ORDER_USD=$10 (see the small-equity note below) — that is a config/account
size issue, NOT this bug, and is now logged rather than silent.

## Small-account fix (2026-10-20) — the bot could not trade at $100 at all
Found while backtesting. `MIN_ORDER_USD` defaulted to an invented **$10** — NOT
an Alpaca rule. Alpaca's crypto floor is a **$1 notional minimum** for buys
(`cost basis must be >= minimal amount of order 1`, code 40310000). At $100
equity no sizing tier could reach $10 (10% tier = $10 exactly, but the measured
committee BUY-confidence ceiling is BTC 0.688 / ETH 0.579 / SOL 0.736, all below
the 0.75 tier), so EVERY entry sized to $0 and the bot took **zero trades in 30
days while logging nothing wrong**. Three fixes:
1. `config.min_order_usd` 10.0 -> **1.0** (the real broker floor).
2. `position_sizing.calculate_trade_size`: when a sentinel cap (0.5/0.6) is what
   pushes an otherwise-affordable trade below the floor, trade the MINIMUM
   instead of 0. A risk-REDUCTION mechanism must not silently become
   "no position". A genuinely unaffordable tier still returns 0.
3. `sentinel.py` buying-power gate read a hardcoded `10.0`; now reads
   `MIN_ORDER_USD` from config.
Also corrected `config.fee_rate` 0.001 -> **0.0025**: the bot's limit orders
cross the spread so BOTH legs are Alpaca Tier-1 TAKERS (0.25%), not 0.10%. This
is behavioural, not cosmetic — the fee feeds `realized_pnl`, which decides
`register_loss()` vs `register_win()`, so the old rate counted sub-breakeven
trades as wins. Regression: `tests/test_small_account_sizing.py` (17 checks,
fails on pre-fix code).
RESULT: $100 account 0 -> 22 trades. But net is still NEGATIVE, so this fixes
viability, NOT profitability — do not read it as an edge.

## Backtest harness (2026-10-20) — reports/backtest_entry_exit.py
First backtest infrastructure in this repo (audits previously flagged its
absence). Replays the REAL modules — 3 brains, committee, sentinel, sizing, and
the full exit ladder — bar-by-bar over real Alpaca 15m crypto bars, with NO
LOOK-AHEAD (bar i sees only bars[0..i]; decisions fill at bar i+1's OPEN).
Crypto bars need NO credentials (CryptoHistoricalDataClient works unauthenticated).
Key facts when extending it:
- Momentum brain is STATEFUL (`_prev_regime` transition memory) — a harness that
  rebuilds brains per bar would silently lose the DUMP->ACCUMULATION BUY signal.
  The harness reuses module-level singletons, matching live.
- 34 trades in 30d at $100k equity: net -$172.46 on $152.32 of fees. Gross is
  ~-$20 on ~$152k traded notional (~0.0%), so FEES ARE 88% OF THE LOSS. The
  strategy is fee-bound, not signal-bound. Do not read the small % as edge.
- 19 of 34 exits were `Max hold 8.0h`; ZERO take-profits fired in 30 days. The
  +6% target is rarely reachable in an 8h window, so the cost side is certain
  and the profit side is mostly unreachable.
- At the LIVE $100 equity the harness takes **0 trades in 30 days**: a BUY needs
  conf >= 0.75 to clear MIN_ORDER_USD=$10 (10% tier x $100), but the 30d BUY
  confidence ceiling is BTC 0.688 / ETH 0.579 / SOL 0.736. This is the real
  "symbol stays at 0" cause at small equity — config, not signal. Needs either a
  larger balance or a deliberate MIN_ORDER_USD change.
- LIMITS: symbols replayed INDEPENDENTLY (max-positions / exposure cap are
  per-symbol, not portfolio-wide); orderbook gate DISABLED (no historical L2, so
  entries are slightly MORE permissive than live); single window, no walk-forward.
See reports/BACKTEST_ENTRY_EXIT.md for the full write-up.

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