#!/usr/bin/env python3
"""
Performance audit v2: Full cycle simulation + before/after logging comparison.
Measures actual latencies with concurrent stall detection.
"""

import asyncio
import time
import statistics
import json
import os
import sys
import logging

# Suppress logging to measure pure computation overhead
logging.getLogger("ApexBot").setLevel(logging.WARNING)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ─── Event loop stall detector ───
class StallDetector:
    def __init__(self, threshold_ms=10):
        self.threshold_ms = threshold_ms
        self.stalls = []
        self._task = None
        self._running = False
        self._last_tick = time.monotonic()

    async def _ticker(self):
        self._running = True
        self._last_tick = time.monotonic()
        while self._running:
            await asyncio.sleep(0.001)
            now = time.monotonic()
            elapsed_ms = (now - self._last_tick) * 1000
            if elapsed_ms > self.threshold_ms:
                self.stalls.append(elapsed_ms)
            self._last_tick = now

    async def start(self):
        self._task = asyncio.create_task(self._ticker())

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def report(self):
        if not self.stalls:
            return {"stall_count": 0, "total_stall_ms": 0, "max_stall_ms": 0, "avg_stall_ms": 0}
        return {
            "stall_count": len(self.stalls),
            "total_stall_ms": round(sum(self.stalls), 2),
            "max_stall_ms": round(max(self.stalls), 2),
            "avg_stall_ms": round(statistics.mean(self.stalls), 2),
        }


async def benchmark_async(fn, *args, iterations=10, warmup=2, **kwargs):
    for _ in range(warmup):
        result = fn(*args, **kwargs)
        if asyncio.iscoroutine(result):
            await result
    
    times = []
    for _ in range(iterations):
        start = time.perf_counter()
        result = fn(*args, **kwargs)
        if asyncio.iscoroutine(result):
            await result
        elapsed = (time.perf_counter() - start) * 1000
        times.append(elapsed)
    
    return {
        "mean_ms": round(statistics.mean(times), 2),
        "median_ms": round(statistics.median(times), 2),
        "min_ms": round(min(times), 2),
        "max_ms": round(max(times), 2),
        "stdev_ms": round(statistics.stdev(times) if len(times) > 1 else 0, 2),
    }


def benchmark_sync(fn, *args, iterations=10, warmup=2, **kwargs):
    for _ in range(warmup):
        fn(*args, **kwargs)
    
    times = []
    for _ in range(iterations):
        start = time.perf_counter()
        fn(*args, **kwargs)
        elapsed = (time.perf_counter() - start) * 1000
        times.append(elapsed)
    
    return {
        "mean_ms": round(statistics.mean(times), 2),
        "median_ms": round(statistics.median(times), 2),
        "min_ms": round(min(times), 2),
        "max_ms": round(max(times), 2),
        "stdev_ms": round(statistics.stdev(times) if len(times) > 1 else 0, 2),
    }


import pandas as pd
import numpy as np

def create_mock_ohlcv(symbol="BTC/USD", rows=80):
    dates = pd.date_range(end=datetime.now(timezone.utc), periods=rows, freq="15min")
    base = 60000 if "BTC" in symbol else (3000 if "ETH" in symbol else 150)
    close_prices = base + np.random.randn(rows).cumsum() * 100
    df = pd.DataFrame({
        "open": close_prices - 10,
        "high": close_prices + 20,
        "low": close_prices - 20,
        "close": close_prices,
        "volume": np.random.rand(rows) * 10,
        "vwap": close_prices,
    }, index=dates)
    return df


from datetime import datetime, timezone

