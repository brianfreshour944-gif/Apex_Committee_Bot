
# main.py — Apex Committee Bot: 4-brain ensemble trading system.
#
# Architecture:
#   Brain 1 (Transformer 50%) — GrokGQA ML model
#   Brain 2 (Quant 30%)       — TA indicators (RSI/MACD/BB/EMA)
#   Brain 3 (Momentum 20%)    — Volume + regime transition detection
#   Sentinel (veto only)      — Risk guardian, cannot be outvoted
#
# Decision flow:
#   1. Fetch 15-min OHLCV + indicators
#   2. Classify market regime (DUMP/ACCUM/UPTREND/DIST)
#   3. All 3 brains cast weighted votes
#   4. Committee tallies -> winning action needs >60% weighted score
#   5. Sentinel checks for danger -> may veto or cap size
#   6. Position sized by confidence tier (51% -> tiny, 90% -> large)
#   7. Order placed

import asyncio
import json
import os
import time
from datetime import datetime, timezone

from alpaca.trading.enums import OrderSide

from config import (
    logger, BOT_NAME, SYMBOLS,
    MAX_OPEN_POSITIONS, MAX_DRAWDOWN_STOP,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, TRAILING_STOP_PCT, MAX_HOLD_HOURS,
    COOLDOWN_SECONDS_BUY, SLEEP_PER_LOOP,
    STATE_FILE_PATH, MIN_BID_ASK_RATIO,
    FEE_RATE,
)
from data_feed import get_ohlcv, compute_indicators, get_account_state, get_all_positions, get_orderbook_ratio
from regime import classify_regime
from brains.transformer import transformer_brain
from brains.quant import quant_brain
from brains.momentum import momentum_brain
from committee import run_committee
from sentinel import sentinel
from position_sizing import calculate_trade_size
from orders import place_order, cancel_stale_orders
from portfolio import normalize_symbol, close_position, close_all_positions, write_heartbeat
from database import init_db, report_equity, record_realized_pnl
from notifications import send_discord_alert
from models import MarketSnapshot

# ── Global state ───────────────────────────────────────────────
entry_times:   dict  = {}    # {alpaca_sym: datetime}
entry_prices:  dict  = {}    # {alpaca_sym: float}
peak_prices:   dict  = {}    # {alpaca_sym: float}  — for trailing stop
cooldowns:     dict  = {}    # {alpaca_sym: float}  — timestamp
start_equity:  float | None = None

# Lock for protecting shared state mutations and save_state
_state_lock:  asyncio.Lock | None = None


def get_state_lock():
    """Lazily create asyncio.Lock (must be called from async context)."""
    global _state_lock
    if _state_lock is None:
        _state_lock = asyncio.Lock()
    return _state_lock


async def save_state():
    """Atomically save persistent state to disk (protected by lock)."""
    lock = get_state_lock()
    async with lock:
        try:
            data = {
                "entry_times":  {k: v.isoformat() for k, v in entry_times.items()},
                "entry_prices": entry_prices,
                "peak_prices":  peak_prices,
                "cooldowns":    cooldowns,
            }
            tmp_path = f"{STATE_FILE_PATH}.tmp"
            def _write():
                with open(tmp_path, "w") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp_path, STATE_FILE_PATH)
            await asyncio.to_thread(_write)
        except Exception as e:
            logger.warning(f"State save failed: {e}")


def load_state():
    """Load persistent state from disk if available."""
    if not os.path.exists(STATE_FILE_PATH):
        return
    try:
        with open(STATE_FILE_PATH, "r") as f:
            data = json.load(f)
        for k, v in data.get("entry_times", {}).items():
            entry_times[k] = datetime.fromisoformat(v)
        entry_prices.update(data.get("entry_prices", {}))
        peak_prices.update(data.get("peak_prices", {}))
        cooldowns.update(data.get("cooldowns", {}))
        logger.info("[DISK] Restored persistent state from disk")
    except Exception as e:
        logger.warning(f"State load failed: {e}")


