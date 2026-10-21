"""Regression tests for the IC statistics used by the transformer experiment.

The experiment's verdict rests on three claims that must be true of the
statistics, not just of the data:

  1. `spearman_ic` is a correct rank correlation (1.0 monotone, -1.0 reversed,
     ~0 independent, tie-aware).
  2. The block-permutation p-value is CALIBRATED under the null: when
     predictions and returns are independent, p < 0.05 at about the nominal 5%
     rate. If it were anti-conservative, "significant" would mean nothing.
  3. The block-bootstrap CI has roughly nominal coverage: a known-IC signal's
     CI contains the true IC about 95% of the time. If it were too narrow, an
     edge would look established when it is not.

  4. `per_symbol_shuffle` destroys the X->y relationship while preserving each
     symbol's return distribution — the property the leakage control relies on.

These are synthetic-data tests with known ground truth, so a failure means the
statistics are wrong, not that the market is uncooperative.

Run:  PYTHONPATH=<repo> python tests/test_ic_stats.py
"""

import os
import sys

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "reports", "transformer_ic"))

import stats as st

_passed = 0
_failed = 0


def check(label, condition, detail=""):
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  PASS: {label}" + (f" | {detail}" if detail else ""))
    else:
        _failed += 1
        print(f"  FAIL: {label}" + (f" | {detail}" if detail else ""))


def test_spearman_basic():
    print("\n--- 1. spearman_ic correctness ---")
    x = np.arange(100, dtype=np.float64)
    check("monotone increasing -> +1", abs(st.spearman_ic(x, x * 3 + 1) - 1.0) < 1e-9)
    check("monotone decreasing -> -1", abs(st.spearman_ic(x, -x) + 1.0) < 1e-9)
    rng = np.random.default_rng(0)
    indep = st.spearman_ic(rng.standard_normal(5000), rng.standard_normal(5000))
    check("independent -> ~0", abs(indep) < 0.05, f"ic={indep:+.4f}")
    # ties: all-equal predictions carry no ordering -> IC must be 0, not NaN
    check("constant predictions -> 0 (no NaN)",
          st.spearman_ic(np.ones(50), rng.standard_normal(50)) == 0.0)
    check("too few points -> 0", st.spearman_ic([1.0, 2.0], [1.0, 2.0]) == 0.0)


def test_permutation_null_is_calibrated():
    print("\n--- 2. permutation p-value calibration under H0 ---")
    rng = np.random.default_rng(1)
    n, block = 1500, 16
    n_sims, n_perm = 120, 120
    reject = 0
    for _ in range(n_sims):
        pred = rng.standard_normal(n)
        ret = rng.standard_normal(n)          # independent by construction
        null = st.block_permutation_null(pred, ret, block, n_perm=n_perm,
                                         seed=int(rng.integers(1e6)))
        ic = st.spearman_ic(pred, ret)
        if st.p_value_from_null(ic, null) < 0.05:
            reject += 1
    rate = reject / n_sims
    # nominal 5%; allow generous slack for 120 sims (binomial sd ~2%)
    check("false-positive rate near 5% under the null", rate <= 0.12,
          f"reject rate {rate:.3f} over {n_sims} sims")


def test_bootstrap_coverage():
    print("\n--- 3. block-bootstrap CI coverage ---")
    rng = np.random.default_rng(2)
    n, block = 1500, 16
    true_rho = 0.0
    covered = 0
    n_sims = 120
    for _ in range(n_sims):
        ret = rng.standard_normal(n)
        # prediction with no relationship: true IC is exactly 0
        pred = rng.standard_normal(n)
        lo, hi, _ = st.block_bootstrap_ci(pred, ret, block, n_boot=120,
                                          seed=int(rng.integers(1e6)))
        if lo <= true_rho <= hi:
            covered += 1
    cov = covered / n_sims
    check("CI covers the true IC (0) about 95% of the time", cov >= 0.85,
          f"coverage {cov:.3f} over {n_sims} sims")


