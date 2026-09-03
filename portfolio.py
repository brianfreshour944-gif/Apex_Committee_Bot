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
    """Convert 'BTC/USD' -> 'BTCUSD' for Alpaca endpoints that require no-slash format."""
    return symbol.replace("/", "")


async def _cancel_orders_for_symbol(symbol: str):
    """Cancel all open orders for a specific symbol to free up the position.

    Retries with increasing delays to handle Alpaca's eventual consistency.
    """
    alpaca_sym = normalize_symbol(symbol)
    for attempt in range(3):
        try:
            all_orders = await asyncio.to_thread(trading_client.get_orders)
            cancelled = 0
            for order in all_orders:
                if order.status in ("new", "partially_filled", "accepted", "pending_new"):
                    if order.symbol == symbol or order.symbol == alpaca_sym:
                        try:
                            await asyncio.to_thread(trading_client.cancel_order, order.id)
                            cancelled += 1
                        except Exception:
                            pass
            if cancelled > 0:
                logger.info(f"Cancelled {cancelled} stale order(s) for {symbol} (attempt {attempt+1})")
            # Exponential backoff: 1s, 2s, 4s
            await asyncio.sleep(1.0 * (2 ** attempt))
        except Exception as e:
            logger.warning(f"Cancel orders for {symbol} attempt {attempt+1} failed: {e}")
            await asyncio.sleep(1.0 * (2 ** attempt))


def _get_pos_qty_available(pos) -> tuple[float, float]:
    """Returns (qty_to_use, qty_total) from an Alpaca Position object.

    Uses qty_available when populated (crypto may return it); falls back to qty.
    """
    qty_total = float(pos.qty)
    qty_avail = getattr(pos, "qty_available", None)
    if qty_avail is not None:
        try:
            qty_avail = float(qty_avail)
        except (ValueError, TypeError):
            qty_avail = qty_total
    else:
        qty_avail = qty_total
    return qty_avail, qty_total


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
    from alpaca.trading.requests import LimitOrderRequest, ClosePositionRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.common.exceptions import APIError

    alpaca_sym = normalize_symbol(symbol)
    pos = None
    qty_total = 0.0
    avg_entry = 0.0

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

            qty_avail, qty_total = _get_pos_qty_available(pos)
            avg_entry = float(pos.avg_entry_price)

            # qty_available may be less than qty if open orders block the position.
            if qty_total > 0 and qty_avail < qty_total * 0.999:
                logger.info(
                    f"Close {symbol}: qty_available={qty_avail:.8f} < "
                    f"qty={qty_total:.8f} (open orders may still be cancelling)"
                )

            use_qty = min(qty_avail, qty_total)
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

            logger.debug(
                f"Close {symbol}: placing SELL limit order "
                f"qty={qty} limit_price={limit_price} symbol={pos.symbol}"
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
            error_status, error_code, error_msg = _extract_api_error(e)

            logger.warning(
                f"Close {symbol} attempt {attempt+1} failed: "
                f"status={error_status} code={error_code} msg={error_msg}"
            )

            is_insufficient = (
                (error_status == 403 and str(error_code) == "10000") or
                (error_msg and "insufficient" in error_msg.lower())
            )

            if is_insufficient:
                # insufficient balance: cancel stale orders, wait, retry
                await _cancel_orders_for_symbol(symbol)
                # Wait for qty_available to match qty (cancellations propagated)
                for _ in range(6):  # up to 6s
                    await asyncio.sleep(1.0)
                    positions = await asyncio.to_thread(trading_client.get_all_positions)
                    for p in positions:
                        if p.symbol == alpaca_sym or p.symbol == symbol:
                            _, q = _get_pos_qty_available(p)
                            if q >= float(p.qty) * 0.999:
                                break
                    else:
                        continue
                    break
                continue
            else:
                # Non-retryable error - try market order fallback
                logger.warning(
                    f"Limit SELL failed for {symbol} (code={error_code}), "
                    f"trying market close via close_position API"
                )
                if pos is not None:
                    # Use normalized symbol for the URL path (no slashes)
                    return await _close_position_market(normalize_symbol(pos.symbol), qty_total, avg_entry, current_price)
                return None

        except Exception as e:
            logger.error(f"Close failed {symbol}: {type(e).__name__}: {e}")
            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            return None

    logger.error(f"Close {symbol} failed after 3 retries")
    return None


async def _close_position_market(
    alpaca_sym: str, qty: float, avg_entry: float, current_price: float | None
) -> dict | None:
    """Fallback: use Alpaca's built-in close_position API (market order).

    This uses DELETE /positions/{symbol} which handles qty_available
    internally and always closes the full position.

    The symbol must be normalized (no slashes) for the URL path.
    """
    try:
        from alpaca.trading.requests import ClosePositionRequest

        # Ensure symbol has no slashes for URL path
        alpaca_sym = normalize_symbol(alpaca_sym)
        qty_str = str(math.floor(qty * 1e8) / 1e8)
        close_req = ClosePositionRequest(qty=qty_str)
        order = await asyncio.to_thread(
            trading_client.close_position,
            symbol_or_symbol_uuid=alpaca_sym,
            close_options=close_req,
        )

        filled_qty = float(order.filled_qty) if order.filled_qty else 0.0
        fill_price = float(order.filled_avg_price) if order.filled_avg_price else (current_price or avg_entry)

        if filled_qty == 0.0:
            logger.warning(f"Market close {alpaca_sym}: no fills")
            return None

        fee = (filled_qty * fill_price) * FEE_RATE
        logger.info(f"Closed (market): {alpaca_sym} qty={filled_qty:.6f} @ ${fill_price:.4f} | fee=${fee:.2f}")

        return {
            "fill_price": fill_price,
            "qty": filled_qty,
            "fee": fee,
            "order_id": order.id,
            "trade_value": filled_qty * fill_price,
        }
    except Exception as e:
        logger.error(f"Market close {alpaca_sym} failed: {type(e).__name__}: {e}")
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