async def sync_state_with_alpaca():
    """Sync internal state with actual Alpaca positions on startup."""
    global start_equity
    try:
        current_positions = await get_all_positions()
        if not current_positions:
            return
        
        alpaca_symbols = set(current_positions.keys())
        local_symbols = set(entry_times.keys())
        
        # Add missing positions from Alpaca to local state
        for alpaca_sym, pdata in current_positions.items():
            if alpaca_sym not in entry_times:
                entry_times[alpaca_sym] = datetime.now(timezone.utc)
                entry_prices[alpaca_sym] = pdata["avg_entry"]
                peak_prices[alpaca_sym] = pdata["avg_entry"]
                logger.info(f"[SYNC] Added missing position: {alpaca_sym} qty={pdata['qty']} entry={pdata['avg_entry']}")
        
        # Remove local state for positions that no longer exist on Alpaca
        for alpaca_sym in local_symbols - alpaca_symbols:
            entry_times.pop(alpaca_sym, None)
            entry_prices.pop(alpaca_sym, None)
            peak_prices.pop(alpaca_sym, None)
            cooldowns.pop(alpaca_sym, None)
            logger.info(f"[SYNC] Removed stale local state: {alpaca_sym}")
        
        await save_state()
    except Exception as e:
        logger.warning(f"State sync failed: {e}")


