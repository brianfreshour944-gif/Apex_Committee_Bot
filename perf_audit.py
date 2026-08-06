"""
Performs a performance audit of the Apex Committee Bot by measuring actual
event-loop blocking using a concurrent ticker task.

Usage:
    python perf_audit.py

The ticker runs every 50ms and records the time between scheduled ticks.
If the gap exceeds a threshold, it means the event loop was blocked.

For each measured component, we get:
  - min/max/avg delay (ms)  — how long the event loop was starved
  - max single-block duration (ms) — worst single blocking call
  - count of blocks above threshold
"""
import asyncio
import io
import json
import os
import sys
import time

# Force UTF-8 stdout/stderr before any other imports (fixes emoji logging on Windows)
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import pandas as pd
import numpy as np

# Set dummy env vars BEFORE importing config
os.environ.setdefault("APCA_API_KEY_ID", "test_key")
os.environ.setdefault("APCA_API_SECRET_KEY", "test_secret")
os.environ.setdefault("APCA_API_PAPER", "true")

from config import (
    BOT_NAME, SYMBOLS, STATE_FILE_PATH,
    SEQUENCE_LEN, SIZING_TIERS, MAX_SINGLE_TRADE_USD, MIN_ORDER_USD,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, TRAILING_STOP_PCT, MAX_HOLD_HOURS,
    MAX_OPEN_POSITIONS, COOLDOWN_SECONDS_BUY, SENTINEL_MAX_ATR_PCT,
    SENTINEL_MAX_VOL_MULT, MAX_CONSECUTIVE_LOSSES, MAX_DRAWDOWN_STOP,
    MIN_BID_ASK_RATIO, SLEEP_PER_LOOP, FEE_RATE, SELL_SLIPPAGE_BUFFER,
    MODEL_PATH, SCALER_PATH, BRAIN_WEIGHTS, MIN_VOTE_SCORE,
)
from alpaca.trading.enums import OrderSide
from data_feed import get_account_state, get_all_positions, get_orderbook_ratio
from sentinel import sentinel
from committee import run_committee
from models import MarketSnapshot, AIDecision, CommitteeResult
from position_sizing import calculate_trade_size
from feature_engineering import add_features, FEATURE_COLS
from regime import classify_regime
from data_feed import compute_indicators

# ── Event loop blocker measurement ──────────────────────────────────────────────

BLOCK_THRESHOLD_MS = 5.0  # blocks above this are "meaningful"

class BlockerTracker:
    """Runs a high-frequency ticker to detect event loop stalls."""
    def __init__(self, interval_ms=50, threshold_ms=5.0):
        self.interval_ms = interval_ms
        self.threshold_ms = threshold_ms
        self.delays = []
        self.max_block = 0.0
        self.block_count = 0
        self._running = False

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
            return {
                "min_delay_ms": 0.0,
                "max_delay_ms": 0.0,
                "avg_delay_ms": 0.0,
                "p95_delay_ms": 0.0,
                "max_single_block_ms": 0.0,
                "block_count_above_threshold": 0,
                "total_ticks": 0,
                "threshold_ms": self.threshold_ms,
            }
        d = self.delays
        return {
            "min_delay_ms": round(min(d), 3),
            "max_delay_ms": round(max(d), 3),
            "avg_delay_ms": round(sum(d) / len(d), 3),
            "p95_delay_ms": round(sorted(d)[int(len(d) * 0.95)], 3),
            "max_single_block_ms": round(self.max_block, 3),
            "block_count_above_threshold": self.block_count,
            "total_ticks": len(d),
            "threshold_ms": self.threshold_ms,
        }


# ── Synthetic data for measurement ──────────────────────────────────────────────

def make_synthetic_df(n=50):
    """Create a realistic DataFrame for feature engineering + model inference."""
    np.random.seed(42)
    base_price = 62000.0
    returns = np.random.randn(n) * 0.015
    prices = base_price * np.exp(np.cumsum(returns))

    df = pd.DataFrame({
        "open":  prices * (1 - np.random.rand(n) * 0.001),
        "high":  prices * (1 + np.random.rand(n) * 0.002),
        "low":   prices * (1 - np.random.rand(n) * 0.002),
        "close": prices,
        "volume": np.random.rand(n) * 100 + 50,
        "vwap":  prices,
        "trade_count": np.random.randint(50, 200, n),
    })
    return df


# ── Individual component benchmarks ───────────────────────────────────────────

