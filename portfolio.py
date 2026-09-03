
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
    try:
        alpaca_sym = normalize_symbol(symbol)

        # Always re-fetch the latest position to avoid stale qty
        # from the cached current_positions in main.py (transformer
        # brain runs ~500ms, during which positions may change).
        positions = await asyncio.to_thread(trading_client.get_all_positions)
        pos = None
        for p in positions:
            if p.symbol == alpaca_sym or p.symbol == symbol:
                pos = p
                break

        if pos is None:
            logger.warning(f"Close failed {symbol}: position no longer exists")
            return None

        qty_available = float(pos.qty_available or pos.qty)
        qty_total = float(pos.qty)
        avg_entry = float(pos.avg_entry_price)

        # qty_available may be less than qty if open orders block the position.
        # Using qty_available prevents 40310000 (insufficient balance) errors.
        if qty_total > 0 and qty_available < qty_total * 0.999:
            logger.warning(
                f"Close {symbol}: qty_available={qty_available:.8f} < "
                f"qty={qty_total:.8f} (open orders may still be cancelling)"
            )
        qty = math.floor(qty_available * 1e8) / 1e8

        if qty <= 0:
            logger.warning(f"Cannot close {symbol}: qty={qty}")
            return None

        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        limit_price = _sanitize_price(current_price * (1.0 - SELL_SLIPPAGE_BUFFER)) if current_price else avg_entry
        order_data = LimitOrderRequest(
            symbol=symbol,
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
    except Exception as e:
        logger.error(f"Close failed {symbol}: {e}")
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
