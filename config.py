import logging
from logging.handlers import RotatingFileHandler
import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from alpaca.trading.client import TradingClient
from alpaca.data.historical import CryptoHistoricalDataClient

# ── Configuration Schema ───────────────────────────────────────────────────────
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
    min_order_usd: float = 10.0
    
    stop_loss_pct: float = 0.04
    take_profit_pct: float = 0.06
    trailing_stop_pct: float = 0.02
    max_hold_hours: float = 8.0
    max_open_positions: int = 3
    cooldown_seconds_buy: int = 900
    
    sentinel_max_atr_pct: float = 6.0
    sentinel_max_vol_mult: float = 4.0
    max_consecutive_losses: int = 4
    
    max_drawdown_stop: float = -0.10
    
    min_bid_ask_ratio: float = 0.65
    state_file_path: str = "committee_bot_state.json"
    sleep_per_loop: int = 60
    
    model_path: str = "grok_gqa_v9_best.pth"
    scaler_path: str = "feature_scaler.pkl"
    
    heartbeat_path: str = "committee_heartbeat.txt"
    discord_webhook_url: str = ""
    
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()

# ── Logging ────────────────────────────────────────────────────────────────────
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

# ── Universe ───────────────────────────────────────────────────────────────────
SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD"]

# ── Committee brain weights ────────────────────────────────────────────────────
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
SENTINEL_MAX_ATR_PCT  = settings.sentinel_max_atr_pct
SENTINEL_MAX_VOL_MULT = settings.sentinel_max_vol_mult
MAX_CONSECUTIVE_LOSSES = settings.max_consecutive_losses
MAX_DRAWDOWN_STOP = settings.max_drawdown_stop
MIN_BID_ASK_RATIO  = settings.min_bid_ask_ratio
STATE_FILE_PATH    = settings.state_file_path
SLEEP_PER_LOOP = settings.sleep_per_loop

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

# ── Alpaca clients ─────────────────────────────────────────────────────────────
API_KEY    = settings.apca_api_key_id
API_SECRET = settings.apca_api_secret_key
PAPER      = settings.apca_api_paper

logger.info(
    f"Credential check — key_len={len(API_KEY)} key_last4={API_KEY[-4:]} | "
    f"secret_len={len(API_SECRET)} | paper={PAPER}"
)

trading_client = TradingClient(api_key=API_KEY, secret_key=API_SECRET, paper=PAPER)
data_client    = CryptoHistoricalDataClient()