async def run_full_audit():
    print("=" * 70)
    print("APEX COMMITTEE BOT - PERFORMANCE AUDIT V2")
    print("=" * 70)
    
    from config import logger as cfg_logger
    cfg_logger.setLevel(logging.WARNING)
    
    from data_feed import (
        compute_indicators, get_account_state, get_all_positions,
    )
    from regime import classify_regime
    from brains.transformer import transformer_brain
    from brains.quant import quant_brain
    from brains.momentum import momentum_brain
    from committee import run_committee
    from sentinel import sentinel
    from feature_engineering import add_features, FEATURE_COLS
    from models import MarketSnapshot, AIDecision, CommitteeResult
    from position_sizing import calculate_trade_size
    
    results = {}
    detector = StallDetector(threshold_ms=10)
    
    # ─── 1. Full pipeline timing (no logging) ───
    print("\n[1/7] Full per-symbol pipeline (no logging)...")
    
    mock_df = create_mock_ohlcv("BTC/USD", 80)
    mock_indicators = compute_indicators(mock_df)
    mock_regime = classify_regime(mock_df, mock_indicators)
    
    snapshot = MarketSnapshot(
        symbol="BTC/USD",
        candles=[],
        indicators=mock_indicators,
        regime=mock_regime,
        atr_pct=mock_indicators["atr_pct"],
        has_position=False,
        position_size=0.0,
        entry_price=None,
        equity=10000.0,
        buying_power=10000.0,
    )
    snapshot.candles_df = mock_df
    
    mock_decisions = [
        AIDecision(brain="transformer", action="BUY", confidence=0.75, regime=mock_regime, reason="test"),
        AIDecision(brain="quant", action="BUY", confidence=0.65, regime=mock_regime, reason="test"),
        AIDecision(brain="momentum", action="HOLD", confidence=0.5, regime=mock_regime, reason="test"),
    ]
    
    # Benchmark with logging SUPPRESSED (current state)
    print("  Measuring with logging suppressed...")
    
    # Feature engineering (in thread)
    feat_result = await benchmark_async(
        lambda: asyncio.to_thread(add_features, mock_df.copy()),
        iterations=10, warmup=2
    )
    results["add_features_silent"] = feat_result
    
    # Transformer inference (in thread)
    if transformer_brain._loaded:
        transformer_result = await benchmark_async(
            lambda: asyncio.to_thread(transformer_brain.decide, snapshot),
            iterations=5, warmup=1
        )
        results["transformer_silent"] = transformer_result
    
    # Quant brain (in thread)
    quant_result = await benchmark_async(
        lambda: asyncio.to_thread(quant_brain.decide, snapshot),
        iterations=20, warmup=2
    )
    results["quant_silent"] = quant_result
    
    # Momentum brain (in thread)
    momentum_result = await benchmark_async(
        lambda: asyncio.to_thread(momentum_brain.decide, snapshot),
        iterations=20, warmup=2
    )
    results["momentum_silent"] = momentum_result
    
    # Committee (sync, but suppressed logging)
    committee_silent = await benchmark_async(
        lambda: run_committee(snapshot, mock_decisions),
        iterations=100, warmup=5
    )
    results["committee_silent"] = committee_silent
    
    # Sentinel
    sentinel_result = await benchmark_async(
        lambda: sentinel.check(snapshot, CommitteeResult(action="BUY", confidence=0.7, regime=mock_regime, votes=[], vote_breakdown={})),
        iterations=100, warmup=5
    )
    results["sentinel_silent"] = sentinel_result
    
    # ─── 2. Full cycle simulation with stall detection ───
    print("\n[2/7] Simulating one full trading cycle with stall detection...")
    await detector.start()
    
    pipeline_times = {}
    
    # Step 1: Feature engineering
    t0 = time.perf_counter()
    feats = await asyncio.to_thread(add_features, mock_df.copy())
    pipeline_times["add_features"] = round((time.perf_counter() - t0) * 1000, 2)
    
    # Step 2: Run all 3 brains in parallel threads
    t0 = time.perf_counter()
    decisions = list(await asyncio.gather(
        asyncio.to_thread(transformer_brain.decide, snapshot) if transformer_brain._loaded else asyncio.to_thread(quant_brain.decide, snapshot),
        asyncio.to_thread(quant_brain.decide, snapshot),
        asyncio.to_thread(momentum_brain.decide, snapshot),
    ))
    pipeline_times["3_brains_parallel"] = round((time.perf_counter() - t0) * 1000, 2)
    
    # Step 3: Committee
    t0 = time.perf_counter()
    committee_result = run_committee(snapshot, decisions)
    pipeline_times["committee"] = round((time.perf_counter() - t0) * 1000, 2)
    
    # Step 4: Sentinel
    t0 = time.perf_counter()
    sentinel_report = sentinel.check(snapshot, committee_result)
    pipeline_times["sentinel"] = round((time.perf_counter() - t0) * 1000, 2)
    
    # Step 5: Position sizing
    t0 = time.perf_counter()
    trade_value = calculate_trade_size(10000.0, committee_result.confidence, sentinel_report.cap_pct)
    pipeline_times["position_sizing"] = round((time.perf_counter() - t0) * 1000, 2)
    
    await detector.stop()
    
    print("  Pipeline timing:")
    for step, t in pipeline_times.items():
        print(f"    {step}: {t:.2f}ms")
    
    total_compute = sum(pipeline_times.values())
    print(f"  Total compute (parallel brains): {total_compute:.2f}ms")
    
    stall_report = detector.report()
    print(f"\n  Event loop stalls: {stall_report['stall_count']} stalls, "
          f"max {stall_report['max_stall_ms']:.1f}ms, avg {stall_report['avg_stall_ms']:.1f}ms")
    
    # ─── 3. Compare committee logging overhead ───
    print("\n[3/7] Committee logging overhead analysis...")
    print("  (run_committee with INFO logging vs suppressed)")
    
    # Temporarily enable INFO logging
    from committee import logger as committee_logger
    original_level = committee_logger.level
    committee_logger.setLevel(logging.INFO)
    
    committee_with_logging = await benchmark_async(
        lambda: run_committee(snapshot, mock_decisions),
        iterations=50, warmup=5
    )
    
    # Restore
    committee_logger.setLevel(original_level)
    
    print(f"  With INFO logging: {committee_with_logging['mean_ms']:.2f}ms mean, {committee_with_logging['max_ms']:.2f}ms max")
    print(f"  Without logging:   {committee_silent['mean_ms']:.2f}ms mean, {committee_silent['max_ms']:.2f}ms max")
    print(f"  Logging overhead:  {committee_with_logging['mean_ms'] - committee_silent['mean_ms']:.2f}ms mean")
    
    results["committee_with_logging"] = committee_with_logging
    results["committee_overhead"] = {
        "mean_ms": committee_with_logging['mean_ms'] - committee_silent['mean_ms'],
        "max_ms": committee_with_logging['max_ms'] - committee_silent['max_ms'],
    }
    
    # ─── 4. API call comparison ───
    print("\n[4/7] Alpaca API call latencies (live)...")
    api_results = {}
    
    try:
        t0 = time.perf_counter()
        equity, bp = await get_account_state()
        api_results["get_account_state"] = round((time.perf_counter() - t0) * 1000, 2)
        print(f"    get_account_state: {api_results['get_account_state']:.1f}ms (equity={equity}, bp={bp})")
    except Exception as e:
        api_results["get_account_state"] = f"error: {e}"
        print(f"    get_account_state: ERROR - {e}")
    
    try:
        t0 = time.perf_counter()
        positions = await get_all_positions()
        api_results["get_all_positions"] = round((time.perf_counter() - t0) * 1000, 2)
        print(f"    get_all_positions: {api_results['get_all_positions']:.1f}ms (positions={len(positions)})")
    except Exception as e:
        api_results["get_all_positions"] = f"error: {e}"
        print(f"    get_all_positions: ERROR - {e}")
    
    results["api_calls"] = api_results
    
    # ─── 5. File I/O latencies ───
    print("\n[5/7] File I/O latencies...")
    
    from config import STATE_FILE_PATH, HEARTBEAT_PATH
    
    # save_state
    test_state = {
        "entry_times": {"BTCUSD": datetime.now(timezone.utc).isoformat()},
        "entry_prices": {"BTCUSD": 60000.0},
        "peak_prices": {"BTCUSD": 61000.0},
        "cooldowns": {"BTCUSD": time.time() + 900},
    }
    
    async def test_save():
        import json as _json, os as _os
        data = test_state.copy()
        pid = os.getpid()
        ts = time.time_ns()
        tmp_path = f"{STATE_FILE_PATH}.audit_{pid}_{ts}.tmp"
        final_path = f"{STATE_FILE_PATH}.audit_{pid}"
        def _write():
            with open(tmp_path, "w") as f:
                _json.dump(data, f, indent=2)
            _os.replace(tmp_path, final_path)
        await asyncio.to_thread(_write)
        try:
            _os.remove(final_path)
        except:
            pass
    
    save_result = await benchmark_async(test_save, iterations=20, warmup=3)
    results["save_state"] = save_result
    print(f"    save_state: {save_result['mean_ms']:.2f}ms mean, {save_result['max_ms']:.2f}ms max")
    
    # write_heartbeat
    from portfolio import write_heartbeat
    hb_result = await benchmark_async(write_heartbeat, iterations=20, warmup=3)
    results["write_heartbeat"] = hb_result
    print(f"    write_heartbeat: {hb_result['mean_ms']:.2f}ms mean, {hb_result['max_ms']:.2f}ms max (median={hb_result['median_ms']:.2f}ms)")
    
    # ─── 6. Memory check ───
    print("\n[6/7] Memory / unbounded growth check...")
    import gc
    gc.collect()
    
    from main import entry_times, entry_prices, peak_prices, cooldowns
    from notifications import _session
    from database import _pool
    
    print(f"  entry_times: {len(entry_times)} entries")
    print(f"  entry_prices: {len(entry_prices)} entries")
    print(f"  peak_prices: {len(peak_prices)} entries")
    print(f"  cooldowns: {len(cooldowns)} entries")
    print(f"  sentinel._consecutive_losses: {len(sentinel._consecutive_losses)} entries")
    print(f"  momentum_brain._prev_regime: {len(momentum_brain._prev_regime)} entries")
    print(f"  notifications._session: {'open' if _session and not _session.closed else 'closed/None'}")
    print(f"  database._pool: {'initialized' if _pool else 'None'}")
    
    # ─── 7. Summary ───
    print("\n[7/7] SUMMARY")
    print("=" * 70)
    
    print("\nCRITICAL (>100ms):")
    for name in ["add_features_silent", "transformer_silent"]:
        if name in results and isinstance(results[name], dict) and "mean_ms" in results[name]:
            r = results[name]
            if r["mean_ms"] > 100:
                print(f"  {name}: {r['mean_ms']:.1f}ms mean, {r['max_ms']:.1f}ms max")
    
    print("\nHIGH (10-100ms):")
    for name in ["committee_silent", "committee_with_logging", "quant_brain", "momentum_brain", "sentinel_silent"]:
        if name in results and isinstance(results[name], dict) and "mean_ms" in results[name]:
            r = results[name]
            if 10 <= r["mean_ms"] <= 100:
                print(f"  {name}: {r['mean_ms']:.1f}ms mean, {r['max_ms']:.1f}ms max")
            elif r["mean_ms"] > 100:
                print(f"  {name}: {r['mean_ms']:.1f}ms mean, {r['max_ms']:.1f}ms max")
    
    print("\nMEDIUM (1-10ms):")
    for name in ["save_state", "write_heartbeat"]:
        if name in results and isinstance(results[name], dict) and "mean_ms" in results[name]:
            r = results[name]
            if 1 < r["mean_ms"] <= 10:
                print(f"  {name}: {r['mean_ms']:.1f}ms mean, {r['max_ms']:.1f}ms max")
    
    print("\nLOW (<1ms):")
    for name in ["quant_silent", "momentum_silent", "sentinel_silent"]:
        if name in results and isinstance(results[name], dict) and "mean_ms" in results[name]:
            r = results[name]
            if r["mean_ms"] < 1:
                print(f"  {name}: {r['mean_ms']:.3f}ms mean")
    
    print("\nPipeline timing (single symbol cycle):")
    for step, t in pipeline_times.items():
        print(f"  {step}: {t:.2f}ms")
    
    print(f"\nTotal stall time during pipeline: {stall_report['total_stall_ms']:.1f}ms "
          f"({stall_report['stall_count']} stalls, max={stall_report['max_stall_ms']:.1f}ms)")
    
    # Save results
    with open("perf_audit_v2_results.json", "w") as f:
        json.dump({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "results": results,
            "pipeline_times": pipeline_times,
            "stall_report": stall_report,
        }, f, indent=2)
    print(f"\nResults saved to perf_audit_v2_results.json")


if __name__ == "__main__":
    asyncio.run(run_full_audit())