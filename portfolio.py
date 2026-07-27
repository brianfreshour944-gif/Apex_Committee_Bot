
import asyncio
import os
from datetime import datetime, timezone
from config import logger, trading_client, HEARTBEAT_PATH


def normalize_symbol(symbol: str) -> str:
    return symbol.replace("/", "")


async def close_position(symbol: str) -> bool:
    try:
        await asyncio.to_thread(trading_client.close_position, normalize_symbol(symbol))
        logger.info(f"🔒 Closed: {symbol}")
        return True
    except Exception as e:
        logger.error(f"Close failed {symbol}: {e}")
        return False


async def close_all_positions():
    try:
        await asyncio.to_thread(trading_client.close_all_positions, cancel_orders=True)
        logger.warning("🚨 All positions closed")
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
