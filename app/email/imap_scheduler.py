import asyncio
import logging
from datetime import datetime, timezone

from app.database import get_supabase_admin
from app.email.processor import poll_imap_mailbox

logger = logging.getLogger(__name__)


def _minutes_since(timestamp_str: str) -> float:
    """Return minutes elapsed since the given ISO timestamp."""
    if not timestamp_str:
        return float("inf")
    try:
        then = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return (now - then).total_seconds() / 60
    except Exception:
        return float("inf")


async def imap_polling_loop() -> None:
    """Background loop that checks active IMAP configs and polls those that are due."""
    # Wait a bit on startup to let the app initialise
    await asyncio.sleep(10)

    while True:
        try:
            sb = get_supabase_admin()
            result = (
                sb.table("imap_configs")
                .select("*")
                .eq("is_active", True)
                .execute()
            )

            configs = result.data or []
            for config in configs:
                interval = config.get("poll_interval_minutes", 5)
                last_polled = config.get("last_polled_at")
                elapsed = _minutes_since(last_polled) if last_polled else float("inf")

                if elapsed >= interval:
                    org_id = config["organisation_id"]
                    logger.info("IMAP poll triggered for org %s", org_id)
                    try:
                        await poll_imap_mailbox(config, org_id)
                        sb.table("imap_configs").update({
                            "last_polled_at": datetime.now(timezone.utc).isoformat(),
                        }).eq("id", config["id"]).execute()
                    except Exception as e:
                        logger.error(
                            "IMAP poll failed for org %s: %s", org_id, str(e)
                        )

        except Exception as e:
            logger.error("IMAP polling loop error: %s", str(e))

        await asyncio.sleep(60)
