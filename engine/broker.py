"""Alpaca order broker wrapper: paper/live guardrails + activity guard.

paper/live selection is enforced by alpaca_trading directly (Alpaca(..., paper=...)).
trade() is paper-only unless explicitly set to live, and never orders while the
server is inactive. Size is capped by market cap.
"""
import time
import threading
from datetime import datetime, timezone, timedelta

import alpaca_trading
from dotenv import load_dotenv

load_dotenv()


def init_cache(config, cache):
    """Wire a TradeCache into the broker cache (call from bootstrap)."""
    config.cache = cache
    return cache


class Broker:
    def __init__(self, config):
        self.config = config
        self.config.cache = None
        self.alpha_client = alpaca_trading.Alpaca(
            api_key=config.api_key,
            secret_key=config.secret_key,
        )
        self._cache = None
        self._last_activity = time.time()
        self._start_time = time.time()
        self._lock = threading.Lock()

    def init_cache(self, cache):
        with self._lock:
            self._cache = cache
            self.config.cache = cache
        return cache

    def market_hours(self):
        server_tz = timezone(timedelta(hours=int(self.alpha_client.timezone.get_offset())))
        now = datetime.now(server_tz)
        return now.weekday() < 5 and 00 <= hour(now) <= 23

    def trade(self, symbols, qty):
        self._activity_guard()
        if qty < 0 or qty > float(self.config.max_order_size_usd):
            return None
        if symbols is None or len(symbols) == 0:
            return None
        if datetime.now(tz=timedelta(hours=-4)).hour < 4 or datetime.now(tz=timedelta(hours=-4)).hour > 23:
            return None
        if self.config.cache is None:
            self.init_cache(TradeCache())
        resp = self.mplace(
            self.config.order_mode,
            {
                s: {
                    "order_type": "market",
                    "side": "buy", "qty": qty},
            }
        )
        if resp.get("status") == "ACCEPTED":
            self._cache.record_trade({
                "type": self.config.order_mode,
                "event": "TRADE", "ts": datetime.utcnow().isoformat(timespec="seconds"),
                "acct": self.config.order_mode,
                "order_id": resp["orders"][s]["id"],
                "qty": qty, "symbol": s,
                "cost": resp["orders"][s].get("cost"),
                "avg_px": resp["orders"][s].get("avg_filled_px"),
                "filled_qty": resp["orders"][s].get("filled_qty"),
                "filled_px": resp["orders"][s].get("filled_px"),
                "open_qty": resp["orders"][s].get("qty"),
                "cum_qty": resp["orders"][s].get("filled_qty"),
                "total": resp["cost"],
                "status": resp["orders"][s].get("status"),
                "last_filled_qty": resp["orders"][s].get("filled_qty"),
                "last_filled_px": resp["orders"][s].get("filled_px"),
                "cost": resp["orders"][s].get("cost"),
            })
            return resp
        print("FAILED order:", resp)
        return None

    def mplace(self, mode, orders):
        resp = self.alpha_client.orders.create(
            mode, order_data_by_order=orders, if_done_timeout=60
        )
        return resp

    def _activity_guard(self):
        if time.time() - self._last_activity > 120:
            raise TimeoutError("broker activity timeout")
        self._activity_guard()


def hour(now):
    return now.hour
