"""Statistics for the daily trend study: block bootstrap, random-entry control,
and a walk-forward rule selector.

These reuse the discipline from reports/transformer_ic/stats.py (block bootstrap,
empirical nulls) but the object of inference is different: the statistic is a
strategy's Sharpe or total return, not an information coefficient.

Two nests of the same question:
  1. Is the strategy's Sharpe distinguishable from zero, accounting for
     autocorrelation? -> block bootstrap CI on the per-bar return series.
  2. Does the TIMING add anything over being long the same fraction of the time?
     -> matched-exposure random-entry control. We generate random long-only
     schedules with the SAME number of round trips and the same average exposure,
     and see where the real rule's Sharpe sits in that distribution. This is the
     control that matters: a long-only rule on a rising asset looks good purely
     from beta, so beating buy-and-hold is the bar, not beating zero.
"""

import numpy as np


def block_indices(n, block_len, rng):
    """A block-resampled index of length n (moving-block bootstrap)."""
    n_blocks = int(np.ceil(n / block_len))
    starts = rng.integers(0, max(1, n - block_len + 1), size=n_blocks)
    idx = np.concatenate([np.arange(s, s + block_len) for s in starts])[:n]
    return idx


def sharpe(returns, ann=252):
    sd = returns.std(ddof=1)
    return float(returns.mean() / sd * np.sqrt(ann)) if sd > 0 else 0.0


def block_bootstrap_stat(returns, stat_fn, block_len=21, n_boot=2000, seed=0, alpha=0.05):
    """Block-bootstrap CI for a statistic computed on a return series.

    Blocks of ~21 bars (one trading month) preserve short-range autocorrelation;
    the daily series is resampled with replacement in blocks.
    """
    returns = np.asarray(returns, dtype=np.float64)
    n = len(returns)
    rng = np.random.default_rng(seed)
    stats = np.empty(n_boot)
    for b in range(n_boot):
        idx = block_indices(n, block_len, rng)
        stats[b] = stat_fn(returns[idx])
    lo = float(np.quantile(stats, alpha / 2))
    hi = float(np.quantile(stats, 1 - alpha / 2))
    return {"stat": float(stat_fn(returns)), "lo": lo, "hi": hi,
            "boot_mean": float(stats.mean()), "boot_sd": float(stats.std()),
            "excludes_zero": bool(lo > 0 or hi < 0)}


def random_entry_control(ret, n_round_trips, exposure, cost_per_round_trip,
                         n_sims=2000, seed=0, stat_fn=None):
    """Matched-exposure random long-only schedules.

    Build random schedules with (approximately) the SAME number of round trips
    and the SAME average exposure, then score them on the same return series. If
    the real rule's statistic does not sit in the upper tail of this
    distribution, its timing carries no information beyond being long.

    A schedule is `k` long blocks totalling `days_long` days, placed at random
    non-overlapping positions. A block is a run of consecutive long days, which
    mimics the persistence of a trend rule (a uniform random subset would have
    unrealistically many one-day trips).
    """
    ret = np.asarray(ret, dtype=np.float64)
    n = len(ret)
    stat_fn = stat_fn or sharpe
    rng = np.random.default_rng(seed)
    days_long = round(np.clip(exposure, 0.0, 1.0) * n)
    k = max(1, min(round(n_round_trips), days_long))
    n_sims = int(n_sims)
    out = np.empty(n_sims)
    cost_per_side = cost_per_round_trip / 2.0

    for s in range(n_sims):
        held = np.zeros(n, dtype=np.float64)
        if days_long > 0:
            lengths = _split_lengths(days_long, k, rng)
            gaps = _split_lengths(n - days_long, len(lengths) + 1, rng)
            pos = gaps[0]
            for j, L in enumerate(lengths):
                held[pos:pos + L] = 1.0
                pos += L + (gaps[j + 1] if j + 1 < len(gaps) else 0)
        change = np.abs(np.diff(held, prepend=0.0))
        out[s] = stat_fn(held * ret - cost_per_side * change)

    return {"sims": out, "mean": float(out.mean()), "sd": float(out.std()),
            "p95": float(np.quantile(out, 0.95))}


def _split_lengths(total, k, rng):
    """Split a non-negative integer `total` into `k` parts (zeros allowed)."""
    total = int(total)
    k = max(1, int(k))
    if total <= 0:
        return [0] * k
    if k == 1:
        return [total]
    if total <= k:
        parts = np.zeros(k, dtype=int)
        parts[rng.choice(k, size=total, replace=False)] = 1
        return parts.tolist()
    cuts = np.sort(rng.choice(np.arange(1, total), size=k - 1, replace=False))
    return [int(p) for p in np.diff(np.concatenate([[0], cuts, [total]]))]


def random_entry_pvalue(real_stat, sim_stats):
    """Upper-tail p-value: fraction of random schedules at least as good."""
    sim_stats = np.asarray(sim_stats, dtype=np.float64)
    return float((np.sum(sim_stats >= real_stat) + 1) / (len(sim_stats) + 1))


def paired_vs_benchmark(rule_ret, bh_ret, block_len=21, n_boot=2000, seed=0):
    """Test whether a rule beats buy & hold on the paired daily return difference.

    The bar for a long-only timing rule is buy & hold, not zero. Form the daily
    difference `d = rule_ret - bh_ret` and block-bootstrap its mean. Because both
    series share the same bars, the difference removes common beta, so this is a
    much more powerful test of *timing* than comparing two Sharpe CIs.

    Reports the annualised excess return and a CI/p-value on the daily mean.
    """
    rule_ret = np.asarray(rule_ret, dtype=np.float64)
    bh_ret = np.asarray(bh_ret, dtype=np.float64)
    d = rule_ret - bh_ret
    rng = np.random.default_rng(seed)
    n = len(d)
    means = np.empty(n_boot)
    for b in range(n_boot):
        means[b] = d[block_indices(n, block_len, rng)].mean()
    lo, hi = float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))
    # two-sided empirical p: |bootstrapped mean| as large as observed
    p = float((np.sum(np.abs(means - d.mean()) >= abs(d.mean())) + 1) / (n_boot + 1))
    return {
        "ann_excess": round(float(d.mean() * 252), 4),
        "daily_mean": float(d.mean()),
        "ci": [round(lo * 252, 4), round(hi * 252, 4)],
        "p_value": round(p, 4),
        "beats_bh": bool(lo > 0),
    }

