"""Entry/exit backtest harness — replays the LIVE decision path on real bars.

WHAT THIS DOES
  Replays the bot's real decision pipeline bar-by-bar over historical 15-minute
  crypto bars with NO LOOK-AHEAD: at bar i the strategy sees only bars[0..i].
  A decision made on bar i's close fills at bar (i+1)'s OPEN. Every brain, the
  committee, the sentinel, sizing, and the full exit ladder are the REAL modules
  imported from the repo — not reimplemented.

FAITHFULNESS — modelled exactly
  * 3 brains (transformer .pth, quant, momentum) via the real decide() methods,
    including momentum's stateful _prev_regime transition memory.
  * committee.run_committee (regime-adaptive thresholds, ACCUMULATION buy bias)
  * sentinel.check (ATR/vol vetoes, DUMP entry block, consecutive-loss pause
    incl. the 4h expiry fix, size caps)
  * position_sizing.calculate_trade_size (SIZING_TIERS, MIN_ORDER_USD floor)
  * entry gates: buy cooldown, post-exit cooldown, blocked regimes
  * the full exit ladder in priority order: stop loss (time-decay + ATR widen),
    take profit, trailing stop (armed only once peak > entry), max hold, then
    the committee SELL override.
  * costs: taker fee FEE_RATE on both legs + slippage on both legs.

KNOWN LIMITATIONS (read before trusting numbers)
  1. FILL MODEL: next bar's OPEN + fixed slippage, full size, no partial fills,
     no liquidity/impact model, no funding. Optimistic on size.
  2. ORDERBOOK GATE: the live whale gate needs live L2 depth, which has no
     historical equivalent. It is DISABLED here (it is a fail-closed VETO that
     would otherwise block every entry). So entries here are slightly MORE
     permissive than live.
  3. SYMBOLS ARE REPLAYED INDEPENDENTLY. MAX_OPEN_POSITIONS and the aggregate
     exposure cap are per-symbol here, not portfolio-wide, because a faithful
     cross-symbol interleave would need a single merged event clock. Treat the
     "portfolio" line as the SUM of independent symbol runs, not a true
     portfolio simulation.
  4. The sentinel loss streak is in-memory for the whole run (matches a single
     continuous live process; live restarts clear it).
  5. 15m bars: intra-bar ordering of stop vs target is unknowable, so if a bar
     spans both, the STOP is assumed first (conservative for a long).

OUTPUT
  Per-symbol: trades, win rate, gross/net, fees, profit factor, avg win/loss,
  max drawdown, and buy&hold over the same window — because beating 0% is not
  the same as beating the market.

Run:  PYTHONPATH=<repo> python reports/backtest_entry_exit.py --days 30
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

os.environ.setdefault("APCA_API_KEY_ID", "test")
os.environ.setdefault("APCA_API_SECRET_KEY", "test")

from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

import config
from brains.momentum import momentum_brain
from brains.quant import quant_brain
from brains.transformer import transformer_brain
from committee import run_committee
from data_feed import compute_indicators
from models import MarketSnapshot
from position_sizing import calculate_trade_size
from regime import classify_regime
from sentinel import Sentinel

WARMUP = 120
BARS_PER_HOUR = 4
SLIPPAGE = config.SELL_SLIPPAGE_BUFFER


@dataclass
class Position:
    symbol: str
    qty: float
    entry_price: float
    entry_time: datetime
    entry_bar: int
    peak_price: float
    confidence: float
    regime: str
    entry_fee: float = 0.0


@dataclass
class Trade:
    symbol: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    qty: float
    gross: float
    fees: float
    net: float
    reason: str
    confidence: float


@dataclass
class Stats:
    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)


def fetch_bars(client, symbol, days):
    end = datetime.now(timezone.utc) - timedelta(days=1)
    start = end - timedelta(days=days)
    r = client.get_crypto_bars(CryptoBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(15, TimeFrameUnit.Minute),
        start=start, end=end,
    ))
    bars = r.data[symbol]
    df = pd.DataFrame([{
        "timestamp": b.timestamp, "open": float(b.open), "high": float(b.high),
        "low": float(b.low), "close": float(b.close), "volume": float(b.volume),
    } for b in bars])
    df.set_index("timestamp", inplace=True)
    return df[df["close"] > 0]


def fee_for(price, qty):
    return price * qty * config.FEE_RATE


def build_snapshot(symbol, df, i, has_pos, pos, equity, regime, ind):
    snap = MarketSnapshot(
        symbol=symbol, candles=[], indicators=ind, regime=regime,
        atr_pct=ind["atr_pct"],
        has_position=has_pos,
        position_size=pos.qty if has_pos else 0.0,
        entry_price=pos.entry_price if has_pos else None,
        equity=equity, buying_power=equity,
    )
    snap.candles_df = df.iloc[:i + 1]
    return snap


async def decide_all(snap):
    return list(await asyncio.gather(
        asyncio.to_thread(transformer_brain.decide, snap),
        asyncio.to_thread(quant_brain.decide, snap),
        asyncio.to_thread(momentum_brain.decide, snap),
    ))


def check_exit(pos, price, bar, ind):
    """Replicates main.py's mechanical exit ladder. Returns (reason, peak)."""
    peak = max(pos.peak_price, price)
    pnl_pct = (price - pos.entry_price) / pos.entry_price
    held_h = (bar - pos.entry_bar) / BARS_PER_HOUR

    trailing_stop_price = peak * (1.0 - config.TRAILING_STOP_PCT)

    effective_stop = config.STOP_LOSS_PCT
    if held_h >= 2.0:
        effective_stop *= 0.50
    elif held_h >= 1.0:
        effective_stop *= 0.75

    atr_pct = ind.get("atr_pct", 0.0)
    if atr_pct > config.STOP_LOSS_ATR_THRESHOLD:
        atr_mult = min(config.STOP_LOSS_ATR_MAX_MULT,
                       atr_pct / config.STOP_LOSS_ATR_THRESHOLD)
        effective_stop *= atr_mult

    if pnl_pct <= -effective_stop:
        return f"Stop loss {pnl_pct*100:.2f}% (thr -{effective_stop*100:.2f}%)", peak
    if pnl_pct >= config.TAKE_PROFIT_PCT:
        return f"Take profit +{pnl_pct*100:.2f}%", peak
    if price < trailing_stop_price and peak > pos.entry_price:
        return f"Trailing stop (peak {peak:.2f})", peak
    if held_h >= config.MAX_HOLD_HOURS:
        return f"Max hold {held_h:.1f}h", peak
    return None, peak


