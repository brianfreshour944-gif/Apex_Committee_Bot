# Strategy & Risk Audit — Apex Committee Bot

> **Addendum (2026-09-20, same day):** the original audit below leaned on
> `FINANCIAL_AUDIT.md`/`AUDIT_REPORT.md` (dated 2026-08-05/07-27) for the
> execution/fee/fill-price claims in §§2/3/7/13 instead of reading
> `orders.py`/`portfolio.py`/`database.py` directly. Those docs were stale:
> fee tracking, real fill-price capture, and realized-PnL calculation were
> already live in current code. The NaN guards on `_bollinger()`/`_macd()`
> flagged in §2 were also already fixed. Sections 1 (edge), 4 (regime
> matrix), 8-10 (backtest/walk-forward/Monte Carlo), and most of 13
> (Sharpe/Sortino/etc.) are unaffected by this correction — those gaps are
> real and confirmed against current code.
>
> Since this audit, the following fixes have been applied (see
> `KNOWN_ISSUES.md` for details): committee confidence/regime/per-brain
> votes and trade slippage are now persisted to the DB on every trade
> (unblocks §3 calibration and §4 regime-performance once enough trades
> accumulate); the committee's SELL vote is now wired into exits as a
> secondary trigger (§6); an aggregate exposure cap (§5) and an
> ATR-scaled stop-loss (§5/§6) were added; a `classify_regime()`
> regression test was added (§4); a re-authorization checklist for the
> drawdown kill-switch was written (`OPERATIONS.md`, §17/§19). The core
> gaps that require actual historical/backtest data rather than a code
> change — model provenance (§1), backtest/walk-forward/Monte Carlo
> (§§8-10), and any real ablation/calibration numbers (§§3/11/13) —
> remain open and cannot be closed by editing code; they need the new
> logging to run for a while, or a dedicated backtest effort.

**Date:** 2026-09-20
**Method:** Static code audit only (no execution). Builds on the existing
`AUDIT_REPORT.md` (code correctness), `FINANCIAL_AUDIT.md` (money math),
`PERF_AUDIT.md` / `LATENCY_SCAN_REPORT.md` (latency), `KNOWN_ISSUES.md`.
This document covers the 20-section strategy/risk framework requested,
answering what is determinable from source code and flagging every
question that **cannot** be answered without running backtests — because
**this repo has no backtesting or historical-performance infrastructure at
all.** That absence is itself the single biggest finding and it gates
almost every empirical question in the framework below.

> **Scope boundary:** `git pull` was run (already up to date) and a
> `performance_dashboard.py --days 30` was requested but does not exist
> anywhere in this repo or in `Apex_oracle_bot`. There is no trade-history
> export, no equity-curve script, no walk-forward harness, and no Monte
> Carlo tooling in this codebase — only a live/paper trading loop
> (`main.py`), a hand-written exit-logic stress test
> (`tests/test_stress_market_drops.py`), and three unit tests. Every
> section below marked **[NOT DETERMINABLE STATICALLY]** requires either
> pulling the Alpaca paper-trading trade history / Postgres `trades` table
> and running a proper analysis, or building a backtest harness — neither
> exists today.

---

## 1. Strategy & Trading Edge

**What it's actually trying to exploit:** Three heterogeneous signals voted
together —
- `transformer` (50% weight): a GQA transformer (`grok_gqa_v9_best.pth`) trained on 11 microstructure features (Parkinson/Garman-Klass vol, Kyle lambda, signed flow, VWAP-z, Amihud illiquidity, autocorrelation, etc. — see [feature_engineering.py](feature_engineering.py:44)) predicting next-bar direction probability.
- `quant` (30%): classic TA — RSI/MACD/EMA-cross/Bollinger/momentum, counted by majority vote ([brains/quant.py](brains/quant.py)).
- `momentum` (20%): regime-transition detection, specifically DUMP→ACCUMULATION and UPTREND→DISTRIBUTION ([brains/momentum.py](brains/momentum.py)).

