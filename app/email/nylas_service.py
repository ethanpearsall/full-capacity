"""Nylas integration service for one-click email connection.

Handles OAuth flow, webhook verification, smart attachment filtering,
and email processing via the Nylas API.
"""

import hashlib
import hmac
import logging
import re
import uuid
from typing import Optional

import httpx

from app.config import settings
from app.database import get_supabase_admin
from app.email.helpers import get_file_extension

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default smart filter rules (applied when no org-specific filters exist)
# ---------------------------------------------------------------------------
DEFAULT_SKIP_CONTENT_TYPES = {
    "image/gif",
    "image/x-icon",
    "image/vnd.microsoft.icon",
    "text/calendar",
    "application/ics",
    "text/html",
}

# Patterns that indicate inline/signature images (logos, tracking pixels, etc.)
INLINE_SKIP_PATTERNS = [
    r"^image\d+\.(png|jpg|gif)$",      # image001.png style (Outlook inline)
    r"^(logo|banner|header|footer|icon|spacer|pixel|tracking)\b",
    r"\bsignature\b",
    r"\btracking\b",
    r"^cid:",
]

# Minimum file size to process (skip tiny images that are likely icons/pixels)
MIN_FILE_SIZE_BYTES = 2048  # 2 KB


def get_nylas_auth_url(provider: str, org_id: str, redirect_uri: str) -> str:
    """Build the Nylas OAuth authorization URL.

    Args:
        provider: 'google' or 'microsoft'
        org_id: Organisation ID to embed in state
        redirect_uri: Callback URL after OAuth completes
    """
    base = settings.NYLAS_API_URI.rstrip("/")
    params = {
        "client_id": settings.NYLAS_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "provider": provider,
        "state": org_id,
    }
    query = "&".join(f"{k}={_url_encode(v)}" for k, v in params.items())
    return f"{base}/v3/connect/auth?{query}"


def _url_encode(value: str) -> str:
    """Simple URL encoding for query parameters."""
    import urllib.parse
    return urllib.parse.quote(str(value), safe="")


