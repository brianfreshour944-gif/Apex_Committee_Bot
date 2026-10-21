"""Shipped-model stability check — is grok_gqa_v9_best.pth's IC durable or period-specific?

The main experiment found the SHIPPED model carries a small but statistically
significant mean per-symbol IC on the test block (~+0.036 at h=32), while a
model RETRAINED on forward returns does not (+0.006, CI includes zero). That
asymmetry has two candidate explanations:

  1. TRAIN CONTAMINATION — the shipped weights saw data overlapping the early
     period, so its apparent skill is memorisation. Diagnostic: its IC would be
     HIGHEST on the train block and decay toward test.
  2. PERIOD-SPECIFIC NOISE — the relationship is not durable and its sign moves
     between disjoint periods. Diagnostic: the IC flips sign across the
     train / val / test blocks.

This script distinguishes them by computing the shipped model's mean per-symbol
IC on each block separately. The same feature standardisation as the main
experiment is used (scaler fit on train only), so the numbers are comparable.

Run:  PYTHONPATH=<repo> python reports/transformer_ic/shipped_stability.py
"""

import json
import os
import sys

import numpy as np
import torch

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
for p in (REPO, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("APCA_API_KEY_ID", "test")
os.environ.setdefault("APCA_API_SECRET_KEY", "test")

import dataset as ds
import stats as st

from brains.transformer import GrokGQA_Transformer
from config import MODEL_PATH, SEQUENCE_LEN

HORIZONS = [1, 4, 8, 32]
OUT = os.path.join(HERE, "results")


def predict_logits(model, X, batch=4096):
    model.eval()
    out = np.empty(len(X), dtype=np.float64)
    with torch.no_grad():
        for i in range(0, len(X), batch):
            out[i:i + batch] = model(torch.from_numpy(X[i:i + batch])).squeeze(-1).numpy()
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    model = GrokGQA_Transformer(input_dim=11, seq_len=SEQUENCE_LEN)
    model.load_state_dict(torch.load(MODEL_PATH, map_location="cpu"), strict=False)
    model.eval()
    raw = ds.load_raw()

    results = {}
    print(f"{'h':>3} {'bars':>5} | {'train':>9} {'val':>9} {'test':>9} | "
          f"{'mean':>9} {'sign_stable':>11}")
    for h in HORIZONS:
        d = ds.build_samples(raw, h)
        sp = ds.time_split(d["ts"], h)
        mean, std = ds.fit_scaler(d["X"][sp["train"]])
        row = {}
        for name in ("train", "val", "test"):
            m = sp[name]
            p = predict_logits(model, ds.apply_scaler(d["X"][m], mean, std))
            ic, per = st.mean_symbol_ic(st.split_by_symbol(p, d["y"][m], d["sym"][m]))
            row[name] = {"ic": round(ic, 5),
                         "per_symbol_ic": {k: round(v, 5) for k, v in per.items()}}
        ics = [row[n]["ic"] for n in ("train", "val", "test")]
        row["mean_ic"] = round(float(np.mean(ics)), 5)
        row["sign_stable_across_blocks"] = len({1 if x > 0 else -1 for x in ics}) == 1
        results[str(h)] = row
        print(f"{h:>3} {h*15:>4}m | {row['train']['ic']:>+9.4f} "
              f"{row['val']['ic']:>+9.4f} {row['test']['ic']:>+9.4f} | "
              f"{row['mean_ic']:>+9.4f} {row['sign_stable_across_blocks']!s:>11}")

    results["interpretation"] = (
        "If train IC is not the largest, contamination is ruled out. If the sign "
        "moves between blocks, the apparent IC is period-specific noise, not a "
        "durable edge."
    )
    out = os.path.join(OUT, "shipped_stability.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()
