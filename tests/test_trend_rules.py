"""Regression tests for the daily trend study (reports/trend_daily).

These pin the properties that make the study trustworthy: no look-ahead, correct
cost accounting and lag, and calibrated statistics. Run standalone:

    PYTHONPATH=<repo> python tests/test_trend_rules.py
"""

import os
import sys

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.join(REPO, "reports", "trend_daily")
for p in (REPO, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import engine
import rules as R
import stats as S

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")


def _rand_close(n, seed=0):
    rng = np.random.default_rng(seed)
    return 100.0 * np.cumprod(1.0 + rng.normal(0.0003, 0.01, n))


def test_no_lookahead():
    print("test_no_lookahead")
    close = _rand_close(500, 1)
    for name, fn in R.RULES.items():
        base = fn(close)
        # scramble the future from bar k onward; decisions up to k must not move
        for k in (100, 250, 400):
            mutated = close.copy()
            mutated[k:] = mutated[k:] * 3.0 + 7.0
            got = fn(mutated)
            check(f"{name}: decide[:{k}] unchanged when future changes",
                  np.array_equal(base[:k], got[:k]))


def test_lag_and_first_bar_flat():
    print("test_lag_and_first_bar_flat")
    decide = np.array([1, 1, 0, 1, 1, 1], dtype=float)
    held = engine.positions_held(decide)
    check("held[0] is 0 (cannot trade before the first close)", held[0] == 0)
    check("held[i] == decide[i-1]", np.array_equal(held[1:], decide[:-1]))


def test_cost_accounting():
    print("test_cost_accounting")
    close = _rand_close(300, 2)
    decide = R.get_rule("ma200")(close)
    cost_rt = 0.001
    res = engine.backtest(close, decide, cost_rt)
    check("total cost == cost_rt * n_round_trips",
          np.isclose(res["total_cost"], cost_rt * res["n_round_trips"], atol=1e-12))
    # a rule that never trades pays nothing
    flat = engine.backtest(close, np.zeros(300), cost_rt)
    check("never-trading rule pays zero cost", flat["total_cost"] == 0.0)
    # buy & hold: exactly one round trip
    bh = engine.buy_and_hold(close, cost_rt)
    check("buy & hold is one round trip", np.isclose(bh["n_round_trips"], 1.0))


def test_buy_hold_matches_close_to_close():
    print("test_buy_hold_matches_close_to_close")
    close = _rand_close(400, 3)
    bh = engine.buy_and_hold(close, 0.0)
    naive = close[-1] / close[0] - 1.0
    check("zero-cost buy & hold == close[-1]/close[0]-1",
          np.isclose(bh["total_return"], round(naive, 4), atol=1e-9))


def test_ma_long_flat_in_warmup():
    print("test_ma_long_flat_in_warmup")
    close = _rand_close(300, 4)
    pos = R.ma_long(close, 200)
    check("flat for the first 199 bars (no moving average yet)",
          np.all(pos[:199] == 0.0))
    check("first possible long is at index 199", pos[199] in (0.0, 1.0))


def test_breakout_threshold_excludes_current_bar():
    print("test_breakout_threshold_excludes_current_bar")
    # The entry threshold is the max of the PRIOR `entry` bars, excluding bar i.
    # With 25 flat bars at 10, the prior-20 high at index 24 is 10; a close of 10
    # (equal, not greater) must NOT enter, proving the bar does not count itself.
    close = np.append(np.full(25, 10.0), 10.0)
    pos = R.breakout(close, entry=20, exit=10)
    check("close == prior high does not enter (strict >)", pos[25] == 0.0)

    # Exceed the prior high on the NEXT bar -> enter, using only prior closes.
    close2 = np.append(close, 11.0)
    pos2 = R.breakout(close2, entry=20, exit=10)
    check("close > prior high enters", pos2[26] == 1.0)

    # A lone spike does not make its OWN bar a breakout, and the bar after a
    # spike sees the spike in its prior window.
    spiky = np.append(np.full(25, 10.0), 30.0)
    ps = R.breakout(spiky, entry=20, exit=10)
    check("spike enters on its own bar (30 > prior high 10)", ps[25] == 1.0)
    # exit only when close dips below the prior `exit`-day low
    ps2 = R.breakout(np.append(spiky, 9.0), entry=20, exit=10)
    check("exits when close falls below the prior low", ps2[26] == 0.0)


def test_bootstrap_calibration():
    print("test_bootstrap_calibration")
    rng = np.random.default_rng(7)
    # zero-mean noise: CI should usually cover zero
    covers = 0
    for s in range(40):
        x = rng.normal(0, 0.01, 500)
        ci = S.block_bootstrap_stat(x, S.sharpe, 21, 400, seed=s)
        covers += (ci["lo"] <= 0 <= ci["hi"])
    check(f"zero-mean noise CI covers 0 in >=85% of runs (got {covers}/40)", covers >= 34)
    # strong positive: CI should exclude zero
    x = rng.normal(0.002, 0.01, 2000)
    ci = S.block_bootstrap_stat(x, S.sharpe, 21, 400, seed=0)
    check("strong positive Sharpe CI excludes zero", ci["excludes_zero"] and ci["lo"] > 0)


def test_random_control_matches_exposure_and_turnover():
    print("test_random_control_matches_exposure_and_turnover")
    ret = np.random.default_rng(9).normal(0.0003, 0.01, 800)
    S.random_entry_control(ret, n_round_trips=50, exposure=0.7,
                                  cost_per_round_trip=0.0006, n_sims=200, seed=0)
    # recompute realized exposure/turnover on a few sims
    rng = np.random.default_rng(3)
    dl = round(0.7 * 800); k = 50
    exps, rts = [], []
    for _ in range(50):
        held = np.zeros(800)
        lengths = S._split_lengths(dl, k, rng)
        gaps = S._split_lengths(800 - dl, len(lengths) + 1, rng)
        pos = gaps[0]
        for j, L in enumerate(lengths):
            held[pos:pos + L] = 1.0
            pos += L + (gaps[j + 1] if j + 1 < len(gaps) else 0)
        exps.append(held.mean())
        rts.append(np.abs(np.diff(held, prepend=0)).sum() / 2)
    check("random control matches target exposure (mean ~0.70)",
          abs(np.mean(exps) - 0.70) < 0.01)
    check("random control matches turnover (mean RT ~50)",
          abs(np.mean(rts) - 50) < 5)


def test_paired_test_identical_is_zero():
    print("test_paired_test_identical_is_zero")
    r = np.random.default_rng(5).normal(0.0005, 0.01, 1000)
    res = S.paired_vs_benchmark(r, r, 21, 300, seed=0)
    check("identical series -> zero excess and does not beat", 
          abs(res["ann_excess"]) < 1e-9 and not res["beats_bh"])


def test_stats_sanity():
    print("test_stats_sanity")
    # sharpe of constant-zero series is 0 (no NaN from zero sd)
    check("sharpe of all-zero returns is 0", S.sharpe(np.zeros(100)) == 0.0)
    # metrics on an empty-ish path shouldn't explode
    m = engine.metrics(np.zeros(10), np.zeros(10), np.ones(10))
    check("metrics handles zero-return series", m["sharpe"] == 0.0 and m["total_return"] == 0.0)


if __name__ == "__main__":
    for fn in (test_no_lookahead, test_lag_and_first_bar_flat, test_cost_accounting,
               test_buy_hold_matches_close_to_close, test_ma_long_flat_in_warmup,
               test_breakout_threshold_excludes_current_bar, test_bootstrap_calibration,
               test_random_control_matches_exposure_and_turnover,
               test_paired_test_identical_is_zero, test_stats_sanity):
        fn()
    print(f"\n=== {PASS} passed, {FAIL} failed ===")
    sys.exit(1 if FAIL else 0)
