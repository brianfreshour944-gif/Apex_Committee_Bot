# position_sizing.py — Confidence-tiered position sizing.
# The higher the committee confidence, the larger the trade.

from config import logger, SIZING_TIERS, MAX_SINGLE_TRADE_USD, MIN_ORDER_USD


def calculate_trade_size(
    equity: float,
    committee_confidence: float,
    sentinel_cap: float | None = None,
) -> float:
    """
    Returns the dollar value to trade.

    Confidence tiers (configurable in config.py):
      ≥ 90%  →  15% of equity
      ≥ 75%  →  10% of equity
      ≥ 60%  →   5% of equity
      fallback →  2.5% of equity

    Sentinel can further cap the result (e.g., to 50% of the tier size).
    Hard ceiling: MAX_SINGLE_TRADE_USD.
    Hard floor: MIN_ORDER_USD (returns 0 if below this).
    """
    # Select tier
    pct = SIZING_TIERS[-1][1]   # fallback
    for threshold, tier_pct in SIZING_TIERS:
        if committee_confidence >= threshold:
            pct = tier_pct
            break

    trade_value = equity * pct

    # Apply sentinel cap
    if sentinel_cap is not None:
        trade_value *= sentinel_cap

    # Hard limits
    trade_value = min(trade_value, MAX_SINGLE_TRADE_USD)

    # A risk-REDUCTION mechanism must never silently block all trading. On a
    # small account the tier size is already near the broker minimum, so a
    # sentinel cap (0.5 / 0.6) could multiply an affordable trade down below
    # MIN_ORDER_USD and zero it out -- e.g. $100 equity, 2.5% fallback = $2.50,
    # x 0.60 cap = $1.50, which is still fine, but 0.5 cap at a smaller equity
    # is not. When the cap is what pushes us under the floor (and the uncapped
    # tier could have afforded it), trade the minimum instead of nothing: the
    # cap's intent is "smaller position", not "no position". The uncapped value
    # is re-checked so a genuinely unaffordable tier still returns 0.
    if (trade_value < MIN_ORDER_USD and sentinel_cap is not None
            and min(equity * pct, MAX_SINGLE_TRADE_USD) >= MIN_ORDER_USD):
        logger.info(
            f"Size floor: sentinel cap {sentinel_cap:.0%} pushed "
            f"${trade_value:.2f} below MIN_ORDER_USD ${MIN_ORDER_USD:.2f} "
            f"— trading the ${MIN_ORDER_USD:.2f} minimum instead of skipping"
        )
        trade_value = MIN_ORDER_USD

    if trade_value < MIN_ORDER_USD:
        logger.warning(
            f"[WAIT] Sizing skip: ${trade_value:.2f} below MIN_ORDER_USD ${MIN_ORDER_USD:.2f} "
            f"(equity=${equity:.2f} x {pct*100:.1f}%"
            f"{f' x {sentinel_cap:.0%} sentinel cap' if sentinel_cap else ''}) "
            f"— no trade this cycle"
        )
        return 0.0

    logger.info(
        f"Size: ${trade_value:.2f} "
        f"(conf={committee_confidence:.3f} -> {pct*100:.0f}% equity"
        f"{f' x {sentinel_cap:.0%} sentinel cap' if sentinel_cap else ''})"
    )
    return round(trade_value, 2)
