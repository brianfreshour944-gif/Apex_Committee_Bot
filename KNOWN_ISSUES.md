# Known Issues / Notes for Later

Last updated: 2026-07-27

## Committee cannot trigger exits (FIXED 2026-09-20)

Previously, the trading loop only acted on `committee.action == "BUY"`.
All exits (SELL) happened exclusively through a separate mechanical
stop-loss / take-profit / trailing-stop / max-hold-time system, not
through the 3-brain weighted committee vote. If the committee decided
SELL with high confidence, that decision was logged but not acted on.

Fix applied: main.py now runs the committee a second time for held
positions, but ONLY as a fallback checked after all four mechanical
exits have already had a chance to fire (see `main.py` around the
"Committee SELL override" comment in the exit block). This is a
deliberate ordering choice -- mechanical risk control stays primary and
non-negotiable; the committee SELL vote adds an *additional* exit
trigger rather than replacing any existing one. Runs the same 3-brain
inference (including the transformer's PyTorch forward pass) an extra
time per held position per cycle -- acceptable given the 60s loop
period and MAX_OPEN_POSITIONS=3, but worth knowing if latency ever
becomes a concern again (see PERF_AUDIT.md).

## ACCUMULATION regime buy bias (confirmed intentional)

Commit 97ce731 deliberately loosened trading logic in ACCUMULATION
regime: lowered vote threshold to 0.45 (vs default 0.60), added a
Transformer brain regime override that converts HOLD/weak-SELL into
BUY, and a flat +0.08 score bonus for BUY in committee.py. These three
mechanisms stack. Confirmed intentional via commit history - not a bug,
just worth knowing the combined effect isn't visible as a single number
anywhere if tuning is needed later.

## Symbol format inconsistency (unverified)

orders.py submits orders using slash format ("BTC/USD"), while
portfolio.py's close_position() explicitly strips it to "BTCUSD" via
normalize_symbol(). This may be correct - different Alpaca API
endpoints have historically expected different formats for crypto
symbols - but hasn't been directly verified against the live API.
Worth a quick live test (place and close a small test order, confirm
no format-related errors) rather than assuming either way.

## data_client has no explicit credentials

In config.py, data_client = CryptoHistoricalDataClient() is
instantiated with no arguments, relying on the Alpaca SDK finding
APCA_API_KEY_ID / APCA_API_SECRET_KEY from raw OS environment
variables. trading_client, by contrast, passes credentials explicitly.
If credentials are ever only set via a .env file (which pydantic
reads internally but does not push into os.environ), this client
could silently fail to authenticate depending on deployment setup.
Low priority since it has apparently been working, but worth passing
credentials explicitly for consistency and to remove the ambiguity.

## No regression test for regime scoring (FIXED 2026-09-20)

The macd_hist tautology bug (fixed in commit c981274/beda27c) went
unnoticed because there was no test coverage for regime.py's scoring
logic. Added `tests/test_regime.py`: one hand-computed scenario per
regime (DUMP/ACCUMULATION/UPTREND/DISTRIBUTION) with the point tally
shown in comments, plus a fail-safe test asserting missing/malformed
indicators default to DUMP (the no-buy regime) rather than raising or
silently favoring a buy-biased regime.

## No calibration / regime-performance / brain-ablation data (FIXED 2026-09-20, data collection only)

Entry decisions (committee confidence, per-brain votes, regime) were
never persisted anywhere queryable -- only shown transiently in Discord
alerts. This made it impossible to ever check "does 90%-confidence
actually win more than 60%-confidence" or "which regime loses money" or
"is the transformer/momentum brain earning its weight" against real
results, because the data to answer those questions didn't exist.

Fix applied: `trades.confidence`, `trades.regime`, `trades.brain_votes`
(JSON), `trades.slippage_pct`, and on `realized_pnl`:
`entry_confidence`, `entry_regime`, `exit_reason`, `slippage_exit_pct`
are now populated on every trade (see database.py, orders.py,
portfolio.py, main.py). This is a data-collection fix only -- it does
not retroactively backfill history, and no calibration/regime/ablation
conclusions can be drawn until enough new trades accumulate under the
new logging. Query `realized_pnl` grouped by `entry_regime` /
`entry_confidence` buckets once there's a meaningful sample.

## Slippage now tracked but SELL exits still use unprotected market orders

`portfolio.py`'s `close_position()` (and its two fallbacks) now compute
and store `slippage_pct` (fill price vs. the pre-order price estimate),
so execution quality on exits is finally visible. The underlying order
mechanism itself was deliberately left unchanged: exits use a bare
market order (see the "FIX: use a MARKET order for exits" comment in
`portfolio.py`), which has zero price protection but was a hard-won fix
for a worse bug -- a SELL *limit* order that could get stuck unfilled
during a dump, leaving a losing position open past its stop-loss
indefinitely (commit e3f587e and the several "40310000" fixes before
it). Revisit only with real slippage data in hand (now being collected)
to judge whether a bounded marketable-limit-then-market-fallback is
worth the added complexity -- don't reintroduce a resting limit order
on the exit path.

## Aggregate exposure cap added

Previously only position COUNT (`MAX_OPEN_POSITIONS=3`) and per-trade
size (`MAX_SINGLE_TRADE_USD`) were bounded -- nothing capped total
dollar exposure, so 3 positions at $5,000 each was allowed regardless
of equity, and BTC/ETH/SOL are typically correlated in crypto
selloffs (3 "diversified" positions can behave like one large bet).
Added `MAX_TOTAL_EXPOSURE_PCT` (config.py, default 30% of equity),
enforced in main.py's entry logic using the already-computed
`computed_equity - cash` as current exposure. Note: this check uses
the exposure snapshot from the start of the cycle, so if multiple BUYs
fire in the same 60s cycle the check doesn't account for earlier BUYs
within that same cycle -- a known, accepted imprecision (consistent
with how `buying_power` is already only reconciled from Alpaca at
cycle start elsewhere in this file), not expected to matter in
practice given MAX_OPEN_POSITIONS=3 already bounds the worst case.

## Stop-loss now widens with ATR (was previously flat regardless of volatility)

Sentinel already capped position SIZE once ATR% exceeded 3% (elevated
volatility), but nothing widened the STOP to match -- a flat 4%
stop-loss in a regime already flagged as unusually volatile was the
most likely way to get stopped out by noise instead of a real reversal.
Added `STOP_LOSS_ATR_THRESHOLD` / `STOP_LOSS_ATR_MAX_MULT` (config.py,
defaults 3.0% / 1.5x) -- effective_stop now scales up proportionally
once ATR exceeds the threshold, on top of the existing time-decay
tightening. Not backtested; if trades start feeling like they're given
too much room in choppy conditions, this is the first place to check.
