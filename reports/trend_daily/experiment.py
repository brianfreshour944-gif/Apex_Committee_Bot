"""Low-turnover daily trend study: long-only MA / breakout filters vs buy & hold.

Question: does a simple long-only trend filter (price above a 100/200-day moving
average, or a 20-day breakout) beat buy-and-hold after realistic costs, with
enough statistical power to tell?

Design
------
- Assets: US/intl/EM equity ETFs, gold, long treasuries, three single names, and
  bitcoin. Daily bars, fixed end date, up to 31 years of history.
- Rules: ma50, ma100, ma200, breakout20 (20-day-high entry / 10-day-low exit).
- Engine: close-to-close returns, decisions lagged one bar (no look-ahead), cost
  charged per side so total cost = cost_per_round_trip * round trips.
- Comparison: every rule is measured against buy-and-hold over the IDENTICAL
  evaluation window (after the rule's warmup), so the only difference is timing.
- Inference: block-bootstrap CI on Sharpe and total return; a matched-exposure
  random-entry control (does the timing beat being long the same fraction of
  time?); and a walk-forward view of per-window stability.

Run:  PYTHONPATH=<repo> python reports/trend_daily/experiment.py
"""

import argparse
import json
import os
import sys
from datetime import UTC, datetime

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
for p in (REPO, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import engine
import rules as R
import stats as S

CACHE = os.path.join(HERE, "cache")
OUT = os.path.join(HERE, "results")
WARMUP = 252                     # longest lookback (ma200) + buffer
COST_RT_DEFAULT = 0.0006         # 6 bps round trip (ETF-like); crypto varies too


def log(msg):
    print(f"[{datetime.now(UTC):%H:%M:%S}] {msg}", flush=True)


def load_assets():
    data = np.load(os.path.join(CACHE, "daily.npz"))
    with open(os.path.join(CACHE, "meta.json")) as f:
        meta = json.load(f)
    return data, meta


def load_close(data, sym):
    return np.asarray(data[f"{sym}__close"], dtype=np.float64)


def eval_window(n):
    """Evaluation window shared by every rule and by buy-and-hold."""
    return slice(min(WARMUP, max(0, n - 1)), n)


def run_asset(close, names, cost_rt):
    win = eval_window(len(close))
    c = close[win]
    out = {}
    for name in list(names) + ["buy_hold"]:
        decide = np.ones(len(c)) if name == "buy_hold" else R.get_rule(name)(close)[win]
        res = engine.backtest(c, decide, cost_rt)
        out[name] = res
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost-rt", type=float, default=COST_RT_DEFAULT,
                    help="round-trip cost as a fraction (default 6 bps)")
    ap.add_argument("--n-boot", type=int, default=2000)
    ap.add_argument("--n-sims", type=int, default=2000)
    ap.add_argument("--windows", type=int, default=6)
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    data, meta = load_assets()
    names = list(R.RULES)
    report = {"meta": meta, "cost_round_trip": args.cost_rt,
              "generated": datetime.now(UTC).isoformat(), "assets": {}}

    log(f"cost/round trip = {args.cost_rt*1e4:.1f} bps | rules = {names}")

    for sym in meta["assets"]:
        if f"{sym}__close" not in data:
            continue
        close = load_close(data, sym)
        res = run_asset(close, names, args.cost_rt)
        n = res["buy_hold"]["bars"]
        rec = {"kind": meta["assets"][sym]["kind"], "bars_eval": n,
               "rules": {}}
        for name in list(names) + ["buy_hold"]:
            r = res[name]["returns"]
            ci_sh = S.block_bootstrap_stat(r, S.sharpe, 21, args.n_boot, seed=0)
            ci_tr = S.block_bootstrap_stat(
                r, lambda x: float(np.cumprod(1.0 + x)[-1] - 1.0),
                21, args.n_boot, seed=0)
            rec["rules"][name] = {
                "cagr": res[name]["cagr"], "sharpe": res[name]["sharpe"],
                "ann_vol": res[name]["ann_vol"],
                "max_drawdown": res[name]["max_drawdown"],
                "exposure": res[name]["exposure"],
                "n_round_trips": round(res[name]["n_round_trips"], 1),
                "total_return": res[name]["total_return"],
                "sharpe_ci": [round(ci_sh["lo"], 3), round(ci_sh["hi"], 3)],
                "sharpe_excl_zero": ci_sh["excludes_zero"],
                "total_return_ci": [round(ci_tr["lo"], 3), round(ci_tr["hi"], 3)],
            }
        report["assets"][sym] = rec
        bh = rec["rules"]["buy_hold"]
        best = max(names, key=lambda nm: rec["rules"][nm]["sharpe"])
        log(f"{sym:<8} B&H Sharpe {bh['sharpe']:+.2f} (CAGR {bh['cagr']:+.1%}) | "
            f"best rule {best} Sharpe {rec['rules'][best]['sharpe']:+.2f} "
            f"(CAGR {rec['rules'][best]['cagr']:+.1%}, RT {rec['rules'][best]['n_round_trips']:.0f})")

    # ---- paired test: does each rule beat buy & hold? (the real bar) ----
    log("\npaired test vs buy & hold (block bootstrap on the daily difference) ...")
    paired = {}
    for sym in meta["assets"]:
        if f"{sym}__close" not in data:
            continue
        close = load_close(data, sym)
        win = eval_window(len(close))
        c = close[win]
        bh_ret = engine.backtest(c, np.ones(len(c)), args.cost_rt)["returns"]
        paired[sym] = {}
        for name in names:
            dec = R.get_rule(name)(close)[win]
            rr = engine.backtest(c, dec, args.cost_rt)["returns"]
            res = S.paired_vs_benchmark(rr, bh_ret, 21, args.n_boot, seed=0)
            paired[sym][name] = res
    report["paired_vs_buy_hold"] = paired

    # ---- cost sensitivity: how much of any edge survives higher costs? ----
    log("cost sensitivity ...")
    cost_sens = {}
    for cost in (0.0, 0.0006, 0.002, 0.005):
        row = {}
        for sym in meta["assets"]:
            if f"{sym}__close" not in data:
                continue
            close = load_close(data, sym)
            win = eval_window(len(close))
            c = close[win]
            row[sym] = {name: engine.backtest(
                c, (np.ones(len(c)) if name == "buy_hold" else R.get_rule(name)(close)[win]),
                cost)["sharpe"] for name in names + ["buy_hold"]}
        cost_sens[f"{cost*1e4:.0f}bps"] = row
    report["cost_sensitivity"] = cost_sens

    # ---- walk-forward stability: fixed rule, per disjoint window Sharpe ----
    log("\nwalk-forward per-window Sharpe (fixed rules) ...")
    wf = {}
    for sym in meta["assets"]:
        if f"{sym}__close" not in data:
            continue
        close = load_close(data, sym)
        c = close[eval_window(len(close))]
        ret = engine.close_to_close(c)
        n = len(c)
        bounds = np.linspace(0, n, args.windows + 1).astype(int)
        wf[sym] = {"buy_hold": [], "ma200": [], "breakout20": []}
        for i in range(args.windows):
            a, b = bounds[i], bounds[i + 1]
            if b - a < 30:
                continue
            for rid in ("buy_hold", "ma200", "breakout20"):
                dec = np.ones(b - a) if rid == "buy_hold" else \
                    R.get_rule(rid)(close)[eval_window(len(close))][a:b]
                r = engine.backtest(c[a:b], dec, args.cost_rt)["returns"]
                wf[sym][rid].append(round(S.sharpe(r), 2))
    report["walk_forward"] = wf

    # ---- walk-forward rule SELECTION: does picking the best rule in-sample help OOS? ----
    # On each train span pick the rule with the best in-sample Sharpe; apply it to
    # the next span, which it has never seen. Stitch the out-of-sample pieces and
    # compare to buy & hold on the same days. This tests whether *choosing* a rule
    # generalises, which is the realistic use of a rule menu.
    log("walk-forward rule selection ...")
    sel = {}
    for sym in meta["assets"]:
        if f"{sym}__close" not in data:
            continue
        close = load_close(data, sym)
        win = eval_window(len(close))
        c = close[win]
        n = len(c)
        k = args.windows
        bounds = np.linspace(0, n, k + 2).astype(int)   # +1 so each has a train prefix
        oos = np.zeros(n)
        chosen_log = []
        for i in range(1, k + 1):
            tr_a, tr_b = 0, bounds[i]           # expanding train window
            te_a, te_b = bounds[i], bounds[i + 1]
            if te_b - te_a < 30:
                continue
            best, best_sh = None, -1e9
            for name in names:
                r = engine.backtest(c[tr_a:tr_b], R.get_rule(name)(close)[win][tr_a:tr_b],
                                    args.cost_rt)["returns"]
                sh = S.sharpe(r)
                if sh > best_sh:
                    best, best_sh = name, sh
            chosen_log.append({"window": i, "chosen": best, "train_sharpe": round(best_sh, 2),
                               "test_range": [int(te_a), int(te_b)]})
            dec = R.get_rule(best)(close)[win][te_a:te_b]
            oos[te_a:te_b] = engine.backtest(c[te_a:te_b], dec, args.cost_rt)["returns"]
        first_test = bounds[1]
        bh = engine.backtest(c[first_test:], np.ones(n - first_test), args.cost_rt)["returns"]
        sel[sym] = {
            "chosen": chosen_log,
            "oos_sharpe": round(S.sharpe(oos[first_test:]), 3),
            "bh_sharpe": round(S.sharpe(bh), 3),
            "beats_bh": bool(S.sharpe(oos[first_test:]) > S.sharpe(bh)),
        }
    report["walk_forward_selection"] = sel

    # ---- matched-exposure random-entry control ----
    log("matched-exposure random-entry control ...")
    ctrl = {}
    for sym in meta["assets"]:
        if f"{sym}__close" not in data:
            continue
        close = load_close(data, sym)
        c = close[eval_window(len(close))]
        ret = engine.close_to_close(c)
        ctrl[sym] = {}
        for name in names:
            decide = R.get_rule(name)(close)[eval_window(len(close))]
            res = engine.backtest(c, decide, args.cost_rt)
            sim = S.random_entry_control(ret, res["n_round_trips"],
                                         res["exposure"], args.cost_rt,
                                         n_sims=args.n_sims, seed=1)
            p = S.random_entry_pvalue(res["sharpe"], sim["sims"])
            bh_sh = engine.backtest(c, np.ones(len(c)), args.cost_rt)["sharpe"]
            ctrl[sym][name] = {"sharpe": res["sharpe"], "random_p95": round(sim["p95"], 3),
                               "p_value": round(p, 3), "beats_buy_hold": res["sharpe"] > bh_sh}
    report["random_entry_control"] = ctrl

    with open(os.path.join(OUT, "trend_study.json"), "w") as f:
        json.dump(report, f, indent=2)
    log(f"\nSaved {os.path.join(OUT, 'trend_study.json')}")
    summarise(report, names)


