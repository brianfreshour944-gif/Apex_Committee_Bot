# Does the signal predict anything? (2026-10-20)

The upstream question behind every earlier experiment. Exits, sizing, and costs
were all ruled out as the problem. This asks whether the *entry signal* carries
information at all.

**Answer: no, not at any horizon that matters — and the signal barely varies.**

Harness: `reports/signal_predictiveness.py`. At every bar where the bot would
consider entering (flat, regime not blocked, committee action == BUY) it records
the committee confidence and the realised forward return at 5 horizons. Cost is
deliberately NOT applied — this measures the raw signal.

60 days, 3 symbols, 377 BUY signals, real Alpaca 15m bars.

## 1. The signal has essentially no predictive power

Pooled (n=377), Spearman IC between confidence and forward return, with a
bootstrap 95% CI:

| horizon | IC | 95% CI | significant? |
|---|---|---|---|
| 15m | +0.000 | [-0.101, +0.100] | no |
| 30m | +0.043 | [-0.058, +0.142] | no |
| 60m | -0.068 | [-0.167, +0.034] | no |
| 120m | -0.045 | [-0.144, +0.058] | no |
| 360m | -0.047 | [-0.150, +0.055] | no |

**Every CI includes zero.** Confidence does not rank forward returns at any
horizon tested. The quartile spreads are not monotonic either (Q4 is usually the
*worst* bucket, but not significantly).

## 2. The confidence distribution explains why the filter failed

```
min 0.461   median 0.500   max 0.583   sd 0.021
```

The committee's BUY confidence sits in a **narrow band of 0.12 width with a
standard deviation of 0.02.** There is almost no variation to filter on. This is
the mechanical reason the earlier `--min-conf 0.55` experiment deleted two-thirds
of trades and made things worse: it was not selecting higher-quality trades, it
was cutting a narrow distribution at an arbitrary point.

**Confidence is effectively a near-constant ~0.50, not a graded conviction
measure.** It drives position sizing (`SIZING_TIERS`), which means sizing is
currently driven by noise.

## 3. The one "significant" result is negative, and I don't trust it

ETH at 120m: IC **-0.282**, CI [-0.435, -0.104] — excludes zero. But:

- It is **negative**: higher confidence predicts *lower* returns. Even taken at
  face value it argues against the signal, not for it.
- It is **1 of 15 tests** (5 horizons x 3 symbols). At alpha=0.05, ~0.75 false
  positives are expected by chance. One lone hit among 15 is entirely consistent
  with noise, and the pooled result — the one with real power — shows nothing.

I am reporting it for completeness, not as a finding.

## 4. Why this is decisive

| quantity | value |
|---|---|
| best pooled mean lift over unconditional | **+0.013%** |
| round-trip cost (taker) | **0.900%** |

The signal's best measured edge is about **69x smaller than the cost of trading
it.** This closes the loop on everything before it:

- Costs are not the *only* problem — but they are unreachable by this signal.
- No exit rule can rescue an edge of 0.01%: the exit sees the same bars.
- No sizing scheme helps: sizing is fed a near-constant 0.50 confidence.
- The maker-order bound (0.30% round trip) still exceeds the best lift by ~23x.

**The bottleneck is the signal itself.** The 3 brains + committee, as
configured, do not produce a directional forecast with measurable information
content on this data.

## 5. Second window: the IC changes SIGN — this is the clincher

Ran the same test on a second 60-day window (offset 60). Pooled ICs:

| horizon | off0 (recent) | off60 (older) | sign |
|---|---|---|---|
| 15m | +0.000 [-0.101,+0.100] | +0.080 [-0.014,+0.176] | same |
| 30m | +0.043 [-0.058,+0.142] | **+0.120 [+0.023,+0.212]** | same |
| 60m | -0.068 [-0.167,+0.034] | **+0.116 [+0.014,+0.211]** | **FLIP** |
| 120m | -0.045 [-0.144,+0.058] | +0.023 [-0.076,+0.117] | **FLIP** |
| 360m | -0.047 [-0.150,+0.055] | -0.002 [-0.104,+0.094] | same |

At 30m and 60m the older window shows a **weakly significant POSITIVE** IC
(+0.12, CI excludes zero) while the recent window shows zero-to-negative. The
sign flips at 60m and 120m.

**A genuine edge does not reverse sign between two adjacent 60-day windows.**
That instability is the signature of noise: one window "finds" an edge by
chance. Combined with section 3 (a lone negative hit among 15 tests), the
pattern across two windows and 30 cells is exactly what pure noise produces.

And even the favourable window does not clear cost: its best mean lift is
**+0.035%** (120m) against a **0.900%** round trip — **26x short**. So the
question is not whether we can find a window where the IC looks good; it is that
even when one appears, the magnitude is nowhere near tradeable.

## Caveats

- One data source (Alpaca 15m), 60 days, 3 symbols. Small-sample by ML standards.
- Confidence is a *weighted score sum*, not a calibrated probability (noted in
  AGENTS.md) — so "IC of confidence" tests the score as a ranking device, which
  is how sizing uses it.
- Regime filtering means these are bars the bot already chose to consider; a
  signal could in principle be predictive outside that subset but it would not
  be tradeable there anyway.
- No multiple-comparison correction was applied to the per-symbol table.

## Recommendation

Stop tuning entries, exits, sizing, and costs — five hypotheses tested, four
refuted, one unproven, and now the upstream signal is shown to carry no
measurable information. The next question worth asking is not "how do we trade
better" but **"is there any configuration of these 3 brains that produces a
predictive signal?"** That is a modelling question (feature/label design,
retraining, validation), not a parameter-tuning one. If the answer is no, the
honest conclusion is that this strategy should not trade real money.

Reproduce:
```
PYTHONPATH=<repo> python reports/signal_predictiveness.py --days 60
PYTHONPATH=<repo> python reports/signal_predictiveness.py --days 60 --offset-days 60
```
