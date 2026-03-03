import logging
import secrets
import time
from typing import Optional

from app.database import get_supabase_admin

logger = logging.getLogger(__name__)

# In-memory store for pending verification codes
# In production, use Redis or similar. Format: {chat_id: {email, code, expires_at, username}}
_pending_links: dict[int, dict] = {}

CODE_EXPIRY_SECONDS = 600  # 10 minutes


async def initiate_link(chat_id: int, email: str, username: str = "") -> str:
    """Start the account linking process. Generates a verification code."""
    sb = get_supabase_admin()

    # Check if already linked
    existing = (
        sb.table("telegram_links")
        .select("id")
        .eq("telegram_chat_id", chat_id)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )

    if existing.data:
        return "Your Telegram account is already linked. Use /unlink first if you want to link a different account."

    # Verify the email exists in our system
    user_result = (
        sb.table("users")
        .select("id, organisation_id, email")
        .eq("email", email.lower().strip())
        .limit(1)
        .execute()
    )

    if not user_result.data:
        return "No Full Capacity account found with that email address. Please check and try again."

    # Generate 6-digit verification code
    code = f"{secrets.randbelow(900000) + 100000}"

    _pending_links[chat_id] = {
        "email": email.lower().strip(),
        "code": code,
        "expires_at": time.time() + CODE_EXPIRY_SECONDS,
        "username": username,
        "user_id": user_result.data[0]["id"],
        "organisation_id": user_result.data[0]["organisation_id"],
    }

    # In a production system, send this code via email.
    # For now, log it (visible in Railway logs) and tell the user.
    logger.info("Verification code for chat_id=%s email=%s: %s", chat_id, email, code)

    return (
        f"A verification code has been generated for *{email}*.\n\n"
        f"Check your server logs for the code, then use:\n"
        f"/verify CODE\n\n"
        f"The code expires in 10 minutes."
    )


async def verify_link(chat_id: int, code: str) -> str:
    """Complete account linking by verifying the code."""
    pending = _pending_links.get(chat_id)
    if not pending:
        return "No pending link found. Use /link your@email.com first."

    # Check expiry
    if time.time() > pending["expires_at"]:
        del _pending_links[chat_id]
        return "Your verification code has expired. Please use /link again."

    # Check code
    if code.strip() != pending["code"]:
        return "Invalid verification code. Please try again."

    # Create the link
    sb = get_supabase_admin()
    try:
        sb.table("telegram_links").insert({
            "user_id": pending["user_id"],
            "organisation_id": pending["organisation_id"],
            "telegram_chat_id": chat_id,
            "telegram_username": pending.get("username"),
            "is_active": True,
        }).execute()
    except Exception:
        logger.exception("Failed to create telegram link for chat_id=%s", chat_id)
        return "Failed to link account. Please try again."

    # Clean up
    del _pending_links[chat_id]

    return (
        "Account linked successfully!\n\n"
        "You can now search your documents by sending me messages like:\n"
        '• "Find the Johnson invoice"\n'
        '• "Show me all contracts from January"\n'
        '• "What\'s the latest bank statement?"\n\n'
        "Type /help for more options."
    )


async def unlink_account(chat_id: int) -> str:
    """Unlink a Telegram account."""
    sb = get_supabase_admin()
    try:
        result = (
            sb.table("telegram_links")
            .update({"is_active": False})
            .eq("telegram_chat_id", chat_id)
            .eq("is_active", True)
            .execute()
        )

        if result.data:
            return "Your account has been unlinked. Use /link to link again."
        else:
            return "No linked account found."
    except Exception:
        logger.exception("Failed to unlink account for chat_id=%s", chat_id)
        return "Failed to unlink account. Please try again."


def get_linked_account(chat_id: int) -> Optional[dict]:
    """Get the linked account for a Telegram chat ID. Returns None if not linked."""
    sb = get_supabase_admin()
    try:
        result = (
            sb.table("telegram_links")
            .select("user_id, organisation_id, telegram_username")
            .eq("telegram_chat_id", chat_id)
            .eq("is_active", True)
            .limit(1)
            .execute()
        )

        if result.data:
            return result.data[0]
    except Exception:
        logger.exception("Failed to check linked account for chat_id=%s", chat_id)
    return None
