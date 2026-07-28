# Latency Scan Report — Apex_Committee_Bot

**Repo:** https://github.com/brianfreshour944-gif/Apex_Committee_Bot.git  
**Date:** 2026-07-27  
**Scanned by:** Cline  
**Entry point (per Dockerfile):** `python main.py`

---

## Executive Summary

The repo contains **two** versions of `main.py`:

| File | Status | Notes |
|------|--------|-------|
| `main.py` | **Active** (Dockerfile `CMD ["python", "main.py"]`) | Has multiple latency and correctness bugs |
| `main (1).py` | Partially-fixed backup | Fixes some issues but introduces a crash (calls `await` on sync functions) |

The active `main.py` has **13 distinct latency/correctness issues**. A partially-fixed `main (1).py` addresses 7 of them but introduces a new crash and leaves 4 latency issues unresolved.

---

## Issue Index

| # | Severity | Issue | File:Line | Fixed in `main (1).py`? |
|---|----------|-------|-----------|------------------------|
| 1 | 🔴 CRITICAL | Sequential OHLCV fetching (no `asyncio.gather`) | `main.py:167` | ✅ Yes |
| 2 | 🟠 HIGH | Blocking `get_account_state()` in async loop | `main.py:124` | ✅ Yes (but crashes — `data_feed.py` still sync) |
| 3 | 🟠 HIGH | Blocking `get_all_positions()` in async loop | `main.py:156` | ✅ Yes (but crashes — `data_feed.py` still sync) |
| 4 | 🟡 MEDIUM | Blocking `report_equity()` DB write every loop | `main.py:129` | ✅ Yes |
| 5 | 🟡 MEDIUM | Blocking `save_state()` file I/O | `main.py:56-70` | ✅ Yes |
| 6 | 🔴 CRITICAL | `close_position()` called without `await` — positions never actually closed | `main.py:235` | ✅ Yes |
| 7 | 🔴 CRITICAL | `sentinel.register_loss()` / `register_win()` called without required `symbol` arg — `TypeError` silently swallowed | `main.py:238-240` | ✅ Yes |
| 8 | 🟠 HIGH | Transformer brain PyTorch inference blocks event loop | `main.py:280-284` | ❌ No |
| 9 | 🟢 LOW | Redundant `df.reset_index().to_dict("records")` — never used by any brain | `main.py:187` | ✅ Yes |
| 10 | 🟢 LOW | Dead-code duplicate `buying_power < trade_value` check | `main.py:316-318` | ✅ Yes |
| 11 | 🟢 LOW | Blocking `urllib.request.urlopen` in transformer brain (startup only) | `transformer.py:32` | ❌ No |
| 12 | 🟡 MEDIUM | Sequential `get_orderbook_ratio()` fetch per BUY symbol | `main.py:303` | ❌ No |
| 13 | 🟢 LOW | Sequential Discord alerts (not fire-and-forget) | `main.py:145-151, 246-257, 341-354` | ❌ No |

---

## Detailed Findings

### 🔴 1. Sequential OHLCV Fetching (CRITICAL)

**File:** `main.py`, lines 160-167

```python
for symbol in SYMBOLS:
    try:
        ...
        df = await get_ohlcv(symbol)  # ← runs one at a time
```

**Problem:** Although `get_ohlcv` is `async` and uses `asyncio.to_thread` internally, the `for` loop awaits each call sequentially. With 3 symbols (BTC/USD, ETH/USD, SOL/USD) and ~200ms per Alpaca API call, this adds ~400ms of unnecessary latency per cycle.

**Fix:** Use `asyncio.gather`:
```python
ohlcv_data = await asyncio.gather(*[get_ohlcv(s) for s in SYMBOLS])
for symbol, df in zip(SYMBOLS, ohlcv_data):
    ...
```
> `main (1).py` already implements this fix (line 164).

**Estimated savings:** ~400ms per cycle (3 symbols × ~200ms).

---

### 🟠 2. Blocking `get_account_state()` in Async Loop (HIGH)

**File:** `main.py`, line 124

```python
equity, buying_power = get_account_state()  # ← synchronous, blocks event loop
```

**Problem:** `get_account_state()` in `data_feed.py` (line 155) is a plain `def` that calls `trading_client.get_account()` — a blocking HTTP request. This runs directly in the asyncio event loop, blocking all other coroutines for the duration of the network round-trip (~100-300ms).

**Fix:** Offload to a thread:
```python
equity, buying_power = await asyncio.to_thread(get_account_state)
```
> `main (1).py` calls `await get_account_state()` (line 126), but `data_feed.py` still defines it as a sync `def` — this would raise `TypeError: object tuple can't be used in 'await' expression`. The `data_feed.py` function must also be made `async`.