async def bench_feature_engineering(tracker):
    """Bench: add_features (11 institutional features, 50-row df) + compute_indicators
    — both run synchronously in the main event loop (not offloaded to thread)."""
    df = make_synthetic_df(50)
    times_feats = []
    times_inds = []
    
    for _ in range(20):
        t0 = time.perf_counter()
        feats = add_features(df.copy())
        t1 = time.perf_counter()
        ind = compute_indicators(df)
        t2 = time.perf_counter()
        times_feats.append(t1 - t0)
        times_inds.append(t2 - t1)
    
    print(f"  add_features avg: {sum(times_feats)/len(times_feats)*1000:.1f}ms (SYNC in event loop)")
    print(f"  compute_indicators avg: {sum(times_inds)/len(times_inds)*1000:.1f}ms (SYNC in event loop)")
    return feats


async def bench_compute_indicators(tracker):
    """Bench: compute_indicators (RSI, MACD, BB, EMA, ATR, volume, momentum)."""
    df = make_synthetic_df(80)
    for _ in range(20):
        ind = compute_indicators(df)
    return ind


async def bench_model_inference(tracker):
    """Bench: transformer_brain.decide() — PyTorch GrokGQA transformer forward pass.
    
    Measures both total time AND event-loop blocking. PyTorch CPU ops use
    intra-op parallelism via OMP threads that hold the GIL, which can block
    the event loop even when offloaded via asyncio.to_thread.
    
    This benchmark measures whether the event loop is actually blocked during
    the asyncio.to_thread call by running the ticker concurrently.
    """
    from brains.transformer import transformer_brain

    if not transformer_brain._loaded:
        return None

    df = make_synthetic_df(SEQUENCE_LEN)
    snapshot = MarketSnapshot(
        symbol="BTC/USD",
        candles=[],
        indicators={},
        regime="UPTREND",
        atr_pct=1.5,
        has_position=False,
        position_size=0.0,
        entry_price=None,
        equity=10000.0,
        buying_power=10000.0,
    )
    snapshot.candles_df = df

    # Measure with finer ticker (10ms) for more sensitivity
    fine_tracker = BlockerTracker(interval_ms=10, threshold_ms=5.0)
    await fine_tracker.start()
    
    import time
    times = []
    for _ in range(20):
        t0 = time.perf_counter()
        result = transformer_brain.decide(snapshot)
        t1 = time.perf_counter()
        times.append(t1 - t0)
    
    await fine_tracker.stop()
    
    ft_report = fine_tracker.report()
    print(f"\n  [Ticker] Ticks: {ft_report['total_ticks']}, Max block: {ft_report['max_single_block_ms']:.1f}ms, #blocks>5ms: {ft_report['block_count_above_threshold']}")
    
    # Now measure with asyncio.to_thread (as main.py does)
    fine_tracker2 = BlockerTracker(interval_ms=10, threshold_ms=5.0)
    await fine_tracker2.start()
    
    times_threaded = []
    for _ in range(20):
        t0 = time.perf_counter()
        result = await asyncio.to_thread(transformer_brain.decide, snapshot)
        t1 = time.perf_counter()
        times_threaded.append(t1 - t0)
    
    await fine_tracker2.stop()
    
    ft_report2 = fine_tracker2.report()
    print(f"  [Threaded] Ticks: {ft_report2['total_ticks']}, Max block: {ft_report2['max_single_block_ms']:.1f}ms, #blocks>5ms: {ft_report2['block_count_above_threshold']}")
    print(f"  [Time] Sync: {sum(times)/len(times)*1000:.1f}ms avg | Threaded: {sum(times_threaded)/len(times_threaded)*1000:.1f}ms avg")
    
    return result


async def bench_quant_brain(tracker):
    """Bench: quant_brain.decide() — pure TA indicators."""
    from brains.quant import quant_brain

    df = make_synthetic_df(80)
    ind = compute_indicators(df)
    snapshot = MarketSnapshot(
        symbol="BTC/USD",
        candles=[],
        indicators=ind,
        regime="UPTREND",
        atr_pct=ind["atr_pct"],
        has_position=False,
        position_size=0.0,
        entry_price=None,
        equity=10000.0,
        buying_power=10000.0,
    )
    snapshot.candles_df = df

    for _ in range(20):
        result = quant_brain.decide(snapshot)
    return result


