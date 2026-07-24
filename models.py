# models.py — Shared dataclasses for the committee bot.

from dataclasses import dataclass, field


@dataclass
class MarketSnapshot:
    """Everything each brain needs to make a decision."""
    symbol:        str
    candles:       list          # list of dicts {t,o,h,l,c,v}
    indicators:    dict          # rsi, macd, ema_fast, ema_slow, bb_upper, bb_lower, atr_pct, volume_ratio
    regime:        str           # DUMP | ACCUMULATION | UPTREND | DISTRIBUTION
    atr_pct:       float
    has_position:  bool
    position_size: float
    entry_price:   float | None
    equity:        float
    buying_power:  float


@dataclass
class AIDecision:
    """A single brain's vote."""
    brain:      str           # "transformer" | "quant" | "momentum"
    action:     str           # "BUY" | "SELL" | "HOLD" | "SKIP"
    confidence: float         # 0.0 – 1.0
    regime:     str           # DUMP | ACCUMULATION | UPTREND | DISTRIBUTION
    reason:     str


@dataclass
class CommitteeResult:
    """The committee's final decision after weighted voting."""
    action:       str
    confidence:   float          # weighted committee confidence
    regime:       str            # most common regime across brains
    votes:        list[AIDecision] = field(default_factory=list)
    vote_breakdown: dict         = field(default_factory=dict)  # action → weighted score


@dataclass
class SentinelReport:
    """The sentinel's veto decision."""
    veto:    bool
    reason:  str
    cap_pct: float | None = None  # if not None, caps position size to this %
