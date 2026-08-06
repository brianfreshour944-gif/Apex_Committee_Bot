"""
Full-cycle performance audit — simulates one trading cycle's worth of work
and measures event-loop blocking.
"""
import asyncio
import os
import sys
import time
import io

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
os.environ.setdefault("APCA_API_KEY_ID", "test_key")
os.environ.setdefault("APCA_API_SECRET_KEY", "test_secret")
os.environ.setdefault("APCA_API_PAPER", "true")

import numpy as np
import pandas as pd

from config import STATE_FILE_PATH, SEQUENCE_LEN
from data_feed import compute_indicators, get_account_state, get_all_positions
from regime import classify_regime
from brains.transformer import transformer_brain
from brains.quant import quant_brain
from brains.momentum import momentum_brain
from committee import run_committee
from sentinel import sentinel
from position_sizing import calculate_trade_size
from feature_engineering import add_features
from models import MarketSnapshot


def make_synthetic_df(n=80):
    np.random.seed(42)
    base_price = 62000.0
    returns = np.random.randn(n) * 0.015
    prices = base_price * np.exp(np.cumsum(returns))
    df = pd.DataFrame({
        "open": prices * (1 - np.random.rand(n) * 0.001),
        "high": prices * (1 + np.random.rand(n) * 0.002),
        "low": prices * (1 - np.random.rand(n) * 0.002),
        "close": prices,
        "volume": np.random.rand(n) * 100 + 50,
        "vwap": prices,
        "trade_count": np.random.randint(50, 200, n),
    })
    return df


class BlockerTracker:
    def __init__(self, interval_ms=10, threshold_ms=5.0):
        self.interval_ms = interval_ms
        self.threshold_ms = threshold_ms
        self.delays = []
        self.max_block = 0.0
        self.block_count = 0
        self._running = False
        self._task = None

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._tick())

    async def stop(self):
        self._running = False
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def _tick(self):
        last = time.perf_counter()
        while self._running:
            await asyncio.sleep(self.interval_ms / 1000.0)
            now = time.perf_counter()
            delay_ms = (now - last - self.interval_ms / 1000.0) * 1000
            self.delays.append(delay_ms)
            if delay_ms > self.max_block:
                self.max_block = delay_ms
            if delay_ms > self.threshold_ms:
                self.block_count += 1
            last = now

    def report(self):
        if not self.delays:
            return {"ticks": 0, "max_block_ms": 0.0, "blocks_above_5ms": 0, "avg_delay_ms": 0.0, "p95_delay_ms": 0.0}
        return {
            "ticks": len(self.delays),
            "max_block_ms": round(self.max_block, 2),
            "blocks_above_5ms": self.block_count,
            "avg_delay_ms": round(sum(self.delays) / len(self.delays), 2),
            "p95_delay_ms": round(sorted(self.delays)[int(len(self.delays) * 0.95)], 2),
        }


async def simulate_full_cycle(tracker):
    """Simulate one full trading cycle: per-symbol processing for all symbols."""
    SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD"]
    equity = 5000.0
    buying_power = 18000.0

    # Per symbol: OHLCV -> features -> indicators -> regime -> committee -> sentinel
    for symbol in SYMBOLS:
        df = make_synthetic_df(80)
        
        # These run SYNC in the main event loop:
        feats = add_features(df.copy())
        ind = compute_indicators(df)
        regime = classify_regime(df, ind)
        
        snapshot = MarketSnapshot(
            symbol=symbol,
            candles=[],
            indicators=ind,
            regime=regime,
            atr_pct=ind["atr_pct"],
            has_position=False,
            position_size=0.0,
            entry_price=None,
            equity=equity,
            buying_power=buying_power,
        )
        snapshot.candles_df = df

        # Brains run via asyncio.to_thread (as main.py does):
        decisions = list(await asyncio.gather(
            asyncio.to_thread(transformer_brain.decide, snapshot),
            asyncio.to_thread(quant_brain.decide, snapshot),
            asyncio.to_thread(momentum_brain.decide, snapshot),
        ))
        committee = run_committee(snapshot, decisions)
        sentinel_report = sentinel.check(snapshot, committee)
        
        if committee.action == "BUY" and not sentinel_report.veto:
            trade_value = calculate_trade_size(equity, committee.confidence, sentinel_report.cap_pct)

    return None