**Estimated savings:** ~100-300ms per cycle.

---

### 🟠 3. Blocking `get_all_positions()` in Async Loop (HIGH)

**File:** `main.py`, line 156

```python
current_positions = get_all_positions()  # ← synchronous, blocks event loop
```

**Problem:** Same as issue #2. `get_all_positions()` in `data_feed.py` (line 166) is a plain `def` calling `trading_client.get_all_positions()` — a blocking HTTP request.

**Fix:** Offload to a thread:
```python
current_positions = await asyncio.to_thread(get_all_positions)
```
> `main (1).py` calls `await get_all_positions()` (line 158), but `data_feed.py` still defines it as sync — same crash as issue #2.

**Estimated savings:** ~100-300ms per cycle.

---

### 🟡 4. Blocking `report_equity()` DB Write Every Loop (MEDIUM)

**File:** `main.py`, line 129

```python
try:
    report_equity(BOT_NAME, equity)  # ← synchronous psycopg2 INSERT, blocks
except Exception:
    pass
```

**Problem:** `report_equity()` in `database.py` (line 91) is a plain `def` that performs a synchronous psycopg2 `INSERT` with `conn.commit()`. This blocks the event loop on every 60-second cycle.

**Fix:** Offload to a thread:
```python
await asyncio.to_thread(report_equity, BOT_NAME, equity)
```
> `main (1).py` already implements this fix (line 131).

**Estimated savings:** ~5-50ms per cycle (depends on DB latency).

---

### 🟡 5. Blocking `save_state()` File I/O (MEDIUM)

**File:** `main.py`, lines 56-70

```python
def save_state():
    ...
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, STATE_FILE_PATH)
```

**Problem:** `save_state()` is a plain `def` that performs synchronous file I/O. It's called after every trade entry and every exit. While individual file writes are fast (~1-5ms), they block the event loop.

**Fix:** Make it `async def` and offload to a thread:
```python
async def save_state():
    ...
    await asyncio.to_thread(_write)
```
> `main (1).py` already implements this fix (lines 56-72).

**Estimated savings:** ~1-5ms per trade/exit.

---

### 🔴 6. `close_position()` Called Without `await` — Positions Never Actually Closed (CRITICAL)

**File:** `main.py`, line 235

```python
success = close_position(symbol)  # ← missing await!
if success:  # ← coroutine is always truthy
    ...
    entry_times.pop(alpaca_sym, None)  # ← state cleaned up
    save_state()
```

**Problem:** `close_position` in `portfolio.py` (line 12) is `async def`. Calling it without `await` returns a **coroutine object** (which is always truthy). The code then proceeds to clean up state (`entry_times.pop`, `save_state`) as if the position was closed, but the actual HTTP close request **never executes**. The position remains open in Alpaca, but the bot thinks it's closed — leading to stale state, incorrect drawdown calculations, and phantom buying power.

**Fix:** Add `await`:
```python
success = await close_position(symbol)
```
> `main (1).py` already implements this fix (line 240).

**Impact:** This is a correctness bug, not just latency — but it also means the event loop is never blocked by the close call (because it never happens), so it's a "false latency improvement."

---

### 🔴 7. `sentinel.register_loss()` / `register_win()` Missing Required `symbol` Argument (CRITICAL)

**File:** `main.py`, lines 238-240

```python
if pnl_pct < 0:
    sentinel.register_loss()       # ← missing symbol arg!
else:
    sentinel.register_win()        # ← missing symbol arg!
```

**Problem:** In `sentinel.py` (lines 38-45), both methods require `symbol: str` as a positional argument:
```python
def register_loss(self, symbol: str):
    self._consecutive_losses[symbol] = self._consecutive_losses.get(symbol, 0) + 1
```

Calling `sentinel.register_loss()` without `symbol` raises `TypeError: register_loss() missing 1 required positional argument: 'symbol'`. This is caught by the per-symbol `try/except` (line 356), so it **silently fails** — the sentinel's consecutive-loss tracking never works, meaning the sentinel can never veto a symbol for consecutive losses.

**Fix:** Pass the symbol:
```python
sentinel.register_loss(alpaca_sym)
sentinel.register_win(alpaca_sym)
```
> `main (1).py` already implements this fix (lines 243, 245).

**Impact:** Correctness bug — sentinel's loss-based veto is completely non-functional.

---

### 🟠 8. Transformer Brain PyTorch Inference Blocks Event Loop (HIGH)

**File:** `main.py`, lines 280-284

```python
decisions = [
    transformer_brain.decide(snapshot),  # ← synchronous, blocks
    quant_brain.decide(snapshot),
    momentum_brain.decide(snapshot),
]
```