async def exchange_code_for_grant(code: str) -> dict:
    """Exchange an OAuth authorization code for a Nylas grant.

    Returns the grant response including grant_id and email.
    Uses Bearer API key auth per Nylas v3 docs (no client_secret in body).
    """
    base = settings.NYLAS_API_URI.rstrip("/")
    url = f"{base}/v3/connect/token"

    payload = {
        "client_id": settings.NYLAS_CLIENT_ID,
        "client_secret": settings.NYLAS_API_KEY,
        "code": code,
        "redirect_uri": settings.NYLAS_CALLBACK_URI,
        "grant_type": "authorization_code",
    }

    headers = {
        "Authorization": f"Bearer {settings.NYLAS_API_KEY}",
        "Content-Type": "application/json",
    }

    logger.error(
        "Nylas token exchange request -- URL: %s, payload: %s",
        url,
        {k: (v if k != "code" else v[:8] + "...") for k, v in payload.items()},
    )

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, headers=headers, timeout=30)

        logger.error(
            "Nylas token exchange response -- status: %s, body: %s",
            resp.status_code,
            resp.text,
        )

        resp.raise_for_status()
        return resp.json()


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    """Verify a Nylas webhook signature using HMAC SHA-256."""
    secret = settings.NYLAS_WEBHOOK_SECRET
    if not secret:
        logger.warning("NYLAS_WEBHOOK_SECRET not configured, skipping verification")
        return True

    expected = hmac.new(
        secret.encode(),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


async def fetch_message(grant_id: str, message_id: str) -> dict:
    """Fetch a single message from Nylas API."""
    base = settings.NYLAS_API_URI.rstrip("/")
    url = f"{base}/v3/grants/{grant_id}/messages/{message_id}"

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {settings.NYLAS_API_KEY}"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("data", resp.json())


async def download_attachment(grant_id: str, message_id: str, attachment_id: str) -> bytes:
    """Download an attachment's content from Nylas API."""
    import urllib.parse

    base = settings.NYLAS_API_URI.rstrip("/")
    encoded_attachment_id = urllib.parse.quote(attachment_id, safe="")
    url = f"{base}/v3/grants/{grant_id}/messages/{message_id}/attachments/{encoded_attachment_id}/download"

    logger.info("Nylas attachment download URL: %s", url)

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url,
            headers={
                "Authorization": f"Bearer {settings.NYLAS_API_KEY}",
                "Accept": "application/octet-stream",
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.content


def should_skip_attachment(
    attachment: dict,
    org_filters: Optional[list] = None,
) -> Optional[str]:
    """Determine if an attachment should be skipped.

    Returns a skip reason string if the attachment should be skipped,
    or None if it should be processed.

    Args:
        attachment: Nylas attachment object with filename, content_type, size, etc.
        org_filters: Organisation-specific filter rules from DB
    """
    filename = (attachment.get("filename") or "").strip()
    content_type = (attachment.get("content_type") or "").lower()
    size = attachment.get("size") or 0
    is_inline = attachment.get("is_inline", False)
    content_id = attachment.get("content_id") or ""

    # Rule 1: Skip inline images (embedded in email body)
    if is_inline:
        return f"Inline attachment: {filename or content_type}"

    # Rule 2: Skip attachments with Content-ID (typically inline references)
    if content_id and not filename:
        return f"Content-ID reference without filename"

    # Rule 3: Skip known non-document content types
    if content_type in DEFAULT_SKIP_CONTENT_TYPES:
        return f"Non-document content type: {content_type}"

    # Rule 4: Skip tiny files (tracking pixels, spacer images, icons)
    if size > 0 and size < MIN_FILE_SIZE_BYTES:
        return f"File too small ({size} bytes, minimum {MIN_FILE_SIZE_BYTES})"

    # Rule 5: Skip files matching inline/signature patterns
    if filename:
        lower_name = filename.lower()
        for pattern in INLINE_SKIP_PATTERNS:
            if re.search(pattern, lower_name, re.IGNORECASE):
                return f"Filename matches skip pattern: {filename}"

    # Rule 6: Apply organisation-specific filters
    if org_filters:
        for f in org_filters:
            if not f.get("is_active", True):
                continue
            ftype = f.get("filter_type", "")
            fvalue = f.get("filter_value", "")

            if ftype == "skip_content_type":
                # Support wildcards like 'image/*'
                if fvalue.endswith("/*"):
                    prefix = fvalue[:-2]
                    if content_type.startswith(prefix + "/"):
                        return f"Org filter: content type matches {fvalue}"
                elif content_type == fvalue.lower():
                    return f"Org filter: content type {content_type}"

            elif ftype == "skip_filename_pattern":
                if filename and re.search(fvalue, filename, re.IGNORECASE):
                    return f"Org filter: filename matches pattern {fvalue}"

            elif ftype == "skip_size_under":
                try:
                    threshold = int(fvalue)
                    if size > 0 and size < threshold:
                        return f"Org filter: file too small ({size} < {threshold})"
                except ValueError:
                    pass

            elif ftype == "skip_size_over":
                try:
                    threshold = int(fvalue)
                    if size > threshold:
                        return f"Org filter: file too large ({size} > {threshold})"
                except ValueError:
                    pass

    return None


async def process_nylas_message(
    grant_id: str,
    message_id: str,
    org_id: str,
    connection_id: str,
) -> None:
    """Process a new email message received via Nylas webhook.

    Fetches the message, evaluates attachments through smart filtering,
    and queues processable attachments through the document pipeline.
    """
    from app.email.processor import process_email_attachments
    from app.email.helpers import check_duplicate_email

    sb = get_supabase_admin()

    # Fetch the full message from Nylas
    try:
        message = await fetch_message(grant_id, message_id)
    except Exception as e:
        logger.error("Failed to fetch Nylas message %s: %s", message_id, str(e))
        return

    # Extract email metadata
    from_list = message.get("from", [])
    from_name = from_list[0].get("name", "") if from_list else ""
    from_email = from_list[0].get("email", "") if from_list else ""
    to_list = message.get("to", [])
    to_email = to_list[0].get("email", "") if to_list else ""
    subject = message.get("subject", "")
    body = message.get("snippet", "")
    nylas_message_id = message.get("id", message_id)
    internet_message_id = message.get("internet_message_id", "")

    # Duplicate check using internet Message-ID
    if internet_message_id:
        is_dup = await check_duplicate_email(internet_message_id, org_id)
        if is_dup:
            logger.info("Duplicate Nylas message %s, skipping", internet_message_id)
            return

    # Get attachments from message
    nylas_attachments = message.get("attachments", [])
    if not nylas_attachments:
        return

    # Load org-specific filters
    try:
        filters_result = (
            sb.table("attachment_filters")
            .select("*")
            .eq("organisation_id", org_id)
            .eq("is_active", True)
            .execute()
        )
        org_filters = filters_result.data or []
    except Exception:
        org_filters = []

    # Create ingestion record
    ingestion_id = str(uuid.uuid4())
    sb.table("email_ingestions").insert({
        "id": ingestion_id,
        "organisation_id": org_id,
        "message_id": internet_message_id or nylas_message_id,
        "from_address": from_email,
        "from_name": from_name,
        "to_address": to_email,
        "subject": subject,
        "body_preview": body[:500] if body else "",
        "source": "nylas",
        "attachment_count": len(nylas_attachments),
        "email_connection_id": connection_id,
        "raw_headers": {
            "from": f"{from_name} <{from_email}>",
            "to": to_email,
            "subject": subject,
            "nylas_message_id": nylas_message_id,
        },
    }).execute()

    # Filter and download attachments
    attachments_to_process = []
    skipped_count = 0

    for att in nylas_attachments:
        skip_reason = should_skip_attachment(att, org_filters)
        if skip_reason:
            logger.info("Skipping attachment '%s': %s", att.get("filename", ""), skip_reason)
            # Record the skip in email_attachments
            sb.table("email_attachments").insert({
                "id": str(uuid.uuid4()),
                "email_ingestion_id": ingestion_id,
                "original_filename": att.get("filename", "unknown"),
                "content_type": att.get("content_type", ""),
                "file_size_bytes": att.get("size", 0),
                "processing_status": "skipped",
                "skip_reason": skip_reason,
            }).execute()
            skipped_count += 1
            continue

        # Download the attachment content
        try:
            content = await download_attachment(
                grant_id, nylas_message_id, att["id"]
            )
            attachments_to_process.append({
                "filename": att.get("filename", "attachment"),
                "content_type": att.get("content_type", ""),
                "content": content,
                "size": len(content),
            })
        except Exception as e:
            logger.error(
                "Failed to download attachment '%s': %s",
                att.get("filename", ""), str(e),
            )
            sb.table("email_attachments").insert({
                "id": str(uuid.uuid4()),
                "email_ingestion_id": ingestion_id,
                "original_filename": att.get("filename", "unknown"),
                "content_type": att.get("content_type", ""),
                "file_size_bytes": att.get("size", 0),
                "processing_status": "failed",
                "skip_reason": f"Download failed: {str(e)[:200]}",
            }).execute()

    # Update attachment count to reflect what we're actually processing
    sb.table("email_ingestions").update({
        "attachment_count": len(nylas_attachments),
    }).eq("id", ingestion_id).execute()

    # Update connection last_sync_at
    sb.table("email_connections").update({
        "last_sync_at": "now()",
    }).eq("id", connection_id).execute()

    if attachments_to_process:
        await process_email_attachments(
            ingestion_id=ingestion_id,
            org_id=org_id,
            attachments=attachments_to_process,
            email_from=from_email,
            email_subject=subject,
        )
    elif skipped_count > 0:
        # All attachments were filtered out
        sb.table("email_ingestions").update({
            "status": "completed",
            "processed_count": 0,
        }).eq("id", ingestion_id).execute()
