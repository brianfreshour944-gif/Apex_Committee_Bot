"""Transformer IC experiment — can the modelling approach carry predictive signal?

THE QUESTION
  Six prior hypotheses (exits, sizing, cost, account size, committee confidence)
  were tested and ruled out as the primary problem. The remaining question sits
  upstream of all of them: can these 3 brains produce a predictive signal at
  all? This tests the transformer brain, because it is the highest-weight brain
  (50%) and the only learned one.

WHAT IT MEASURES
  1. SHIPPED model — grok_gqa_v9_best.pth, the exact deployed weights — scored
     out-of-sample as raw logit vs forward return, at every horizon. No
     retraining, no tuning: is the model running in production predictive on
     data it has never been evaluated against?
  2. CONTROLS — random, a 32-bar momentum rule, a ridge regression on the same
     features, and a model trained on SHUFFLED labels. The shuffled-label model
     is the leakage detector: if destroyed labels still show test IC away from
     zero, the pipeline leaks and no other number here can be trusted.
  3. RETRAINED model — the SAME architecture and features, trained directly
     against forward returns with a purged, time-ordered split. This is the
     "can the approach work at all" test: it removes the possibility that the
     shipped weights are simply mislabelled or badly fit.

VERDICT CRITERIA (stated before running, so the result cannot be reinterpreted)
  * Edge exists  : test IC CI excludes zero AND holds its sign across adjacent
                   test sub-periods AND top-minus-bottom bucket spread exceeds
                   the ~90 bps round-trip cost.
  * Approach dead: test IC CI includes zero, OR the sign flips between adjacent
                   sub-periods (a real edge does not reverse between neighbours),
                   OR the spread is far below cost.

RUNTIME / CHECKPOINTING
  The container trains at ~440 samples/s, so the full run takes tens of minutes.
  The script checkpoints results/experiment.json after every phase and resumes
  from it, so a runtime restart does not lose completed work.

Run:  PYTHONPATH=<repo> python reports/transformer_ic/experiment.py [--quick]
"""

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime

import numpy as np
import torch
from torch import nn

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

OUT = os.path.join(HERE, "results")
RESULTS = os.path.join(OUT, "experiment.json")
CKPT = os.path.join(OUT, "checkpoint.json")
BLOCK_BARS = 64              # 16h: exceeds the prediction autocorrelation timescale
ROUND_TRIP_COST_BPS = 90.0   # 0.25% taker x2 legs + slippage x2 (per AGENTS.md)
PRIMARY_H = 32               # 8h = the bot's max-hold window


def log(msg):
    print(f"[{datetime.now(UTC):%H:%M:%S}] {msg}", flush=True)


def load_ckpt():
    if os.path.exists(CKPT):
        with open(CKPT) as f:
            return json.load(f)
    return {"config": {}, "shipped": {}, "controls": {}, "models": {}}


def save_ckpt(c):
    os.makedirs(OUT, exist_ok=True)
    c["updated"] = datetime.now(UTC).isoformat()
    tmp = CKPT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(c, f, indent=2)
    os.replace(tmp, CKPT)


# ── Model loading / inference ─────────────────────────────────────────────────

def load_shipped_model():
    model = GrokGQA_Transformer(input_dim=11, seq_len=SEQUENCE_LEN)
    state = torch.load(MODEL_PATH, map_location="cpu")
    model.load_state_dict(state, strict=False)
    model.eval()
    return model


def predict_logits(model, X, batch=4096):
    model.eval()
    out = np.empty(len(X), dtype=np.float64)
    with torch.no_grad():
        for i in range(0, len(X), batch):
            out[i:i + batch] = model(torch.from_numpy(X[i:i + batch])).squeeze(-1).numpy()
    return out


def ridge_baseline(Xtr, ytr, Xte, alpha=10.0):
    """Ridge on flattened feature windows — the linear ceiling for these features."""
    A = Xtr.reshape(len(Xtr), -1).astype(np.float64)
    b = ytr.astype(np.float64)
    AtA = A.T @ A
    w = np.linalg.solve(AtA + alpha * np.eye(AtA.shape[0]), A.T @ b)
    return Xte.reshape(len(Xte), -1).astype(np.float64) @ w


