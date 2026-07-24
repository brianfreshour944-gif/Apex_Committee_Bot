# notifications.py — Shared-session Discord webhook alerts.
import aiohttp
from config import logger, DISCORD_WEBHOOK_URL

_session = None

async def _get_session():
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session

async def send_discord_alert(title: str, description: str, color: int = 0x7B2FBE):
    if not DISCORD_WEBHOOK_URL: return
    try:
        s = await _get_session()
        async with s.post(DISCORD_WEBHOOK_URL,
            json={"embeds": [{"title": title, "description": description, "color": color}]},
            timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status not in (200, 204):
                logger.warning(f"Discord HTTP {r.status}")
    except Exception as e:
        logger.warning(f"Discord failed: {e}")