async def replay_symbol(symbol, df, starting_equity, sentinel, min_conf=0.0):
    st = Stats()
    pos = None
    realized = 0.0
    equity = starting_equity
    cooldown_until = -1
    exit_cooldown_until = -1
    n = 0

    # Pending order decided on bar i, filled at open of bar i+1. The fill price
    # is captured AT DECISION TIME so the fill cannot drift to a later bar.
    pending = None
    pf = {}

    for i in range(WARMUP, len(df) - 1):
        ts = df.index[i]
        sub = df.iloc[:i + 1]
        ind = compute_indicators(sub)
        if not ind or not ind.get("price"):
            continue
        price = ind["price"]
        regime = classify_regime(sub, ind)
        n += 1

        # ── 1. Fill the order decided on the previous bar ───────────────────
        if pending == "BUY" and pos is None:
            fill = pf["price"] * (1.0 + SLIPPAGE)
            qty = pf["qty"]
            if qty > 0 and fill > 0:
                entry_fee = fee_for(fill, qty)
                realized -= entry_fee
                equity = starting_equity + realized
                pos = Position(symbol, qty, fill, pf["ts"], pf["bar"], fill,
                               pf["conf"], pf["regime"], entry_fee)
            pending = None
        elif pending == "SELL" and pos is not None:
            fill = pf["price"] * (1.0 - SLIPPAGE)
            gross = (fill - pos.entry_price) * pos.qty
            exit_fee = fee_for(fill, pos.qty)
            total_fee = pos.entry_fee + exit_fee
            # net is the trade's FULL contribution: entry fee was already
            # deducted from realized, so subtract only the exit fee here.
            net = gross - total_fee
            realized += gross - exit_fee
            equity = starting_equity + realized
            st.trades.append(Trade(
                symbol, pos.entry_time, ts, pos.entry_price, fill, pos.qty,
                gross, total_fee, net, pf["reason"], pos.confidence))
            if net < 0:
                sentinel.register_loss(symbol)
            else:
                sentinel.register_win(symbol)
            pos = None
            exit_cooldown_until = i + int(config.EXIT_COOLDOWN_SECONDS / 900)
            pending = None

        if pending is None:
            st.equity_curve.append((ts, equity))

        # ── 2. EXIT evaluation on this bar's close ──────────────────────────
        if pos is not None:
            reason, peak = check_exit(pos, price, i, ind)
            pos.peak_price = peak
            if reason is None:
                snap = build_snapshot(symbol, df, i, True, pos, equity, regime, ind)
                c = run_committee(snap, await decide_all(snap))
                if c.action == "SELL":
                    reason = f"Committee SELL override conf={c.confidence:.3f}"
            if reason is not None:
                pending = "SELL"
                pf = {"price": float(df["open"].iloc[i + 1]), "reason": reason}
            continue

        # ── 3. ENTRY evaluation (flat only) ─────────────────────────────────
        if i < cooldown_until or i < exit_cooldown_until:
            continue
        if regime in config.BLOCKED_ENTRY_REGIMES:
            continue

        snap = build_snapshot(symbol, df, i, False, None, equity, regime, ind)
        decisions = await decide_all(snap)
        committee = run_committee(snap, decisions)
        if committee.action != "BUY":
            continue

        # Optional cost-aware filter: skip marginal-confidence entries. Every
        # round trip costs ~0.9% (fee + slippage, both legs), so a low-edge
        # trade is more likely to be a net loser after costs.
        if committee.confidence < min_conf:
            continue

        report = sentinel.check(snap, committee)
        if report.veto:
            continue

        trade_value = calculate_trade_size(equity, committee.confidence,
                                          report.cap_pct)
        if trade_value <= 0:
            continue

        nxt_open = float(df["open"].iloc[i + 1])
        if nxt_open <= 0:
            continue
        pending = "BUY"
        pf = {"price": nxt_open, "qty": trade_value / nxt_open, "ts": ts,
              "bar": i, "conf": committee.confidence, "regime": committee.regime}
        cooldown_until = i + int(config.COOLDOWN_SECONDS_BUY / 900)

    # ── Final unwind at the last close ──────────────────────────────────────
    if pos is not None:
        last_ts = df.index[-1]
        fill = float(df["close"].iloc[-1]) * (1.0 - SLIPPAGE)
        gross = (fill - pos.entry_price) * pos.qty
        exit_fee = fee_for(fill, pos.qty)
        total_fee = pos.entry_fee + exit_fee
        net = gross - total_fee
        realized += gross - exit_fee
        equity = starting_equity + realized
        st.trades.append(Trade(symbol, pos.entry_time, last_ts, pos.entry_price,
                               fill, pos.qty, gross, total_fee, net,
                               "Final unwind (open at end)", pos.confidence))
        st.equity_curve.append((last_ts, equity))

    return st.trades, st.equity_curve, n