# ── Evaluation ────────────────────────────────────────────────────────────────

def evaluate(name, pred, y, ts, sym, n_boot=200, n_perm=200):
    """Symbol-aware IC report: mean per-symbol IC, CI, p-value, spread, stability."""
    rep = st.ic_report(pred, y, BLOCK_BARS, n_boot=n_boot, n_perm=n_perm, sym=sym)
    spread = st.long_short_spread(pred, y, sym=sym)
    # Sign stability: mean per-symbol IC within each time chunk. A real edge
    # keeps its sign across adjacent sub-periods; a level-bias artefact does not.
    sub = ds.test_subperiods(ts, n_chunks=4)
    sub_ics = [st.mean_symbol_ic(st.split_by_symbol(pred[m], y[m], sym[m]))[0]
               for m in sub]
    signs = {1 if s > 0 else -1 for s in sub_ics}
    rep.update({
        "name": name,
        "n": len(y),
        "subperiod_ics": [round(s, 4) for s in sub_ics],
        "sign_stable": len(signs) == 1,
        "spread_bps": round(spread["mean_top_minus_bottom_bps"], 2),
        "spread_per_symbol_bps": spread["per_symbol_bps"],
        "spread_clears_cost": spread["mean_top_minus_bottom_bps"] > ROUND_TRIP_COST_BPS,
    })
    rep["edge"] = bool(rep["excludes_zero"] and rep["sign_stable"]
                       and rep["spread_clears_cost"])
    verdict = "EDGE" if rep["edge"] else "no edge"
    log(f"  {name:<24} IC {rep['ic']:+.4f} [{rep['ci_lo']:+.4f},{rep['ci_hi']:+.4f}] "
        f"p={rep['p']:.3f} spread {rep['spread_bps']:+7.1f}bps "
        f"sub={rep['subperiod_ics']} -> {verdict}")
    return rep


# ── Training ──────────────────────────────────────────────────────────────────

