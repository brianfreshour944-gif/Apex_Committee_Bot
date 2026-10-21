"""IC (information coefficient) statistics for the transformer experiment.

IC = Spearman rank correlation between a model's prediction and the realised
forward return. It is scale-free and monotone, so it measures whether the model
ORDERS outcomes correctly, independent of calibration.

WHY PER-SYMBOL IC, NOT POOLED
  The experiment pools BTC/ETH/SOL samples. A pooled IC is contaminated by
  BETWEEN-symbol differences: if one symbol simply has a higher mean return and
  the model outputs a symbol-specific level, pooled IC is large even though the
  model has no within-symbol timing skill. That is a level bias, not a signal —
  and it is exactly what a model trained on destroyed labels can still exploit.

  A trade is a WITHIN-symbol timing decision, so the honest metric is the mean
  of the per-symbol ICs. The leakage control (shuffled labels) only reads ~0
  under the per-symbol metric; pooled it reads far from zero. Every function
  here is therefore symbol-aware, with `sym=None` meaning "one symbol" for
  simple synthetic tests.

WHY BLOCK BOOTSTRAP
  Samples are 15 minutes apart and the label spans h bars, so consecutive
  samples' label windows overlap and the IC series is serially dependent. An
  i.i.d. bootstrap would understate the CI. We resample contiguous BLOCKS of
  time (length ~4h bars) WITHIN each symbol, so dependence is preserved and no
  cross-symbol structure is introduced.

WHY A BLOCK-PERMUTATION NULL
  The null "no predictive information" must respect the same dependence. We
  permute predictions in blocks within each symbol and recompute the mean
  per-symbol IC many times to build the null distribution; the p-value is the
  fraction of null ICs at least as extreme. A pipeline that leaks the future
  shows up here as a shuffled-label model with an IC far from zero.
"""

import numpy as np


def spearman_ic(pred, ret):
    """Spearman rank IC. Returns 0.0 for degenerate (constant) input."""
    pred = np.asarray(pred, dtype=np.float64)
    ret = np.asarray(ret, dtype=np.float64)
    if len(pred) < 3:
        return 0.0
    pr = _rank(pred)
    rr = _rank(ret)
    pr = pr - pr.mean()
    rr = rr - rr.mean()
    denom = np.sqrt((pr * pr).sum() * (rr * rr).sum())
    if denom == 0.0:
        return 0.0
    return float((pr * rr).sum() / denom)


def _rank(x):
    """Average-rank transform (ties get the mean rank)."""
    order = np.argsort(x, kind="stable")
    ranks = np.empty(len(x), dtype=np.float64)
    ranks[order] = np.arange(1, len(x) + 1, dtype=np.float64)
    xs = x[order]
    i = 0
    n = len(x)
    while i < n:
        j = i + 1
        while j < n and xs[j] == xs[i]:
            j += 1
        if j - i > 1:
            ranks[order[i:j]] = ranks[order[i:j]].mean()
        i = j
    return ranks


def _block_indices(n, block_len, rng):
    """Contiguous-block resample indices covering [0, n)."""
    if n <= block_len:
        return np.arange(n)
    n_blocks = int(np.ceil(n / block_len))
    starts = rng.integers(0, n - block_len + 1, size=n_blocks)
    idx = np.concatenate([np.arange(s, s + block_len) for s in starts])
    return idx[:n]


def split_by_symbol(pred, ret, sym):
    """Group predictions and returns per symbol -> {sid: (pred_s, ret_s)}."""
    pred = np.asarray(pred, dtype=np.float64)
    ret = np.asarray(ret, dtype=np.float64)
    if sym is None:
        return {0: (pred, ret)}
    sym = np.asarray(sym)
    return {int(s): (pred[sym == s], ret[sym == s]) for s in np.unique(sym)}