def test_ic_detects_planted_signal():
    print("\n--- 4. IC detects a planted signal ---")
    rng = np.random.default_rng(3)
    n = 4000
    signal = rng.standard_normal(n)
    # return = 0.3*signal + noise -> positive rank association
    ret = 0.3 * signal + rng.standard_normal(n)
    rep = st.ic_report(signal, ret, block_len=16, n_boot=150, n_perm=150)
    check("planted positive signal -> IC > 0 and CI excludes zero",
          rep["ic"] > 0.05 and rep["excludes_zero"],
          f"ic={rep['ic']:+.4f} ci=[{rep['ci_lo']:+.4f},{rep['ci_hi']:+.4f}]")
    # reversed signal -> negative
    rep2 = st.ic_report(-signal, ret, block_len=16, n_boot=150, n_perm=150)
    check("reversed signal -> IC < 0 and CI excludes zero",
          rep2["ic"] < -0.05 and rep2["excludes_zero"],
          f"ic={rep2['ic']:+.4f}")


def test_long_short_spread():
    print("\n--- 5. long/short spread ---")
    rng = np.random.default_rng(4)
    n = 5000
    pred = rng.standard_normal(n)
    ret = 2.0 * pred + rng.standard_normal(n)     # strong monotone relation
    sp = st.long_short_spread(pred, ret, n_buckets=5)
    check("top-minus-bottom positive and large",
          sp["mean_top_minus_bottom_bps"] > 0,
          f"spread={sp['mean_top_minus_bottom_bps']:.2f}")