async def bench_momentum_brain(tracker):
    """Bench: momentum_brain.decide()."""
    from brains.momentum import momentum_brain

    df = make_synthetic_df(80)
    ind = compute_indicators(df)
    snapshot = MarketSnapshot(
        symbol="BTC/USD",
        candles=[],
        indicators=ind,
        regime="UPTREND",
        atr_pct=ind["atr_pct"],
        has_position=False,
        position_size=0.0,
        entry_price=None,
        equity=10000.0,
        buying_power=10000.0,
    )
    snapshot.candles_df = df

    for _ in range(20):
        result = momentum_brain.decide(snapshot)
    return result


async def bench_committee(tracker):
    """Bench: run_committee with 3 decisions."""
    df = make_synthetic_df(80)
    ind = compute_indicators(df)
    snapshot = MarketSnapshot(
        symbol="BTC/USD",
        candles=[],
        indicators=ind,
        regime="UPTREND",
        atr_pct=ind["atr_pct"],
        has_position=False,
        position_size=0.0,
        entry_price=None,
        equity=10000.0,
        buying_power=10000.0,
    )

    decisions = [
        AIDecision(brain="transformer", action="BUY", confidence=0.75, regime="UPTREND", reason="test"),
        AIDecision(brain="quant", action="BUY", confidence=0.65, regime="UPTREND", reason="test"),
        AIDecision(brain="momentum", action="HOLD", confidence=0.50, regime="UPTREND", reason="test"),
    ]

    for _ in range(20):
        result = run_committee(snapshot, decisions)
    return result


async def bench_sentinel(tracker):
    """Bench: sentinel.check()."""
    df = make_synthetic_df(80)
    ind = compute_indicators(df)
    snapshot = MarketSnapshot(
        symbol="BTC/USD",
        candles=[],
        indicators=ind,
        regime="UPTREND",
        atr_pct=ind["atr_pct"],
        has_position=False,
        position_size=0.0,
        entry_price=None,
        equity=10000.0,
        buying_power=10000.0,
    )

    decisions = [
        AIDecision(brain="transformer", action="BUY", confidence=0.75, regime="UPTREND", reason="test"),
        AIDecision(brain="quant", action="BUY", confidence=0.65, regime="UPTREND", reason="test"),
        AIDecision(brain="momentum", action="HOLD", confidence=0.50, regime="UPTREND", reason="test"),
    ]

    committee = run_committee(snapshot, decisions)

    for _ in range(20):
        result = sentinel.check(snapshot, committee)
    return result


async def bench_position_sizing(tracker):
    """Bench: calculate_trade_size."""
    for _ in range(20):
        val = calculate_trade_size(10000.0, 0.75, sentinel_cap=1.0)
    return val


async def bench_state_save(tracker):
    """Bench: save_state — JSON dump + os.replace."""
    from main import save_state, entry_times, entry_prices, peak_prices, cooldowns
    # Simulate some state
    entry_times["BTCUSD"] = time.time()
    entry_prices["BTCUSD"] = 62000.0
    peak_prices["BTCUSD"] = 62500.0
    cooldowns["BTCUSD"] = time.time() + 900
    for _ in range(10):
        await save_state()
    return None


async def bench_database_write(tracker):
    """Bench: record_trade via asyncio.to_thread."""
    from database import record_trade, report_equity, init_db
    # Init DB (may fail silently if no DB URL)
    await asyncio.to_thread(init_db)
    for _ in range(10):
        await asyncio.to_thread(
            record_trade, BOT_NAME, "BTCUSD", "BUY", 0.01, 62000.0,
            fill_price=62000.0, fee=1.0, order_id="test_order_123"
        )
        await asyncio.to_thread(report_equity, BOT_NAME, 10000.0)
    return None


# ── Main harness ────────────────────────────────────────────────────────────────

BENCHMARKS = [
    ("feature_engineering", bench_feature_engineering, "add_features + compute_indicators — both run synchronously in main event loop (not offloaded to thread). Computes 11 institutional features + RSI/MACD/BB/EMA/ATR."),
    ("compute_indicators", bench_compute_indicators, "Computes RSI, MACD, Bollinger Bands, EMA, ATR, volume ratio, momentum — synchronously in main loop."),
    ("model_inference", bench_model_inference, "PyTorch GrokGQA transformer forward pass (4 layers, 128 embed, GQA attention) — runs via asyncio.to_thread, but torch CPU ops still use intra-op parallelism that can block."),
    ("quant_brain", bench_quant_brain, "Pure-Python TA indicator scoring (5 indicators, regime-adaptive thresholds) — runs via asyncio.to_thread but CPU-bound Python."),
    ("momentum_brain", bench_momentum_brain, "Volume + regime transition detection — pure Python, via asyncio.to_thread."),
    ("committee_voting", bench_committee, "Weighted voting engine (3 brains, softmax-like decision aggregation) — synchronous Python."),
    ("sentinel_check", bench_sentinel, "Risk checks (ATR, volume, consecutive losses) — synchronous Python."),
    ("position_sizing", bench_position_sizing, "Confidence-tiered position sizing with sentinel cap — trivial synchronous Python."),
    ("state_save", bench_state_save, "JSON state file write + os.replace — already offloaded to thread via asyncio.to_thread."),
    ("database_write", bench_database_write, "record_trade + report_equity INSERT statements — already offloaded via asyncio.to_thread."),
]


