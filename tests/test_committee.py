import pytest
from committee import run_committee
from models import MarketSnapshot, AIDecision

def test_committee_scoring():
    # Setup dummy snapshot
    snapshot = MarketSnapshot(
        symbol="BTC/USD",
        candles=[],
        indicators={"price": 60000, "rsi": 50, "macd": 10, "bb_upper": 61000, "bb_mid": 60000, "bb_lower": 59000, "ema_fast": 60000, "ema_slow": 59000, "atr_pct": 1.5},
        regime="UPTREND",
        atr_pct=1.5,
        has_position=False,
        position_size=0.0,
        entry_price=None,
        equity=10000.0,
        buying_power=10000.0
    )
    
    decisions = [
        AIDecision(brain="transformer", action="BUY", confidence=0.8, regime="UPTREND", reason="Mock"),
        AIDecision(brain="quant", action="BUY", confidence=0.6, regime="UPTREND", reason="Mock"),
        AIDecision(brain="momentum", action="HOLD", confidence=0.0, regime="UPTREND", reason="Mock")
    ]
    
    # Run committee
    result = run_committee(snapshot, decisions)
    
    # Weights from config: Transformer 0.5, Quant 0.3, Momentum 0.2
    # Momentum has confidence=0.0 but failed=False (default), so it IS included in weight normalization.
    # total_active_weight = 0.5 + 0.3 + 0.2 = 1.0
    # transformer: 0.5/1.0 = 0.5, quant: 0.3/1.0 = 0.3, momentum: 0.2/1.0 = 0.2
    # BUY score = (0.8 * 0.5) + (0.6 * 0.3) = 0.40 + 0.18 = 0.58
    # HOLD score = (0.0 * 0.2) = 0.0
    # Since 0.58 < 0.60 (MIN_VOTE_SCORE), this returns SKIP
    import config
    expected_buy = 0.58
    if expected_buy >= config.MIN_VOTE_SCORE:
        assert result.action == "BUY"
    else:
        assert result.action == "SKIP"


def test_committee_scoring_legit_zero_confidence_vote_is_not_excluded():
    """Regression guard: a brain that RAN FINE and genuinely voted with 0
    confidence (failed=False, the default) is a real opinion, not a failure.
    It must stay in the weight denominator..."""
    snapshot = MarketSnapshot(
        symbol="BTC/USD",
        candles=[],
        indicators={"price": 60000, "rsi": 50, "macd": 10, "bb_upper": 61000, "bb_mid": 60000, "bb_lower": 59000, "ema_fast": 60000, "ema_slow": 59000, "atr_pct": 1.5},
        regime="UPTREND",
        atr_pct=1.5,
        has_position=False,
        position_size=0.0,
        entry_price=None,
        equity=10000.0,
        buying_power=10000.0
    )
    decisions = [
        AIDecision(brain="transformer", action="BUY", confidence=0.8, regime="UPTREND", reason="Mock"),
        AIDecision(brain="quant", action="BUY", confidence=0.6, regime="UPTREND", reason="Mock"),
        AIDecision(brain="momentum", action="HOLD", confidence=0.0, regime="UPTREND", reason="Mock")
        # failed defaults to False
    ]
    result = run_committee(snapshot, decisions)
    # BUY score = 0.58, below 0.60 threshold → must be SKIP
    assert result.action == "SKIP"
