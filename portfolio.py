# portfolio.py — Position management helpers.
import os
from datetime import datetime, timezone
from config import logger, trading_client, HEARTBEAT_PATH


def normalize_symbol(symbol: str) -> str:
    return symbol.replace("/", "")


def close_position(symbol: str) -> bool:
    try:
        trading_client.close_position(normalize_symbol(symbol))
        logger.info(f"🔒 Closed: {symbol}")
        return True
    except Exception as e:
        logger.error(f"Close failed {symbol}: {e}")
        return False


def close_all_positions():
    try:
        trading_client.close_all_positions(cancel_orders=True)
        logger.warning("🚨 All positions closed")
    except Exception as e:
        logger.error(f"Emergency close failed: {e}")


def write_heartbeat():
    try:
        path = HEARTBEAT_PATH
        dirn = os.path.dirname(path)
        if dirn:
            os.makedirs(dirn, exist_ok=True)
        with open(path, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
    except Exception as e:
        logger.error(f"Heartbeat failed: {e}")
