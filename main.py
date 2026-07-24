#!/usr/bin/env python3
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
#   4. Committee tallies → winning action needs >60% weighted score
#   5. Sentinel checks for danger → may veto or cap size
#   6. Position sized by confidence tier (51% → tiny, 90% → large)
#   7. Order placed

import asyncio
import os
import time
from datetime import datetime, timezone

from alpaca.trading.enums import OrderSide

from config import (
    logger, BOT_NAME, SYMBOLS,
    MAX_OPEN_POSITIONS, MAX_DRAWDOWN_STOP,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, TRAILING_STOP_PCT, MAX_HOLD_HOURS,
    COOLDOWN_SECONDS_BUY, SLEEP_PER_LOOP, HEARTBEAT_PATH,
    STATE_FILE_PATH, MIN_BID_ASK_RATIO,
)
from data_feed import get_ohlcv, compute_indicators, get_account_state, get_all_positions, get_orderbook_ratio
from regime import classify_regime
from brains.transformer import transformer_brain
from brains.quant import quant_brain
from brains.momentum import momentum_brain
from committee import run_committee
from sentinel import sentinel
from position_sizing import calculate_trade_size
from orders import place_order
from portfolio import normalize_symbol, close_position, close_all_positions, write_heartbeat
from database import init_db, report_equity
from notifications import send_discord_alert
from models import MarketSnapshot

# ── Global state ───────────────────────────────────────────────────────────────
entry_times:   dict  = {}    # {alpaca_sym: datetime}
entry_prices:  dict  = {}    # {alpaca_sym: float}
peak_prices:   dict  = {}    # {alpaca_sym: float}  — for trailing stop
cooldowns:     dict  = {}    # {alpaca_sym: float}  — timestamp
start_equity:  float | None = None

import json

def save_state():
    try:
        data = {
            "entry_times":  {k: v.isoformat() for k, v in entry_times.items()},
            "entry_prices": entry_prices,
            "peak_prices":  peak_prices,
            "cooldowns":    cooldowns,
        }
        tmp_path = f"{STATE_FILE_PATH}.tmp"
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, STATE_FILE_PATH)
    except Exception as e:
        logger.warning(f"State save failed: {e}")


def load_state():
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
        logger.info("💾 Restored persistent state from disk")
    except Exception as e:
        logger.warning(f"State load failed: {e}")