That's closest to **regime-based mean-reversion at extremes + trend continuation in the middle** — buy stabilization after a dump, ride an uptrend, avoid buying tops. Categorized against the checklist: primarily **mean reversion** (accumulation bottoms) blended with **trend persistence** (uptrend hold) and a **market microstructure** component (the transformer's academic features).

**Why the edge should exist:** Not stated anywhere in the code or docs. The feature_engineering.py header cites Kyle (1985) and Amihud (2002) as theoretical grounding for *why the features carry information*, but nowhere is there a stated economic thesis for why *this specific bot, trading BTC/ETH/SOL on 60-second cycles*, should have an edge over other participants using the same public OHLCV data and the same well-known academic microstructure proxies. No justification for why the edge would survive discovery, and no discussion of what would kill it (e.g., more sophisticated market makers arbitraging the same signals, exchange fee changes, or regime shifts the model wasn't trained on).

**Edge verification: [NOT DETERMINABLE STATICALLY].** There is no code or artifact in this repo showing:
- Performance across different assets/time periods/regimes/exchanges
- Parameter sensitivity analysis
- Percent of profit from best 5/10/20% of trades
- Whether the transformer's out-of-sample accuracy was ever measured (the training script isn't in this repo — `grok_gqa_v9_best.pth` and `feature_scaler.pkl` are committed binary artifacts with no accompanying training log, validation metrics, or dataset provenance)

This is a material gap: **the deployed model's edge cannot be verified from this repository at all.** You are trusting a `.pth` file with no visible train/validation/test split, no reported accuracy, and no walk-forward result.

**Edge decay:** Not measured. 60-second polling cycle + institutional-grade microstructure features (Kyle lambda, Amihud) are exactly the kind of short-horizon signal that decays fastest and is most contested by HFT/market-making participants — this strategy is plausibly competing for the shortest-lived edges in the market, which is the highest-risk edge category, and there's no monitoring for decay (see §12 Model Drift below — none exists).