**Problem:** `transformer_brain.decide()` in `brains/transformer.py` (line 144) is a plain `def` that:
1. Calls `add_features(df.copy())` — copies the DataFrame and computes 11 rolling-window features (O(n) pandas operations)
2. Applies `self._scaler.transform(data)` — scikit-learn transform
3. Runs `torch.no_grad()` + `self._model(tensor)` — PyTorch forward pass on CPU

All of this runs synchronously in the event loop. With 3 symbols, this is 3× the blocking time. PyTorch inference on CPU for a 4-layer GQA transformer with seq_len=32 can take 50-500ms per symbol.

**Fix:** Offload to a thread:
```python
decisions = await asyncio.gather(*[
    asyncio.to_thread(transformer_brain.decide, snapshot),
    asyncio.to_thread(quant_brain.decide, snapshot),
    asyncio.to_thread(momentum_brain.decide, snapshot),
])
```

> ⚠️ **Neither `main.py` nor `main (1).py` fixes this.** This is the most impactful remaining latency issue.

**Estimated savings:** ~150-1500ms per cycle (3 symbols × 50-500ms).

---

### 🟢 9. Redundant `df.reset_index().to_dict("records")` Conversion (LOW)

**File:** `main.py`, line 187

```python
snapshot = MarketSnapshot(
    ...
    candles=df.reset_index().to_dict("records"),  # ← never used by any brain
    ...
)
```

**Problem:** The `candles` field is populated by converting the entire DataFrame to a list of dicts. However, no brain actually reads `snapshot.candles` — the transformer brain uses `snapshot.candles_df = df` (line 197) instead. This is an unnecessary O(n) memory allocation and serialization.

**Fix:** Set `candles=[]` (or remove the field entirely).
> `main (1).py` already implements this fix (line 192: `candles=[]`).

**Estimated savings:** ~1-5ms per symbol.

---

### 🟢 10. Dead-Code Duplicate `buying_power < trade_value` Check (LOW)

**File:** `main.py`, lines 313-318

```python
if trade_value <= 0 or buying_power < trade_value:
    continue

if buying_power < trade_value:  # ← dead code: already checked above
    logger.warning(f"🚫 Insufficient BP ...")
    continue
```

**Problem:** The second `if buying_power < trade_value:` can never be True because the first `if` already `continue`s when `buying_power < trade_value`. The warning log is unreachable.

**Fix:** Remove the dead-code block.
> `main (1).py` already implements this fix (removes lines 321-323).

---

### 🟢 11. Blocking `urllib.request.urlopen` in Transformer Brain (LOW)

**File:** `brains/transformer.py`, line 32

```python
urllib.request.urlopen(req, timeout=10)
```

**Problem:** `_alert_transformer_load_failure()` uses synchronous `urllib.request.urlopen` to send a Discord webhook alert when the model fails to load. This is a blocking HTTP call. It's only called once at startup (during `TransformerBrain._load()`), so it doesn't affect per-loop latency — but if the model file is missing and the Discord webhook is slow/unreachable, it blocks startup for up to 10 seconds.

**Fix:** Use `aiohttp` (already a dependency) or `asyncio.to_thread`:
```python
await asyncio.to_thread(urllib.request.urlopen, req, timeout=10)
```
Or better, use the existing `notifications.send_discord_alert()` function.

> ⚠️ **Neither `main.py` nor `main (1).py` fixes this.**

---

### 🟡 12. Sequential `get_orderbook_ratio()` Fetch Per BUY Symbol (MEDIUM)

**File:** `main.py`, line 303

```python
ob_ratio = get_orderbook_ratio(symbol)  # ← sequential Alpaca API call
```

