import logging
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"


def _api_url(method: str) -> str:
    """Build the Telegram Bot API URL for a given method."""
    return f"{TELEGRAM_API_BASE.format(token=settings.TELEGRAM_BOT_TOKEN)}/{method}"


async def send_telegram_message(chat_id: int, text: str, parse_mode: str = "Markdown") -> bool:
    """Send a message to a Telegram chat. Returns True on success."""
    # Telegram messages have a 4096 char limit
    if len(text) > 4000:
        text = text[:4000] + "\n\n_...truncated_"

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(_api_url("sendMessage"), json=payload)
            if response.status_code != 200:
                # Retry without parse_mode if markdown fails
                payload["parse_mode"] = None
                response = await client.post(_api_url("sendMessage"), json=payload)
            return response.status_code == 200
    except Exception:
        logger.exception("Failed to send Telegram message to chat_id=%s", chat_id)
        return False


async def set_webhook(url: str) -> bool:
    """Set the Telegram webhook URL."""
    payload = {
        "url": url,
        "secret_token": settings.TELEGRAM_WEBHOOK_SECRET,
        "allowed_updates": ["message"],
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(_api_url("setWebhook"), json=payload)
            data = response.json()
            if data.get("ok"):
                logger.info("Telegram webhook set to %s", url)
                return True
            else:
                logger.error("Failed to set webhook: %s", data)
                return False
    except Exception:
        logger.exception("Failed to set Telegram webhook")
        return False


def parse_command(text: str) -> tuple[Optional[str], str]:
    """Parse a Telegram command from message text.

    Returns (command, args) or (None, '') if not a command.
    """
    if not text.startswith("/"):
        return None, ""

    parts = text.split(None, 1)
    command = parts[0].lstrip("/").lower()
    # Strip @botname suffix if present
    if "@" in command:
        command = command.split("@")[0]
    args = parts[1] if len(parts) > 1 else ""
    return command, args
