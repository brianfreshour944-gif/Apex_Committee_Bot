"""Environment + config loading.

Paper by default. LIVE requires an explicit flag and is never assumed.
Secrets MUST be pasted by the user into .env (never committed; .env is gitignored).
"""
import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv

    DOTENV_LOADED = True
except Exception:  # pragma: no cover - dotenv required
    DOTENV_LOADED = False


@dataclass
class BotConfig:
    """Validated runtime configuration."""

    api_key: str
    secret_key: str
    base_url: str = "https://paper-api.alpaca.markets"
    stream_token: str = ""
    order_mode: str = "paper"
    max_order_size_usd: float = 25.0

    @classmethod
    def load(cls, dotenv_path: str = ".env", **kwargs) -> "BotConfig":
        os.environ.setdefault("PACA_API_KEY", os.getenv("PACA_API_KEY", ""))
        load_dotenv(dotenv_path, override=False)

        config = cls(
            api_key=os.getenv("PACA_API_KEY", ""),
            secret_key=os.getenv("PACA_SECRET_KEY", ""),
            base_url=os.getenv("PACA_BASE_URL", "https://paper-api.alpaca.markets"),
            stream_token=os.getenv("PACA_STREAMING_TOKEN", ""),
            order_mode=os.getenv("DEFAULT_ORDER_MODE", "paper"),
            max_order_size_usd=float(os.getenv("MAX_ORDER_SIZE_USD", "25.00")),
        )
        if not config.api_key or not config.secret_key:
            raise RuntimeError(
                "PACA_API_KEY and PACA_SECRET_KEY are not set. "
                "Copy .env.example -> .env and paste your Paper credentials."
            )
        if config.base_url and not config.base_url.endswith("/"):
            config.base_url += "/"
        if config.order_mode not in ("paper", "live"):
            config.order_mode = "paper"
        os.environ.setdefault("PACA_SECRET_KEY", config.secret_key)
        return config


if __name__ == "__main__":
    try:
        cfg = BotConfig.load()
        print(f"mode={cfg.order_mode.upper()} max_order_usd={cfg.max_order_size_usd}")
    except RuntimeError as e:
        print("ERROR:", e)
