"""Per-feature IC with forward returns — how much signal is in each input?

Establishes the ceiling: no model built on these features can exceed the
information they carry. If every raw feature has a near-zero IC, a neural
network cannot manufacture an edge from them.

Reports the mean per-symbol Spearman IC of each feature's value at the decision
bar against the forward return, on the held-out test block, at three horizons.

Run:  PYTHONPATH=<repo> python reports/transformer_ic/feature_ic.py
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
for p in (REPO, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("APCA_API_KEY_ID", "test")
os.environ.setdefault("APCA_API_SECRET_KEY", "test")

import dataset as ds
import stats as st

from feature_engineering import FEATURE_COLS

HORIZONS = [1, 8, 32]
OUT = os.path.join(HERE, "results")


def main():
    os.makedirs(OUT, exist_ok=True)
    raw = ds.load_raw()
    results = {}
    for h in HORIZONS:
        d = ds.build_samples(raw, h)
        te = ds.time_split(d["ts"], h)["test"]
        X, y, sym = d["X"][te], d["y"][te], d["sym"][te]
        rows = {}
        for j, name in enumerate(FEATURE_COLS):
            ic, _ = st.mean_symbol_ic(st.split_by_symbol(X[:, -1, j], y, sym))
            rows[name] = round(float(ic), 5)
        results[str(h)] = dict(sorted(rows.items(),
                                      key=lambda kv: -abs(kv[1])))
        print(f"\n=== h={h} ({h*15}m) per-feature IC on test block ===")
        for name, ic in results[str(h)].items():
            print(f"  {name:<18} {ic:+.4f}")
        del d
    with open(os.path.join(OUT, "feature_ic.json"), "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved {os.path.join(OUT, 'feature_ic.json')}")


if __name__ == "__main__":
    main()