### Required Output
- **Edge confidence: Low.** Not because the underlying academic features are bad ideas, but because there is zero empirical evidence in this repository that the specific deployed model has out-of-sample predictive power, and zero live/paper trade-history analysis to check it against reality.
- **Primary evidence:** Theoretical only — citations to Parkinson/Garman-Klass/Kyle/Amihud justify the *feature choices*, not the *model's* performance.
- **Biggest weakness:** No backtest, no walk-forward, no out-of-sample metric anywhere for the transformer model that carries 50% of the vote. The quant and momentum brains are simple enough to reason about directly, but the highest-weighted brain is a black box with unverified provenance.
- **Recommended test:** Before risking more capital, export whatever paper-trading history exists (Postgres `trades` table via `database.py`, or Alpaca's own paper account history) and compute win rate, profit factor, and calibration (§3) separately for each brain's votes and for the committee overall. If there's no historical data yet, that itself is a red flag for a bot that's apparently already live (see `KNOWN_ISSUES.md` referencing a running bot and observed "2/3 positions" state).

---

## 2. Data Integrity & Information Quality

**Data source:** Alpaca `CryptoHistoricalDataClient`, 1-minute-ish OHLCV bars (`SEQUENCE_LEN = 32` bars per decision), refreshed every `SLEEP_PER_LOOP` (60s).

**Point-in-time integrity — GOOD, verified by reading code:**
- `get_ohlcv()` fetches bars up to "now" and computes indicators only from historical closes ([data_feed.py](data_feed.py), confirmed correct in `AUDIT_REPORT.md` §5).
- No look-ahead: indicators use only `.rolling()`/`.ewm()` trailing windows on already-closed bars.
- No survivorship bias risk (fixed 3-symbol universe: BTC/USD, ETH/USD, SOL/USD — not periodically re-selected from a "top N by market cap" list, which would introduce survivorship bias; this static universe avoids that specific bias but also means the strategy has never been tested on a delisted/failed asset).

**Data quality gaps found:**
- `_bollinger()` and `_macd()` don't NaN-guard on short data (`AUDIT_REPORT.md` Finding 5.6) — could feed NaN into `classify_regime()` or the committee on a cold start or symbol with a data gap.
- No explicit check for duplicate timestamps, missing candles, or corrupted OHLC (e.g., `high < low`) anywhere in `data_feed.py`.
- `feature_engineering.py`'s `_sanitize()` does defensively coerce non-numeric/inf/NaN → 0.0 or a neutral fill, which is a reasonable last line of defense, but it also means **silently corrupted upstream data (e.g., a bad tick) is invisibly converted into a neutral feature value rather than flagged** — the bot has no data-quality alerting, so a bad data feed degrades signal quality without anyone knowing.

**Feature quality / stability: [NOT DETERMINABLE STATICALLY].** No feature-importance analysis, no ablation, no drift monitoring exists for the 11 transformer features or the TA indicators. `roll_autocorr`, `vol_of_vol`, `kyle_lambda`, etc. are all Z-scored over a rolling 20-bar window inside `add_features()` — this makes them regime-relative by construction (good for stationarity), but there is no code anywhere that checks whether they still correlate with forward returns.

### Required Output
- **Data-quality score: Medium.** Point-in-time correctness is solid; defensive NaN/inf handling exists; but there's no anomaly detection, no gap detection, and two known unguarded NaN paths.
- **Potential leakage detected:** None found in the feature pipeline itself. The one leakage-adjacent risk is unverifiable: whether `grok_gqa_v9_best.pth` was trained with any leakage, which can't be checked without the training script/notebook (not in this repo).
- **Most valuable features:** Unknown — no feature-importance analysis exists.
- **Redundant features:** Likely candidates by construction: `kyle_lambda` and `amihud_z` are both `|return|/volume`-style illiquidity proxies and are probably highly correlated; `parkinson_vol` and `garman_klass_vol` are both OHLC range-based vol estimators and are almost certainly highly correlated (Garman-Klass is a refinement of Parkinson). This is a reasonable guess from the formulas' definitions, not a measured correlation — worth an actual correlation matrix.
- **Features requiring investigation:** `_bollinger()`/`_macd()` NaN handling (already flagged in `AUDIT_REPORT.md`); whether `roll_autocorr` behaves consistently across the 3 symbols (BTC/ETH/SOL have very different volatility regimes, and Z-scoring over only 20 bars may not generalize).

---

## 3. Signal Generation & Decision Quality

**Entry (`main.py`):** committee's weighted vote action must be `BUY`, score above a regime-dependent threshold (0.45 ACCUMULATION/DUMP, 0.68 DISTRIBUTION, 0.60 otherwise), sentinel must not veto, buying power/cooldown/max-open-positions checks must pass.

**Exit:** purely mechanical — stop-loss (time-decayed), take-profit, trailing-stop, max-hold-time. **The committee's SELL vote is never acted on** — confirmed in `KNOWN_ISSUES.md` and `FINANCIAL_AUDIT.md` §5.3. This is a significant, openly-acknowledged design gap: the most sophisticated part of the system (3-brain weighted vote) only ever gets to say "go", never "stop" — all risk-side judgment is reduced to four fixed percentage thresholds that don't know why they're exiting, just that price crossed a line.

**Decision type:** committee/weighted-vote (deterministic given the same inputs — no randomness, no explicit probabilistic sampling).

**Does every model contribute measurable value? [NOT DETERMINABLE STATICALLY]** — no ablation test exists (`system - transformer`, `system - quant`, `system - momentum` are never compared). Structurally, `momentum` only fires on regime *transitions* it detects itself via `_prev_regime`, so it may rarely vote with high confidence — its 20% weight might be diluting the committee's average confidence during long stretches where it just returns 0.50 "no clear signal" HOLDs/SKIPs.

**Signal strength / calibration:** The framework asks for predicted-probability-vs-actual-win-rate tables (e.g., "70% confidence should win ~70% of the time"). **This cannot be produced** — there is no logged mapping from committee confidence at entry time to trade outcome anywhere in the codebase. `database.py`'s `record_trade()` doesn't even store the committee confidence or the individual brain votes that led to the trade (only `bot_name, symbol, side, qty, price, order_id` — see `FINANCIAL_AUDIT.md` §3.3, the `pnl_pct` param is dead code and confidence isn't captured at all). **This is a concrete, fixable gap**: if you want to ever answer "is 90%-confidence tier outperforming 60%-confidence tier," the data isn't being collected today.

**Threshold testing (top 10/20/30% of signals): [NOT DETERMINABLE STATICALLY]** — same root cause, no confidence-vs-outcome data captured.

**Notable calibration red flag independent of missing data:** `brains/transformer.py` computes `confidence = min(0.95, 0.5 + (prob - 0.5) * 1.8)` — this **stretches** a raw sigmoid probability by 1.8x before capping at 0.95. A model outputting `prob=0.55` (a fairly weak signal) gets reported as `confidence=0.59`; `prob=0.60` becomes `confidence=0.68`. This stretching is an arbitrary transform with no stated justification and, absent calibration data, has no way of being checked against reality — it could easily make the committee systematically overconfident in weak transformer signals, especially combined with the "narrower HOLD zone" comment (BUY at prob≥0.505 instead of 0.52) which was explicitly tuned to reduce SKIPs, i.e. to trade more.

### Required Output
This section's core ask (a probability-calibration table) cannot be produced. The prerequisite work — logging committee confidence, individual brain votes, and regime at entry time into `record_trade()` (or a new table) — doesn't exist yet and should be treated as a near-term priority independent of anything else in this report, because without it none of sections 3/9/10/11/13 of this framework are answerable going forward either.

---

## 4. Market Regime Detection

`regime.py` classifies DUMP / ACCUMULATION / UPTREND / DISTRIBUTION via a hand-tuned point-scoring system (not learned, not backtested — thresholds like `rsi < 35`, `mom5 < -3.0` etc. appear to be judgment calls). The committee explicitly uses this to change its required score threshold and to nudge BUY (documented, intentional per `KNOWN_ISSUES.md`: "ACCUMULATION regime buy bias — confirmed intentional").

**Does the bot recognize regime before trading?** Yes — regime is computed once per symbol per cycle and passed into all three brains and the committee.

**Does it adapt to regime changes?** Only in the sense of using the current regime's thresholds; there's no memory of regime performance (e.g., no tracking of "the bot loses money specifically in DISTRIBUTION regime, so tighten it further").

**Regime performance matrix: [NOT DETERMINABLE STATICALLY]** — requires trade-level history tagged with regime-at-entry, which (per §3) isn't being recorded. Cannot fill in the requested Regime × {trades, win rate, avg P&L, profit factor, max DD} table without that data.

**Notable concern:** The regime scoring in `regime.py` was already caught with a "tautological check" bug that inflated `accum_score` on every call (documented as fixed in the code comment, lines 51-55) — and per `KNOWN_ISSUES.md`, **there's still no regression test for `classify_regime()`** despite that history. Given that regime classification gates both the committee's decision threshold and (via `momentum.py`) a chunk of the vote, an untested scoring function that already had one silent logic bug is a standing risk.

---

## 5. Risk Management

**Position sizing:** confidence-tiered fixed percentage of equity (2.5%–15%, see `position_sizing.py`), capped at `MAX_SINGLE_TRADE_USD=$5,000`, floored at `MIN_ORDER_USD=$10`. **Not volatility-adjusted directly** — sizing depends on committee confidence, not ATR, except indirectly through the sentinel's 50%/60% caps when ATR or consecutive losses are elevated (`sentinel.py`). So: **"is the bot risking the same amount when volatility doubles?"** — largely yes, unless ATR%>3.0 specifically triggers the sentinel's 50% cap; there's no continuous vol-scaling (e.g., no ATR-based inverse sizing), just a binary threshold.

**Portfolio risk:**
- Max simultaneous positions: 3 (`MAX_OPEN_POSITIONS`)
- Max single trade: $5,000
- **No aggregate exposure limit** — confirmed in `FINANCIAL_AUDIT.md` §6.3: 3 positions at $5,000 each = $15,000 exposure with no check against equity %, and no correlation-based exposure limit (BTC/ETH/SOL are typically highly correlated in crypto selloffs, so "3 positions" can behave like one large correlated bet, not diversification).
- Max daily loss: not tracked separately from the portfolio drawdown stop.
- Max drawdown kill-switch: **exists** — `MAX_DRAWDOWN_STOP = -0.10` (10%), and when triggered, `main.py` liquidates all positions and `break`s out of the trading loop entirely (process exit, not a graceful pause) — see `main.py:283-296`. That's a real hard stop, which is good, but it's a full-stop with no defined re-authorization procedure (§17/§19 below).
- Max consecutive losses: tracked per-symbol (`MAX_CONSECUTIVE_LOSSES=4`), pauses that symbol only, not portfolio-wide — a deliberate and reasonable fix documented in `sentinel.py`'s own docstring.

**Drawdown / tail risk / risk of ruin: [NOT DETERMINABLE STATICALLY]** — no historical drawdown data, no Monte Carlo, no risk-of-ruin calculation exists. `tests/test_stress_market_drops.py` simulates *hypothetical* 3-10% price drops against the exit-logic formulas (useful for verifying the stop-loss math is wired correctly — it is, per `FINANCIAL_AUDIT.md` §5) but this is a unit test of arithmetic, not a Monte Carlo or historical stress test of the strategy's actual P&L distribution.

### Required Output
- Sizing logic is coherent and bounded (hard floor/ceiling exist) — this is a real positive.
- The two concrete gaps worth fixing: (1) no aggregate/correlated exposure cap despite trading 3 historically-correlated crypto assets, (2) sizing responds to a binary ATR threshold rather than continuous volatility scaling.
- Everything under "Tail Risk" and "Risk of Ruin" in the framework needs actual trade history or a backtest to answer and currently cannot be computed.

---

## 6. Stop-Loss & Exit Logic

Already the most thoroughly audited part of the codebase (`FINANCIAL_AUDIT.md` §5, and `tests/test_stress_market_drops.py`). Summary:
- Stop-loss 4%, time-decayed (2% after 1h, 1% after 2h — i.e. tightens the longer a loser is held, which is a sound design instinct: give a trade room early, cut it faster later).
- Take-profit fixed at 6%.
- Trailing stop 2% off peak, only fires when `pnl_pct > 0` (correct — prevents double-triggering with stop-loss).
- Max hold 8 hours.
- All formulas verified arithmetically correct for long-only positions (`FINANCIAL_AUDIT.md` numeric examples 1-4).

**Is the stop too tight or too wide? [NOT DETERMINABLE STATICALLY]** — this requires MAE/MFE analysis (how far price moved against/in-favor of stopped-out and winning trades) against real trade history, which doesn't exist. Structurally: crypto's typical intraday volatility on BTC/ETH/SOL can exceed 4% in normal (non-crash) swings, so a flat 4% stop with no ATR-normalization risks being "too tight" specifically during the sentinel's own "elevated ATR" regime (>3% ATR) — the same condition that makes the sentinel cap position *size* doesn't also widen the stop, which is an inconsistency: the bot recognizes "this is unusually volatile" for sizing purposes but not for stop-placement purposes.

**Known structural gap (already flagged in KNOWN_ISSUES.md and FINANCIAL_AUDIT.md):** the committee's SELL vote is completely disconnected from exits. This means a high-conviction 3-brain SELL signal (e.g., a momentum-detected UPTREND→DISTRIBUTION top, its single highest-conviction call per `brains/momentum.py`) is logged and thrown away while the position rides the mechanical trailing stop instead. That is very plausibly leaving profit on the table specifically in the scenario the momentum brain was built to catch.

---

## 7. Execution & Trading Costs

Covered in depth by `FINANCIAL_AUDIT.md` §3 — restating the critical points because they bear directly on whether the *edge* (§1) can survive real costs:

- **Fees are hardcoded to `0` everywhere** — `database.py`'s `fee` column always inserts 0; `record_trade()` has no fee parameter. `config.py` does define `FEE_RATE = 0.001` (0.1%) and `SELL_SLIPPAGE_BUFFER = 0.002`, but **grep confirms these two config values are dead — never referenced outside `config.py`** (worth double-checking at the code level, since `FINANCIAL_AUDIT.md` predates their addition to `config.py`; if they're truly unused, that's a discrepancy between the config's stated intent and the actual code path).
- **Slippage is completely untracked** — actual Alpaca fill price (`order.filled_avg_price`) is never read; DB records the last-known close price instead of the real fill.
- **SELL orders are market orders with zero price protection**, while BUY orders use a 0.1%-premium limit order — asymmetric protection that favors not-getting-filled on entry over controlling price on exit, which is backwards from a risk-management perspective (the exit is where you most want price control, e.g., during a fast drop that triggered the stop-loss in the first place).

**Slippage stress test (2x/3x normal costs): [NOT DETERMINABLE STATICALLY]** without a backtest harness, but the qualitative conclusion already stands: if fees are silently zero in every historical record, **any profit-factor number you might compute today from the existing trade log would be systematically overstated**, and the true edge could already be materially thinner or negative once ~0.2%/round-trip in fees plus real slippage on unprotected SELL market orders is subtracted. Given `FINANCIAL_AUDIT.md`'s own numeric example (a $250 gross winner with $10 in fees is a 4% overstatement on that single trade — and that's fees alone, before slippage), this is a real, not hypothetical, risk to the viability of the strategy as currently measured.