def test_per_symbol_shuffle():
    print("\n--- 6. per_symbol_shuffle destroys the X->y link ---")
    sys.path.insert(0, os.path.join(REPO, "reports", "transformer_ic"))
    import dataset as ds
    rng = np.random.default_rng(5)
    n = 3000
    # two "symbols" with different return distributions
    sym = np.concatenate([np.zeros(n // 2, np.int8), np.ones(n // 2, np.int8)])
    y = np.concatenate([rng.normal(0, 1, n // 2), rng.normal(5, 3, n // 2)])
    pred = y + rng.normal(0, 0.1, n)             # strongly predictive
    d = {"y": y, "sym": sym}
    ic_before = st.mean_symbol_ic(st.split_by_symbol(pred, y, sym))[0]
    y_shuf = ds.per_symbol_shuffle(d, seed=7)
    ic_after = st.mean_symbol_ic(st.split_by_symbol(pred, y_shuf, sym))[0]
    check("original within-symbol link is strong", ic_before > 0.9, f"ic={ic_before:.3f}")
    check("shuffled within-symbol link is destroyed", abs(ic_after) < 0.1,
          f"ic={ic_after:.3f}")
    # per-symbol distribution preserved
    m0 = sym == 0
    check("symbol 0 mean preserved", abs(y_shuf[m0].mean() - y[m0].mean()) < 0.15,
          f"{y_shuf[m0].mean():.2f} vs {y[m0].mean():.2f}")
    m1 = sym == 1
    check("symbol 1 mean preserved (no cross-symbol mixing)",
          abs(y_shuf[m1].mean() - y[m1].mean()) < 0.25,
          f"{y_shuf[m1].mean():.2f} vs {y[m1].mean():.2f}")


def test_per_symbol_metric_removes_level_bias():
    """The core methodological fix: pooled IC is inflated by between-symbol
    level differences; the mean per-symbol IC is not.

    Construct a prediction that has NO within-symbol information but is offset
    per symbol to match each symbol's mean return. Pooled IC will be large
    (the model 'predicts' which symbol wins); per-symbol IC must be ~0 (it
    cannot time within a symbol). If this test fails, every 'significant' IC in
    the experiment could be a level-bias artefact.
    """
    print("\n--- 8. per-symbol IC removes between-symbol level bias ---")
    rng = np.random.default_rng(9)
    n = 6000
    sym = np.repeat(np.arange(3, dtype=np.int8), n // 3)
    levels = np.array([-5.0, 0.0, 5.0])            # symbol mean returns (bps)
    y = levels[sym] + rng.standard_normal(n)
    # prediction = pure per-symbol level, no within-symbol variation
    pred = levels[sym] + rng.standard_normal(n) * 1e-6

    pooled = st.spearman_ic(pred, y)
    by_sym = st.split_by_symbol(pred, y, sym)
    per_ic, per = st.mean_symbol_ic(by_sym)
    check("pooled IC is large (level bias)", abs(pooled) > 0.3, f"pooled={pooled:+.3f}")
    check("per-symbol IC is ~0 (no within-symbol skill)", abs(per_ic) < 0.05,
          f"per_symbol={per_ic:+.4f} {per}")

    # and the real signal case: within-symbol skill IS detected
    y2 = levels[sym] + 2.0 * rng.standard_normal(n)
    pred2 = np.zeros(n)
    for s in (0, 1, 2):
        m = sym == s
        pred2[m] = y2[m] + rng.standard_normal(m.sum()) * 0.1
    per_ic2 = st.mean_symbol_ic(st.split_by_symbol(pred2, y2, sym))[0]
    check("within-symbol skill IS detected", per_ic2 > 0.5, f"per_symbol={per_ic2:+.3f}")


def test_permutation_null_centred():
    """The permutation null must be centred near zero and must NOT reproduce the
    observed IC.

    Bug pinned here: an earlier version applied the same resample index to both
    predictions and returns, which preserves their pairing. The null mean then
    equalled the observed IC and every p-value was ~0.5-0.7. A correct null
    resamples the predictions only.
    """
    print("\n--- 9. permutation null is centred and detects no signal ---")
    rng = np.random.default_rng(11)
    n = 3000
    sym = np.repeat(np.arange(3, dtype=np.int8), n // 3)
    levels = np.array([-5.0, 0.0, 5.0])
    y = levels[sym] + rng.standard_normal(n)
    # pure level-bias prediction: big pooled IC, no within-symbol skill
    pred = levels[sym] + rng.standard_normal(n) * 1e-6
    pooled = st.spearman_ic(pred, y)
    ic = st.mean_symbol_ic(st.split_by_symbol(pred, y, sym))[0]
    null = st.block_permutation_null(pred, y, block_len=16, n_perm=200,
                                     seed=1, sym=sym)
    check("null mean is near zero", abs(null.mean()) < 0.02,
          f"null_mean={null.mean():+.4f}")
    check("null does NOT reproduce the large pooled (level-bias) IC",
          abs(null.mean() - pooled) > 0.3,
          f"pooled={pooled:+.3f} null_mean={null.mean():+.4f}")
    p = st.p_value_from_null(ic, null)
    check("spurious level-bias signal is NOT significant", p > 0.05, f"p={p:.3f}")

    # a genuine within-symbol signal must still be detected
    y2 = levels[sym] + 2.0 * rng.standard_normal(n)
    pred2 = np.zeros(n)
    for s in (0, 1, 2):
        m = sym == s
        pred2[m] = y2[m] + rng.standard_normal(m.sum()) * 0.1
    ic2 = st.mean_symbol_ic(st.split_by_symbol(pred2, y2, sym))[0]
    null2 = st.block_permutation_null(pred2, y2, block_len=16, n_perm=200,
                                      seed=1, sym=sym)
    p2 = st.p_value_from_null(ic2, null2)
    check("genuine within-symbol signal IS significant", p2 < 0.05,
          f"ic={ic2:+.3f} p={p2:.3f}")


def test_time_split_purge():
    print("\n--- 7. purged time split ---")
    import dataset as ds
    n = 20000
    ts = np.arange(n, dtype=np.int64) * 900 + 1_600_000_000
    h = 8
    sp = ds.time_split(ts, h)
    purge = sp["purge_seconds"]
    tr_max = ts[sp["train"]].max()
    va_min = ts[sp["val"]].min()
    va_max = ts[sp["val"]].max()
    te_min = ts[sp["test"]].min()
    check("train precedes val", tr_max < va_min)
    check("val precedes test", va_max < te_min)
    check("train->val gap >= purge", (va_min - tr_max) >= purge,
          f"gap={va_min-tr_max}s purge={purge}s")
    check("val->test gap >= purge", (te_min - va_max) >= purge,
          f"gap={te_min-va_max}s purge={purge}s")
    check("all three blocks non-empty",
          sp["train"].sum() > 0 and sp["val"].sum() > 0 and sp["test"].sum() > 0)


if __name__ == "__main__":
    test_spearman_basic()
    test_permutation_null_is_calibrated()
    test_bootstrap_coverage()
    test_ic_detects_planted_signal()
    test_long_short_spread()
    test_per_symbol_shuffle()
    test_per_symbol_metric_removes_level_bias()
    test_permutation_null_centred()
    test_time_split_purge()
    print(f"\n=== {_passed} passed, {_failed} failed ===")
    raise SystemExit(1 if _failed else 0)
