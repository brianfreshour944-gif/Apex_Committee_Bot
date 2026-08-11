
import asyncio
import math
from decimal import Decimal, ROUND_DOWN

from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from config import logger, trading_client, BOT_NAME, FEE_RATE, SELL_SLIPPAGE_BUFFER
from database import record_trade


def _normalize_symbol(symbol: str) -> str:
    return symbol.replace("/", "")


async def cancel_stale_orders(symbol: str | None = None):
    """Cancel all open orders for a symbol (or all symbols if None).

    Called at the start of each cycle to clean up unfilled limit orders
    from previous cycles. This prevents order accumulation that ties up
    buying power and causes the 'new' order pile-up.
    """
    try:
        if symbol is not None:
            alpaca_sym = _normalize_symbol(symbol)
            orders = await asyncio.to_thread(
                trading_client.get_orders,
                status="open",
                symbols=[alpaca_sym],
            )
        else:
            orders = await asyncio.to_thread(
                trading_client.get_orders, status="open"
            )

        cancelled = 0
        for order in orders:
            try:
                await asyncio.to_thread(trading_client.cancel_order, order.id)
                cancelled += 1
            except Exception:
                pass

        if cancelled > 0:
            logger.info(f"Cancelled {cancelled} stale open order(s)")
    except Exception as e:
        logger.warning(f"Order cancellation failed: {e}")


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


async def place_order(
    symbol: str, side: OrderSide, qty: float, price: float = None
) -> dict | None:
    """
    Places an order and returns a dict with execution details.

    Returns dict with:
        - success: bool
        - qty: float (filled quantity)
        - fill_price: float or None (actual fill price)
        - fee: float (estimated fee)
        - order_id: str
    Returns None on failure.
    """
    try:
        if side == OrderSide.BUY:
            # BUY limit price should be below market to ensure fill (not above)
            raw_limit   = price * (1.0 - SELL_SLIPPAGE_BUFFER) if price else None
            limit_price = _sanitize_price(raw_limit) if raw_limit else None
            order_data  = LimitOrderRequest(
                symbol=symbol, qty=qty, side=side,
                time_in_force=TimeInForce.GTC, limit_price=limit_price,
            )
        else:
            qty        = math.floor(qty * 1e8) / 1e8
            raw_limit  = price * (1.0 - SELL_SLIPPAGE_BUFFER) if price else None
            limit_price = _sanitize_price(raw_limit) if raw_limit else None
            order_data = LimitOrderRequest(
                symbol=symbol, qty=qty, side=side,
                time_in_force=TimeInForce.GTC, limit_price=limit_price,
            )

        # Offload blocking Alpaca HTTP + DB calls to threads
        order = await asyncio.to_thread(trading_client.submit_order, order_data=order_data)

        # Extract actual fill information
        fill_price = float(order.filled_avg_price) if order.filled_avg_price else None
        filled_qty = float(order.filled_qty) if order.filled_qty else qty
        fee = (filled_qty * (fill_price or price or 0)) * FEE_RATE if fill_price else (qty * (price or 0)) * FEE_RATE

        # Record with actual fill price and fee
        trade_price = fill_price if fill_price else price
        await asyncio.to_thread(record_trade, BOT_NAME, symbol, side.value, qty,
                                price, fill_price=trade_price, fee=fee,
                                order_id=order.id)
        logger.info(f"{'BUY' if side == OrderSide.BUY else 'SELL'} {symbol} qty={filled_qty:.6f} @ ${trade_price:.4f} | fee=${fee:.2f}")

        return {
            "success": True,
            "qty": filled_qty,
            "fill_price": trade_price,
            "fee": fee,
            "order_id": order.id,
        }

    except Exception as e:
        logger.error(f"Order failed ({side.value} {symbol}): {e}")
        return None