---

## 8-10. Backtesting Integrity, Walk-Forward, Monte Carlo — [NOT DETERMINABLE / NOT PRESENT]

**There is no backtest in this repository.** No training/validation/out-of-sample split is visible, no walk-forward harness, no Monte Carlo simulation. `Apex_oracle_bot` (a separate, sibling repo) has `run_adaptive_backtest.py`, `run_live_backtest.py`, `sweep_params.py` — none of that exists here. If `Apex_Committee_Bot`'s model or strategy logic was validated before deployment, that work isn't checked into this repository and can't be audited from what's here.

This is the honest, most important gap in the whole framework as applied to this repo: **sections 8, 9, and 10 of the requested audit cannot be performed at all without either (a) locating the original training/backtest artifacts if they exist elsewhere, or (b) building this infrastructure from scratch** — pulling historical OHLCV for BTC/ETH/SOL, replaying the committee logic bar-by-bar with realistic fees/slippage, and only then running walk-forward and Monte Carlo analysis on top.

---

## 11. Model & AI Contribution

Structurally:
- **transformer** (50%): opaque neural net, unverifiable edge (§1), stretched confidence scoring (§3), explicit ACCUMULATION-regime override that converts HOLD/weak-SELL into BUY (`brains/transformer.py:239-246`) — i.e. the "most trusted" brain has hand-coded logic bolted onto its raw model output specifically to make it agree with the committee's pre-existing BUY bias in one regime.
- **quant** (30%): fully transparent, simple majority-of-5-indicators vote — easiest brain to reason about and to backtest cheaply, but also the most "generic retail TA" (the feature_engineering.py header explicitly frames institutional features as *superior* to this style, which is a slight tension: the bot gives the style it considers inferior a fixed 30% vote weight without evidence for that specific weighting).
- **momentum** (20%): the only brain built specifically to catch turning points, but by construction fires rarely (needs a detected regime *transition*, not just a regime).

