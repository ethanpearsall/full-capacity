import hashlib
import hmac
import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import JSONResponse

from app.auth.dependencies import get_current_user
from app.config import settings
from app.database import get_supabase_admin
from app.chat.bot import send_telegram_message, parse_command
from app.chat.auth_link import (
    initiate_link,
    verify_link,
    unlink_account,
    get_linked_account,
)
from app.chat.query_engine import process_query

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

# Rate limit: track per chat_id -> list of timestamps
_rate_limit_cache: dict[int, list[float]] = {}
RATE_LIMIT_MAX = 30
RATE_LIMIT_WINDOW = 3600  # 1 hour


def _check_rate_limit(chat_id: int) -> bool:
    """Return True if the user is within rate limits."""
    now = time.time()
    timestamps = _rate_limit_cache.get(chat_id, [])
    # Prune old entries
    timestamps = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW]
    if len(timestamps) >= RATE_LIMIT_MAX:
        _rate_limit_cache[chat_id] = timestamps
        return False
    timestamps.append(now)
    _rate_limit_cache[chat_id] = timestamps
    return True


def _validate_telegram_webhook(request_body: bytes, secret_token: str) -> bool:
    """Validate the Telegram webhook request using X-Telegram-Bot-Api-Secret-Token header."""
    # This is handled via the secret_token parameter set on the webhook
    return True


@router.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    """Handle incoming Telegram webhook messages."""
    # Validate webhook secret
    secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    expected_secret = settings.TELEGRAM_WEBHOOK_SECRET
    if expected_secret and secret_header != expected_secret:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    message = body.get("message")
    if not message:
        return {"ok": True}

    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "").strip()
    username = message.get("from", {}).get("username", "")

    if not chat_id or not text:
        return {"ok": True}

    # Rate limiting
    if not _check_rate_limit(chat_id):
        await send_telegram_message(chat_id, "You've exceeded the rate limit (30 queries/hour). Please wait a bit.")
        return {"ok": True}

    # Handle commands
    command, args = parse_command(text)
    if command:
        await _handle_command(chat_id, command, args, username)
        return {"ok": True}

    # For non-command messages, user must be linked
    linked = get_linked_account(chat_id)
    if not linked:
        await send_telegram_message(
            chat_id,
            "You need to link your Full Capacity account first.\n\n"
            "Use /link your@email.com to get started.",
        )
        return {"ok": True}

    org_id = linked["organisation_id"]

    # Log the user query (not the document content)
    sb = get_supabase_admin()
    _log_chat_message(sb, chat_id, org_id, "user", text)

    # Process the natural language query
    try:
        response_text, doc_ids = await process_query(text, org_id)
    except Exception:
        logger.exception("Query processing failed for chat_id=%s", chat_id)
        response_text = "Sorry, something went wrong processing your query. Please try again."
        doc_ids = []

    # Log the assistant response
    _log_chat_message(sb, chat_id, org_id, "assistant", response_text, doc_ids)

    await send_telegram_message(chat_id, response_text)
    return {"ok": True}


async def _handle_command(chat_id: int, command: str, args: str, username: str):
    """Handle bot slash commands."""
    if command == "start":
        await send_telegram_message(
            chat_id,
            "*Welcome to Full Capacity* \n\n"
            "I can help you search and query your document library.\n\n"
            "*To get started:*\n"
            "1. Use /link your@email.com to link your account\n"
            "2. Enter the verification code with /verify CODE\n"
            "3. Then just ask me anything about your documents!\n\n"
            "Type /help for more commands.",
        )

    elif command == "help":
        await send_telegram_message(
            chat_id,
            "*Available Commands:*\n\n"
            "/link email@example.com — Link your account\n"
            "/verify 123456 — Complete account linking\n"
            "/recent — Show 5 most recent documents\n"
            "/stats — Document statistics\n"
            "/unlink — Unlink your Telegram account\n"
            "/help — Show this message\n\n"
            "*Example queries:*\n"
            '• "Find the Johnson invoice"\n'
            '• "Show me all contracts from January"\n'
            '• "How much did we bill Smith last month?"\n'
            '• "What\'s the latest bank statement?"',
        )

    elif command == "link":
        if not args:
            await send_telegram_message(chat_id, "Please provide your email: /link your@email.com")
            return
        result = await initiate_link(chat_id, args.strip(), username)
        await send_telegram_message(chat_id, result)

    elif command == "verify":
        if not args:
            await send_telegram_message(chat_id, "Please provide the code: /verify 123456")
            return
        result = await verify_link(chat_id, args.strip())
        await send_telegram_message(chat_id, result)

    elif command == "unlink":
        result = await unlink_account(chat_id)
        await send_telegram_message(chat_id, result)

    elif command == "recent":
        linked = get_linked_account(chat_id)
        if not linked:
            await send_telegram_message(chat_id, "Please link your account first with /link your@email.com")
            return
        response_text, _ = await process_query("show me the 5 most recently filed documents", linked["organisation_id"])
        await send_telegram_message(chat_id, response_text)

    elif command == "stats":
        linked = get_linked_account(chat_id)
        if not linked:
            await send_telegram_message(chat_id, "Please link your account first with /link your@email.com")
            return
        response_text, _ = await process_query("how many documents are there and break down by type", linked["organisation_id"])
        await send_telegram_message(chat_id, response_text)

    else:
        await send_telegram_message(chat_id, "Unknown command. Type /help for available commands.")


@router.post("/api/chat/query")
async def web_chat_query(request: Request, user: dict = Depends(get_current_user)):
    """Handle chat queries from the web dashboard widget."""
    body = await request.json()
    query = body.get("query", "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Query is required")

    org_id = user["organisation_id"]

    sb = get_supabase_admin()
    _log_chat_message(sb, None, org_id, "user", query)

    try:
        response_text, doc_ids = await process_query(query, org_id)
    except Exception:
        logger.exception("Web chat query failed")
        response_text = "Sorry, something went wrong. Please try again."
        doc_ids = []

    _log_chat_message(sb, None, org_id, "assistant", response_text, doc_ids)

    return {"response": response_text, "document_ids": doc_ids}


def _log_chat_message(
    sb, chat_id: Optional[int], org_id: str, role: str, content: str, doc_ids: Optional[list] = None
):
    """Log a chat message for audit trail."""
    try:
        sb.table("chat_messages").insert({
            "telegram_chat_id": chat_id,
            "organisation_id": org_id,
            "role": role,
            "content": content,
            "documents_referenced": doc_ids or [],
        }).execute()
    except Exception:
        logger.exception("Failed to log chat message")
