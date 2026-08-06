# Financial Correctness Audit — Apex Committee Bot

**Date:** 2026-08-05  
**Scope:** All money-related calculations across the codebase  
**Method:** Traced each calculation path with concrete numeric examples, verified with actual code execution

---

## 1. Position Sizing

### Code Path
```
main.py:320  →  trade_value = calculate_trade_size(equity, confidence, sentinel_cap)
position_sizing.py:7  →  calculate_trade_size() returns dollar value
main.py:330  →  qty = trade_value / price
orders.py:25  →  place_order(symbol, OrderSide.BUY, qty, price)
```

### Configuration (config.py)
```python
SIZING_TIERS = [(0.90, 0.15), (0.75, 0.10), (0.60, 0.05), (0.00, 0.025)]
MAX_SINGLE_TRADE_USD = 5000.0
MIN_ORDER_USD = 10.0
```

### Numeric Example 1: Basic sizing at 90% confidence (VERIFIED)
- **Equity:** $100,000
- **Committee confidence:** 0.92 (≥ 0.90, so tier = 15%)
- **Trade value:** $100,000 × 0.15 = $15,000
- **No sentinel cap** (sentinel_cap = None)
- **MAX_SINGLE_TRADE_USD = $5,000**, so `min($15,000, $5,000) = $5,000`
- **Result:** trade_value = $5,000.00 ✓ (verified by running code)
- **Price:** $60,000/BTC
- **qty = $5,000 / $60,000 = 0.08333333333333333 BTC**
- **Verified by code execution:** `0.08333333333333333` (full float precision, no rounding applied)

### Numeric Example 2: Low confidence at 2.5% tier (VERIFIED)
- **Equity:** $100,000
- **Committee confidence:** 0.45 (< 0.60, fallback tier = 2.5%)
- **Trade value:** $100,000 × 0.025 = $2,500
- **No sentinel cap**
- **MIN_ORDER_USD = $10**, so $2,500 > $10 → passes
- **Result:** trade_value = $2,500.00 ✓

### Numeric Example 3: Sentinel cap (VERIFIED)
- **Equity:** $100,000
- **Confidence:** 0.92 → tier = 15% → trade_value = $15,000
- **Sentinel cap:** 0.50 (from sentinel.py line 94, when atr_pct > 3.0%)
- **After cap:** $15,000 × 0.50 = $7,500
- **MAX_SINGLE_TRADE_USD = $5,000**, so `min($7,500, $5,000) = $5,000`
- **Result:** trade_value = $5,000.00 ✓ (cap applied correctly, verified by running code)

### Numeric Example 4: Below minimum order (VERIFIED)
- **Equity:** $300
- **Confidence:** 0.45 → tier = 2.5% → trade_value = $300 × 0.025 = $7.50
- **MIN_ORDER_USD = $10**
- **$7.50 < $10** → returns 0.0 (skip trade)
- **Result:** 0.0 ✓ (verified by running code)

### Finding 1.1: Quantity truncation for SELL orders only (VERIFIED)
- **orders.py line 37:** SELL orders use `qty = math.floor(qty * 1e8) / 1e8` to truncate to 8 decimal places.
- **BUY orders** (line 30-31) do NOT apply this truncation — qty is passed directly as a raw float.
- **Verified numerically by code execution:**
  - BUY qty: `0.08333333333333333` (full float precision)
  - SELL qty (truncated): `0.08333333`
  - **Difference: 3.33e-9 BTC** (~$0.0002 at $60k/BTC) — dust remains in position after partial closure
  - This is a minor inconsistency but unlikely to cause material issues since exchanges typically truncate internally.

### Finding 1.2: BUY limit price uses ROUND_DOWN (VERIFIED)
- **orders.py line 30:** BUYs use limit orders with `raw_limit = price * 1.001`, then `_sanitize_price` truncates with `ROUND_DOWN`.
- **Verified numerically by code execution:**
  - market price = $60,000.00
  - raw_limit = $60,000 × 1.001 = $60,059.99999999999
  - sanitized limit = $60,059.99 (truncated 1 cent down)
  - Intended buffer: 0.10%, actual buffer: ~0.10% (negligible truncation loss of $0.01)
  - For price = $60,000.129: raw_limit = $60,060.12912899999, sanitized = $60,060.12, truncation loss = $0.00913

