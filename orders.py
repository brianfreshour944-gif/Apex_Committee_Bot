
import asyncio
import math
from decimal import Decimal, ROUND_DOWN

from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.common.exceptions import APIError

from config import logger, trading_client, BOT_NAME, FEE_RATE, SELL_SLIPPAGE_BUFFER
from database import record_trade


def _normalize_symbol(symbol: str) -> str:
    return symbol.replace("/", "").replace("-", "").replace("_", "").upper()


def _extract_api_error(e: Exception) -> tuple[int | None, str | None, int | None]:
    """Safely extract status_code, code, and message from an APIError."""
    error_code = None
    error_msg = str(e)
    error_status = None
    try:
        error_code = e.code
    except Exception:
        pass
    try:
        error_msg = e.message
    except Exception:
        pass
    try:
        error_status = e.status_code
    except Exception:
        pass
    return error_status, error_code, error_msg


async def cancel_stale_orders(symbol: str | None = None):
    """Cancel all open orders for a symbol (or all symbols if None).

    Called at the start of each cycle to clean up unfilled limit orders
    from previous cycles. This prevents order accumulation that ties up
    buying power and causes the 'new' order pile-up.
    """
    try:
        # Alpaca SDK get_orders() doesn't accept status parameter; filter manually
        if symbol is not None:
            alpaca_sym = _normalize_symbol(symbol)
            all_orders = await asyncio.to_thread(trading_client.get_orders)
            orders = [o for o in all_orders if (o.symbol == alpaca_sym or o.symbol == symbol) and o.status in ("new", "partially_filled", "accepted", "pending_new")]
        else:
            all_orders = await asyncio.to_thread(trading_client.get_orders)
            orders = [o for o in all_orders if o.status in ("new", "partially_filled", "accepted", "pending_new")]
        
        cancelled = 0
        for order in orders:
            try:
                await asyncio.to_thread(trading_client.cancel_order_by_id, order.id)
                cancelled += 1
                logger.info(f"Cancelled stale order {order.id} for {symbol or 'all'} (symbol={order.symbol})")
            except Exception as e:
                logger.warning(f"Failed to cancel stale order {order.id}: {e}")
        
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
    from alpaca.common.exceptions import APIError

    def _extract_api_error(e: Exception) -> tuple[int | None, str | None, int | None]:
        error_code = None
        error_msg = str(e)
        error_status = None
        try:
            error_code = e.code
        except Exception:
            pass
        try:
            error_msg = e.message
        except Exception:
            pass
        try:
            error_status = e.status_code
        except Exception:
            pass
        return error_status, error_code, error_msg

    for attempt in range(3):
        try:
            if side == OrderSide.BUY:
                # BUY limit price should be below market to ensure fill (not above)
                raw_limit = price * (1.0 - SELL_SLIPPAGE_BUFFER) if price else None
                limit_price = _sanitize_price(raw_limit) if raw_limit else None
                order_data = LimitOrderRequest(
                    symbol=symbol, qty=qty, side=side,
                    time_in_force=TimeInForce.GTC, limit_price=limit_price,
                )
            else:
                qty = math.floor(qty * 1e8) / 1e8
                raw_limit = price * (1.0 - SELL_SLIPPAGE_BUFFER) if price else None
                limit_price = _sanitize_price(raw_limit) if raw_limit else None
                order_data = LimitOrderRequest(
                    symbol=symbol, qty=qty, side=side,
                    time_in_force=TimeInForce.GTC, limit_price=limit_price,
                )

            logger.debug(
                f"Place {side.value} {symbol}: qty={qty} limit_price={limit_price} symbol={symbol}"
            )

            order = await asyncio.to_thread(trading_client.submit_order, order_data=order_data)

            # Extract actual fill information
            fill_price = float(order.filled_avg_price) if order.filled_avg_price else None
            filled_qty = float(order.filled_qty) if order.filled_qty else 0.0

            # If order not filled yet (limit order), wait briefly and check fill status
            if filled_qty == 0.0 and fill_price is None:
                # Poll for fill status (limit order may fill asynchronously)
                for _ in range(5):  # up to 5 seconds
                    await asyncio.sleep(1)
                    order = await asyncio.to_thread(trading_client.get_order_by_id, order.id)
                    if order.filled_qty and float(order.filled_qty) > 0:
                        fill_price = float(order.filled_avg_price)
                        filled_qty = float(order.filled_qty)
                        break

            # If still no fill, return failure (caller will handle)
            if filled_qty == 0.0:
                logger.warning(f"Order {order.id} not filled after wait")
                return None

            fee = (filled_qty * fill_price) * FEE_RATE

            # Record with actual fill price and fee
            await asyncio.to_thread(record_trade, BOT_NAME, symbol, side.value, filled_qty,
                                    fill_price, fill_price=fill_price, fee=fee,
                                    order_id=order.id)
            logger.info(f"{'BUY' if side == OrderSide.BUY else 'SELL'} {symbol} qty={filled_qty:.6f} @ ${fill_price:.4f} | fee=${fee:.2f}")

            return {
                "success": True,
                "qty": filled_qty,
                "fill_price": fill_price,
                "fee": fee,
                "order_id": order.id,
                "trade_value": filled_qty * fill_price,  # actual dollar value filled
            }

        except APIError as e:
            error_status = None
            error_code = None
            error_msg = str(e)
            try:
                error_status = e.status_code
            except Exception:
                pass
            try:
                error_code = e.code
            except Exception:
                pass
            try:
                error_msg = e.message
            except Exception:
                pass

            logger.warning(
                f"Place {side.value} {symbol} attempt {attempt+1} failed: "
                f"status={error_status} code={error_code} msg={error_msg}"
            )

            # Check for insufficient balance (buying power)
            is_insufficient = (
                (error_status == 403 and str(error_code) == "10000") or
                (error_msg and "insufficient" in error_msg.lower())
            )

            if is_insufficient:
                logger.warning(
                    f"Insufficient balance for {symbol} {side.value}, retrying..."
                )
                # Brief pause before retry
                await asyncio.sleep(2 * (attempt + 1))
                continue
            else:
                logger.error(f"Order failed ({side.value} {symbol}): {error_msg}")
                return None

        except Exception as e:
            logger.error(f"Order failed ({side.value} {symbol}): {type(e).__name__}: {e}")
            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            return None

    logger.error(f"Place {side.value} {symbol} failed after 3 retries")
    return None
