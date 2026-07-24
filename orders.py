# orders.py — Order placement with Decimal price sanitization.

import math
from decimal import Decimal, ROUND_DOWN

from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from config import logger, trading_client, BOT_NAME
from database import record_trade


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
) -> bool:
    try:
        if side == OrderSide.BUY:
            raw_limit   = price * 1.001 if price else None
            limit_price = _sanitize_price(raw_limit) if raw_limit else None
            order_data  = LimitOrderRequest(
                symbol=symbol, qty=qty, side=side,
                time_in_force=TimeInForce.GTC, limit_price=limit_price,
            )
        else:
            qty        = math.floor(qty * 1e8) / 1e8
            order_data = MarketOrderRequest(
                symbol=symbol, qty=qty, side=side,
                time_in_force=TimeInForce.GTC,
            )

        order = trading_client.submit_order(order_data=order_data)
        record_trade(BOT_NAME, symbol, side.value, qty, price, order_id=order.id)
        logger.info(f"✅ {side.value.upper()} {symbol} qty={qty:.6f} @ ~${price:.4f}")
        return True

    except Exception as e:
        logger.error(f"❌ Order failed ({side.value} {symbol}): {e}")
        return False
