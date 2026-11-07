"""Async Alpaca bot orchestrator.

    Bot.load(config); bot = bot; await bot.start(); then await bot.stop()

paper/live is enforced by the Alpaca client itself (paper=...). Data stream and
trade loop share one async context; the ws closes cleanly on stop.
"""
import asyncio  # noqa: E402


class Bot:
    def __init__(self, config):
        self.config = config
        self.strategy = None
        self.broker = self._make_broker(config)
        self._data_task = None
        self._data_ws = None
        self._stop_requested = False

    def _make_broker(self, config):
        from engine import broker as broker_engine  # noqa: F401
        return broker_engine.Broker(config)

    @classmethod
    def load(cls, config):
        return cls(config)

    async def connect(self):
        from engine import feed as feed_engine

        self._data_task = asyncio.ensure_future(self._handle_datastream())
        await self._data_task

    async def _handle_datastream(self):
        token = self.config.stream_token
        if not token:
            print("[feed] no stream token set — data stream unavailable "
                  "(paper-only on trades).")
            return
        try:
            import websockets

            async with websockets.connect(
                "wss://data.stream.alpaca.markets", close_timeout=10, open_timeout=120
            ) as ws:
                self._data_ws = ws
                async for raw in ws:
                    print("[feed] ", raw[:200])
        except Exception as e:
            print("[feed] stream error:", e)
        finally:
            if self._data_task:
                self._data_task = None


    async def stop(self):
        """Lifecycle stop (called by server on loop end). No-op if not running."""
        self._stop_requested = True
        if self._data_ws is not None:
            try:
                await self._data_ws.close()
            except Exception:
                pass
            self._data_ws = None


async def run_until_server_stop(bot):
    """Wire the data stream and main loop; run until the user stops the server."""
    try:
        await bot.connect()
        while True:
            pos = bot.strategy.get_strategy_positions() if bot.strategy else []
            if not pos:
                break
            entry = bot.strategy.get_strategy_symbols_and_entries(
                (pos or [{}])[0], bot
            )
            if entry:
                symbols, qty = entry
                print(f"[loop] placing market buy: symbols={symbols} qty={qty}")
                broker_call = bot.broker.call(symbols, qty)
            if bot._stop_requested:
                break
            await asyncio.sleep(1)
    finally:
        await bot.stop()
