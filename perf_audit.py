#!/usr/bin/env python3
"""
Performance audit script for Apex Committee Bot.
Measures actual latencies of blocking operations and detects event loop stalls.
"""

import asyncio
import time
import statistics
import json
import os
import sys
import logging
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

# Suppress verbose logging from the bot modules to avoid unicode encoding issues
logging.getLogger("ApexBot").setLevel(logging.WARNING)

# Add project to path
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
            await asyncio.sleep(0.001)  # 1ms resolution
            now = time.monotonic()
            elapsed_ms = (now - self._last_tick) * 1000
            if elapsed_ms > self.threshold_ms:
                self.stalls.append({
                    "duration_ms": round(elapsed_ms, 2),
                    "timestamp": now,
                })
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
            return "No stalls detected"
        total_stall_time = sum(s["duration_ms"] for s in self.stalls)
        return {
            "stall_count": len(self.stalls),
            "total_stall_ms": round(total_stall_time, 2),
            "max_stall_ms": round(max(s["duration_ms"] for s in self.stalls), 2),
            "avg_stall_ms": round(statistics.mean(s["duration_ms"] for s in self.stalls), 2),
            "stalls": self.stalls[:10],  # first 10
        }


# ─── Benchmark helpers ───
async def benchmark_async(fn, *args, iterations=10, warmup=2, **kwargs):
    """Benchmark an async function."""
    # Warmup
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
        "samples": times,
    }


def benchmark_sync(fn, *args, iterations=10, warmup=2, **kwargs):
    """Benchmark a sync function."""
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
        "samples": times,
    }


# ─── Mock data for testing ───
import pandas as pd
import numpy as np

def create_mock_ohlcv(symbol="BTC/USD", rows=80):
    """Create mock OHLCV data for testing."""
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


