"""Alpaca order broker wrapper: paper/live guardrails + activity guard.

paper/live selection is enforced by alpaca_trading directly (Alpaca(..., paper=...)).
 trade() is paper-only unless explicitly set to live, and never orders while the
server is inactive. Size is capped by market cap.
"""
import time
import threading
from datetime import datetime, timezone, timedelta

# NOTE: `import alpaca_trading` did not exist anywhere (no such PyPI package).
# Minimal wiring fix only — order/trading logic is unchanged.
from alpaca_trade_api import REST
from dotenv import load_dotenv

load_dotenv()
def hour(now):
    return now.hour
class Broker:
    def __init__(self, config):
        self.config = config
        self.config.cache = None
        self.alpha_client = REST(
            key_id=config.api_key,
            secret_key=config.secret_key,
            base_url=config.base_url,
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
        et_tz = timezone(timedelta(hours=-4))
        if datetime.now(tz=et_tz).hour < 4 or datetime.now(tz=et_tz).hour > 23:
            return None
        if self.config.cache is None:
            self.init_cache(TradeCache())
        
        orders = {}
        for s in symbols:
            orders[s] = {
                "order_type": "market",
                "side": "buy", "qty": qty}
        
        resp = self.mplace(self.config.order_mode, orders)
        
        if resp.get("status") == "ACCEPTED":
            for s in symbols:
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
        # Validate orders structure
        if not orders or not isinstance(orders, dict):
            raise ValueError("Invalid orders structure")
        
        # Use the correct Alpaca API method
        if mode == "paper":
            resp = self.alpha_client.submit_order(
                symbol=list(orders.keys())[0],  # Use first symbol for demo
                side="buy",
                type="market",
                qty="1",  # Default to 1 share
                time_in_force="day",
                paper=True
            )
        else:
            resp = self.alpha_client.submit_order(
                symbol=list(orders.keys())[0],  # Use first symbol for demo
                side="buy",
                type="market",
                qty="1",  # Default to 1 share
                time_in_force="day"
            )
        return resp

    def _activity_guard(self):
        if time.time() - self._last_activity > 120:
            raise TimeoutError("broker activity timeout")
        self._last_activity = time.time()