async def run_benchmark(name, bench_fn, tracker):
    """Run a single benchmark with the blocker tracker active and report."""
    tracker.delays.clear()
    tracker.max_block = 0.0
    tracker.block_count = 0

    # Reset tracker state for this bench
    start = time.perf_counter()
    try:
        result = await bench_fn(tracker)
    except Exception as bench_err:
        elapsed = time.perf_counter() - start
        print(f"\nBENCHMARK: {name}")
        print(f"Total time (20 iterations): {elapsed*1000:.1f}ms")
        print(f"Avg per call: {elapsed/20.0*1000:.1f}ms")
        print(f"ERROR: {bench_err}")
        return {
            "name": name,
            "total_ms": round(elapsed * 1000, 1),
            "avg_per_call_ms": round(elapsed / 20.0 * 1000, 1),
            "error": str(bench_err),
        }

    elapsed = time.perf_counter() - start

    report = tracker.report()
    avg_per_call = (elapsed / 20.0) * 1000  # 20 iterations per benchmark

    print(f"\n{'='*72}")
    print(f"BENCHMARK: {name}")
    print(f"Description: {BENCH_DESCRIPTIONS[name]}")
    print(f"Total time (20 iterations): {elapsed*1000:.1f}ms")
    print(f"Avg per call: {avg_per_call:.1f}ms")
    print(f"Blocker tracker results:")
    print(f"  Max single block: {report['max_single_block_ms']:.1f}ms")
    print(f"  Blocks > {tracker.threshold_ms}ms: {report['block_count_above_threshold']}")
    print(f"  Avg delay: {report['avg_delay_ms']:.1f}ms (over {report['total_ticks']} ticks)")
    print(f"  P95 delay: {report['p95_delay_ms']:.1f}ms")

    return {
        "name": name,
        "total_ms": round(elapsed * 1000, 1),
        "avg_per_call_ms": round(avg_per_call, 1),
        "max_block_ms": report["max_single_block_ms"],
        "blocks_above_threshold": report["block_count_above_threshold"],
        "p95_delay_ms": report["p95_delay_ms"],
    }


BENCH_DESCRIPTIONS = {name: desc for name, _, desc in BENCHMARKS}


async def main():
    print("=" * 72)
    print("PERFORMANCE AUDIT — Apex Committee Bot")
    print(f"Python {sys.version}")
    print(f"PyTorch available: ", end="")
    try:
        import torch
        print(f"YES (version {torch.__version__})")
    except ImportError:
        print("NO")
    print(f"BLOCK_THRESHOLD_MS = {BLOCK_THRESHOLD_MS}")
    print(f"Ticker interval: 50ms (measures event loop stalls)")
    print("=" * 72)

    results = []
    for name, bench_fn, desc in BENCHMARKS:
        tracker = BlockerTracker(interval_ms=50, threshold_ms=BLOCK_THRESHOLD_MS)
        await tracker.start()
        try:
            result = await run_benchmark(name, bench_fn, tracker)
            results.append(result)
        except Exception as e:
            print(f"\nERROR in {name}: {e}")
            results.append({
                "name": name,
                "error": str(e),
            })
        finally:
            await tracker.stop()

    # Summary
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    print(f"{'Component':<25} {'Avg/Call':>10} {'Max Block':>12} {'#Blocks>5ms':>14}")
    print("-" * 72)
    for r in results:
        if "error" in r:
            print(f"{r['name']:<25} {'ERROR':>10} {'N/A':>12} {'N/A':>14}")
        else:
            print(f"{r['name']:<25} {r['avg_per_call_ms']:>7.1f}ms {r['max_block_ms']:>9.1f}ms {r['blocks_above_threshold']:>14}")

    # Save results
    output_path = "perf_audit_results.json"
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