async def run():
    global start_equity

    try:
        await asyncio.to_thread(init_db)
    except Exception as e:
        logger.warning(f"Database init failed (continuing without DB): {e}")

    load_state()
    # Sync internal state with actual Alpaca positions
    await sync_state_with_alpaca()
    logger.info("[BRAIN] Apex Committee Bot started — 4-brain ensemble")
    logger.info(f"⚖️  Brain weights: Transformer=50% | Quant=30% | Momentum=20%")
    logger.info(f"🛡️  Sentinel active — veto threshold ATR>{6}%")
    logger.info(f"📋 Symbols: {SYMBOLS}")

    try:
        await send_discord_alert(
            title="[BRAIN] Apex Committee Bot Started",
            description=(
                "**Brains:** Transformer (50%) + Quant (30%) + Momentum (20%)\n"
                "**Sentinel:** Active — vetoes on volatility/anomaly\n"
                f"**Symbols:** {', '.join(SYMBOLS)}\n"
                "**Confidence sizing:** 51%->tiny | 75%->medium | 90%->large"
            ),
            color=0x7B2FBE,
        )
    except Exception as e:
        logger.warning(f"Discord startup alert failed: {e}")

    while True:
        cycle_start = time.time()
        try:
            await write_heartbeat()

            # ── Cancel stale orders from previous cycle ──────────────────────
            # Prevents accumulation of unfilled limit orders that tie up buying
            # power and cause the bot to sit idle with pending orders.
            await cancel_stale_orders()
            # Brief delay to let Alpaca API propagate cancellations
            # (frees up qty_available and buying_power before we query them)
            await asyncio.sleep(0.5)

            # ── Account state ──────────────────────────────────────────────
            equity, buying_power, cash = await get_account_state()
            if start_equity is None:
                start_equity = equity

            try:
                await asyncio.to_thread(report_equity, BOT_NAME, equity)
            except Exception:
                pass  # non-critical

            # ── Fetch all positions ────────────────────────────────────────
            current_positions = await get_all_positions()

            # ── Parallel OHLCV fetch ─────────────────────────────────────────
            # Fetch all symbols' OHLCV data concurrently to avoid sequential
            # network latency (3 symbols × ~200ms = ~600ms -> ~200ms with gather)
            ohlcv_data = await asyncio.gather(*[get_ohlcv(s) for s in SYMBOLS])

            # ── Parallel indicator computation ───────────────────────────────
            # compute_indicators (RSI, MACD, BB, EMA, ATR, vol, momentum) is
            # CPU-bound synchronous code (~13ms per symbol). Offload to threads
            # and gather concurrently to reduce from ~39ms sequential to ~13ms.
            indicator_results = await asyncio.gather(*[
                asyncio.to_thread(compute_indicators, df) if df is not None else None
                for df in ohlcv_data
            ])

            # ── Portfolio-level equity verification ────────────────────────
            # Compute equity using current market prices for accuracy.
            # Alpaca's equity includes unrealized PnL, so simple cash+market_value
            # can differ. Use current prices from indicators.
            price_by_symbol = {}
            for symbol, indicators in zip(SYMBOLS, indicator_results):
                if indicators is not None:
                    price_by_symbol[symbol] = indicators["price"]

            computed_equity = cash
            for sym, pdata in current_positions.items():
                qty = pdata.get("qty", 0.0)
                current_price = price_by_symbol.get(sym)
                if current_price and qty > 0:
                    computed_equity += qty * current_price
                else:
                    computed_equity += pdata.get("market_value", 0.0)

            if equity > 0 and abs(computed_equity - equity) / equity > 0.05:  # 5% threshold
                logger.warning(
                    f"[VERIFY] Equity mismatch: Alpaca=${equity:,.2f} "
                    f"computed=${computed_equity:,.2f} "
                    f"diff={(computed_equity - equity):,.2f} "
                    f"({(computed_equity - equity)/equity*100:+.2f}%)"
                )

            drawdown = (equity - start_equity) / start_equity if start_equity > 0 else 0.0

            logger.info(
                f"[CHART] Equity: ${equity:,.2f} | BP: ${buying_power:,.2f} | "
                f"Drawdown: {drawdown*100:.2f}% | Positions: {len(entry_times)}/{MAX_OPEN_POSITIONS}"
            )

            # Liquidate only when drawdown exceeds permitted loss threshold (negative value)
            # MAX_DRAWDOWN_STOP is a negative number (e.g., -0.10 = 10% loss limit)
            if drawdown <= MAX_DRAWDOWN_STOP and drawdown < 0:
                logger.error(f"[ALERT] MAX DRAWDOWN {drawdown*100:.1f}% — liquidating all")
                try:
                    await send_discord_alert(
                        title="[ALERT] EMERGENCY: Max Drawdown",
                        description=f"Drawdown: {drawdown*100:.1f}%\nAll positions liquidated.",
                        color=0xFF0000,
                    )
                except Exception:
                    pass
                await close_all_positions()
                break

            now = time.time()

            # ── Per-symbol loop ────────────────────────────────────────────
            for symbol, df, indicators in zip(SYMBOLS, ohlcv_data, indicator_results):
                try:
                    alpaca_sym = symbol
                    pos_data   = current_positions.get(alpaca_sym) or current_positions.get(normalize_symbol(symbol))
                    has_pos    = pos_data is not None and pos_data["qty"] > 0

                    if df is None or indicators is None:
                        logger.warning(f"No data for {symbol} — skipping")
                        continue
                    regime     = classify_regime(df, indicators)
                    price      = indicators["price"]

                    if price <= 0:
                        continue

                    logger.info(
                        f"─── {symbol} | ${price:.4f} | Regime: {regime} | "
                        f"RSI: {indicators['rsi']:.1f} | ATR: {indicators['atr_pct']:.2f}%"
                    )

                    # ── Attach df to snapshot (transformer brain needs it) ─────────
                    snapshot = MarketSnapshot(
                        symbol=symbol,
                        candles=[],  # removed redundant df->dict conversion (not used by any brain)
                        indicators=indicators,
                        regime=regime,
                        atr_pct=indicators["atr_pct"],
                        has_position=has_pos,
                        position_size=pos_data["qty"] if has_pos else 0.0,
                        entry_price=entry_prices.get(alpaca_sym),
                        equity=equity,
                        buying_power=buying_power,
                    )
                    snapshot.candles_df = df  # extra attr for transformer feature builder
                    # ── EXIT logic ─────────────────────────────────────────────
                    if has_pos:
                        lock = get_state_lock()
                        async with lock:
                            avg_entry  = entry_prices.get(alpaca_sym, pos_data["avg_entry"])
                            peak_price = peak_prices.get(alpaca_sym, avg_entry)

                            # Update peak price for trailing stop
                            if price > peak_price:
                                peak_prices[alpaca_sym] = price
                                peak_price = price

                            pnl_pct    = (price - avg_entry) / avg_entry if avg_entry > 0 else 0.0
                            entry_dt   = entry_times.get(alpaca_sym, datetime.now(timezone.utc))
                        held_h     = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600

                        # Calculate trailing stop price (FIXED: was previously undefined)
                        trailing_stop_price = peak_price * (1.0 - TRAILING_STOP_PCT)

                        # Dynamic time-decay stop loss (tightens after 1h and 2h)
                        effective_stop = STOP_LOSS_PCT
                        if held_h >= 2.0:
                            effective_stop *= 0.50  # Tighten by 50% after 2 hours
                        elif held_h >= 1.0:
                            effective_stop *= 0.75  # Tighten by 25% after 1 hour

                        exit_reason = None
                        if pnl_pct <= -effective_stop:
                            exit_reason = f"Stop loss {pnl_pct*100:.1f}% (decayed threshold: -{effective_stop*100:.2f}%)"
                        elif pnl_pct >= TAKE_PROFIT_PCT:
                            exit_reason = f"Take profit +{pnl_pct*100:.1f}%"
                        elif price < trailing_stop_price and pnl_pct > 0:
                            exit_reason = f"Trailing stop (peak ${peak_price:.4f} -> ${trailing_stop_price:.4f})"
                        elif held_h >= MAX_HOLD_HOURS:
                            exit_reason = f"Max hold {held_h:.1f}h | PnL {pnl_pct*100:+.1f}%"

                        if exit_reason:
                            logger.info(f"EXIT {symbol}: {exit_reason}")
                            exit_result = await close_position(symbol, pos_data, current_price=price)
                            if exit_result:
                                fill_price = exit_result["fill_price"]
                                exit_qty = exit_result["qty"]
                                exit_fee = exit_result["fee"]

                                # Calculate realized PnL in dollars (including fees)
                                gross_pnl = (fill_price - avg_entry) * exit_qty
                                buy_fee = avg_entry * exit_qty * FEE_RATE
                                total_fee = buy_fee + exit_fee
                                realized_pnl = gross_pnl - total_fee

                                # Record realized PnL in database
                                try:
                                    await asyncio.to_thread(
                                        record_realized_pnl, BOT_NAME, alpaca_sym, "SELL",
                                        avg_entry, fill_price, exit_qty,
                                        realized_pnl, gross_pnl, total_fee,
                                        exit_result.get("order_id")
                                    )
                                except Exception:
                                    pass

                                logger.info(
                                    f"Realized PnL {symbol}: "
                                    f"entry=${avg_entry:.4f}, exit=${fill_price:.4f}, "
                                    f"gross=${gross_pnl:.2f}, fees=${total_fee:.2f}, "
                                    f"net=${realized_pnl:.2f}"
                                )

                                if realized_pnl < 0:
                                    sentinel.register_loss(symbol)
                                else:
                                    sentinel.register_win(symbol)
                                # Sync buying_power from Alpaca after SELL
                                _, buying_power, _ = await get_account_state()
                                # Atomic cleanup of state under lock
                                async with lock:
                                    entry_times.pop(alpaca_sym, None)
                                    entry_prices.pop(alpaca_sym, None)
                                    peak_prices.pop(alpaca_sym, None)
                                await save_state()
                                try:
                                    await send_discord_alert(
                                        title=f"{'[BULL]' if realized_pnl<0 else '[GREEN]'} SOLD {symbol}",
                                        description=(
                                            f"**Exit price:** ${fill_price:.4f}\n"
                                            f"**Entry price:** ${avg_entry:.4f}\n"
                                            f"**PnL (net of fees):** ${realized_pnl:.2f} ({realized_pnl/(avg_entry * exit_qty if avg_entry * exit_qty > 0 else 1)*100:+.2f}%)\n"
                                            f"**Gross PnL:** ${gross_pnl:.2f}\n"
                                            f"**Fees:** ${total_fee:.2f}\n"
                                            f"**Reason:** {exit_reason}\n"
                                            f"**Regime:** {regime}"
                                        ),
                                        color=0xFF4444 if realized_pnl < 0 else 0x44FF44,
                                    )
                                except Exception:
                                    pass
                            continue

                        logger.info(
                            f"📌 HOLDING {symbol} | PnL: {pnl_pct*100:+.2f}% | "
                            f"Peak: ${peak_price:.4f} | Trail: ${trailing_stop_price:.4f}"
                        )
                        continue

                    # ── ENTRY logic ────────────────────────────────────────────

                    # Cooldown check
                    if now < cooldowns.get(alpaca_sym, 0):
                        remaining = int(cooldowns[alpaca_sym] - now)
                        logger.info(f"[WAIT] {symbol} on cooldown ({remaining}s remaining)")
                        continue

                    # Max positions check
                    if len(entry_times) >= MAX_OPEN_POSITIONS:
                        logger.info(f"🚫 Max {MAX_OPEN_POSITIONS} positions — skipping {symbol}")
                        continue

                    # ── Run the committee ──────────────────────────────────────
                    # Offloaded to threads: transformer_brain.decide() runs a
                    # PyTorch CPU forward pass (~500-900ms measured), which
                    # would otherwise block the event loop for the full
                    # duration -- starving heartbeat writes, Discord alerts,
                    # and processing of other symbols for the whole cycle.
                    decisions = list(await asyncio.gather(
                        asyncio.to_thread(transformer_brain.decide, snapshot),
                        asyncio.to_thread(quant_brain.decide, snapshot),
                        asyncio.to_thread(momentum_brain.decide, snapshot),
                    ))
                    committee = run_committee(snapshot, decisions)

                    # ── Sentinel check ────────────────────────────────────────
                    sentinel_report = sentinel.check(snapshot, committee)

                    if sentinel_report.veto:
                        logger.warning(f"🛡️  SENTINEL VETO {symbol}: {sentinel_report.reason}")
                        continue

                    # ── Execute ───────────────────────────────────────────────
                    if committee.action != "BUY":
                        logger.info(
                            f"⏭️  {symbol}: committee={committee.action} "
                            f"score={committee.confidence:.3f} regime={committee.regime}"
                        )
                        continue

                    # ── L2 Orderbook Whale Gate ──────────────────────────────────
                    ob_ratio = await get_orderbook_ratio(symbol)
                    if ob_ratio is not None and ob_ratio < MIN_BID_ASK_RATIO:
                        logger.warning(
                            f"🐋 WHALE GATE VETO {symbol}: Bid/Ask depth ratio {ob_ratio:.2f} < min {MIN_BID_ASK_RATIO}"
                        )
                        continue

                    trade_value = calculate_trade_size(
                        equity, committee.confidence, sentinel_report.cap_pct
                    )
                    if trade_value <= 0:
                        continue

                    if buying_power < trade_value:
                        logger.warning(f"Insufficient BP (${buying_power:.2f}) for ${trade_value:.2f}")
                        continue

                    qty = trade_value / price
                    logger.info(
                        f"BUY {symbol} ${trade_value:.2f} @ ${price:.4f} "
                        f"| Committee: {committee.confidence:.3f} | Regime: {committee.regime}"
                    )

                    success_result = await place_order(symbol, OrderSide.BUY, qty, price)
                    if success_result and success_result.get("success"):
                        fill_price = success_result.get("fill_price") or price
                        filled_qty = success_result.get("qty", qty)
                        filled_value = success_result.get("trade_value", filled_qty * fill_price)
                        # Sync buying_power from Alpaca to stay accurate
                        _, buying_power, _ = await get_account_state()
                        # Atomic update of all shared state under lock
                        lock = get_state_lock()
                        async with lock:
                            entry_times[alpaca_sym]  = datetime.now(timezone.utc)
                            entry_prices[alpaca_sym] = fill_price
                            peak_prices[alpaca_sym]  = fill_price
                            cooldowns[alpaca_sym]    = now + COOLDOWN_SECONDS_BUY
                        await save_state()

                        # Format vote breakdown for Discord
                        vote_lines = "\n".join(
                            f"• **{d.brain}** ({d.action} {d.confidence:.2f}): {d.reason}"
                            for d in decisions
                        )
                        try:
                            await send_discord_alert(
                                title=f"[GREEN] BOUGHT {symbol}",
                                description=(
                                    f"**Price:** ${price:.4f}\n"
                                    f"**Fill price:** ${fill_price:.4f}\n"
                                    f"**Fee:** ${success_result.get('fee', 0):.2f}\n"
                                    f"**Size:** ${filled_value:.2f}\n"
                                    f"**Committee score:** {committee.confidence:.3f}\n"
                                    f"**Regime:** {committee.regime}\n"
                                    f"**Sentinel:** {sentinel_report.reason}\n\n"
                                    f"**Brain votes:**\n{vote_lines}"
                                ),
                                color=0x7B2FBE,
                            )
                        except Exception:
                            pass
                    else:
                        logger.warning(f"BUY {symbol} order failed or unfilled")

                except Exception as symbol_err:
                    logger.error(f"[!]️ Error processing {symbol}: {symbol_err}")
                    continue

        except Exception as e:
            logger.error(f"[!]️ Main loop error: {e}", exc_info=True)
            await asyncio.sleep(30)
            continue

        elapsed = time.time() - cycle_start
        sleep_t = max(0, SLEEP_PER_LOOP - elapsed)
        logger.info(f"💤 Cycle done in {elapsed:.1f}s — sleeping {sleep_t:.0f}s")
        await asyncio.sleep(sleep_t)


if __name__ == "__main__":
    asyncio.run(run())
