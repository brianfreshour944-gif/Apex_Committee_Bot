# Coolify Deployment Guide — alpaca-penny-bot

This repo ships a Coolify-ready setup: a `Dockerfile`, a Coolify-compatible
`docker-compose.yml`, and env-var wiring. The bot is a **background worker**
(there is no HTTP endpoint), so no domain/port mapping is required.

## Option A — Dockerfile deploy (simplest)

1. Coolify dashboard → **+ New Resource** → **Public Repository**
2. Repo: `https://github.com/brianfreshour944-gif/alpaca-penny-bot.git`
   Branch: `main`
3. Build pack: **Dockerfile** (Coolify auto-detects `Dockerfile` at the root)
4. Leave the **port** field empty — this app does not listen on HTTP.
   If Coolify forces a port, set it to `8000` and **do not** attach a domain.
5. Add **Environment Variables** (mark `PACA_*` as **secrets**, not build args):

   | Variable | Example | Notes |
   |---|---|---|
   | `PACA_API_KEY` | `PK...` | Alpaca key ID (**required**) |
   | `PACA_SECRET_KEY` | `***` | Alpaca secret (**required**) |
   | `PACA_BASE_URL` | `https://paper-api.alpaca.markets` | Default = paper. Live: `https://api.alpaca.markets` |
   | `PACA_STREAMING_TOKEN` | `***` | Only needed for the live data stream |
   | `DEFAULT_ORDER_MODE` | `paper` | `paper` (default) or `live` |
   | `MAX_ORDER_SIZE_USD` | `25.00` | Max USD per order |

6. **Persistent Storage**: add a volume
   - Name: `alpaca-bot-data` → Mount path: `/app/data`
   (persists `data/trades.jsonl` across deploys)
7. **Deploy**. The container shows `running` when the bot loop is up.

## Option B — Docker Compose deploy

1. Same as Option A but pick build pack **Docker Compose**
   (Coolify finds `docker-compose.yml` in the repo root).
2. Define the same environment variables in the Coolify UI — the compose file
   passes them through `${VAR}` interpolation.
3. The `alpaca-bot-data` named volume is declared in the compose file;
   Coolify creates and persists it automatically.

## Known gaps in the bot code (as of this scaffold)

The deployment infra below is complete and validated, but the bot code itself
is an early scaffold. Known issues (not fixed under the "minimal import fixes
only" scope):

- **`bot.strategy` is never wired** (`engine/bot.py` leaves it `None`), so the
  main loop in `run_until_server_stop` exits immediately after startup. The
  container starts, prints `[server] order mode: ...`, then exits cleanly —
  Coolify will restart it per its restart policy. No orders are placed as-is.
- **Order-path code is untested against the real SDK**: `Broker.mplace()` calls
  `self.alpha_client.orders.create(...)` and `market_hours()` uses
  `self.alpha_client.timezone.get_offset()` — neither exists on
  `alpaca_trade_api.REST`. Those paths are unreachable today (see above) but
  will need a real implementation before live ordering works.
- **`Broker._activity_guard()` calls itself recursively** (infinite recursion
  if ever reached).
- `engine/feed.py` is largely unused; `engine/bot.py` manages the data stream
  directly via `websockets`.

Bottom line: the Coolify app deploys and runs the container, but trading
requires wiring a strategy and implementing the broker order path first.

## Post-deploy

- **Logs**: container logs show `[server] order mode: PAPER` at startup,
  `[feed]` lines for the data stream, and `[loop]` lines for orders.
- **Healthcheck**: the Dockerfile defines a process-based `HEALTHCHECK`
  (`pgrep -f "server.server"`), since there is no HTTP endpoint to probe.
- **Paper safety**: the bot always starts **paper** unless you explicitly set
  `DEFAULT_ORDER_MODE=live` (and `PACA_BASE_URL` to the live endpoint).
- **Verify your account mode in Alpaca's dashboard too** — this is a research
  scaffold, not a production trading system.

## Local test (optional)

```bash
docker compose build
docker compose up   # fails fast if PACA_API_KEY/PACA_SECRET_KEY are missing
```