# ─── Main audit ───
async def run_performance_audit():
    print("=" * 70)
    print("APEX COMMITTEE BOT — PERFORMANCE AUDIT")
    print("=" * 70)
    print()

    results = {}
    detector = StallDetector(threshold_ms=5)
    await detector.start()

    # ─── Import all modules ───
    print("[1/9] Importing modules...")
    from config import (
        SEQUENCE_LEN, STATE_FILE_PATH, HEARTBEAT_PATH,
        trading_client, data_client, logger
    )
    from data_feed import (
        get_ohlcv, compute_indicators, get_account_state,
        get_all_positions, get_orderbook_ratio
    )
    from regime import classify_regime
    from brains.transformer import transformer_brain
    from brains.quant import quant_brain
    from brains.momentum import momentum_brain
    from committee import run_committee
    from sentinel import sentinel, SentinelReport
    from position_sizing import calculate_trade_size
    from orders import place_order
    from portfolio import close_position, close_all_positions, write_heartbeat
    from database import init_db, report_equity, record_trade
    from notifications import send_discord_alert
    from models import MarketSnapshot, AIDecision, CommitteeResult

    print("    OK Imports complete")
    print()

    # ─── 2. Transformer model inference ───
    print("[2/9] Measuring transformer_brain.decide() latency...")
    print("    (This runs PyTorch CPU inference — the heaviest operation)")
    
    # Create a mock snapshot with enough data
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

    # Check if model is loaded
    if transformer_brain._loaded:
        print("    Model loaded — measuring inference...")
        transformer_result = await benchmark_async(
            lambda: asyncio.to_thread(transformer_brain.decide, snapshot),
            iterations=5, warmup=1
        )
        results["transformer_inference"] = transformer_result
        print(f"    Mean: {transformer_result['mean_ms']:.1f}ms  "
              f"Median: {transformer_result['median_ms']:.1f}ms  "
              f"Max: {transformer_result['max_ms']:.1f}ms")
    else:
        print("    Model NOT loaded — skipping inference measurement")
        results["transformer_inference"] = {"error": "model_not_loaded"}

    print()

    # ─── 3. Quant & Momentum brains (should be fast) ───
    print("[3/9] Measuring quant_brain.decide() & momentum_brain.decide() latency...")
    quant_result = await benchmark_async(
        lambda: asyncio.to_thread(quant_brain.decide, snapshot),
        iterations=20, warmup=2
    )
    results["quant_brain"] = quant_result
    print(f"    quant_brain:  Mean: {quant_result['mean_ms']:.2f}ms  Max: {quant_result['max_ms']:.2f}ms")

    momentum_result = await benchmark_async(
        lambda: asyncio.to_thread(momentum_brain.decide, snapshot),
        iterations=20, warmup=2
    )
    results["momentum_brain"] = momentum_result
    print(f"    momentum_brain: Mean: {momentum_result['mean_ms']:.2f}ms  Max: {momentum_result['max_ms']:.2f}ms")
    print()

    # ─── 4. Committee & Sentinel ───
    print("[4/9] Measuring committee & sentinel latency...")
    
    mock_decisions = [
        AIDecision(brain="transformer", action="BUY", confidence=0.75, regime=mock_regime, reason="test"),
        AIDecision(brain="quant", action="BUY", confidence=0.65, regime=mock_regime, reason="test"),
        AIDecision(brain="momentum", action="HOLD", confidence=0.5, regime=mock_regime, reason="test"),
    ]
    
    committee_result = await benchmark_async(
        lambda: run_committee(snapshot, mock_decisions),
        iterations=100, warmup=5
    )
    results["committee"] = committee_result
    print(f"    run_committee: Mean: {committee_result['mean_ms']:.3f}ms  Max: {committee_result['max_ms']:.3f}ms")

    sentinel_result = await benchmark_async(
        lambda: sentinel.check(snapshot, CommitteeResult(action="BUY", confidence=0.7, regime=mock_regime, votes=[], vote_breakdown={})),
        iterations=100, warmup=5
    )
    results["sentinel"] = sentinel_result
    print(f"    sentinel.check: Mean: {sentinel_result['mean_ms']:.3f}ms  Max: {sentinel_result['max_ms']:.3f}ms")
    print()

    # ─── 5. Alpaca API calls (using to_thread) ───
    print("[5/9] Measuring Alpaca API call latencies (via asyncio.to_thread)...")
    print("    NOTE: These will hit REAL Alpaca API if credentials are set")
    print("    Set APCA_API_KEY_ID=test to skip real calls")
    
    api_key = os.getenv("APCA_API_KEY_ID", "")
    if api_key and api_key != "test_key":
        print("    Real credentials detected — measuring live API calls...")
        
        # get_ohlcv
        ohlcv_result = await benchmark_async(
            lambda: get_ohlcv("BTC/USD"),
            iterations=5, warmup=1
        )
        results["get_ohlcv"] = ohlcv_result
        print(f"    get_ohlcv: Mean: {ohlcv_result['mean_ms']:.1f}ms  Max: {ohlcv_result['max_ms']:.1f}ms")

        # get_account_state
        acct_result = await benchmark_async(
            get_account_state,
            iterations=5, warmup=1
        )
        results["get_account_state"] = acct_result
        print(f"    get_account_state: Mean: {acct_result['mean_ms']:.1f}ms  Max: {acct_result['max_ms']:.1f}ms")

        # get_all_positions
        pos_result = await benchmark_async(
            get_all_positions,
            iterations=5, warmup=1
        )
        results["get_all_positions"] = pos_result
        print(f"    get_all_positions: Mean: {pos_result['mean_ms']:.1f}ms  Max: {pos_result['max_ms']:.1f}ms")

        # get_orderbook_ratio
        ob_result = await benchmark_async(
            lambda: get_orderbook_ratio("BTC/USD"),
            iterations=5, warmup=1
        )
        results["get_orderbook_ratio"] = ob_result
        print(f"    get_orderbook_ratio: Mean: {ob_result['mean_ms']:.1f}ms  Max: {ob_result['max_ms']:.1f}ms")
    else:
        print("    Test credentials — skipping live API measurements")
        results["alpaca_api"] = {"skipped": "test_credentials"}
    print()

    # ─── 6. Database operations ───
    print("[6/9] Measuring database operation latencies...")
    db_url = os.getenv("DATABASE_URL", "")
    if db_url:
        print("    DATABASE_URL set — measuring live DB...")
        
        init_result = await benchmark_async(
            lambda: asyncio.to_thread(init_db),
            iterations=3, warmup=0
        )
        results["init_db"] = init_result
        print(f"    init_db: Mean: {init_result['mean_ms']:.1f}ms  Max: {init_result['max_ms']:.1f}ms")

        equity_result = await benchmark_async(
            lambda: asyncio.to_thread(report_equity, "perf_test", 10000.0),
            iterations=10, warmup=2
        )
        results["report_equity"] = equity_result
        print(f"    report_equity: Mean: {equity_result['mean_ms']:.1f}ms  Max: {equity_result['max_ms']:.1f}ms")

        trade_result = await benchmark_async(
            lambda: asyncio.to_thread(record_trade, "perf_test", "BTC/USD", "BUY", 0.1, 60000.0),
            iterations=10, warmup=2
        )
        results["record_trade"] = trade_result
        print(f"    record_trade: Mean: {trade_result['mean_ms']:.1f}ms  Max: {trade_result['max_ms']:.1f}ms")
    else:
        print("    No DATABASE_URL — skipping live DB measurements")
        results["database"] = {"skipped": "no_database_url"}
    print()

    # ─── 7. File I/O operations ───
    print("[7/9] Measuring file I/O latencies...")
    
    # save_state (writes JSON to disk)
    import json
    test_state = {
        "entry_times": {"BTCUSD": datetime.now(timezone.utc).isoformat()},
        "entry_prices": {"BTCUSD": 60000.0},
        "peak_prices": {"BTCUSD": 61000.0},
        "cooldowns": {"BTCUSD": time.time() + 900},
    }
    
    async def test_save_state():
        data = test_state.copy()
        tmp_path = f"{STATE_FILE_PATH}.perf_test_{os.getpid()}_{time.time_ns()}.tmp"
        def _write():
            with open(tmp_path, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, f"{STATE_FILE_PATH}.perf_test_{os.getpid()}")
        await asyncio.to_thread(_write)
    
    save_result = await benchmark_async(test_save_state, iterations=20, warmup=3)
    results["save_state"] = save_result
    print(f"    save_state (JSON write): Mean: {save_result['mean_ms']:.2f}ms  Max: {save_result['max_ms']:.2f}ms")
    
    # Clean up test file
    try:
        os.remove(f"{STATE_FILE_PATH}.perf_test_{os.getpid()}")
    except:
        pass

    # write_heartbeat
    hb_result = await benchmark_async(write_heartbeat, iterations=20, warmup=3)
    results["write_heartbeat"] = hb_result
    print(f"    write_heartbeat: Mean: {hb_result['mean_ms']:.2f}ms  Max: {hb_result['max_ms']:.2f}ms")
    print()

    # ─── 8. Discord alerts ───
    print("[8/9] Measuring Discord alert latency...")
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "")
    if webhook:
        print("    DISCORD_WEBHOOK_URL set — measuring live Discord...")
        discord_result = await benchmark_async(
            lambda: send_discord_alert("Perf Test", "Performance audit test message", 0x7B2FBE),
            iterations=5, warmup=1
        )
        results["discord_alert"] = discord_result
        print(f"    send_discord_alert: Mean: {discord_result['mean_ms']:.1f}ms  Max: {discord_result['max_ms']:.1f}ms")
    else:
        print("    No Discord webhook — skipping")
        results["discord_alert"] = {"skipped": "no_webhook"}
    print()

    # ─── 9. Feature engineering ───
    print("[9/9] Measuring feature_engineering.add_features() latency...")
    from feature_engineering import add_features, FEATURE_COLS
    
    feat_df = create_mock_ohlcv("BTC/USD", 100)
    feat_result = await benchmark_async(
        lambda: asyncio.to_thread(add_features, feat_df.copy()),
        iterations=10, warmup=2
    )
    results["add_features"] = feat_result
    print(f"    add_features (11 features, 100 rows): Mean: {feat_result['mean_ms']:.1f}ms  Max: {feat_result['max_ms']:.1f}ms")
    print()

    # ─── Stop stall detector ───
    await detector.stop()
    
    # ─── Check for memory leaks / unbounded growth ───
    print("Checking for unbounded data structures...")
    import gc
    gc.collect()
    
    # Check sentinel._consecutive_losses (should be bounded by symbols)
    print(f"  sentinel._consecutive_losses: {len(sentinel._consecutive_losses)} entries")
    
    # Check momentum_brain._prev_regime
    print(f"  momentum_brain._prev_regime: {len(momentum_brain._prev_regime)} entries")
    
    # Check global dicts in main.py (would need to import main)
    from main import entry_times, entry_prices, peak_prices, cooldowns
    print(f"  main.entry_times: {len(entry_times)} entries")
    print(f"  main.entry_prices: {len(entry_prices)} entries")
    print(f"  main.peak_prices: {len(peak_prices)} entries")
    print(f"  main.cooldowns: {len(cooldowns)} entries")
    
    # Check notifications._session
    from notifications import _session
    print(f"  notifications._session: {'open' if _session and not _session.closed else 'closed/None'}")
    
    # Check database._pool
    from database import _pool
    print(f"  database._pool: {'initialized' if _pool else 'None'}")
    print()

    # ─── Stall detection report ───
    print("Event loop stall detection:")
    stall_report = detector.report()
    if isinstance(stall_report, str):
        print(f"  {stall_report}")
    else:
        print(f"  Stalls detected: {stall_report['stall_count']}")
        print(f"  Total stall time: {stall_report['total_stall_ms']:.1f}ms")
        print(f"  Max single stall: {stall_report['max_stall_ms']:.1f}ms")
        print(f"  Avg stall: {stall_report['avg_stall_ms']:.1f}ms")
        for stall in stall_report['stalls'][:5]:
            print(f"    - {stall['duration_ms']:.1f}ms at {stall['timestamp']:.3f}")
    print()

    # ─── Summary ───
    print("=" * 70)
    print("PERFORMANCE AUDIT SUMMARY")
    print("=" * 70)
    
    # Key findings
    print("\nCRITICAL (>100ms blocking):")
    critical = []
    for name, data in results.items():
        if isinstance(data, dict) and "mean_ms" in data:
            if data["mean_ms"] > 100:
                critical.append(f"  {name}: {data['mean_ms']:.1f}ms mean, {data['max_ms']:.1f}ms max")
    if critical:
        for c in critical:
            print(c)
    else:
        print("  None")
    
    print("\nHIGH (10-100ms blocking):")
    high = []
    for name, data in results.items():
        if isinstance(data, dict) and "mean_ms" in data:
            if 10 < data["mean_ms"] <= 100:
                high.append(f"  {name}: {data['mean_ms']:.1f}ms mean, {data['max_ms']:.1f}ms max")
    if high:
        for h in high:
            print(h)
    else:
        print("  None")
    
    print("\nMEDIUM (1-10ms):")
    medium = []
    for name, data in results.items():
        if isinstance(data, dict) and "mean_ms" in data:
            if 1 < data["mean_ms"] <= 10:
                medium.append(f"  {name}: {data['mean_ms']:.1f}ms mean")
    if medium:
        for m in medium:
            print(m)
    else:
        print("  None")
    
    print("\nLOW (<1ms):")
    low = []
    for name, data in results.items():
        if isinstance(data, dict) and "mean_ms" in data:
            if data["mean_ms"] <= 1:
                low.append(f"  {name}: {data['mean_ms']:.3f}ms mean")
    if low:
        for l in low:
            print(l)
    else:
        print("  None")

    # Save results
    output_file = "perf_audit_results.json"
    with open(output_file, "w") as f:
        json.dump({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "results": results,
            "stall_report": stall_report if isinstance(stall_report, dict) else {"stalls": 0},
        }, f, indent=2)
    print(f"\n[FILE] Full results saved to {output_file}")

    return results


if __name__ == "__main__":
    asyncio.run(run_performance_audit())