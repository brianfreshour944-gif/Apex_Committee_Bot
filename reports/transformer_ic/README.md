# Does the transformer brain carry predictive signal?

**Question.** Brain 1 (`GrokGQA_Transformer`, 50% of the committee's weight) is
the single largest input to every entry decision. Does it carry measurable
predictive signal against forward returns? Deliverable: an honest Information
Coefficient (IC) study under `reports/transformer_ic/`.

**Short answer.** The shipped model carries a *small* IC that is statistically
significant and sign-stable at short horizons (~+0.03 to +0.06), but it is far
too weak to overcome the ~90 bps round-trip cost, and the same architecture
retrained on forward returns does not reproduce it. The signal is either
non-viable (best-case spread is ~12x short of cost) or partly an artefact of how
the shipped weights were produced. Either way it is not tradeable.

---

## Method (what makes this faithful)

- **Real modules.** Samples are built by calling the live `feature_engineering.add_features`
  (11 features) over real Alpaca 15-minute crypto bars, with the live
  `GrokGQA_Transformer` architecture (`brains/transformer.py`) and the live
  `SEQUENCE_LEN` (32). Nothing is re-implemented from memory.
- **Causality verified.** For every symbol, features were recomputed on growing
  prefixes of the bar series and compared to the full-series values at the same
  bar; 12 probes/symbol, all identical. No feature uses future data.
- **No look-ahead.** Labels are strictly forward: `y = close[i+h]/close[i] - 1`
  in basis points. A purged, chronological split (train < val < test, with a
  `h`-bar gap at each boundary) prevents label overlap across blocks.
- **Honest metric.** IC is the mean **per-symbol** Spearman correlation. Pooled
  IC is inflated by between-symbol level differences (BTC vs ETH vs SOL mean
  returns); the per-symbol metric removes that confound (see `tests/test_ic_stats.py`,
  test 8: a pure level-bias prediction scores pooled +0.89 but per-symbol +0.02).
- **Honest uncertainty.** 95% CIs come from a **block bootstrap** (blocks of 64
  bars, longer than the prediction autocorrelation timescale) that resamples
  `(prediction, return)` pairs together. p-values come from a **block permutation
  null** that resamples *only the predictions*. Controls: a random predictor, a
  raw-momentum predictor, a ridge regression on the same features, and a
  **shuffled-label model** trained through the identical pipeline (leakage detector).
- **Cost.** 90 bps round trip (0.25% taker fee x2 legs + slippage x2), per AGENTS.md.

Data: BTC/ETH/SOL 15m bars, 2022-10-21 to 2026-10-19. X = (379,522, 32, 11).
Split: train ~211.7k / val ~62.9k / test ~104.9k. Horizons: 1, 2, 4, 8, 24, 32
bars (15m to 8h; 8h = the bot's max hold). All three symbols appear in every block.

---

## Result 1 — Shipped model vs forward returns

Mean per-symbol IC on the test block (block-bootstrap CI, permutation p):

| horizon | IC | 95% CI | p | top-minus-bottom spread |
|---|---|---|---|---|
| 15m  | +0.0289 | [+0.0236, +0.0348] | 0.000 | +0.5 bps |
| 30m  | +0.0304 | [+0.0227, +0.0386] | 0.000 | +0.6 bps |
| 60m  | +0.0326 | [+0.0225, +0.0444] | 0.000 | +0.6 bps |
| 120m | +0.0359 | [+0.0214, +0.0496] | 0.000 | +2.0 bps |
| 360m | +0.0387 | [+0.0207, +0.0557] | 0.000 | +7.3 bps |
| 480m | +0.0356 | [+0.0179, +0.0587] | 0.000 | +11.0 bps |

So the shipped model *does* carry a small but statistically significant
association with forward returns. The practical question is the last column: the
best long/short spread at any horizon is **+11 bps against a 90 bps cost** — about
**12x short**. Even taken at face value, this signal cannot pay for itself.

The shipped IC is **not** uniform across time. Per disjoint block:

| horizon | train | val | test | sign-stable? |
|---|---|---|---|---|
| 15m  | +0.0258 | +0.0182 | +0.0333 | yes |
| 60m  | +0.0294 | +0.0209 | +0.0501 | yes |
| 120m | +0.0259 | +0.0239 | +0.0596 | yes |
| 480m | +0.0192 | **-0.0307** | +0.0437 | **no** |

At 15m-120m the sign is stable across all three blocks and test > train, which
rules out plain train-set contamination. At 480m it flips sign — the target the
bot actually holds for is the least stable one.

## Result 2 — Retraining the same architecture

Train the identical architecture on forward returns with the same purged split,
select the best-validation epoch, then score on test. Controls in the same table.

| predictor | IC | 95% CI | p | spread |
|---|---|---|---|---|
| random | -0.0013 | [-0.0076, +0.0045] | 0.693 | -1.1 bps |
| raw momentum | -0.0333 | [-0.0546, -0.0090] | 0.007 | -7.4 bps |
| ridge (same features) | -0.0024 | [-0.0244, +0.0197] | 0.873 | +3.7 bps |
| **shuffled-label (leakage ctl.)** | **-0.0037** | [-0.0209, +0.0095] | 0.560 | +4.2 bps |
| retrained @ 480m | +0.0065 | [-0.0103, +0.0236] | 0.560 | +0.8 bps |

The shuffled-label control sits at ~0 (IC -0.0037, p=0.56) — **the pipeline is
clean; there is no leakage**. The retrained model lands at the same place as the
control (IC +0.0065, CI includes zero). Ridge on the same 11 features is also ~0,
so the features carry no *linear* signal at 8h either.

Because the shipped model's stable region was 15m-120m, I retrained there too
(`retrain_check.py`):

| horizon | shipped IC | retrained IC | ridge IC |
|---|---|---|---|
| 15m | +0.0333 | -0.0110 [-0.0168, -0.0067] | +0.0049 |
| 120m | +0.0596 | -0.0042 [-0.0139, +0.0049] | +0.0070 |

At every horizon the retrained model fails to reproduce the shipped model's IC —
at 15m it is significantly *negative*. The forward-return objective does not
recover whatever weak association the shipped weights express.

## Result 3 — Per-feature IC (the information ceiling)

No model built on these 11 features can exceed the information they carry. The
strongest single feature at 15m is `z_return` at IC **-0.037**; at 120m the best
is `z_return` at **-0.014**; at 480m the best is `roll_autocorr` at **+0.012**.
Every other feature is below |0.03|. The feature set is near its information
limit, and that limit is low.

---

## Honest interpretation

1. **The shipped model's IC is real but not tradeable.** A ~+0.03 IC that is
   sign-stable across three disjoint periods is unlikely to be pure chance, but
   its best-case long/short spread (+11 bps) is ~12x smaller than the 90 bps
   round-trip cost. No position-sizing or exit change can close a 12x gap.

2. **Retraining does not reproduce it.** The same architecture trained on forward
   returns produces no out-of-sample IC at 15m-480m (all CIs include zero; 15m is
   significantly negative). Two readings, and this experiment cannot separate them:
   - the shipped model's IC is a weak artefact of how those weights were produced
     (its training data or objective is not reproduced here), or
   - the signal is so faint that a 3-epoch CPU run on the same architecture
     cannot reliably recover it.
   Either way, "retrain the transformer" is not a demonstrated path to an edge.

3. **The controls validate the method.** Random, ridge, and the shuffled-label
   model all sit at IC ~0 with CIs covering zero; the block permutation null is
   correctly centred (an earlier version was a no-op that reproduced the observed
   IC — fixed and pinned by `tests/test_ic_stats.py` test 9). So the shipped
   model's non-zero IC is not a measurement artefact of the harness.

4. **What this does NOT claim.** It does not prove the transformer is worthless —
   a small, real IC could in principle be useful if costs were far lower. It does
   show that, at Alpaca crypto taker costs, this signal cannot be monetised as a
   directional entry, which is exactly the committee's use of it.

## What would falsify this conclusion

- A model trained with a *different* objective (e.g. ranking / cost-aware loss,
  or classification of the top/bottom return decile) that reaches a positive,
  CI-excluding-zero out-of-sample IC at h >= 32 **and** a long/short spread
  materially above 90 bps.
- A feature set whose per-feature ICs are an order of magnitude larger.
- Maker execution (post-only, adverse-selection-adjusted) reducing the round-trip
  cost below the achievable spread — but the achievable spread at these ICs is
  ~11 bps, so cost would have to fall ~8x.

## Files

| file | purpose |
|---|---|
| `build_dataset.py` | fetch real bars, run real `add_features`, verify causality, cache |
| `dataset.py` | sample construction, purged split, train-only scaler, per-symbol shuffle |
| `stats.py` | per-symbol IC, block bootstrap, block permutation null, long/short spread |
| `experiment.py` | shipped-model IC, controls, retrain, leakage check, verdicts |
| `shipped_stability.py` | shipped-model IC per train/val/test block (contamination vs noise) |
| `retrain_check.py` | retrain at the stable horizons (15m, 120m) + ridge reference |
| `feature_ic.py` | per-feature IC = the information ceiling |
| `results/*.json` | all raw numbers behind the tables above |
| `tests/test_ic_stats.py` | 26 checks: metric calibration, null calibration, level-bias, leakage |

Reproduce: `pip install torch numpy scipy pandas scikit-learn alpaca-py`, then
`PYTHONPATH=<repo> python reports/transformer_ic/experiment.py --horizons 1,2,4,8,24,32 --epochs 3`.