def summarise(report, names):
    print("\n=== SUMMARY: rule vs buy & hold (Sharpe; * beats B&H) ===")
    hdr = f"{'asset':<8}" + "".join(f"{n:>11}" for n in names) + f"{'buy_hold':>11}"
    print(hdr)
    for sym, rec in report["assets"].items():
        bh = rec["rules"]["buy_hold"]["sharpe"]
        cells = ""
        for n in names:
            s = rec["rules"][n]["sharpe"]
            cells += f"{s:>+10.2f}{'*' if s > bh else ' '}"
        print(f"{sym:<8}{cells}{bh:>+11.2f}")

    print("\n=== random-entry control p-values (does timing beat matched-exposure random?) ===")
    print(f"{'asset':<8}" + "".join(f"{n:>11}" for n in names))
    for sym, d in report["random_entry_control"].items():
        print(f"{sym:<8}" + "".join(f"{d[n]['p_value']:>11.3f}" for n in names))

    print("\n=== paired vs buy & hold: annualised excess return (CI); * CI excludes 0 ===")
    print(f"{'asset':<8}" + "".join(f"{n:>18}" for n in names))
    for sym, d in report.get("paired_vs_buy_hold", {}).items():
        cells = ""
        for n in names:
            e = d[n]
            cells += f"{e['ann_excess']:>+9.1%}{'*' if e['beats_bh'] else ' '}" \
                     f"[{e['ci'][0]:+.0%},{e['ci'][1]:+.0%}]"
        print(f"{sym:<8}{cells}")

    print("\n=== walk-forward rule SELECTION (OOS Sharpe vs B&H) ===")
    print(f"{'asset':<8}{'OOS Sharpe':>12}{'B&H Sharpe':>12}{'beats?':>8}   chosen rules (in-sample best)")
    for sym, d in report.get("walk_forward_selection", {}).items():
        picks = " ".join(f"w{c['window']}:{c['chosen']}" for c in d["chosen"])
        print(f"{sym:<8}{d['oos_sharpe']:>+12.2f}{d['bh_sharpe']:>+12.2f}"
              f"{'YES' if d['beats_bh'] else 'no':>8}   {picks}")


if __name__ == "__main__":
    main()