def mean_symbol_ic(by_sym):
    """Mean of the per-symbol ICs. Returns (mean_ic, per_symbol_dict)."""
    per = {sid: spearman_ic(p, r) for sid, (p, r) in by_sym.items()}
    vals = list(per.values())
    return (float(np.mean(vals)) if vals else 0.0), per


def block_bootstrap_ci(pred, ret, block_len, n_boot=400, alpha=0.05, seed=0,
                       sym=None):
    """Percentile CI for the mean per-symbol IC under a within-symbol block bootstrap."""
    rng = np.random.default_rng(seed)
    by_sym = split_by_symbol(pred, ret, sym)
    boots = np.empty(n_boot)
    for b in range(n_boot):
        ics = []
        for p, r in by_sym.values():
            idx = _block_indices(len(p), block_len, rng)
            ics.append(spearman_ic(p[idx], r[idx]))
        boots[b] = np.mean(ics) if ics else 0.0
    lo = float(np.quantile(boots, alpha / 2))
    hi = float(np.quantile(boots, 1 - alpha / 2))
    return lo, hi, float(boots.mean())


def block_permutation_null(pred, ret, block_len, n_perm=400, seed=1, sym=None):
    """Null mean-per-symbol IC from block-resampling predictions within each symbol.

    Only the PREDICTIONS are resampled; returns stay in place. Applying the same
    index to both series would preserve the (pred, ret) pairing and make the
    null a no-op — the null mean would then equal the observed IC.
    """
    rng = np.random.default_rng(seed)
    by_sym = split_by_symbol(pred, ret, sym)
    null = np.empty(n_perm)
    for b in range(n_perm):
        ics = []
        for p, r in by_sym.values():
            idx = _block_indices(len(p), block_len, rng)
            ics.append(spearman_ic(p[idx], r))
        null[b] = np.mean(ics) if ics else 0.0
    return null


def p_value_from_null(ic, null):
    """Two-sided empirical p-value against a null distribution."""
    null = np.asarray(null)
    return float((np.abs(null) >= abs(ic)).mean())


def ic_report(pred, ret, block_len, n_boot=400, n_perm=400, seed=0, sym=None):
    """Full IC summary: mean per-symbol IC, block-bootstrap CI, permutation p-value."""
    by_sym = split_by_symbol(pred, ret, sym)
    ic, per = mean_symbol_ic(by_sym)
    lo, hi, _ = block_bootstrap_ci(pred, ret, block_len, n_boot=n_boot,
                                   seed=seed, sym=sym)
    null = block_permutation_null(pred, ret, block_len, n_perm=n_perm,
                                  seed=seed + 1000, sym=sym)
    p = p_value_from_null(ic, null)
    return {"ic": ic, "ci_lo": lo, "ci_hi": hi, "p": p,
            "excludes_zero": bool(lo > 0 or hi < 0),
            "per_symbol_ic": {k: round(v, 5) for k, v in per.items()}}


def long_short_spread(pred, ret, n_buckets=5, sym=None):
    """Mean forward return (bps) of the top vs bottom prediction bucket.

    Computed WITHIN each symbol (bucket edges are per-symbol quantiles) and
    averaged, so the number reflects timing skill rather than symbol levels.
    Economic significance: a spread smaller than trading cost is not tradeable.
    Cost in this repo is 0.90% round trip = 90 bps.
    """
    by_sym = split_by_symbol(pred, ret, sym)
    spreads, per_symbol = [], {}
    for sid, (p, r) in by_sym.items():
        edges = np.quantile(p, np.linspace(0, 1, n_buckets + 1))
        edges[-1] += 1e-12
        b = np.clip(np.digitize(p, edges[1:-1]), 0, n_buckets - 1)
        means = np.array([r[b == k].mean() if (b == k).any() else np.nan
                          for k in range(n_buckets)])
        s = float(means[-1] - means[0])
        spreads.append(s)
        per_symbol[sid] = round(s, 2)
    return {"mean_top_minus_bottom_bps": float(np.mean(spreads)) if spreads else 0.0,
            "per_symbol_bps": per_symbol}
