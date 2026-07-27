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
