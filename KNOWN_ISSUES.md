# Known Issues / Notes for Later

Last updated: 2026-07-27

## Committee cannot trigger exits

In main.py, the trading loop only acts on `committee.action == "BUY"`.
All exits (SELL) happen exclusively through a separate mechanical
stop-loss / take-profit / trailing-stop / max-hold-time system, not
through the 3-brain weighted committee vote. If the committee decides
SELL with high confidence, that decision is logged but not acted on.

Not fixed as of this note - unclear whether intentional (keep exits
simple/predictable) or an oversight. Bot has been running without
obvious issues, so left as-is for now. If revisiting: the fix would be
wiring a committee SELL decision (when a position is open) through to
portfolio.close_position(), similar to how the mechanical exit logic
already calls it.

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

## No regression test for regime scoring

The macd_hist tautology bug (fixed in commit c981274/beda27c) went
unnoticed because there's no test coverage for regime.py's scoring
logic. tests/test_features.py and tests/test_committee.py exist but
don't cover classify_regime(). Worth adding a test that feeds known
indicator values and asserts the expected regime + score breakdown,
so similar logic bugs get caught automatically in the future.