async def simulate_full_cycle_sync_inference(tracker):
    """Same as above but with transformer_brain.call() called directly (no to_thread)."""
    SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD"]
    equity = 5000.0
    buying_power = 18000.0

    for symbol in SYMBOLS:
        df = make_synthetic_df(80)
        
        # These run SYNC in the main event loop:
        feats = add_features(df.copy())
        ind = compute_indicators(df)
        regime = classify_regime(df, ind)
        
        snapshot = MarketSnapshot(
            symbol=symbol,
            candles=[],
            indicators=ind,
            regime=regime,
            atr_pct=ind["atr_pct"],
            has_position=False,
            position_size=0.0,
            entry_price=None,
            equity=equity,
            buying_power=buying_power,
        )
        snapshot.candles_df = df

        # Brains called DIRECTLY (not via to_thread) — simulates the old blocking pattern:
        decisions = [
            transformer_brain.decide(snapshot),
            quant_brain.decide(snapshot),
            momentum_brain.decide(snapshot),
        ]
        committee = run_committee(snapshot, decisions)
        sentinel_report = sentinel.check(snapshot, committee)

    return None


async def main():
    print("=" * 72)
    print("FULL-CYCLE PERFORMANCE AUDIT")
    print(f"PyTorch available: ", end="")
    try:
        import torch
        print(f"YES (v{torch.__version__})")
        print(f"torch num_threads: {torch.get_num_threads()}")
        print(f"torch num_interop_threads: {torch.get_num_interop_threads()}")
    except ImportError:
        print("NO")

    print(f"BLOCK_THRESHOLD_MS = 5.0")
    print(f"Ticker interval: 10ms (high-resolution for blocking detection)")
    print("=" * 72)

    # ── Test 1: Full cycle with to_thread (current main.py pattern) ──────
    print("\n--- Test 1: Full cycle with asyncio.to_thread (current pattern) ---")
    tracker1 = BlockerTracker(interval_ms=10, threshold_ms=5.0)
    await tracker1.start()
    t0 = time.perf_counter()
    await simulate_full_cycle(tracker1)
    t1 = time.perf_counter()
    await tracker1.stop()
    r1 = tracker1.report()
    print(f"  Total cycle time: {(t1-t0)*1000:.1f}ms")
    print(f"  Ticks: {r1['ticks']}")
    print(f"  Max single block: {r1['max_block_ms']:.1f}ms")
    print(f"  Blocks > 5ms: {r1['blocks_above_5ms']}")
    print(f"  Avg delay: {r1['avg_delay_ms']:.1f}ms")
    print(f"  P95 delay: {r1['p95_delay_ms']:.1f}ms")

    # ── Test 2: Full cycle with synchronous inference (old pattern) ─────
    if transformer_brain._loaded:
        print("\n--- Test 2: Full cycle with sync inference (worst-case blocking) ---")
        tracker2 = BlockerTracker(interval_ms=10, threshold_ms=5.0)
        await tracker2.start()
        t0 = time.perf_counter()
        await simulate_full_cycle_sync_inference(tracker2)
        t1 = time.perf_counter()
        await tracker2.stop()
        r2 = tracker2.report()
        print(f"  Total cycle time: {(t1-t0)*1000:.1f}ms")
        print(f"  Ticks: {r2['ticks']}")
        print(f"  Max single block: {r2['max_block_ms']:.1f}ms")
        print(f"  Blocks > 5ms: {r2['blocks_above_5ms']}")
        print(f"  Avg delay: {r2['avg_delay_ms']:.1f}ms")
        print(f"  P95 delay: {r2['p95_delay_ms']:.1f}ms")

    # ── Test 3: Per-call breakdown ───────────────────────────────────────
    print("\n--- Per-component breakdown (avg over 20 iterations) ---")
    
    df = make_synthetic_df(80)
    ind = compute_indicators(df)
    
    # feature_engineering (add_features)
    times = []
    for _ in range(20):
        t0 = time.perf_counter()
        add_features(df.copy())
        t1 = time.perf_counter()
        times.append(t1 - t0)
    print(f"  add_features:              {sum(times)/len(times)*1000:.1f}ms/call (SYNC)")
    
    # compute_indicators
    times = []
    for _ in range(20):
        t0 = time.perf_counter()
        compute_indicators(df)
        t1 = time.perf_counter()
        times.append(t1 - t0)
    print(f"  compute_indicators:        {sum(times)/len(times)*1000:.1f}ms/call (SYNC)")
    
    # transformer_brain.decide (sync)
    if transformer_brain._loaded:
        snapshot = MarketSnapshot(
            symbol="BTC/USD", candles=[], indicators=ind, regime="UPTREND",
            atr_pct=ind["atr_pct"], has_position=False, position_size=0.0,
            entry_price=None, equity=5000.0, buying_power=18000.0,
        )
        snapshot.candles_df = df
        times = []
        for _ in range(20):
            t0 = time.perf_counter()
            transformer_brain.decide(snapshot)
            t1 = time.perf_counter()
            times.append(t1 - t0)
        print(f"  transformer_brain.decide:  {sum(times)/len(times)*1000:.1f}ms/call (via to_thread)")
        
        # threaded version
        times_threaded = []
        for _ in range(20):
            t0 = time.perf_counter()
            await asyncio.to_thread(transformer_brain.decide, snapshot)
            t1 = time.perf_counter()
            times_threaded.append(t1 - t0)
        print(f"  transformer (to_thread):   {sum(times_threaded)/len(times_threaded)*1000:.1f}ms/call (incl thread overhead)")
    
    # quant_brain
    times = []
    for _ in range(20):
        t0 = time.perf_counter()
        quant_brain.decide(snapshot)
        t1 = time.perf_counter()
        times.append(t1 - t0)
    print(f"  quant_brain.decide:        {sum(times)/len(times)*1000:.1f}ms/call (via to_thread)")
    
    # momentum_brain
    times = []
    for _ in range(20):
        t0 = time.perf_counter()
        momentum_brain.decide(snapshot)
        t1 = time.perf_counter()
        times.append(t1 - t0)
    print(f"  momentum_brain.decide:     {sum(times)/len(times)*1000:.1f}ms/call (via to_thread)")
    
    # run_committee
    decisions = [
        AIDecision(brain="transformer", action="BUY", confidence=0.75, regime="UPTREND", reason="test"),
        AIDecision(brain="quant", action="BUY", confidence=0.65, regime="UPTREND", reason="test"),
        AIDecision(brain="momentum", action="HOLD", confidence=0.50, regime="UPTREND", reason="test"),
    ]
    times = []
    for _ in range(20):
        t0 = time.perf_counter()
        run_committee(snapshot, decisions)
        t1 = time.perf_counter()
        times.append(t1 - t0)
    print(f"  run_committee:             {sum(times)/len(times)*1000:.1f}ms/call (SYNC)")
    
    # sentinel.check
    committee = run_committee(snapshot, decisions)
    times = []
    for _ in range(20):
        t0 = time.perf_counter()
        sentinel.check(snapshot, committee)
        t1 = time.perf_counter()
        times.append(t1 - t0)
    print(f"  sentinel.check:            {sum(times)/len(times)*1000:.1f}ms/call (SYNC)")
    
    # calculate_trade_size
    times = []
    for _ in range(20):
        t0 = time.perf_counter()
        calculate_trade_size(5000.0, 0.75, 1.0)
        t1 = time.perf_counter()
        times.append(t1 - t0)
    print(f"  calculate_trade_size:      {sum(times)/len(times)*1000:.1f}ms/call (SYNC)")

    # State save
    from main import save_state, entry_times, entry_prices, peak_prices, cooldowns
    entry_times["test"] = time.time()
    entry_prices["test"] = 62000.0
    peak_prices["test"] = 62000.0
    cooldowns["test"] = time.time() + 900
    times = []
    for _ in range(10):
        t0 = time.perf_counter()
        await save_state()
        t1 = time.perf_counter()
        times.append(t1 - t0)
    print(f"  save_state (to_thread):    {sum(times)/len(times)*1000:.1f}ms/call (to_thread)")
    
    print("\n" + "=" * 72)
    print("DONE")
    print("=" * 72)


if __name__ == "__main__":
    from models import AIDecision
    asyncio.run(main())
