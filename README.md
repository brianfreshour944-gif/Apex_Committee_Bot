# Alpaca Bot

Paper-first Alpaca trading bot. **PAPER by default** until you explicitly flip the
mode flag; no secrets are committed (`.env` is gitignored, `.env.example` is the
template to copy).

## Run

```powershell
pip install -r requirements.txt
```

1. Copy the template and paste a **test account** key + secret (and a streaming token
   only if you use the live data feed):

   ```powershell
   copy .env.example .env
   notepad .env   # paste PACA_API_KEY / PACA_SECRET_KEY, then save
   ```

2. Start paper (default) or explicit LIVE trading:

   ```powershell
   python -m server.server
   python -m server.server --order-mode live   # only when you mean it
   ```

The loop polls the trading cache for open positions, and each fresh/updated bar
updates the last-close cache the strategy keys off for decisions and order sizing.

## Config

- `DEFAULT_ORDER_MODE` — `paper` (default) or `live`. Always starts paper unless you
  override by mode flag.
- `MAX_ORDER_SIZE_USD` — max shares/order.

## Notes

- Data feed is paper-capable as paper-only on trades: no live streaming order routing
  unless you set `DEFAULT_ORDER_MODE=live`.
- `.env` is ignored by git — never commit it.
- This is a research scaffold, not a final trading system — sanity-check every line
  before running against any account.
