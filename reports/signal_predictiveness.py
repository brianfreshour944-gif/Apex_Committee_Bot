"""Signal predictiveness — does committee confidence predict forward returns?

THE QUESTION
  Every exit/sizing/cost experiment so far says the bot has no edge. This script
  asks the upstream question: does the SIGNAL contain information at all?

  At every bar where the bot WOULD consider entering (flat, not in a blocked
  regime, committee action == BUY), record the committee's confidence and the
  realised forward return over several horizons. If confidence does not rank
  forward returns, no amount of exit or cost tuning can create an edge.

METHOD
  For each bar i: build the same snapshot the live bot would, run the real
  committee, and if the action is BUY record confidence + the forward return
  from bar i's close to bar i+h's close (h in HORIZONS). Cost is NOT applied
  here on purpose -- this measures the raw signal, before fees.

  Then report, per horizon:
    * Spearman rank correlation between confidence and forward return
    * mean forward return in confidence deciles/quartiles (monotonicity)
    * information coefficient (IC) and a bootstrap CI on it

NO LOOK-AHEAD: features use bars[0..i]; the forward return uses bars[i+h],
which is the thing being predicted (that is the point), never an input.

Run:  PYTHONPATH=<repo> python reports/signal_predictiveness.py --days 60
"""

import argparse
import asyncio
import json
import os
import random
import statistics
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

os.environ.setdefault("APCA_API_KEY_ID", "test")
os.environ.setdefault("APCA_API_SECRET_KEY", "test")

from alpaca.data.historical import CryptoHistoricalDataClient

# REPO must be on sys.path before these imports resolve.
from backtest_entry_exit import WARMUP, build_snapshot, decide_all, fetch_bars

import config
from committee import run_committee
from data_feed import compute_indicators
from regime import classify_regime

HORIZONS = [1, 2, 4, 8, 24]   # bars (= 15m), so 15m / 30m / 1h / 2h / 6h


def spearman(xs, ys):
    """Rank correlation without scipy: rank both, then Pearson on the ranks."""
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    return num / (dx * dy) if dx and dy else 0.0


async def collect(symbol, df):
    """Every BUY-signal bar: (confidence, {horizon: forward_return_pct})."""
    rows = []
    baseline = {h: [] for h in HORIZONS}
    for i in range(WARMUP, len(df) - max(HORIZONS) - 1):
        sub = df.iloc[:i + 1]
        ind = compute_indicators(sub)
        if not ind or not ind.get("price"):
            continue
        regime = classify_regime(sub, ind)
        # Unconditional baseline: forward return of EVERY bar, so we can test
        # whether BUY signals beat simply holding at a random moment.
        close_i0 = float(df["close"].iloc[i])
        for h in HORIZONS:
            baseline[h].append(
                (float(df["close"].iloc[i + h]) - close_i0) / close_i0 * 100.0)
        if regime in config.BLOCKED_ENTRY_REGIMES:
            continue
        equity = 100_000.0
        snap = build_snapshot(symbol, df, i, False, None, equity, regime, ind)
        committee = run_committee(snap, await decide_all(snap))
        if committee.action != "BUY":
            continue
        close_i = float(df["close"].iloc[i])
        fwd = {}
        for h in HORIZONS:
            close_h = float(df["close"].iloc[i + h])
            fwd[h] = (close_h - close_i) / close_i * 100.0
        rows.append({"symbol": symbol, "conf": committee.confidence,
                     "regime": regime, "fwd": fwd})
    return rows, baseline


def analyse(rows, label, baseline=None):
    print(f"\n{'='*74}\n{label}: {len(rows)} BUY signals\n{'='*74}")
    if len(rows) < 10:
        print("  too few signals to analyse")
        return {}
    confs = [r["conf"] for r in rows]
    print(f"  confidence: min {min(confs):.3f}  median "
          f"{statistics.median(confs):.3f}  max {max(confs):.3f}")
    out = {}
    for h in HORIZONS:
        fwd = [r["fwd"][h] for r in rows]
        ic = spearman(confs, fwd)
        # bootstrap CI on the IC
        random.seed(0)
        bs = []
        for _ in range(2000):
            idx = [random.randrange(len(rows)) for _ in range(len(rows))]
            bs.append(spearman([confs[i] for i in idx], [fwd[i] for i in idx]))
        bs.sort()
        lo, hi = bs[50], bs[1950]
        # quartiles by confidence
        order = sorted(range(len(rows)), key=lambda i: confs[i])
        q = len(order) // 4
        qm = []
        for k in range(4):
            part = order[k * q:(k + 1) * q] if k < 3 else order[3 * q:]
            qm.append(statistics.mean([fwd[i] for i in part]) if part else 0.0)
        out[h] = {"ic": ic, "ci": (lo, hi), "q": qm,
                  "mean": statistics.mean(fwd)}
        edge = ""
        if baseline and baseline.get(h):
            b = statistics.mean(baseline[h])
            edge = f"  (uncond {b:+.3f}%, lift {statistics.mean(fwd)-b:+.3f}%)"
        print(f"  h={h:>2} bars ({h*15:>3}m): mean fwd {statistics.mean(fwd):+7.3f}%  "
              f"IC {ic:+.3f} CI[{lo:+.3f},{hi:+.3f}]  "
              f"Q1..Q4 {qm[0]:+.3f} {qm[1]:+.3f} {qm[2]:+.3f} {qm[3]:+.3f}{edge}")
    return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--symbols", default="BTC/USD,ETH/USD,SOL/USD")
    ap.add_argument("--offset-days", type=int, default=0)
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    client = CryptoHistoricalDataClient()
    print(f"signal predictiveness | {args.days}d | offset {args.offset_days} | "
          f"{', '.join(symbols)}")

    allrows = []
    pooled_base = {}
    for sym in symbols:
        try:
            df = fetch_bars(client, sym, args.days, args.offset_days)
        except Exception as e:  # noqa: BLE001 - one bad symbol must not abort
            print(f"{sym}: fetch failed: {e}")
            continue
        rows, base = await collect(sym, df)
        analyse(rows, sym, base)
        allrows.extend(rows)
        for h in HORIZONS:
            pooled_base.setdefault(h, []).extend(base[h])

    res = analyse(allrows, "POOLED", pooled_base)
    out = _write(args, res, allrows)
    print(f"\nwrote {out}")
    return res


def _write(args, res, allrows):
    """Sync helper so the async runner never does blocking file I/O."""
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       f"signal_predictiveness_d{args.days}_off{args.offset_days}.json")
    with open(out, "w") as f:
        json.dump({"params": vars(args), "n_signals": len(allrows),
                   "by_horizon": {str(k): v for k, v in res.items()},
                   "rows": [{"symbol": r["symbol"], "conf": r["conf"],
                             "regime": r["regime"],
                             "fwd": {str(k): v for k, v in r["fwd"].items()}}
                            for r in allrows]}, f, indent=2)
    return out


if __name__ == "__main__":
    asyncio.run(main())
