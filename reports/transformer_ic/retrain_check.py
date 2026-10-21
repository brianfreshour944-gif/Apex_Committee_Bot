"""Retrain the shipped architecture at the horizons where the shipped model IS stable.

The main experiment retrained only at h=32 (8h), the bot's max-hold window — and
found no out-of-sample IC. But the stability check showed the SHIPPED model's IC
is sign-stable across disjoint train/val/test blocks at h=1,4,8 (a weak but
persistent short-horizon association), and only breaks down at h=32.

So the single-horizon retrain tested the architecture at the one horizon where
the target is unstable. This script retrains at the STABLE horizons to answer the
right question: can this architecture be trained to reproduce the weak signal
that the shipped weights appear to capture?

If the retrained model reproduces a positive, CI-excludes-zero IC at h=1/8, the
architecture CAN learn the signal — it is simply too small to trade (spread vs
cost). If it does not, the shipped model's short-horizon IC is something the
forward-return objective does not recover.

Run:  PYTHONPATH=<repo> python reports/transformer_ic/retrain_check.py
"""

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

os.environ.setdefault("APCA_API_KEY_ID", "test")
os.environ.setdefault("APCA_API_SECRET_KEY", "test")

import dataset as ds
from experiment import evaluate, predict_logits, train_model

HORIZONS = [1, 8]
EPOCHS = 3
SEED = 0
OUT = os.path.join(HERE, "results")


def main():
    os.makedirs(OUT, exist_ok=True)
    raw = ds.load_raw()
    results = {"horizons": HORIZONS, "epochs": EPOCHS, "seed": SEED,
               "started": datetime.now(UTC).isoformat(), "runs": {}}

    for h in HORIZONS:
        print(f"\n=== retrain at h={h} ({h*15}m) ===", flush=True)
        d = ds.build_samples(raw, h)
        sp = ds.time_split(d["ts"], h)
        tr, va, te = sp["train"], sp["val"], sp["test"]
        mean, std = ds.fit_scaler(d["X"][tr])
        Xtr = ds.apply_scaler(d["X"][tr], mean, std)
        Xva = ds.apply_scaler(d["X"][va], mean, std)
        Xte = ds.apply_scaler(d["X"][te], mean, std)
        ytr, yva, yte = d["y"][tr], d["y"][va], d["y"][te]
        ts_te, sym_te = d["ts"][te], d["sym"][te]
        sym_va = d["sym"][va]

        # shipped model on the same test block for a like-for-like comparison
        import torch

        from brains.transformer import GrokGQA_Transformer
        from config import MODEL_PATH, SEQUENCE_LEN
        shipped = GrokGQA_Transformer(input_dim=11, seq_len=SEQUENCE_LEN)
        shipped.load_state_dict(torch.load(MODEL_PATH, map_location="cpu"),
                                strict=False)
        shipped_rep = evaluate(f"shipped h={h}", predict_logits(shipped, Xte),
                               yte, ts_te, sym_te, 150, 150)

        model, info = train_model(Xtr, ytr, Xva, yva, Xte, yte, sym_va, sym_te,
                                  f"retrained_h{h}", EPOCHS, SEED)
        retr_rep = evaluate(f"retrained h={h}", predict_logits(model, Xte),
                            yte, ts_te, sym_te, 150, 150)

        # ridge on the same features/horizon as a linear reference
        A = Xtr.reshape(len(Xtr), -1).astype(np.float64)
        w = np.linalg.solve(A.T @ A + 10.0 * np.eye(A.shape[1]), A.T @ ytr.astype(np.float64))
        ridge_rep = evaluate(f"ridge h={h}", Xte.reshape(len(Xte), -1).astype(np.float64) @ w,
                             yte, ts_te, sym_te, 150, 150)

        results["runs"][str(h)] = {"shipped": shipped_rep, "retrained": retr_rep,
                                   "ridge": ridge_rep, "train_info": info}
        with open(os.path.join(OUT, "retrain_check.json"), "w") as f:
            json.dump(results, f, indent=2)
        del d, Xtr, Xva, Xte

    print("\n=== SUMMARY ===")
    for h, r in results["runs"].items():
        print(f"h={h}: shipped IC {r['shipped']['ic']:+.4f} | "
              f"retrained IC {r['retrained']['ic']:+.4f} "
              f"[{r['retrained']['ci_lo']:+.4f},{r['retrained']['ci_hi']:+.4f}] "
              f"| ridge IC {r['ridge']['ic']:+.4f}")
    print(f"\nSaved {os.path.join(OUT, 'retrain_check.json')}")


if __name__ == "__main__":
    main()