async def run():
    global start_equity

    init_db()
    load_state()
    logger.info("🧠 Apex Committee Bot started — 4-brain ensemble")
    logger.info(f"⚖️  Brain weights: Transformer=50% | Quant=30% | Momentum=20%")
    logger.info(f"🛡️  Sentinel active — veto threshold ATR>{6}%")
    logger.info(f"📋 Symbols: {SYMBOLS}")

    await send_discord_alert(
        title="🧠 Apex Committee Bot Started",
        description=(
            "**Brains:** Transformer (50%) + Quant (30%) + Momentum (20%)\n"
            "**Sentinel:** Active — vetoes on volatility/anomaly\n"
            f"**Symbols:** {', '.join(SYMBOLS)}\n"
            "**Confidence sizing:** 51%→tiny | 75%→medium | 90%→large"
        ),
        color=0x7B2FBE,
    )

    while True:
        cycle_start = time.time()
        try:
            write_heartbeat()

            # ── Account state ──────────────────────────────────────────────────
            equity, buying_power = get_account_state()
            if start_equity is None:
                start_equity = equity
            report_equity(BOT_NAME, equity)

            drawdown = (equity - start_equity) / start_equity if start_equity > 0 else 0.0

            logger.info(
                f"📊 Equity: ${equity:,.2f} | BP: ${buying_power:,.2f} | "
                f"Drawdown: {drawdown*100:.2f}% | Positions: {len(entry_times)}/{MAX_OPEN_POSITIONS}"
            )

            if drawdown <= MAX_DRAWDOWN_STOP:
                logger.error(f"🚨 MAX DRAWDOWN {drawdown*100:.1f}% — liquidating all")
                await send_discord_alert(
                    title="🚨 EMERGENCY: Max Drawdown",
                    description=f"Drawdown: {drawdown*100:.1f}%\nAll positions liquidated.",
                    color=0xFF0000,
                )
                close_all_positions()
                break

            # ── Fetch all positions ────────────────────────────────────────────
            current_positions = get_all_positions()
            now               = time.time()

            # ── Per-symbol loop ────────────────────────────────────────────────
            for symbol in SYMBOLS:
                alpaca_sym = normalize_symbol(symbol)
                pos_data   = current_positions.get(alpaca_sym)
                has_pos    = pos_data is not None and pos_data["qty"] > 0

                # ── Fetch data ─────────────────────────────────────────────────
                df = await get_ohlcv(symbol)
                if df is None:
                    logger.warning(f"⚠️ No data for {symbol} — skipping")
                    continue

                indicators = compute_indicators(df)
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
                    candles=df.reset_index().to_dict("records"),
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

                # ── EXIT logic ─────────────────────────────────────────────────
                if has_pos:
                    avg_entry  = entry_prices.get(alpaca_sym, pos_data["avg_entry"])
                    peak_price = peak_prices.get(alpaca_sym, avg_entry)
                    pnl_pct    = (price - avg_entry) / avg_entry
                    entry_dt   = entry_times.get(alpaca_sym, datetime.now(timezone.utc))
                    held_h     = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600

                    # Dynamic time-decay stop loss (tightens after 1h and 2h)
                    effective_stop = STOP_LOSS_PCT
                    if held_h >= 2.0:
                        effective_stop *= 0.50  # Tighten by 50% after 2 hours
                    elif held_h >= 1.0:
                        effective_stop *= 0.75  # Tighten by 25% after 1 hour

                    exit_reason = None
                    if pnl_pct <= -effective_stop:
                        exit_reason = f"🛑 Stop loss {pnl_pct*100:.1f}% (decayed threshold: -{effective_stop*100:.2f}%)"
                    elif pnl_pct >= TAKE_PROFIT_PCT:
                        exit_reason = f"✅ Take profit +{pnl_pct*100:.1f}%"
                    elif price < trailing_stop_price and pnl_pct > 0:
                        exit_reason = f"📉 Trailing stop (peak ${peak_price:.4f} → ${trailing_stop_price:.4f})"
                    elif held_h >= MAX_HOLD_HOURS:
                        exit_reason = f"⏰ Max hold {held_h:.1f}h | PnL {pnl_pct*100:+.1f}%"

                    if exit_reason:
                        logger.info(f"🔴 EXIT {symbol}: {exit_reason}")
                        success = close_position(symbol)
                        if success:
                            if pnl_pct < 0:
                                sentinel.register_loss()
                            else:
                                sentinel.register_win()
                            entry_times.pop(alpaca_sym, None)
                            entry_prices.pop(alpaca_sym, None)
                            peak_prices.pop(alpaca_sym, None)
                            save_state()
                            await send_discord_alert(
                                title=f"{'🔴' if pnl_pct<0 else '🟢'} SOLD {symbol}",
                                description=(
                                    f"**Price:** ${price:.4f}\n"
                                    f"**PnL:** {pnl_pct*100:+.2f}%\n"
                                    f"**Reason:** {exit_reason}\n"
                                    f"**Regime:** {regime}"
                                ),
                                color=0xFF4444 if pnl_pct < 0 else 0x44FF44,
                            )
                        continue

                    logger.info(
                        f"📌 HOLDING {symbol} | PnL: {pnl_pct*100:+.2f}% | "
                        f"Peak: ${peak_price:.4f} | Trail: ${trailing_stop_price:.4f}"
                    )
                    continue

                # ── ENTRY logic ────────────────────────────────────────────────

                # Cooldown check
                if now < cooldowns.get(alpaca_sym, 0):
                    remaining = int(cooldowns[alpaca_sym] - now)
                    logger.info(f"⏳ {symbol} on cooldown ({remaining}s remaining)")
                    continue

                # Max positions check
                if len(entry_times) >= MAX_OPEN_POSITIONS:
                    logger.info(f"🚫 Max {MAX_OPEN_POSITIONS} positions — skipping {symbol}")
                    continue

                # ── Run the committee ──────────────────────────────────────────
                decisions = [
                    transformer_brain.decide(snapshot),
                    quant_brain.decide(snapshot),
                    momentum_brain.decide(snapshot),
                ]
                committee = run_committee(snapshot, decisions)

                # ── Sentinel check ─────────────────────────────────────────────
                sentinel_report = sentinel.check(snapshot, committee)

                if sentinel_report.veto:
                    logger.warning(f"🛡️  SENTINEL VETO {symbol}: {sentinel_report.reason}")
                    continue

                # ── Execute ────────────────────────────────────────────────────
                if committee.action != "BUY":
                    logger.info(
                        f"⏭️  {symbol}: committee={committee.action} "
                        f"score={committee.confidence:.3f} regime={committee.regime}"
                    )
                    continue

                # ── L2 Orderbook Whale Gate ────────────────────────────────────
                ob_ratio = get_orderbook_ratio(symbol)
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
                    logger.warning(f"🚫 Insufficient BP (${buying_power:.2f}) for ${trade_value:.2f}")
                    continue

                qty = trade_value / price
                logger.info(
                    f"🟢 BUY {symbol} ${trade_value:.2f} @ ${price:.4f} "
                    f"| Committee: {committee.confidence:.3f} | Regime: {committee.regime}"
                )

                success = await place_order(symbol, OrderSide.BUY, qty, price)
                if success:
                    entry_times[alpaca_sym]  = datetime.now(timezone.utc)
                    entry_prices[alpaca_sym] = price
                    peak_prices[alpaca_sym]  = price
                    cooldowns[alpaca_sym]    = now + COOLDOWN_SECONDS_BUY
                    buying_power            -= trade_value
                    save_state()

                    # Format vote breakdown for Discord
                    vote_lines = "\n".join(
                        f"• **{d.brain}** ({d.action} {d.confidence:.2f}): {d.reason}"
                        for d in decisions
                    )
                    await send_discord_alert(
                        title=f"🟢 BOUGHT {symbol}",
                        description=(
                            f"**Price:** ${price:.4f}\n"
                            f"**Size:** ${trade_value:.2f}\n"
                            f"**Committee score:** {committee.confidence:.3f}\n"
                            f"**Regime:** {committee.regime}\n"
                            f"**Sentinel:** {sentinel_report.reason}\n\n"
                            f"**Brain votes:**\n{vote_lines}"
                        ),
                        color=0x7B2FBE,
                    )

        except Exception as e:
            logger.error(f"⚠️ Main loop error: {e}")
            await asyncio.sleep(30)
            continue

        elapsed = time.time() - cycle_start
        sleep_t = max(0, SLEEP_PER_LOOP - elapsed)
        logger.info(f"💤 Cycle done in {elapsed:.1f}s — sleeping {sleep_t:.0f}s")
        await asyncio.sleep(sleep_t)


if __name__ == "__main__":
    asyncio.run(run())