**Ablation testing: [NOT DETERMINABLE STATICALLY]** — no `system − brain X` comparison exists. Given that `KNOWN_ISSUES.md` documents the ACCUMULATION regime bias as "three mechanisms stack[ing]" (lower threshold + transformer override + committee score bonus) with "the combined effect isn't visible as a single number anywhere," **the system's designers have already flagged, in their own words, that they can't currently isolate what any one component is contributing.** That's an unusually candid and useful admission — and it means an ablation study here isn't just a nice-to-have from this checklist, it directly answers a question the project's own maintainers said they don't have visibility into.

---

## 12. Adaptive / Self-Learning Behavior

**None found.** The bot does not retrain, does not adjust its own weights, and does not detect its own prediction drift. `BRAIN_WEIGHTS` (50/30/20) are static config values. The only "adaptation" is the pre-programmed regime-conditional threshold/bias logic (§4), which is fixed rules, not learning. This means:
- No overfitting-to-recent-noise risk from online adaptation (a plus — the failure mode the framework worries about here doesn't apply).
- But also **no drift detection at all** — if the transformer model's real-world accuracy degrades (market regime shift, feature distribution change), nothing in this codebase would notice or respond. There's no calibration-drift, feature-drift, or win-rate-drift monitor, and no defined "reduce risk / pause / retrain / rollback" ladder (§12's own requested framework). The only thing that will eventually flag serious trouble is the blunt portfolio-level 10% drawdown kill-switch — a lagging, coarse signal that fires only after the damage is already done.

---

## 13. Performance Metrics — [NOT DETERMINABLE STATICALLY]

None of Sharpe/Sortino/Calmar/VaR/Expected-Shortfall/profit-factor/expectancy can be computed from this repository as it stands, for the same root reason as §§3/8-10: **no trade-level P&L history is being captured with enough fidelity** (no fees, no fill price, no confidence/regime tagging — see §§3, 7). Even if you pulled the raw Alpaca paper-account fill history right now, `database.py`'s own trade log would still be internally unreliable for reconciliation purposes because it stores intended price, not fill price, and zero fees.

---

## 14. Benchmark Against Simpler Alternatives

No benchmark script exists (confirmed: no `performance_dashboard.py`, no buy-and-hold or single-indicator baseline anywhere in the repo). This should be cheap to build once trade-history logging is fixed: at minimum, compare the bot's realized return against (a) buy-and-hold BTC/ETH/SOL over the same window, and (b) the `quant` brain alone (simplest, fully transparent component) trading solo. If the full 3-brain committee doesn't beat the `quant`-alone baseline after fees, the transformer and momentum brains' complexity isn't earning its keep (this directly answers framework §18/§20's "smallest system that produces most of the performance" question, but again, cannot be run without a backtest harness or real trade history).

---

## 15-16. Operational Reliability & Monitoring

This is the strongest area of the codebase per the existing audits:
- Crash recovery: robust (`AUDIT_REPORT.md` §9) — position reconciliation on startup and every cycle, atomic state-file writes, stale-order cancellation on boot.
- Async correctness: the historical bugs (missing `await` on `close_position()`, missing `symbol` arg on sentinel calls, sync `init_db()` blocking the loop) are all documented as fixed, with reasoning that matches the code.
- Discord alerting exists for startup, trades, and the drawdown kill-switch.
- **Gaps:** no explicit "LLM/model unavailable" ladder beyond the transformer's own `failed=True` fallback (committee correctly re-normalizes weights across the remaining 2 brains when transformer fails to load — verified in `AUDIT_REPORT.md` §7.2/§4.2/§4.3, this is genuinely well-built); no monitoring for prediction-confidence drift, feature-distribution drift, or abnormal trade-frequency (§16's specific asks); no alert on slippage exceeding normal range (impossible today anyway since slippage isn't tracked, §7).

---

## 17. Human Oversight

Not formally defined anywhere in the codebase (no config flag or code path that distinguishes "auto-allowed" vs. "needs human approval" actions). In practice, everything the bot does is fully autonomous within its config-defined limits — the only "requires a human" moment is implicit: after the 10% drawdown kill-switch fires and the process exits, a human must manually restart it (no auto-restart logic found, and restarting isn't gated behind any explicit review checklist in code). Given this bot appears to already be live/paper-trading (multiple audit docs and `KNOWN_ISSUES.md` reference observed runtime behavior), it would be worth explicitly writing down — even just in a markdown file — what changes to `config.py` (position size tiers, weights, thresholds, the model file itself) you intend to always review manually versus deploy directly, since nothing currently enforces that distinction.

---

## 18. Complexity Budget

Three brains + a sentinel + regime classifier + 11 hand-engineered features + a stacked set of ACCUMULATION-regime overrides that the maintainers themselves say they can't see the combined effect of (`KNOWN_ISSUES.md`). Per §11/§14, there's currently no evidence that the transformer and momentum brains earn their complexity over the much simpler and fully-transparent `quant` brain — not because they're bad ideas, but because nobody has measured it. This is the single most actionable structural question in the whole audit: **run the ablation in §11 before adding anything else to this system.**

---

## 19. Kill Criteria

**Exists, partially:** portfolio drawdown ≥10% triggers full liquidation + process exit (`main.py:283-296`). Per-symbol consecutive losses ≥4 pauses that symbol (`sentinel.py`). ATR/volume spike vetoes new entries.

**Missing, per the framework's own list:**
- No "rolling expectancy goes negative" check (can't be computed anyway without trade-history fixes, §13).
- No "prediction calibration deteriorates" check (no calibration data captured, §3).
- No "execution costs exceed assumptions" check (no cost data captured at all, §7).
- No defined re-authorization workflow after the drawdown kill-switch fires — the loop just `break`s; nothing in the code prevents someone from immediately restarting the container and resuming trading with the exact same config that just produced a 10% loss, with no forced diagnose/review step.

---

## 20. The Most Important Questions — Direct Answers

**Edge:** Regime-conditioned reversal/continuation signal blended from a black-box transformer (50%), classic TA (30%), and regime-transition detection (20%). No stated reason it should exist or persist, and no evidence in-repo that it does.

**Evidence:** None that's checkable from this repository. The model weights and scaler are committed with no training/validation record.

**Robustness:** Untested — no backtest exists across assets, periods, parameters, regimes, fees, or slippage.

**Simplicity:** Unknown, but the `quant` brain alone is the obvious baseline to test the other two against; nothing currently proves the transformer or momentum brains add value over it.

**Failure:** The worst-documented failure mode already happened in production and is on record: a missing `await` on `close_position()` caused a phantom-closed position that stayed open on the exchange with no way to trigger its time-based exit (`LATENCY_SCAN_REPORT.md`, "2/3 Position Discrepancy" section) — this is fixed now, but it demonstrates the failure class (silent async bugs → real capital left unmanaged) is real and has occurred, not just hypothetical.

**Adaptation:** No — the bot has no mechanism to detect that its own assumptions (or the transformer model's accuracy) have stopped holding. The only thing that eventually catches serious failure is a blunt, lagging 10% drawdown stop.

**Complexity:** The 30% `quant` weight and 20% `momentum` weight are unvalidated design choices, not measured contributions. The stacked ACCUMULATION-regime bias (lower threshold + transformer override + committee bonus) is explicitly flagged by the project's own notes as having an unmeasured combined effect.

**Improvement, single smallest highest-expected-value change:** **Start logging committee confidence, individual brain votes, regime, and actual Alpaca fill price/fee into the trade record on every entry and exit.** This one change unblocks answering nearly every "[NOT DETERMINABLE STATICALLY]" item in this report — calibration (§3), regime performance (§4), true net P&L (§7/§13), and ablation (§11) — none of which can be done retroactively once the bot has already traded without this data.

**Honesty test:** A skeptical quant researcher would go straight for two things: (1) "show me the transformer's out-of-sample validation" — and there is currently nothing to show; (2) "your own trade log doesn't record real fill prices or fees, so how do you know your live P&L numbers, if you have any, aren't fictional?" — per `FINANCIAL_AUDIT.md`, that criticism would be entirely correct today.

**Final question — what survives if you remove every component that can't demonstrate measurable out-of-sample value:** As of today, *nothing* in this system has demonstrated out-of-sample value, because nothing has been measured. That's not the same as saying nothing works — it's saying the honest answer to this question is currently unavailable, and fixing that (via the confidence/fee/fill logging change above, then a real backtest/ablation pass) is the prerequisite for every other judgment in this framework.