**Verdict: Position sizing correctly implements the intended risk-per-trade percentage. The tier selection, sentinel cap, MAX_SINGLE_TRADE_USD ceiling, and MIN_ORDER_USD floor all work correctly. Rounding direction is truncation (ROUND_DOWN for prices, floor for SELL qty) — no rounding bias toward the house. The only inconsistency is that BUY qty is not truncated while SELL qty is, leaving sub-cent dust on partial closures.**

---

## 2. PnL Calculation

### Code Path
```
main.py:206  →  avg_entry = entry_prices.get(alpaca_sym, pos_data["avg_entry"])
main.py:214  →  pnl_pct = (price - avg_entry) / avg_entry
```

The bot tracks PnL as a **percentage** only (`pnl_pct`), not as a dollar amount.

### How PnL is tracked:
1. **Entry:** `entry_prices[alpaca_sym] = price` (main.py:339)
2. **Unrealized PnL:** `pnl_pct = (price - avg_entry) / avg_entry` (main.py:214)
   - This uses the **last known `price`** from `indicators["price"]`, which is `close.iloc[-1]` — the most recent completed candle's close.
   - It does NOT use a real-time mid or bid/ask.
3. **Exit decision** is based on `pnl_pct` vs thresholds:
   - Stop loss: `pnl_pct <= -effective_stop` (main.py:229)
   - Take profit: `pnl_pct >= TAKE_PROFIT_PCT` (main.py:231)
   - Trailing stop: `price < trailing_stop_price and pnl_pct > 0` (main.py:233)

### Numeric Example: Unrealized PnL (VERIFIED)
- **Entry price:** $60,000 (stored in `entry_prices["BTCUSD"]`)
- **Current price (last close):** $63,000
- **PnL:** ($63,000 - $60,000) / $60,000 = $3,000 / $60,000 = 0.05 = +5.0%
- **TAKE_PROFIT_PCT = 0.06**, so 5.0% < 6.0% → no exit yet ✓

### Critical Finding 2.1: No dollar PnL tracking
- **The bot NEVER tracks realized PnL in dollars.** It only stores a binary win/loss flag via `sentinel.register_win(symbol)` or `sentinel.register_loss(symbol)` (main.py:243-245).
- `record_trade` in database.py (line 72) records `price, qty, value, fee=0` but the `fee` is always hardcoded to `0` (line 81: `fee NUMERIC DEFAULT 0`). No actual fee is recorded.
- There is **no realized PnL calculation anywhere in the codebase**. PnL is only tracked as a percentage, and only used for exit decisions.

