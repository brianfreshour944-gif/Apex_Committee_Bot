import logging
from logging.handlers import RotatingFileHandler
import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from alpaca.trading.client import TradingClient
from alpaca.data.historical import CryptoHistoricalDataClient

# -- Configuration Schema -------------------------------------------------------
class Settings(BaseSettings):
    bot_name: str = "Apex_Committee_v1"
    
    # Alpaca credentials
    apca_api_key_id: str
    apca_api_secret_key: str
    apca_api_paper: bool = True
    
    # Brain weights
    weight_transformer: float = 0.50
    weight_quant: float = 0.30
    weight_momentum: float = 0.20
    
    min_vote_score: float = 0.60
    
    max_single_trade_usd: float = 5000.0
    # Minimum order value. This is a BROKER constraint, not a risk preference:
    # Alpaca rejects crypto BUY orders below $1 notional ("cost basis must be
    # >= minimal amount of order 1", code 40310000). The old default of $10 was
    # an invented round number, and on a small account it was fatal: at $100
    # equity NO sizing tier could reach $10 (10% x $100 = $10 exactly, but the
    # measured committee BUY confidence ceiling is ~0.74, below the 0.75 tier
    # needed), so the bot took ZERO trades. Lowered to the real $1 floor.
    # NOTE: the per-symbol QUANTITY minimum is also checked by Alpaca; at $1
    # notional the quantity is ~1e-5 BTC, far above the 1e-8 floor, so the
    # notional floor strictly dominates here.
    min_order_usd: float = 1.0
    
    stop_loss_pct: float = 0.04
    take_profit_pct: float = 0.06
    trailing_stop_pct: float = 0.02
    max_hold_hours: float = 8.0
    max_open_positions: int = 3
    cooldown_seconds_buy: int = 900
    # Cooldown applied when a position CLOSES (any exit reason). The buy
    # cooldown above only covers the 900s after an ENTRY, so a symbol that
    # stopped out at -4% was immediately re-enterable on the next cycle --
    # and because a DUMP reads as oversold RSI, the quant brain would vote
    # BUY again, producing a stop-out -> re-buy whipsaw. This pauses a symbol
    # after it exits. Kept shorter than cooldown_seconds_buy so a genuinely
    # good re-entry after a take-profit is not blocked for a full 15 min.
    exit_cooldown_seconds: int = 300
    
    # Fee and slippage
    fee_rate: float = 0.0025  # 0.25% per side: Alpaca crypto Tier-1 TAKER fee
    # The bot submits LIMIT orders that CROSS the spread to fill immediately
    # (BUY at price*(1+buffer), SELL at price*(1-buffer)), so both legs are
    # TAKERS and pay the taker rate, not the 0.15% maker rate. The old default
    # of 0.001 (0.10%) understated the real cost by 2.5x. 30d volume tier 1.
    # BEHAVIOURAL EFFECT: this is not purely cosmetic. It is added to
    # total_fee -> realized_pnl (main.py exit block), and realized_pnl decides
    # register_loss() vs register_win(). So a more accurate (higher) fee can
    # flip a marginal winner to a loser and arm the per-symbol sentinel pause.
    # That is the intended outcome -- the old rate understated cost and let
    # sub-breakeven trades count as wins.
    sell_slippage_buffer: float = 0.002  # 0.2% slippage buffer for SELL limit orders
    
    sentinel_max_atr_pct: float = 6.0
    sentinel_max_vol_mult: float = 4.0
    max_consecutive_losses: int = 4
    # How long a symbol stays paused after hitting max_consecutive_losses.
    # The pause MUST expire: the sentinel veto blocks every new entry, and the
    # only thing that resets the streak is a win on the exit path -- so an
    # indefinite pause would make the symbol permanently unenterable AND
    # unexitable (the original one-way-latch bug). Default 4h.
    sentinel_loss_cooldown_seconds: int = 14400
    # Regimes in which NEW entries are hard-blocked (comma-separated). DUMP is
    # an active downtrend -- the falling-knife case. This is the last line of
    # defense: even if the committee votes BUY, the sentinel vetoes the entry.
    # Exits (SELL) are never blocked, so a blocked regime can still close out.
    blocked_entry_regimes: str = "DUMP"

    max_drawdown_stop: float = -0.10

    # Aggregate exposure cap (sum of all open position values as % of equity).
    # Separate from MAX_OPEN_POSITIONS x MAX_SINGLE_TRADE_USD, which bounds
    # position COUNT and per-trade size but not total dollar exposure --
    # 3 positions at $5,000 each is allowed today with no check against equity.
    max_total_exposure_pct: float = 0.30

    # Stop-loss widens (multiplicatively) once ATR% exceeds this threshold,
    # up to stop_loss_atr_max_mult. Mirrors the threshold sentinel already
    # uses to cap position SIZE (sentinel.py: atr_pct > 3.0) -- but until now
    # nothing widened the STOP to match the same "this is unusually volatile"
    # judgment, so normal noise in an elevated-vol regime could stop out a
    # trade that a vol-adjusted stop would have let ride.
    stop_loss_atr_threshold: float = 3.0
    # 1.25 caps the effective stop at -5% (fresh) / -2.5% (after 2h decay).
    # Previously 1.5 (-6% / -3%), which let the stop stretch widest exactly
    # when protection matters most -- flash crashes spike ATR% past the
    # threshold, so the cap was reachable in precisely the drops it should
    # not widen into. Tighten further at your own risk; below ~1.2 the stop
    # stops tracking volatility at all.
    stop_loss_atr_max_mult: float = 1.25

    # Transformer brain: raw sigmoid probability is stretched by this factor
    # before being reported as "confidence" (e.g. prob=0.55 -> confidence=0.59).
    # Pulled out to config so it can be tuned once real calibration data
    # (confidence vs actual outcome) exists -- see database.py trade logging.
    transformer_confidence_stretch: float = 1.8
    
    min_bid_ask_ratio: float = 0.65
    # Fail-closed orderbook (whale) gate. The gate used to return a passing
    # ratio (1.0) when the L2 book was empty, and a passing None when the fetch
    # errored -- so it never blocked a trade. Now a missing/empty/errored book
    # VETOES the entry (we cannot verify liquidity, so we don't take the trade),
    # unless this is set False to restore the old fail-open behavior.
    orderbook_gate_fail_closed: bool = True
    # Consecutive cycles with a FAILED account fetch (auth/network) before a
    # Discord alert is raised. A bad/revoked key makes every account, position
    # and order call return 401 while market data (which needs no credentials)
    # keeps flowing -- so the bot looks alive in the log but cannot trade, and
    # without this it fails closed forever in silence. Set to 0 to disable.
    account_failure_alert_threshold: int = 3
    state_file_path: str = "committee_bot_state.json"
    sleep_per_loop: int = 60
    
    model_path: str = "grok_gqa_v9_best.pth"
    scaler_path: str = "feature_scaler.pkl"
    
    heartbeat_path: str = "committee_heartbeat.txt"
    discord_webhook_url: str = ""
    
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()