def summarize(symbol, trades, equity_curve, starting_equity, buy_hold_pct):
    wins = [t for t in trades if t.net > 0]
    losses = [t for t in trades if t.net <= 0]
    gross = sum(t.gross for t in trades)
    fees = sum(t.fees for t in trades)
    net = sum(t.net for t in trades)
    gp = sum(t.net for t in wins)
    gl = abs(sum(t.net for t in losses))
    pf = (gp / gl) if gl > 0 else (float("inf") if gp > 0 else 0.0)

    peak = -1e18
    max_dd = 0.0
    for _, eq in equity_curve:
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)

    ret_pct = 100.0 * net / starting_equity
    return {
        "symbol": symbol,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(100.0 * len(wins) / len(trades), 1) if trades else 0.0,
        "gross": round(gross, 2),
        "fees": round(fees, 2),
        "net": round(net, 2),
        "net_pct": round(ret_pct, 2),
        "profit_factor": round(pf, 2) if pf != float("inf") else None,
        "avg_win": round(statistics.mean([t.net for t in wins]), 2) if wins else 0.0,
        "avg_loss": round(statistics.mean([t.net for t in losses]), 2) if losses else 0.0,
        "max_dd_pct": round(100.0 * max_dd, 2),
        "buy_hold_pct": round(buy_hold_pct, 2),
        "excess_vs_hold_pct": round(ret_pct - buy_hold_pct, 2),
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--symbols", default="BTC/USD,ETH/USD,SOL/USD")
    ap.add_argument("--equity", type=float, default=100000.0)
    ap.add_argument("--min-conf", type=float, default=0.0,
                    help="skip BUY entries below this committee confidence")
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    client = CryptoHistoricalDataClient()

    results, all_trades = [], []
    print(f"\n{'='*78}")
    print(f"ENTRY/EXIT BACKTEST — next-open fills, real bars, {args.days}d, "
          f"equity ${args.equity:,.0f}")
    print(f"costs: fee {config.FEE_RATE*100:.3f}%/leg + slip {SLIPPAGE*100:.3f}%/leg "
          f"= {2*(config.FEE_RATE+SLIPPAGE)*100:.3f}% round trip")
    print("orderbook gate: DISABLED (no historical L2) | fills: next bar open")
    print("symbols replayed INDEPENDENTLY (not a true portfolio sim)")
    print(f"{'='*78}")

    for sym in symbols:
        try:
            df = fetch_bars(client, sym, args.days)
        except Exception as e:
            print(f"{sym}: fetch failed: {e}")
            continue
        sentinel = Sentinel()
        trades, curve, n = await replay_symbol(
            sym, df, args.equity, sentinel, args.min_conf)

        first = float(df["close"].iloc[WARMUP])
        last = float(df["close"].iloc[-1])
        bh = 100.0 * (last - first) / first

        results.append(summarize(sym, trades, curve, args.equity, bh))
        all_trades.extend(trades)
        r = results[-1]
        print(f"\n--- {sym} ({len(df)} bars, {n} evaluated) ---")
        print(f"   trades={r['trades']} win_rate={r['win_rate']}% "
              f"net=${r['net']:,.2f} ({r['net_pct']:+.2f}%) fees=${r['fees']:,.2f} "
              f"PF={r['profit_factor']} maxDD={r['max_dd_pct']}%")
        print(f"   buy&hold={r['buy_hold_pct']:+.2f}%  "
              f"strategy-minus-hold={r['excess_vs_hold_pct']:+.2f}%")
        for t in trades[:8]:
            print(f"      {t.entry_time:%m-%d %H:%M}->{t.exit_time:%m-%d %H:%M} "
                  f"net=${t.net:+9.2f} {t.reason[:40]}")
        if len(trades) > 8:
            print(f"      ... {len(trades)-8} more")

    if results:
        tot_net = sum(r["net"] for r in results)
        tot_fees = sum(r["fees"] for r in results)
        tot_trades = sum(r["trades"] for r in results)
        wins = [t for t in all_trades if t.net > 0]
        print(f"\n{'='*78}")
        if tot_trades:
            print(f"SUM OF INDEPENDENT RUNS: {tot_trades} trades, "
                  f"net=${tot_net:,.2f} ({100*tot_net/args.equity:+.2f}%), "
                  f"fees=${tot_fees:,.2f}, win_rate={100*len(wins)/tot_trades:.1f}%")
        else:
            print("NO TRADES")
        print(f"{'='*78}")

    out = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        f"backtest_entry_exit_d{args.days}_eq{int(args.equity)}_mc{args.min_conf}.json",
    )
    with open(out, "w") as f:
        json.dump({"params": vars(args), "results": results}, f, indent=2)
    print(f"\nwrote {out}")
    return results


if __name__ == "__main__":
    asyncio.run(main())