### Critical Finding 2.2: No partial fill handling / single entry price (VERIFIED)
- The bot stores a single `entry_prices[alpaca_sym]` as a scalar float. There is no FIFO/LIFO/average-cost tracking for multiple fills into the same symbol.
- If the Alpaca position has multiple fills with different prices, the bot uses `pos_data["avg_entry"]` (from `get_all_positions` → Alpaca's reported `avg_entry_price`). But the bot's internal `entry_prices` dict only stores the LAST entry price at the time of the bot's own BUY signal.
- **No averaging logic exists in the bot's code at all.** The bot relies entirely on Alpaca's `avg_entry_price` field for positions it didn't create in this session.
- **Verified numerically:**
  - `avg_entry = entry_prices.get(alpaca_sym, pos_data["avg_entry"])` returns the bot's stored price if it exists, NOT Alpaca's avg_entry.
  - Multi-entry scenario:
    - Bot BUY at $60,000 → `entry_prices["BTCUSD"] = 60000`
    - External BUY at $62,000 → Alpaca updates `avg_entry_price = $61,000` (weighted avg of 1 @ $60k + 1 @ $62k)
    - `entry_prices.get("BTCUSD", ...)` returns $60,000 (the bot's stored price), NOT $61,000
    - Current price = $63,000:
      - Bot PnL: ($63,000 - $60,000) / $60,000 = **+5.00%**
      - Actual PnL: ($63,000 - $61,000) / $61,000 = **+3.28%**
      - **Discrepancy: 1.72 percentage points** — bot overstates PnL by 53.6% relative
    - This would also cause stop-loss/take-profit to trigger at wrong thresholds based on the wrong avg_entry

### Finding 2.3: `record_trade` stores intended price, not actual fill price (VERIFIED)
- **orders.py line 45:** `record_trade(BOT_NAME, symbol, side.value, qty, price, order_id=order.id)`
  - The `price` passed is the **last close price** (from `indicators["price"]`), NOT the actual fill price from the Alpaca order response.
  - The `order` object returned by `trading_client.submit_order` contains `order.filled_avg_price`, `order.filled_qty`, etc. — these are **never read**.
  - **database.py line 79:** `value = (price or 0) * qty` uses this same incorrect price.
  - **No fill price is ever captured or logged.** The `order` object is used only for `order.id`.

### Finding 2.4: PnL uses last close, not actual exit price
- When the bot exits (calls `close_position`), the actual exit price is determined by the exchange/market, not by `price` from the last candle.
- The `pnl_pct` reported in Discord and logs uses the last close price, which may differ from the actual fill price at exit.
- **Example:** Last close = $63,000 → bot reports PnL = +5.00%. Market order sells at $62,980 (slippage of $20) → actual PnL = +4.97%. PnL discrepancy = 0.03pp.

**Verdict: No formal realized PnL calculation exists. Unrealized PnL percentage is correctly computed from entry price and current close. No multi-entry averaging — the bot stores a single entry price per symbol and never averages. The `avg_entry` fallback to `pos_data["avg_entry"]` at line 206 is never actually used because `entry_prices` always has a value when a position is open (the bot only opens positions when it stores the entry price immediately after).**

---

## 3. Fee and Slippage Handling

### Fees (VERIFIED: completely absent)
- Grep for `(fee|commission|cost)` across all `.py` files returns matches only for `feature_engineering.py` (referring to "cost of carry" features, not trading fees), `data_feed.py` (referring to "fetch cost" of API calls, not trading fees), and `database.py` (the `fee` column definition).
- **database.py line 63:** `fee NUMERIC DEFAULT 0` — column always defaults to zero.
- **database.py line 80-82:** INSERT statement hardcodes `fee` to `0` (the literal value `0` appears in the VALUES clause).
- **`record_trade` signature:** `def record_trade(bot_name, symbol, side, qty, price, pnl_pct=None, order_id=None)` — there is no `fee` parameter at all. Fees are never passed in.
- **Alpaca's actual fees** (typically 0.1% for crypto on paper trading, varies for other assets) are **never accounted for** in any calculation in this bot.

### Numeric Example: Fee impact (VERIFIED)
- Bot enters BTC/USD at $60,000, size = $5,000 → qty = 0.08333333 BTC
- Exit at $63,000, gross PnL = $5,000 × (63,000/60,000 - 1) = $5,000 × 0.05 = **$250**
- Alpaca fee (0.1% each way) = $5,000 × 0.001 × 2 = **$10.00**
- **Net PnL = $250 - $10 = $240**
- **Bot reports PnL = +5.00% gross, never mentions the $10 fee** — overstates net return by 4.17% relative ($10/$240)

### Slippage (VERIFIED: completely untracked)
- Grep for `(slippage|fill_price|fill_avg|expected_price|actual_fill)` returns **zero matches**.
- **Finding 3.1: No slippage modeling anywhere.** No code captures `order.filled_avg_price` or any execution metric from the Alpaca order response.
- For BUY orders, `orders.py` uses a **limit order with a 0.1% premium** (line 30: `raw_limit = price * 1.001`).
  - This means the bot sets its limit price 0.1% above the current close, which may result in the order not filling if the market moves away, or filling at a worse price than the close if the market moves through quickly.
- For SELL orders, `orders.py` uses a **market order** (line 38) with no price limit at all.
  - **SELL orders have NO slippage protection** — the actual fill price is entirely at the mercy of market conditions.
- **No slippage is logged, computed, or tracked anywhere.** The bot has zero visibility into execution quality.

### Finding 3.2: `record_trade` stores intended price, not fill price (VERIFIED)
- `orders.py line 45`: `record_trade(BOT_NAME, symbol, side.value, qty, price, order_id=order.id)`
  - The `price` passed is the **last close price** passed as argument, NOT the actual fill price.
  - The `order` object returned by `trading_client.submit_order` contains `filled_avg_price`, `filled_qty`, `status`, etc. — these are **never read**.
- **database.py line 79:** `value = (price or 0) * qty` uses this same incorrect price.
- **Example:** Bot sends BUY limit at $60,059.99 (sanitized $60,000 × 1.001). If the market moves and the order fills at $60,080.00, the DB records `price=60000.0` (the last close), not `60080.0` (actual fill). **Price discrepancy: $80.**

### Finding 3.3: `pnl_pct` parameter in `record_trade` is a dead parameter
- `database.py line 72`: `def record_trade(bot_name, symbol, side, qty, price, pnl_pct=None, order_id=None)`
- The `pnl_pct` parameter is accepted but:
  1. **Never passed** from `orders.py` (line 45 only passes `bot_name, symbol, side.value, qty, price, order_id=order.id`)
  2. **Never stored** in the SQL INSERT statement (line 80-82 — the INSERT columns are `bot_name, exchange, symbol, side, price, quantity, value, fee, order_id, timestamp` — `pnl_pct` is not included)
- This parameter is completely useless dead code.

### Finding 3.4: SELL orders use market orders with zero slippage protection
- **orders.py line 38:** SELL orders use `MarketOrderRequest` with no price limit.
- BUY orders (line 32) use `LimitOrderRequest` with `limit_price = price * 1.001` (0.1% premium buffer).
- **This means the bot has slippage protection on buys but absolutely zero slippage protection on sells** — the side that matters most for realizing PnL.
- In a fast-moving market, a SELL market order could fill significantly worse than the intended exit price, with no mechanism to control or even observe the slippage.

**Verdict: Fees are completely ignored (hardcoded as 0 in DB, never passed to `record_trade`). Slippage is completely untracked — actual fill prices are never captured, compared, or logged. The BUY side uses a 0.1% limit order buffer (which may result in non-fills), while the SELL side uses market orders with no protection at all. The `pnl_pct` parameter in `record_trade` is dead code — never passed, never stored. This makes all performance analysis and audit trails unreliable.**

---

## 4. Float vs. Decimal Usage

### Audit of all money-related calculations:

| File | Function | Variable | Type | Risk |
|------|----------|----------|------|------|
| `position_sizing.py` | `calculate_trade_size` | `equity` | float | Low — dollar amounts, `round(x, 2)` |
| `position_sizing.py` | `calculate_trade_size` | `trade_value` | float | Low — rounded to 2 decimals |
| `main.py` | line 330 | `qty = trade_value / price` | float | Low — crypto high precision OK |
| `main.py` | line 214 | `pnl_pct` | float | Low — percentage, not money |
| `orders.py` | `_sanitize_price` | `price` | float→Decimal→float | OK — converts to Decimal for rounding |
| `orders.py` | line 37 | `qty` (SELL) | float, `math.floor` | OK — truncation is correct |
| `data_feed.py` | line 79-102 | `rsi`, `macd`, etc. | float | N/A — indicators, not money |
| `database.py` | line 79 | `value = price * qty` | float | Medium — stored as `NUMERIC` in DB |
| `config.py` | `stop_loss_pct` etc. | float | float | Low — percentages |

### Finding 4.1: `database.py` uses float math before DB insert (VERIFIED)
- **Line 79:** `value = (price or 0) * qty` — this is a float multiplication using Python's IEEE 754 double precision.
- The result is inserted into a `NUMERIC` column, but the **computation** is done in float.
- **Verified numerically by code execution:**
  - `price = 60000.0`, `qty = 0.08333333333333333`
  - `value = 60000.0 * 0.08333333333333333 = 5000.0` (exact in this case because the float happens to be exact)
  - `qty * price = 5000.0` (exact round-trip)
  - Float drift test: 100,000 additions/subtractions of 0.1 resulted in 0.0 error (Python's float is surprisingly stable for this operation)
  - In practice, the error per trade is ~$1e-10 for dollar amounts, which is extremely low risk.
- However, it is technically impure — the same calculation in `Decimal` would be exact and consistent with best practices for financial software.

### Finding 4.2: `main.py line 330` — float division for qty (VERIFIED)
- `qty = trade_value / price` where both are floats.
- `trade_value` is a float (from `calculate_trade_size` which returns `round(trade_value, 2)`), and `price` is a float from `indicators["price"]`.
- **Verified numerically:** `5000.0 / 60000.0 = 0.08333333333333333` — standard float division, precision is sufficient for crypto.
- **No cumulative drift issue** because qty is only computed per-trade, not re-used or accumulated.

### Finding 4.3: `main.py line 135` — drawdown calculation in float (VERIFIED)
- `drawdown = (equity - start_equity) / start_equity` — pure float math.
- `equity` and `start_equity` are floats from `get_account_state()` which does `float(acct.equity)`.
- Alpaca returns equity as a string in the API. Converting to float introduces a single rounding. Over many cycles, this doesn't accumulate because `start_equity` is set once and `equity` is freshly fetched each cycle.
- **Risk: None** — the values are independent each cycle.

### Finding 4.4: `pnl_pct` and trailing stop calculations (VERIFIED)
- `pnl_pct = (price - avg_entry) / avg_entry` — float division, no drift risk (computed fresh each cycle).
- `trailing_stop_price = peak_price * (1.0 - TRAILING_STOP_PCT)` — float multiplication, no drift risk.

**Verdict: `Decimal` is correctly used in `_sanitize_price` (orders.py:13-22) for price rounding with ROUND_DOWN. All other money calculations use `float`, but none involve repeated accumulation that could cause drift — each calculation is independent and computed fresh per-trade or per-cycle. The only mildly concerning case is `database.py:79` `value = price * qty` in float (could use Decimal), but the error is sub-cent and only affects DB reporting, not execution.**

---

## 5. Stop-Loss / Take-Profit Trigger Prices

### Code Path (main.py lines 206-236):
```python
avg_entry = entry_prices.get(alpaca_sym, pos_data["avg_entry"])
peak_price = peak_prices.get(alpaca_sym, avg_entry)

# Update peak price for trailing stop
if price > peak_price:
    peak_prices[alpaca_sym] = price
    peak_price = price

pnl_pct = (price - avg_entry) / avg_entry
trailing_stop_price = peak_price * (1.0 - TRAILING_STOP_PCT)

# Dynamic time-decay stop loss
effective_stop = STOP_LOSS_PCT
if held_h >= 2.0:
    effective_stop *= 0.50
elif held_h >= 1.0:
    effective_stop *= 0.75

exit_reason = None
if pnl_pct <= -effective_stop:      # Stop loss
    exit_reason = ...
elif pnl_pct >= TAKE_PROFIT_PCT:    # Take profit
    exit_reason = ...
elif price < trailing_stop_price and pnl_pct > 0:  # Trailing stop
    exit_reason = ...
```

### Configuration (config.py):
```python
STOP_LOSS_PCT = 0.04    # 4%
TAKE_PROFIT_PCT = 0.06  # 6%
TRAILING_STOP_PCT = 0.02  # 2%
MAX_HOLD_HOURS = 8.0
```

### Numeric Example 1: Long position basic stop-loss (VERIFIED)
- **Entry price:** $60,000
- **Current price:** $57,000
- **PnL:** ($57,000 - $60,000) / $60,000 = -5.0%
- **STOP_LOSS_PCT = 4.0%**, `effective_stop = 0.04` (held_h < 1.0h)
- **-5.0% <= -4.0%** → True → exit triggered ✓

### Numeric Example 2: Take profit (VERIFIED)
- **Entry price:** $60,000
- **Current price:** $64,000
- **PnL:** ($64,000 - $60,000) / $60,000 = +6.67%
- **TAKE_PROFIT_PCT = 6.0%**
- **6.67% >= 6.0%** → True → exit triggered ✓

### Numeric Example 3: Trailing stop (VERIFIED)
- **Entry price:** $60,000
- **Peak price:** $65,000 (updated as price rose)
- **trailing_stop_price = $65,000 × (1 - 0.02) = $63,700**
- **Current price:** $63,500
- **$63,500 < $63,700** → True, and **pnl_pct = +5.83% > 0** → True → exit triggered ✓

### Numeric Example 4: Time-decay stop tightening (VERIFIED)
- **Entry price:** $60,000
- **Held for 2.5 hours** → held_h = 2.5 >= 2.0 → effective_stop = 0.04 × 0.50 = 0.02 (2%)
- **Current price:** $58,900
- **PnL:** ($58,900 - $60,000) / $60,000 = -1.83%
- **-1.83% <= -2.0%** → False → no exit yet
- If price drops to $58,800: PnL = -2.0% → -2.0% <= -2.0% → True → exit ✓

### Finding 5.1: Trailing stop uses strict `<` comparison (VERIFIED edge case)
- `price < trailing_stop_price` uses strict less-than, not `<=`.
- **Verified edge case:** If peak = $65,000, trailing_stop_price = $63,700.00:
  - At price = $63,700.00: `63700.0 < 63700.0` → False → trailing stop does NOT fire
  - The position would need to drop to $63,699.99 or below to trigger.
  - This is a **minor edge case** — typically price moves in ticks, not exact cent values. But at exact thresholds, the strict `<` causes a one-cent delay in triggering.

### Finding 5.2: Stop-loss and trailing-stop logic only supports LONG positions
- The exit logic at main.py:229-236 is **exclusively designed for long positions**:
  - `pnl_pct = (price - avg_entry) / avg_entry` — this formula assumes the position is LONG.
  - For a SHORT position, PnL would be `(avg_entry - price) / avg_entry`, and the formulas for stop-loss and take-profit would be inverted.
  - There is **no code path for short selling** in this bot. The bot only ever places BUY orders (main.py:336: `place_order(symbol, OrderSide.BUY, qty, price)`).
  - **This is not a bug** — the bot is designed to only take long positions.

### Finding 5.3: The bot only ever BUYs — no SELL signal is ever acted upon
- **main.py line 305:** `if committee.action != "BUY": continue` — only BUY actions proceed.
- All exits happen mechanically via stop-loss/take-profit/trailing-stop/max-hold-time.
- The committee's SELL votes are completely ignored.
- This is **by design** (documented in KNOWN_ISSUES.md), but it means:
  - The "stop-loss trigger" is the **only** exit mechanism besides take-profit, trailing-stop, and max-hold.
  - There is no regime-based SELL from the committee.

### Finding 5.4: `pnl_pct > 0` guard on trailing stops is valid design
- `price < trailing_stop_price and pnl_pct > 0` (main.py:233)
- This means the trailing stop only fires if:
  1. Price has dropped below the trailing stop level, AND
  2. The position is still in profit (pnl_pct > 0)
- **Verified:** If price drops past trailing stop AND into negative territory, the stop-loss check (first elif in the chain) fires first (since it's checked in the same if/elif chain). The trailing stop's `pnl_pct > 0` guard ensures it only catches profit-taking scenarios, not loss scenarios. This is a valid conservative design — not a bug. ✓

**Verdict: Stop-loss and take-profit triggers are correctly calculated for LONG positions. The formulas are internally consistent. The bot only supports long positions, so there are no short-position errors. The `pnl_pct > 0` guard on trailing stops is a valid design choice. The only edge case is the strict `<` comparison on trailing stop triggering (negligible — sub-cent delay).**

---

## 6. Portfolio-Level Equity/Exposure Totals

### How equity is tracked:
1. **External source:** `equity` comes from Alpaca's API via `get_account_state()` (data_feed.py:182: `float(acct.equity)`).
2. **Internal tracking:** `entry_times`, `entry_prices`, `peak_prices`, `cooldowns` are per-symbol dicts keyed by `alpaca_sym`. These are NOT used for portfolio-level equity computation.
3. **No portfolio-level recomputation:** The bot does NOT independently compute total equity or total exposure by summing position values. It trusts Alpaca's equity figure entirely.

### Finding 6.1: No independent portfolio-level equity verification
- There is **no code** that sums `sum(qty_i * price_i)` across all positions and compares it to `equity`.
- The bot uses `equity` from Alpaca as the source of truth for:
  - Position sizing (`calculate_trade_size(equity, ...)`)
  - Drawdown calculation (`(equity - start_equity) / start_equity`)
  - Discord alerts (reporting equity)
- **No cross-check** is performed. If Alpaca's equity figure were wrong (e.g., due to a stale price feed), the bot would use the wrong number for everything.

### Numeric Example: Portfolio drift scenario
Suppose the bot has two open positions:
- BTC: qty=1.0, current price=$100,000 → value = $100,000
- ETH: qty=10.0, current price=$3,000 → value = $30,000
- Total exposure = $130,000
- Cash balance = $20,000 (from $150,000 starting equity)
- **True equity = $130,000 + $20,000 = $150,000**

Now suppose Alpaca's API returns stale prices:
- BTC price stale at $90,000 → position value = $90,000
- ETH price stale at $2,500 → position value = $25,000
- **Alpaca reports equity = $90,000 + $25,000 + $20,000 = $135,000**

The bot would calculate:
- `trade_value = $135,000 × 0.05 = $6,750` (instead of correct `$150,000 × 0.05 = $7,500`)
- **Under-investment of $750 (10% less than it should be)**

**There is no internal cross-check.** The bot cannot detect this.

### Finding 6.2: `buying_power` is decremented locally but never reconciled (VERIFIED)
- **main.py line 342:** `buying_power -= trade_value` — after placing a BUY order, the bot subtracts the trade value from the locally-stored `buying_power`.
- `buying_power` was obtained from `get_account_state()` at the top of the cycle (main.py:126).
- Within a single cycle, if multiple trades execute:
  - First trade: bp = $10,000 → bp -= $5,000 → bp = $5,000
  - Second trade: bp = $5,000 → bp -= $3,000 → bp = $2,000
  - At the top of the **next cycle**, bp is fetched fresh from Alpaca again.
- **No drift across cycles** because bp is always re-fetched from Alpaca at the start of each cycle. Within a single cycle, the local updates are consistent with the order of execution. ✓
- The local `buying_power` decrement is only used for the `buying_power < trade_value` check at main.py:326, which prevents over-spending within a single cycle. This is a reasonable client-side guard. ✓

### Finding 6.3: No aggregate exposure limit
- The bot checks `MAX_OPEN_POSITIONS` (max 3 concurrent positions), but does NOT check:
  - Total dollar exposure (sum of all position sizes)
  - Exposure as a percentage of equity
  - Per-symbol exposure concentration
- **No portfolio-level position size aggregation exists.** A trader could have 3 positions each at $5,000 (the max per trade), totaling $15,000 exposure, which could be 15% of a $100,000 portfolio — all within the per-trade and position-count limits but potentially excessive aggregate exposure.

### Finding 6.4: Equity and buying_power from same API call (VERIFIED consistent)
- `get_account_state()` fetches both `acct.equity` and `acct.buying_power` from the same `trading_client.get_account()` call.
- In Alpaca: `equity = cash + market_value_of_positions`, `buying_power = cash * 4` (for crypto, margin is 4x).
- These are always consistent because they come from the same API response. ✓

**Verdict: There is no portfolio-level equity/exposure reconciliation. The bot trusts Alpaca's equity number 100%. There is no code that sums individual position values to verify portfolio totals — so no drift is possible because the bot never attempts its own aggregation. However, this means a single bad equity feed from Alpaca propagates to position sizing, drawdown limits, and Discord reporting without any internal verification. This is a reliability risk, not a computational bug. No aggregate exposure limits exist beyond position count (max 3).**

---

## Summary of Findings

| # | Area | Finding | Severity |
|---|------|---------|----------|
| 1.1 | Position Sizing | SELL qty truncated to 8 decimals, BUY qty uses full float precision — sub-cent dust on partial closure | Low |
| 1.2 | Position Sizing | BUY limit price truncated with ROUND_DOWN — minor favor to bot (~$0.01) | Low |
| 2.1 | PnL | No dollar PnL tracking — only percentage PnL stored; no realized PnL anywhere | **High** |
| 2.2 | PnL | No partial fill averaging — single entry price per symbol; wrong avg_entry if external fills occur | **High** |
| 2.3 | PnL | `record_trade` stores intended price, not actual fill price — DB trade records are inaccurate | High |
| 2.4 | PnL | PnL uses last close, not actual exit price — reporting discrepancy | Medium |
| 3.1 | Fees | Fees completely ignored — hardcoded as 0 in database, never passed to record_trade | **Critical** |
| 3.2 | Slippage | No slippage tracking — actual fill price never captured from Alpaca order response | **Critical** |
| 3.3 | Fees | `pnl_pct` parameter in `record_trade` is dead code — never passed, never stored | High |
| 3.4 | Slippage | SELL orders use market orders with zero slippage protection | **Critical** |
| 4.1 | Float/Decimal | `database.py` float multiplication before NUMERIC insert — sub-cent risk only | Low |
| 4.2 | Float/Decimal | All other float usage is per-trade, no accumulation risk | Low |
| 5.1 | Stops | Trailing stop uses strict `<` — sub-cent edge case at exact threshold | Negligible |
| 5.2 | Stops | PnL formula hardcoded for longs only — correct for this long-only bot | N/A |
| 5.3 | Stops | Committee SELL signals ignored — mechanical exits only | By design |
| 5.4 | Stops | Trailing stop `pnl_pct > 0` guard is valid conservative design | OK |
| 6.1 | Portfolio | No independent equity verification — trusts Alpaca 100% | **Medium** |
| 6.2 | Portfolio | Local `buying_power` decremented per-cycle, re-fetched each cycle — no drift | OK |
| 6.3 | Portfolio | No aggregate exposure limit beyond position count (max 3) | Medium |
| 6.4 | Portfolio | Equity/buying_power from same API call — internally consistent | OK |

### Critical Issues Requiring Immediate Fix:
1. **Fees are completely ignored** — every trade records `fee=0`, and no fees are deducted from PnL calculations. Overstated returns by 4%+ for a typical 0.1% fee round-trip.
2. **Actual fill prices are never captured** — `record_trade` stores the intended close price, not the actual execution price. Slippage is completely invisible. All DB trade records have incorrect prices.
3. **SELL orders use market orders with zero slippage protection** — the exit side has no price control whatsoever.

### High Issues:
1. **No realized PnL tracking** — the bot only stores win/loss binary flags, not dollar P&L.
2. **No partial fill averaging** — if external fills occur in an Alpaca position, the bot uses its own stale single entry price for PnL and exit decisions, causing up to 53.6% PnL miscalculation in the worst case.
3. **`pnl_pct` parameter in `record_trade` is dead code** — accepted but never passed or stored.
4. **DB trade records store the wrong price** — `record_trade` stores `indicators["price"]` (last close), not the actual fill price.

### Medium Issues:
1. No portfolio-level equity reconciliation — single point of failure on Alpaca's numbers.
2. No aggregate exposure limits beyond position count.
3. PnL reporting uses last close, not actual exit price.

### Low Issues:
1. BUY qty not truncated while SELL qty is — sub-cent dust on partial closures.
2. `database.py` uses float for `value = price * qty` before NUMERIC insert — sub-cent risk.
3. Trailing stop strict `<` comparison — sub-cent edge case.
