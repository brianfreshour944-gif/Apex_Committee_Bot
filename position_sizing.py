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

    if trade_value < MIN_ORDER_USD:
        logger.debug(f"Trade value ${trade_value:.2f} below min ${MIN_ORDER_USD} — skipping")
        return 0.0

    logger.info(
        f"💰 Size: ${trade_value:.2f} "
        f"(conf={committee_confidence:.3f} → {pct*100:.0f}% equity"
        f"{f' × {sentinel_cap:.0%} sentinel cap' if sentinel_cap else ''})"
    )
    return round(trade_value, 2)
