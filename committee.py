# committee.py — Weighted voting engine. Tallies 3 brain votes and produces a final decision.
# Requires the winning action to achieve >MIN_VOTE_SCORE weighted confidence to execute.

from collections import defaultdict
from config import logger, BRAIN_WEIGHTS, MIN_VOTE_SCORE
from models import MarketSnapshot, AIDecision, CommitteeResult


def run_committee(
    snapshot: MarketSnapshot,
    decisions: list[AIDecision],
) -> CommitteeResult:
    """
    Weighted vote:
      transformer = 50%
      quant       = 30%
      momentum    = 20%

    Winning action must exceed MIN_VOTE_SCORE (default 0.60) to execute.
    Below threshold → SKIP.

    Special rules:
      - When flat (no position), SELL votes are ignored / converted to SKIP
      - In ACCUMULATION regime we bias toward BUY and use a lower threshold (0.45)
    """

    # ── Pre-process decisions: when flat, ignore SELL votes ──────────────
    processed = []
    for d in decisions:
        if not snapshot.has_position and d.action == "SELL":
            # Convert SELL → SKIP when we have no position to sell
            processed.append(AIDecision(
                brain=d.brain,
                action="SKIP",
                confidence=d.confidence * 0.5,  # dampen the confidence
                regime=d.regime,
                reason=f"{d.reason} (SELL ignored while flat)",
            ))
        else:
            processed.append(d)

    decisions = processed

    # Accumulate weighted scores per action
    action_scores: dict[str, float] = defaultdict(float)

    # Dynamically normalize weights among active non-skipped brains if model not loaded
    active_weights = {}
    total_active_weight = 0.0
    for decision in decisions:
        w = BRAIN_WEIGHTS.get(decision.brain, 0.0)
        if not decision.failed:
            active_weights[decision.brain] = w
            total_active_weight += w
        else:
            active_weights[decision.brain] = 0.0

    # Rescale active weights to sum to 1.0 if any are active
    if total_active_weight > 0:
        for b in active_weights:
            active_weights[b] = active_weights[b] / total_active_weight

    for decision in decisions:
        weight = active_weights.get(decision.brain, 0.0)
        weighted_conf = weight * decision.confidence
        action_scores[decision.action] += weighted_conf

    # ── Bias toward BUY in ACCUMULATION regime ───────────────────────
    regime = _majority_regime(decisions)
    if regime == "ACCUMULATION" and not snapshot.has_position:
        # Give BUY a small boost so borderline cases can fire
        action_scores["BUY"] = action_scores.get("BUY", 0.0) + 0.08

    # Find winning action
    winning_action = max(action_scores, key=action_scores.get)
    winning_score  = action_scores[winning_action]

    # Regime-adaptive threshold (loosened)
    if regime in ["ACCUMULATION", "DUMP"]:
        required_score = 0.45   # Lowered from 0.52 to catch bottoms earlier
    elif regime == "DISTRIBUTION":
        required_score = 0.68   # Higher threshold to avoid buying top exhaustion
    else:
        required_score = MIN_VOTE_SCORE

    # Log the full vote breakdown
    logger.info("🗳️  Committee vote:")
    for d in decisions:
        weight = active_weights.get(d.brain, 0)
        logger.info(
            f"   [{d.brain:12s}] {d.action:4s} conf={d.confidence:.3f} "
            f"(weight={weight:.0%}) → weighted={weight*d.confidence:.3f} | {d.reason}"
        )
    logger.info(f"   Action scores: { {k: round(v,3) for k,v in action_scores.items()} }")
    logger.info(f"   Winner: {winning_action} score={winning_score:.3f} (regime={regime} threshold >{required_score:.2f})")

    # Threshold gate
    if winning_score < required_score:
        return CommitteeResult(
            action="SKIP",
            confidence=winning_score,
            regime=regime,
            votes=decisions,
            vote_breakdown=dict(action_scores),
        )

    # SKIP always skips regardless of score
    if winning_action == "SKIP":
        return CommitteeResult(
            action="SKIP",
            confidence=winning_score,
            regime=regime,
            votes=decisions,
            vote_breakdown=dict(action_scores),
        )

    # When flat we only allow BUY (already converted SELLs above)
    if not snapshot.has_position and winning_action == "SELL":
        return CommitteeResult(
            action="SKIP",
            confidence=winning_score,
            regime=regime,
            votes=decisions,
            vote_breakdown=dict(action_scores),
        )

    return CommitteeResult(
        action=winning_action,
        confidence=round(winning_score, 4),
        regime=regime,
        votes=decisions,
        vote_breakdown=dict(action_scores),
    )


def _majority_regime(decisions: list[AIDecision]) -> str:
    from collections import Counter
    regimes = [d.regime for d in decisions]
    return Counter(regimes).most_common(1)[0][0]
