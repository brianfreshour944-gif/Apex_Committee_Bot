
import asyncio
import os
from datetime import datetime, timezone
from config import logger, trading_client, HEARTBEAT_PATH, FEE_RATE, SELL_SLIPPAGE_BUFFER


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
    Returns None on failure.
    """
    try:
        alpaca_sym = normalize_symbol(symbol)

        if pos_data is None:
            positions = await asyncio.to_thread(trading_client.get_all_positions)
            pos = None
            for p in positions:
                if p.symbol == alpaca_sym:
                    pos = p
                    break
            if pos is None:
                logger.warning(f"Cannot close {symbol}: position not found")
                return None
            qty = float(pos.qty)
            avg_entry = float(pos.avg_entry_price)
        else:
            qty = pos_data.get("qty", 0.0)
            avg_entry = pos_data.get("avg_entry", 0.0)

        if qty <= 0:
            logger.warning(f"Cannot close {symbol}: qty={qty}")
            return None

        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        limit_price = current_price * (1.0 - SELL_SLIPPAGE_BUFFER) if current_price else avg_entry
        order_data = LimitOrderRequest(
            symbol=alpaca_sym,
            qty=qty,
            limit_price=limit_price,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )

        order = await asyncio.to_thread(trading_client.submit_order, order_data=order_data)

        fill_price = float(order.filled_avg_price) if order.filled_avg_price else avg_entry
        fee = (qty * fill_price) * FEE_RATE

        logger.info(f"Closed: {symbol} qty={qty} @ ${fill_price:.4f} | fee=${fee:.2f}")

        return {
            "fill_price": fill_price,
            "qty": qty,
            "fee": fee,
            "order_id": order.id,
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