# -- Logging --------------------------------------------------------------------
logger = logging.getLogger("ApexBot")
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s | %(levelname)s | %(message)s')

# Console handler
ch = logging.StreamHandler()
ch.setFormatter(formatter)
logger.addHandler(ch)

# File handler with rotation (10MB max per file, keep 5 backups)
log_dir = "logs"
os.makedirs(log_dir, exist_ok=True)
fh = RotatingFileHandler(os.path.join(log_dir, "bot.log"), maxBytes=10*1024*1024, backupCount=5)
fh.setFormatter(formatter)
logger.addHandler(fh)

BOT_VERSION = "2026-07-21-r2"
BOT_NAME = settings.bot_name
logger.info(f"Bot version: {BOT_VERSION} | Name: {BOT_NAME}")

# -- Universe -------------------------------------------------------------------
SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD"]

# -- Committee brain weights ----------------------------------------------------
BRAIN_WEIGHTS = {
    "transformer": settings.weight_transformer,
    "quant":       settings.weight_quant,
    "momentum":    settings.weight_momentum,
}
MIN_VOTE_SCORE = settings.min_vote_score

SIZING_TIERS = [
    (0.90, 0.15),
    (0.75, 0.10),
    (0.60, 0.05),
    (0.00, 0.025),
]
MAX_SINGLE_TRADE_USD = settings.max_single_trade_usd
MIN_ORDER_USD        = settings.min_order_usd
STOP_LOSS_PCT        = settings.stop_loss_pct
TAKE_PROFIT_PCT      = settings.take_profit_pct
TRAILING_STOP_PCT    = settings.trailing_stop_pct
MAX_HOLD_HOURS       = settings.max_hold_hours
MAX_OPEN_POSITIONS   = settings.max_open_positions
COOLDOWN_SECONDS_BUY = settings.cooldown_seconds_buy
EXIT_COOLDOWN_SECONDS = settings.exit_cooldown_seconds
SENTINEL_MAX_ATR_PCT  = settings.sentinel_max_atr_pct
SENTINEL_MAX_VOL_MULT = settings.sentinel_max_vol_mult
MAX_CONSECUTIVE_LOSSES = settings.max_consecutive_losses
SENTINEL_LOSS_COOLDOWN_SECONDS = settings.sentinel_loss_cooldown_seconds
BLOCKED_ENTRY_REGIMES = frozenset(
    r.strip().upper() for r in settings.blocked_entry_regimes.split(",") if r.strip()
)
MAX_DRAWDOWN_STOP = settings.max_drawdown_stop
MAX_TOTAL_EXPOSURE_PCT = settings.max_total_exposure_pct
STOP_LOSS_ATR_THRESHOLD = settings.stop_loss_atr_threshold
STOP_LOSS_ATR_MAX_MULT = settings.stop_loss_atr_max_mult
TRANSFORMER_CONFIDENCE_STRETCH = settings.transformer_confidence_stretch
MIN_BID_ASK_RATIO  = settings.min_bid_ask_ratio
ORDERBOOK_GATE_FAIL_CLOSED = settings.orderbook_gate_fail_closed
ACCOUNT_FAILURE_ALERT_THRESHOLD = settings.account_failure_alert_threshold
STATE_FILE_PATH    = settings.state_file_path
SLEEP_PER_LOOP = settings.sleep_per_loop
FEE_RATE           = settings.fee_rate
SELL_SLIPPAGE_BUFFER = settings.sell_slippage_buffer

def _resolve_path(path_val: str, default_name: str) -> str:
    if os.path.exists(path_val):
        return path_val
    for candidate in [os.path.join(os.getcwd(), default_name), f"/app/{default_name}", default_name]:
        if os.path.exists(candidate):
            return candidate
    return f"/app/{default_name}"

MODEL_PATH   = _resolve_path(settings.model_path, "grok_gqa_v9_best.pth")
SCALER_PATH  = _resolve_path(settings.scaler_path, "feature_scaler.pkl")
SEQUENCE_LEN = 32

HEARTBEAT_PATH      = settings.heartbeat_path
DISCORD_WEBHOOK_URL = settings.discord_webhook_url

# -- Alpaca clients -------------------------------------------------------------
API_KEY    = settings.apca_api_key_id
API_SECRET = settings.apca_api_secret_key
PAPER      = settings.apca_api_paper

logger.info(f"Alpaca client initialized (paper={PAPER})")

trading_client = TradingClient(api_key=API_KEY, secret_key=API_SECRET, paper=PAPER)
data_client    = CryptoHistoricalDataClient()