def train_model(Xtr, ytr, Xva, yva, Xte, yte, sym_va, sym_te, tag, epochs, seed,
                batch=512, lr=1e-3, ckpt=None):
    """Train the shipped architecture on forward returns; checkpoint per epoch.

    Selects the epoch with the best validation IC — the honest model-selection
    criterion, since test is never used to pick the checkpoint. Both the val and
    test diagnostics use the mean PER-SYMBOL IC (see stats.py) so a between-symbol
    level bias cannot masquerade as skill. Returns the best-val state dict.
    """
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    y_mean, y_std = float(ytr.mean()), float(ytr.std() or 1.0)
    yt = ((ytr - y_mean) / y_std).astype(np.float32)

    model = GrokGQA_Transformer(input_dim=11, seq_len=SEQUENCE_LEN)
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-5)
    lossf = nn.MSELoss()
    Xt = torch.from_numpy(Xtr)
    Yt = torch.from_numpy(yt)
    n = len(Xt)

    def sym_ic(pred, y, sym):
        return st.mean_symbol_ic(st.split_by_symbol(pred, y, sym))[0]

    best = {"val_ic": -np.inf, "epoch": -1, "test_ic_at_best": None}
    best_state = None
    per_epoch = []
    for ep in range(epochs):
        model.train()
        perm = rng.permutation(n)
        t0 = time.time()
        total = 0.0
        for i in range(0, n, batch):
            idx = torch.from_numpy(perm[i:i + batch])
            opt.zero_grad()
            loss = lossf(model(Xt[idx]).squeeze(-1), Yt[idx])
            loss.backward()
            opt.step()
            total += float(loss.detach()) * len(idx)
        val_ic = sym_ic(predict_logits(model, Xva), yva, sym_va)
        test_ic = sym_ic(predict_logits(model, Xte), yte, sym_te)
        rec = {"epoch": ep, "train_loss": round(total / n, 5),
               "val_ic": round(val_ic, 5), "test_ic": round(test_ic, 5),
               "secs": round(time.time() - t0, 1)}
        per_epoch.append(rec)
        if val_ic > best["val_ic"]:
            best = {"val_ic": val_ic, "epoch": ep, "test_ic_at_best": test_ic}
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
        log(f"    [{tag}] ep{ep}: loss {rec['train_loss']:.4f} "
            f"val_IC {val_ic:+.4f} test_IC {test_ic:+.4f} ({rec['secs']:.0f}s)"
            + ("  *" if ep == best["epoch"] else ""))
        if ckpt is not None:
            ckpt["models"][tag] = {"per_epoch": per_epoch, "best": best}
            save_ckpt(ckpt)

    if best_state is not None:
        model.load_state_dict(best_state)
    return model, {"per_epoch": per_epoch, "best": best}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--horizons", default="1,2,4,8,24,32")
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--n-boot", type=int, default=200)
    ap.add_argument("--n-perm", type=int, default=200)
    args = ap.parse_args()

    horizons = [int(x) for x in args.horizons.split(",")]
    if args.quick:
        horizons = [4, 32]
        args.epochs = 1
        args.n_boot = args.n_perm = 40
    epochs = args.epochs
    torch.set_num_threads(4)
    log(f"torch {torch.__version__} | horizons {horizons} | epochs {epochs} | "
        f"boots {args.n_boot} perms {args.n_perm}")

    ckpt = load_ckpt()
    ckpt["config"] = {"horizons": horizons, "epochs": epochs, "seed": args.seed,
                      "block_bars": BLOCK_BARS, "cost_bps": ROUND_TRIP_COST_BPS,
                      "primary_h": PRIMARY_H, "quick": args.quick}
    raw = ds.load_raw()

    # ── Phase 1: shipped model, all horizons ─────────────────────────────────
    log("\n=== 1. SHIPPED MODEL (grok_gqa_v9_best.pth) vs forward returns ===")
    shipped = load_shipped_model()
    for h in horizons:
        key = str(h)
        if key in ckpt["shipped"]:
            log(f"  shipped h={h} (cached)")
            continue
        d = ds.build_samples(raw, h)
        sp = ds.time_split(d["ts"], h)
        m = sp["test"]
        if args.quick:
            keep = np.where(m)[0][:8000]
            mask = np.zeros(len(d["y"]), bool); mask[keep] = True
            m = mask
        pred = predict_logits(shipped, d["X"][m])
        ckpt["shipped"][key] = evaluate(f"shipped h={h} ({h*15}m)",
                                        pred, d["y"][m], d["ts"][m], d["sym"][m],
                                        args.n_boot, args.n_perm)
        save_ckpt(ckpt)
        del d

    # ── Phase 2: retrain + controls at the primary horizon ───────────────────
    h = PRIMARY_H
    log(f"\n=== 2. RETRAIN + CONTROLS at h={h} ({h*15}m = max hold) ===")
    d = ds.build_samples(raw, h)
    sp = ds.time_split(d["ts"], h)
    tr, va, te = sp["train"], sp["val"], sp["test"]
    if args.quick:
        tr = tr & (np.arange(len(tr)) % 8 == 0)
        va = va & (np.arange(len(va)) % 4 == 0)
        te = te & (np.arange(len(te)) % 4 == 0)
    log(f"  train={tr.sum()} val={va.sum()} test={te.sum()} "
        f"(purge {sp['purge_seconds']//60}min)")

    mean, std = ds.fit_scaler(d["X"][tr])
    Xtr = ds.apply_scaler(d["X"][tr], mean, std)
    Xva = ds.apply_scaler(d["X"][va], mean, std)
    Xte = ds.apply_scaler(d["X"][te], mean, std)
    ytr, yva, yte, ts_te, sym_te = (d["y"][tr], d["y"][va], d["y"][te],
                                    d["ts"][te], d["sym"][te])

    # cheap baselines on the full test block
    if "random" not in ckpt["controls"]:
        ckpt["controls"]["random"] = evaluate(
            "random", np.random.default_rng(7).standard_normal(len(yte)),
            yte, ts_te, sym_te, args.n_boot, args.n_perm)
        save_ckpt(ckpt)
    if "momentum_32" not in ckpt["controls"]:
        ckpt["controls"]["momentum_32"] = evaluate(
            "momentum_32", d["mom"][te], yte, ts_te, sym_te,
            args.n_boot, args.n_perm)
        save_ckpt(ckpt)
    if "ridge" not in ckpt["controls"]:
        rp = ridge_baseline(Xtr, ytr, Xte)
        ckpt["controls"]["ridge"] = evaluate("ridge", rp, yte, ts_te, sym_te,
                                             args.n_boot, args.n_perm)
        save_ckpt(ckpt)
    del Xtr, Xva  # free memory before training

    # shuffled-label control (leakage detector): train on destroyed labels, then
    # score the best-val checkpoint on test. A clean pipeline gives IC ~ 0.
    if "shuffled_label" not in ckpt["models"]:
        log("\n  training shuffled-label control ...")
        shuf_y = ds.per_symbol_shuffle(d, seed=99)
        m_shuf, info = train_model(
            ds.apply_scaler(d["X"][tr], mean, std), shuf_y[tr],
            ds.apply_scaler(d["X"][va], mean, std), shuf_y[va],
            Xte, yte, d["sym"][va], sym_te, "shuffled_label", epochs,
            args.seed + 1, ckpt=ckpt)
        info["final_test"] = evaluate("shuffled_label_test",
                                      predict_logits(m_shuf, Xte), yte, ts_te,
                                      sym_te, args.n_boot, args.n_perm)
        ckpt["models"]["shuffled_label"] = info
        save_ckpt(ckpt)
        del m_shuf
    else:
        log("  shuffled-label control (cached)")

    # retrained on real labels
    tag = f"retrained_seed{args.seed}"
    if tag not in ckpt["models"]:
        log("\n  training model on forward returns ...")
        m_real, info = train_model(
            ds.apply_scaler(d["X"][tr], mean, std), ytr,
            ds.apply_scaler(d["X"][va], mean, std), yva,
            Xte, yte, d["sym"][va], sym_te, tag, epochs, args.seed, ckpt=ckpt)
        info["final_test"] = evaluate(f"{tag}_test",
                                      predict_logits(m_real, Xte), yte, ts_te,
                                      sym_te, args.n_boot, args.n_perm)
        ckpt["models"][tag] = info
        save_ckpt(ckpt)
    else:
        log(f"  {tag} (cached)")

    # ── Verdicts ─────────────────────────────────────────────────────────────
    sl = ckpt["models"]["shuffled_label"]["final_test"]
    rt = ckpt["models"][tag]["final_test"]
    ckpt["leakage_detected"] = bool(sl["excludes_zero"])
    ckpt["verdicts"] = {
        "leakage_detected": ckpt["leakage_detected"],
        "shuffled_label_ic": sl["ic"],
        "shipped_max_abs_ic": round(max(abs(r["ic"]) for r in ckpt["shipped"].values()), 5),
        "shipped_any_horizon_edge": any(r["edge"] for r in ckpt["shipped"].values()),
        "retrained_test_ic": rt["ic"],
        "retrained_ci": [rt["ci_lo"], rt["ci_hi"]],
        "retrained_spread_bps": rt["spread_bps"],
        "retrained_edge": rt["edge"],
        "cost_bps": ROUND_TRIP_COST_BPS,
    }
    save_ckpt(ckpt)
    with open(RESULTS, "w") as f:
        json.dump(ckpt, f, indent=2)
    log("\n=== VERDICT ===")
    for k, v in ckpt["verdicts"].items():
        log(f"  {k}: {v}")
    log(f"\nSaved {RESULTS}")


if __name__ == "__main__":
    main()
