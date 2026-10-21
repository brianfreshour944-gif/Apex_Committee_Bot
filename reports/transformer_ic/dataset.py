"""Dataset construction + purged time splits for the transformer IC experiment.

Reads the raw per-symbol feature cache produced by build_dataset.py and derives
labelled samples for a given forward-return horizon.

SAMPLE DEFINITION (matches live inference exactly)
  X[i] = features for bars [i-31 .. i]            (32-bar window, as fed to the
         shipped model by brains/transformer.py: df.iloc[:i+1].tail(32))
  y[i] = (open[i+1+h] / open[i+1] - 1) * 1e4      forward return in bps from the
         next bar's OPEN (the harness fill price) to h bars later.

  The decision is made on bar i's close; the fill is bar i+1's open. The label
  therefore begins one bar after the feature window ends, so features and labels
  never overlap in time.

PURGED, TIME-ORDERED SPLIT
  Samples from all three symbols are pooled and sorted by close timestamp. The
  split is on wall-clock time, not array position, so every symbol appears in
  every block. A purge gap of (h+1) bars is dropped at each boundary: two
  samples closer than h bars apart share part of their label window, and the
  later one's outcome would otherwise leak into the earlier one's block.

  Purge size = (h + 1) bars, i.e. (h + 1) * 15 minutes.

SCALING
  Feature standardisation is fit on the TRAIN block only (mean/std), never on
  val/test — fitting on the full series would leak test-period statistics into
  the training inputs.
"""

import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "cache", "raw.npz")

SEQ_LEN = 32
BAR_SECONDS = 15 * 60
# 1=15m 2=30m 4=60m 8=120m 24=360m 32=8h (the bot's max-hold window)
HORIZONS = [1, 2, 4, 8, 24, 32]


def load_raw():
    if not os.path.exists(RAW):
        raise FileNotFoundError(
            f"{RAW} missing — run reports/transformer_ic/build_dataset.py first"
        )
    return np.load(RAW)


def build_samples(raw, h, seq_len=SEQ_LEN):
    """Pool samples for horizon `h` across all symbols, sorted by timestamp.

    Returns dict with X (N,32,11) float32, y (N,) float32 bps, ts (N,) int64
    epoch seconds, sym (N,) int8, entry/exit open prices (N,) float64.
    """
    Xs, ys, ts, syms, ents, exts, clss, moms = [], [], [], [], [], [], [], []
    symbols = [str(s) for s in raw["symbols"]]
    for sid, symbol in enumerate(symbols):
        key = symbol.replace("/", "")
        feats = raw[f"{key}_feats"]
        open_ = raw[f"{key}_open"]
        close_ = raw[f"{key}_close"]
        ts_all = raw[f"{key}_ts"]
        n = len(feats)

        first = seq_len - 1
        last = n - 2 - h
        if last < first:
            continue
        idx = np.arange(first, last + 1)

        # X[j] = feats[idx[j]-31 : idx[j]+1]
        win = np.lib.stride_tricks.sliding_window_view(feats, seq_len, axis=0)
        win = np.transpose(win, (0, 2, 1))          # (n-31, 32, 11)
        Xs.append(win[idx - first].astype(np.float32))

        entry = open_[idx + 1]
        exit_ = open_[idx + 1 + h]
        ys.append(((exit_ / entry - 1.0) * 1e4).astype(np.float32))
        ents.append(entry)
        exts.append(exit_)
        clss.append(close_[idx])
        # 31-bar trailing return over the feature window, in bps. Computed per
        # symbol BEFORE the global time sort — a pooled rolling return would
        # straddle the symbol boundary.
        mom = np.zeros(len(idx), dtype=np.float32)
        prev = close_[idx - (seq_len - 1)]
        ok = prev > 0
        mom[ok] = (close_[idx][ok] / prev[ok] - 1.0) * 1e4
        moms.append(mom)
        ts.append(ts_all[idx])
        syms.append(np.full(len(idx), sid, dtype=np.int8))

    X = np.concatenate(Xs)
    y = np.concatenate(ys)
    ts = np.concatenate(ts)
    sym = np.concatenate(syms)
    ent = np.concatenate(ents)
    ext = np.concatenate(exts)
    cls = np.concatenate(clss)
    mom = np.concatenate(moms)

    order = np.argsort(ts, kind="stable")
    return {
        "X": X[order], "y": y[order], "ts": ts[order], "sym": sym[order],
        "entry": ent[order], "exit": ext[order], "close": cls[order],
        "mom": mom[order], "symbols": symbols,
    }


def time_split(ts, h, frac_train=0.60, frac_val=0.15, seq_len=SEQ_LEN):
    """Purged, time-ordered train/val/test masks.

    Splits on wall-clock time so every symbol appears in every block. Drops a
    purge gap of (h+1) bars at each boundary to stop label-window overlap.
    """
    purge = (h + 1) * BAR_SECONDS
    t_min, t_max = ts.min(), ts.max()
    span = t_max - t_min
    t1 = t_min + int(span * frac_train)          # train/val boundary
    t2 = t_min + int(span * (frac_train + frac_val))  # val/test boundary

    train = ts < (t1 - purge)
    val = (ts >= t1) & (ts < (t2 - purge))
    test = ts >= t2
    return {"train": train, "val": val, "test": test, "t1": t1, "t2": t2,
            "purge_seconds": purge}


def fit_scaler(X_train):
    """Per-feature mean/std from the train block only."""
    flat = X_train.reshape(-1, X_train.shape[-1]).astype(np.float64)
    mean = flat.mean(axis=0)
    std = flat.std(axis=0)
    std[std == 0] = 1.0
    return mean.astype(np.float32), std.astype(np.float32)


def apply_scaler(X, mean, std):
    return (X - mean) / std


def test_subperiods(ts_test, n_chunks=4):
    """Split test-block timestamps into n_chunks equal-time slices.

    Returns a list of boolean masks. Used for sign-stability: a real edge keeps
    its sign across adjacent sub-periods.
    """
    lo, hi = ts_test.min(), ts_test.max()
    edges = np.linspace(lo, hi + 1, n_chunks + 1).astype(np.int64)
    return [(ts_test >= edges[i]) & (ts_test < edges[i + 1])
            for i in range(n_chunks)]


def per_symbol_shuffle(d, seed=0):
    """Shuffle the label column within each symbol.

    Permuting the pooled array would mix symbols, so the shuffled labels would
    carry a cross-symbol mean-return signal the model could learn — a false
    positive in the leakage control. Shuffling within a symbol destroys the
    X->y relationship while preserving each symbol's return distribution.
    """
    rng = np.random.default_rng(seed)
    y = d["y"].copy()
    for sid in np.unique(d["sym"]):
        m = d["sym"] == sid
        y[m] = rng.permutation(y[m])
    return y


if __name__ == "__main__":
    raw = load_raw()
    for h in HORIZONS:
        d = build_samples(raw, h)
        sp = time_split(d["ts"], h)
        print(f"h={h:>2} ({h*15:>3}m): N={len(d['y']):>7} "
              f"train={sp['train'].sum():>7} val={sp['val'].sum():>6} "
              f"test={sp['test'].sum():>6} | purge={sp['purge_seconds']//60}min")