**Problem:** After the committee decides BUY, `get_orderbook_ratio()` makes another Alpaca API call (`data_client.get_crypto_orderbook`). Although it uses `asyncio.to_thread` internally (so it doesn't block the event loop), it's called sequentially within the per-symbol loop. If multiple symbols trigger BUY in the same cycle, each orderbook fetch adds ~100-200ms of sequential wait time.

**Fix:** Collect all BUY candidates first, then fetch all orderbook ratios in parallel:
```python
buy_candidates = [s for s in symbols if committee_decided_buy(s)]
ob_ratios = await asyncio.gather(*[get_orderbook_ratio(s) for s in buy_candidates])
```

> ⚠️ **Neither `main.py` nor `main (1).py` fixes this.**

**Estimated savings:** ~100-200ms per additional BUY symbol.

---

### 🟢 13. Sequential Discord Alerts (LOW)

**File:** `main.py`, lines 145-151, 246-257, 341-354

```python
await send_discord_alert(...)  # ← sequential
```

**Problem:** Multiple `await send_discord_alert(...)` calls are made sequentially within the loop. Each is an aiohttp POST to Discord (~50-100ms). While individually fast, they add up when multiple alerts fire in a single cycle (e.g., startup alert + buy alert + exit alert).

**Fix:** Use fire-and-forget with `asyncio.create_task()`:
```python
asyncio.create_task(send_discord_alert(...))
```

> ⚠️ **Neither `main.py` nor `main (1).py` fixes this.**

**Estimated savings:** ~50-100ms per additional alert.

---

## Summary of Remaining Issues (after `main (1).py` fixes)

Even if you switch to `main (1).py`, the following latency issues remain:

| # | Issue | Estimated Savings |
|---|-------|-------------------|
| 8 | Transformer brain PyTorch inference blocks event loop | ~150-1500ms/cycle |
| 11 | Blocking `urllib.request.urlopen` in transformer brain | ~0-10s at startup |
| 12 | Sequential orderbook ratio fetches | ~100-200ms/cycle |
| 13 | Sequential Discord alerts | ~50-100ms/cycle |

**Total estimated remaining latency:** ~300ms-1.8s per cycle.

---

## Additional Notes

### `main (1).py` Introduces a Crash
`main (1).py` calls `await get_account_state()` (line 126) and `await get_all_positions()` (line 158), but `data_feed.py` still defines both as plain `def` (sync). This would raise:
```
TypeError: object tuple can't be used in 'await' expression
```
To use `main (1).py`, `get_account_state()` and `get_all_positions()` in `data_feed.py` must also be made `async` (or wrapped in `asyncio.to_thread`).

### Dockerfile Entry Point
The Dockerfile (`CMD ["python", "main.py"]`) confirms `main.py` is the active entry point, not `main (1).py`.

### No `time.sleep` Found
No blocking `time.sleep()` calls were found in the codebase. All sleeps use `asyncio.sleep()` correctly.

---

## State Synchronization Issue: "2/3" Position Discrepancy

### Observed Behavior
The bot's header reports `Positions: 2/3` (from `len(entry_times)`), but all three symbols (BTC, ETH, SOL) show as 📌 HOLDING in the per-symbol loop (from Alpaca's `current_positions`). This means one position exists on the exchange but is not tracked in the bot's internal `entry_times` dict.

### Root Cause: Cascading Consequence of Bug #6 (Missing `await` on `close_position()`)

The code flow in `main.py` explains the discrepancy:

1. **Header count** (line 137): `len(entry_times)` — bot's internal tracking dict
2. **Position detection** (line 164): `has_pos = pos_data is not None and pos_data["qty"] > 0` — from Alpaca's `get_all_positions()`
3. **Entry time fallback** (line 210): `entry_dt = entry_times.get(alpaca_sym, datetime.now(timezone.utc))` — falls back to "now" if not tracked
4. **Held hours** (line 211): `held_h ≈ 0` when `entry_dt` is "now"
5. **MAX_HOLD_HOURS exit** (line 230): `elif held_h >= MAX_HOLD_HOURS:` — **never fires** for orphaned positions

When the bot attempts to close a position via `close_position(symbol)` (line 235) **without `await`**:
1. The coroutine is created but never executed — the HTTP close request **never reaches Alpaca**
2. The code proceeds to `entry_times.pop(alpaca_sym, None)` (line 241) — the position is removed from internal tracking
3. But the position **remains open on the exchange**
4. On the next cycle, Alpaca reports 3 positions, but `entry_times` only has 2
5. The orphaned position has `held_h ≈ 0` (falls back to `datetime.now(timezone.utc)`)
6. The MAX_HOLD_HOURS exit condition can **never fire** for this position — it's stuck until stop-loss, take-profit, or trailing-stop triggers (if the price moves favorably enough)

### Consequence
The orphaned position cannot be exited via the time-based MAX_HOLD_HOURS mechanism. It can only be closed if:
- Price drops enough to trigger stop loss (`pnl_pct <= -effective_stop`)
- Price rises enough to trigger take profit (`pnl_pct >= TAKE_PROFIT_PCT`)
- Price falls below the trailing stop (only if `pnl_pct > 0`)

If the position is near breakeven and price is flat, it could remain stuck indefinitely.

### Is It a Manual/Earlier-Version Position?
No state file (`committee_bot_state.json`) exists in the working directory, so we can't inspect the persisted state directly. However, given the code analysis, the most likely cause is **not** a manually-opened or earlier-version position — it's the **missing `await` on `close_position()`** (bug #6). The bot attempted to close a position, thought it succeeded (coroutine is truthy), removed it from `entry_times`, but the position was never actually closed on Alpaca.

### Fix
This is resolved by the same fix for bug #6: add `await` to `close_position()`. The `main (1).py` file already implements this fix (line 240: `success = await close_position(symbol)`).
