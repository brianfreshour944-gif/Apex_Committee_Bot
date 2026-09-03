
import asyncio
import math
import os
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from config import logger, trading_client, HEARTBEAT_PATH, FEE_RATE, SELL_SLIPPAGE_BUFFER


def _sanitize_price(price: float) -> float:
    d = Decimal(str(price))
    if price >= 1.0:
        return float(d.quantize(Decimal('0.01'), rounding=ROUND_DOWN))
    elif price >= 0.01:
        return float(d.quantize(Decimal('0.0001'), rounding=ROUND_DOWN))
    elif price >= 0.0001:
        return float(d.quantize(Decimal('0.000001'), rounding=ROUND_DOWN))
    else:
        return float(d.quantize(Decimal('0.00000001'), rounding=ROUND_DOWN))


def normalize_symbol(symbol: str) -> str:
    return symbol.replace("/", "")


async def _cancel_orders_for_symbol(symbol: str):
    """Cancel all open orders for a specific symbol to free up the position."""
    try:
        from alpaca.trading.enums import OrderSide
        alpaca_sym = normalize_symbol(symbol)
        all_orders = await asyncio.to_thread(trading_client.get_orders)
        for order in all_orders:
            if order.status in ("new", "partially_filled", "accepted", "pending_new"):
                if order.symbol == symbol or order.symbol == alpaca_sym:
                    try:
                        await asyncio.to_thread(trading_client.cancel_order, order.id)
                    except Exception:
                        pass
        # Give Alpaca a moment to propagate cancellations
        await asyncio.sleep(0.5)
    except Exception as e:
        logger.warning(f"Cancel orders for {symbol} failed: {e}")


async def close_position(symbol: str, pos_data: dict | None = None,
                         current_price: float | None = None) -> dict | None:
    """
    Closes a position and returns execution details.

    Args:
        symbol: The symbol to close (e.g., "BTC/USD")
        pos_data: Optional position data dict from get_all_positions().
                  If None, fetches positions from Alpaca.
                  Keys expected: qty, avg_entry
        current_price: Current market price for SELL limit order with
                  slippage buffer protection. If None and pos_data is None,
                  falls back to avg_entry as limit price.

    Returns dict with:
        - fill_price: float (actual fill price)
        - qty: float (quantity closed)
        - fee: float (estimated fee)
        - order_id: str
        - trade_value: float (actual dollar value filled)
    Returns None on failure.
    """
    from alpaca.trading.requests import LimitOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.common.exceptions import APIError

    alpaca_sym = normalize_symbol(symbol)

    for attempt in range(3):
        try:
            # Always re-fetch the latest position to avoid stale qty
            positions = await asyncio.to_thread(trading_client.get_all_positions)
            pos = None
            for p in positions:
                if p.symbol == alpaca_sym or p.symbol == symbol:
                    pos = p
                    break

            if pos is None:
                logger.warning(f"Close failed {symbol}: position no longer exists")
                return None

            qty_total = float(pos.qty)
            # qty_available may be None for crypto; fall back to qty
            qty_available = float(pos.qty_available or pos.qty)
            avg_entry = float(pos.avg_entry_price)

            # Use qty_available if it's meaningfully less than qty
            # (indicates open orders still blocking the position)
            if qty_available > 0 and qty_available < qty_total:
                logger.info(
                    f"Close {symbol}: using qty_available={qty_available:.8f} "
                    f"< qty={qty_total:.8f}"
                )
            use_qty = min(qty_available, qty_total)

            # Floor to 8 decimal places to avoid float precision issues
            qty = math.floor(use_qty * 1e8) / 1e8

            # On retries, reduce qty slightly to account for any
            # remaining precision gaps or partially-cancelled orders
            if attempt > 0:
                qty = qty * (0.999 - attempt * 0.001)
                qty = math.floor(qty * 1e8) / 1e8

            if qty <= 0:
                logger.warning(f"Cannot close {symbol}: qty={qty}")
                return None

            limit_price = _sanitize_price(current_price * (1.0 - SELL_SLIPPAGE_BUFFER)) if current_price else avg_entry
            order_data = LimitOrderRequest(
                symbol=pos.symbol,
                qty=qty,
                limit_price=limit_price,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
            )

            order = await asyncio.to_thread(trading_client.submit_order, order_data=order_data)

            # Poll for fill status (limit order may fill asynchronously)
            fill_price = None
            filled_qty = 0.0
            for _ in range(10):  # up to 10 seconds
                await asyncio.sleep(1)
                order = await asyncio.to_thread(trading_client.get_order_by_id, order.id)
                if order.filled_qty and float(order.filled_qty) > 0:
                    fill_price = float(order.filled_avg_price) if order.filled_avg_price else avg_entry
                    filled_qty = float(order.filled_qty)
                    break

            if filled_qty == 0.0:
                logger.warning(f"SELL {symbol} not filled after wait, cancelling")
                try:
                    await asyncio.to_thread(trading_client.cancel_order, order.id)
                except Exception:
                    pass
                return None

            fee = (filled_qty * fill_price) * FEE_RATE

            logger.info(f"Closed: {symbol} qty={filled_qty:.6f} @ ${fill_price:.4f} | fee=${fee:.2f}")

            return {
                "fill_price": fill_price,
                "qty": filled_qty,
                "fee": fee,
                "order_id": order.id,
                "trade_value": filled_qty * fill_price,
            }

        except APIError as e:
            error_code = e.code if hasattr(e, 'code') else None
            error_msg = e.message if hasattr(e, 'message') else str(e)
            error_status = e.status_code if hasattr(e, 'status_code') else None
            logger.warning(
                f"Close {symbol} attempt {attempt+1} failed: "
                f"status={error_status} code={error_code} msg={error_msg}"
            )
            if error_status == 403 and error_code == 10000:
                # insufficient balance: cancel stale orders, wait, retry
                await _cancel_orders_for_symbol(symbol)
                await asyncio.sleep(2 * (attempt + 1))
                continue
            else:
                # Non-retryable error
                logger.error(f"Close failed {symbol}: {error_msg}")
                return None
        except Exception as e:
            logger.error(f"Close failed {symbol}: {e}")
            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            return None

    logger.error(f"Close {symbol} failed after 3 retries")
    return None


async def close_all_positions():
    try:
        await asyncio.to_thread(trading_client.close_all_positions, cancel_orders=True)
        logger.warning("All positions closed")
    except Exception as e:
        logger.error(f"Emergency close failed: {e}")


async def write_heartbeat():
    try:
        path = HEARTBEAT_PATH
        dirn = os.path.dirname(path)
        if dirn:
            os.makedirs(dirn, exist_ok=True)
        # Offload file I/O to a thread to avoid blocking the event loop
        def _write():
            with open(path, "w") as f:
                f.write(datetime.now(timezone.utc).isoformat())
        await asyncio.to_thread(_write)
    except Exception as e:
        logger.error(f"Heartbeat failed: {e}")
