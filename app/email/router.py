import json
import logging
import re
import uuid
from datetime import date, datetime
from typing import Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Form,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth.dependencies import get_current_user, get_optional_user
from app.config import settings
from app.database import get_supabase_admin
from app.email.helpers import (
    check_duplicate_email,
    extract_header,
    parse_email_address,
    resolve_organisation,
)
from app.email.processor import (
    process_email_attachments,
)
from app.email.nylas_service import (
    exchange_code_for_grant,
    fetch_message,
    get_nylas_auth_url,
    process_nylas_message,
    verify_webhook_signature,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/email", tags=["email"])
templates = Jinja2Templates(directory="templates")

# Webhook secret for SendGrid verification
EMAIL_WEBHOOK_SECRET = getattr(settings, "EMAIL_WEBHOOK_SECRET", "")


def _generate_org_slug(org_name: str) -> str:
    """Generate a URL-safe slug from an organisation name."""
    slug = org_name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug or "default"


def _get_forwarding_address(org_name: str) -> str:
    """Build the forwarding address for an organisation."""
    slug = _generate_org_slug(org_name)
    return f"docs-{slug}@inbound.fullcapacity.ai"


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@router.get("/page", response_class=HTMLResponse, include_in_schema=False)
async def email_dashboard_page(request: Request):
    """Render the email ingestion dashboard page."""
    user = await get_optional_user(request)
    if not user:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(
        "email_dashboard.html", {"request": request, "user": user}
    )


@router.get("/page/{ingestion_id}", response_class=HTMLResponse, include_in_schema=False)
async def email_detail_page(ingestion_id: str, request: Request):
    """Render the email detail page."""
    user = await get_optional_user(request)
    if not user:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(
        "email_detail.html",
        {"request": request, "user": user, "ingestion_id": ingestion_id},
    )


@router.get("/settings-page", response_class=HTMLResponse, include_in_schema=False)
async def email_settings_page(request: Request):
    """Render the email settings page."""
    user = await get_optional_user(request)
    if not user:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(
        "email_settings.html", {"request": request, "user": user}
    )


# ---------------------------------------------------------------------------
# Webhook receiver (SendGrid Inbound Parse)
# ---------------------------------------------------------------------------

@router.post("/inbound")
async def receive_email_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    token: str = Query(""),
):
    """Receive inbound email from SendGrid Inbound Parse.

    Always returns 200 to prevent SendGrid from retrying.
    """
    # Verify webhook token
    if EMAIL_WEBHOOK_SECRET and token != EMAIL_WEBHOOK_SECRET:
        logger.warning("Invalid webhook token received")
        return {"status": "ok", "message": "Invalid token"}

    try:
        form = await request.form()
    except Exception as e:
        logger.error("Failed to parse webhook form data: %s", str(e))
        return {"status": "ok", "message": "Parse error"}

    from_address = form.get("from", "")
    to_address = form.get("to", "")
    subject = form.get("subject", "")
    body_text = form.get("text", "")
    headers_raw = form.get("headers", "")
    envelope_raw = form.get("envelope", "{}")
    attachment_info_raw = form.get("attachment-info", "{}")

    sender_name, sender_email = parse_email_address(str(from_address))

    # Resolve organisation
    org_id = await resolve_organisation(str(to_address), sender_email)
    if not org_id:
        logger.warning(
            "Could not resolve organisation for email from %s to %s",
            sender_email, to_address,
        )
        return {"status": "ok", "message": "Organisation not found, email logged but not processed"}

    # Duplicate check
    message_id = extract_header(str(headers_raw), "Message-ID")
    if message_id:
        is_dup = await check_duplicate_email(message_id, org_id)
        if is_dup:
            return {"status": "ok", "message": "Duplicate email, already processed"}

    sb = get_supabase_admin()

    # Create ingestion record
    ingestion_id = str(uuid.uuid4())
    sb.table("email_ingestions").insert({
        "id": ingestion_id,
        "organisation_id": org_id,
        "message_id": message_id,
        "from_address": sender_email,
        "from_name": sender_name,
        "to_address": str(to_address),
        "subject": str(subject),
        "body_preview": str(body_text)[:500] if body_text else "",
        "source": "webhook",
        "attachment_count": 0,
        "raw_headers": {
            "from": str(from_address),
            "to": str(to_address),
            "subject": str(subject),
        },
    }).execute()

    # Collect file attachments from the form
    attachments = []
    for key in form:
        value = form[key]
        if hasattr(value, "filename") and value.filename:
            file_content = await value.read()
            attachments.append({
                "filename": value.filename,
                "content_type": value.content_type or "",
                "content": file_content,
                "size": len(file_content),
            })

    # Update attachment count
    sb.table("email_ingestions").update(
        {"attachment_count": len(attachments)}
    ).eq("id", ingestion_id).execute()

    if attachments:
        background_tasks.add_task(
            process_email_attachments,
            ingestion_id=ingestion_id,
            org_id=org_id,
            attachments=attachments,
            email_from=sender_email,
            email_subject=str(subject),
        )

    return {
        "status": "ok",
        "message": f"Email received, {len(attachments)} attachments queued for processing",
    }


# ---------------------------------------------------------------------------
# Email ingestion list & detail API
# ---------------------------------------------------------------------------

@router.get("/ingestions")
async def list_email_ingestions(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user),
):
    """List email ingestions for the user's organisation."""
    sb = get_supabase_admin()
    org_id = current_user["organisation_id"]
    offset = (page - 1) * per_page

    query = (
        sb.table("email_ingestions")
        .select("*")
        .eq("organisation_id", org_id)
        .order("received_at", desc=True)
        .range(offset, offset + per_page - 1)
    )

    if status:
        query = query.eq("status", status)

    result = query.execute()
    return {"ingestions": result.data, "count": len(result.data), "page": page}


@router.get("/ingestions/{ingestion_id}")
async def get_email_ingestion_detail(
    ingestion_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Get details of a specific email ingestion including attachments."""
    sb = get_supabase_admin()
    org_id = current_user["organisation_id"]

    ingestion_result = (
        sb.table("email_ingestions")
        .select("*")
        .eq("id", ingestion_id)
        .eq("organisation_id", org_id)
        .single()
        .execute()
    )

    if not ingestion_result.data:
        raise HTTPException(status_code=404, detail="Email ingestion not found")

    ingestion = ingestion_result.data

    attachments_result = (
        sb.table("email_attachments")
        .select("*")
        .eq("email_ingestion_id", ingestion_id)
        .order("created_at")
        .execute()
    )

    ingestion["attachments"] = attachments_result.data
    return ingestion


@router.post("/ingestions/{ingestion_id}/reprocess")
async def reprocess_email(
    ingestion_id: str,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
):
    """Reprocess failed/skipped attachments for an email ingestion."""
    sb = get_supabase_admin()
    org_id = current_user["organisation_id"]

    ingestion_result = (
        sb.table("email_ingestions")
        .select("*")
        .eq("id", ingestion_id)
        .eq("organisation_id", org_id)
        .single()
        .execute()
    )

    if not ingestion_result.data:
        raise HTTPException(status_code=404, detail="Email ingestion not found")

    # Get failed/skipped attachments that we could retry
    attachments_result = (
        sb.table("email_attachments")
        .select("*")
        .eq("email_ingestion_id", ingestion_id)
        .in_("processing_status", ["failed", "skipped"])
        .execute()
    )

    if not attachments_result.data:
        return {"status": "ok", "message": "No attachments to reprocess"}

    # Reset their status
    for att in attachments_result.data:
        sb.table("email_attachments").update({
            "processing_status": "pending",
            "skip_reason": None,
        }).eq("id", att["id"]).execute()

    return {"status": "ok", "message": f"{len(attachments_result.data)} attachments queued for reprocessing"}


@router.delete("/ingestions/{ingestion_id}")
async def delete_email_ingestion(
    ingestion_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Delete an email ingestion record and its attachments."""
    sb = get_supabase_admin()
    org_id = current_user["organisation_id"]

    result = (
        sb.table("email_ingestions")
        .select("id")
        .eq("id", ingestion_id)
        .eq("organisation_id", org_id)
        .single()
        .execute()
    )

    if not result.data:
        raise HTTPException(status_code=404, detail="Email ingestion not found")

    # Cascade delete handles email_attachments
    sb.table("email_ingestions").delete().eq("id", ingestion_id).execute()
    return {"status": "ok", "message": "Email ingestion deleted"}


# ---------------------------------------------------------------------------
# Stats & Status
# ---------------------------------------------------------------------------

@router.get("/stats")
async def get_email_stats(current_user: dict = Depends(get_current_user)):
    """Get email ingestion statistics for dashboard cards."""
    sb = get_supabase_admin()
    org_id = current_user["organisation_id"]

    all_ingestions = (
        sb.table("email_ingestions")
        .select("id, status, received_at, attachment_count, processed_count")
        .eq("organisation_id", org_id)
        .execute()
    )

    ingestions = all_ingestions.data or []
    total_emails = len(ingestions)

    today = date.today().isoformat()
    emails_today = sum(
        1 for i in ingestions
        if i.get("received_at", "").startswith(today)
    )

    total_attachments = sum(i.get("attachment_count", 0) for i in ingestions)
    total_processed = sum(i.get("processed_count", 0) for i in ingestions)

    success_rate = 0.0
    if total_attachments > 0:
        success_rate = round((total_processed / total_attachments) * 100, 1)

    return {
        "total_emails": total_emails,
        "emails_today": emails_today,
        "attachments_processed": total_processed,
        "total_attachments": total_attachments,
        "success_rate": success_rate,
    }


@router.get("/status")
async def get_email_forwarding_status(
    current_user: dict = Depends(get_current_user),
):
    """Get email forwarding status for the settings page."""
    sb = get_supabase_admin()
    org_id = current_user["organisation_id"]

    # Resolve org name for forwarding address
    org_data = current_user.get("organisations")
    if isinstance(org_data, dict):
        org_name = org_data.get("name", "")
    else:
        org_name = ""
    if not org_name:
        try:
            org_result = sb.table("organisations").select("name").eq("id", org_id).single().execute()
            org_name = org_result.data.get("name", "") if org_result.data else ""
        except Exception:
            org_name = ""

    forwarding_address = _get_forwarding_address(org_name)

    # Latest ingestion
    try:
        latest_result = (
            sb.table("email_ingestions")
            .select("received_at, from_address, subject")
            .eq("organisation_id", org_id)
            .order("received_at", desc=True)
            .limit(1)
            .execute()
        )
        latest = latest_result.data[0] if latest_result.data else None
    except Exception:
        latest = None

    # Total count
    try:
        count_result = (
            sb.table("email_ingestions")
            .select("id")
            .eq("organisation_id", org_id)
            .execute()
        )
        total_count = len(count_result.data) if count_result.data else 0
    except Exception:
        total_count = 0

    return {
        "forwarding_address": forwarding_address,
        "is_active": total_count > 0,
        "total_emails_received": total_count,
        "last_email_received": latest["received_at"] if latest else None,
        "last_email_from": latest["from_address"] if latest else None,
        "last_email_subject": latest["subject"] if latest else None,
    }


# ---------------------------------------------------------------------------
# Sender whitelist
# ---------------------------------------------------------------------------

@router.get("/whitelist")
async def get_whitelist(current_user: dict = Depends(get_current_user)):
    """Get the sender whitelist for the user's organisation."""
    sb = get_supabase_admin()
    result = (
        sb.table("email_sender_whitelist")
        .select("*")
        .eq("organisation_id", current_user["organisation_id"])
        .order("created_at")
        .execute()
    )
    return {"entries": result.data}


@router.post("/whitelist")
async def add_whitelist_entry(
    address_or_domain: str = Form(...),
    current_user: dict = Depends(get_current_user),
):
    """Add an email address or domain to the whitelist."""
    sb = get_supabase_admin()
    sb.table("email_sender_whitelist").insert({
        "id": str(uuid.uuid4()),
        "organisation_id": current_user["organisation_id"],
        "address_or_domain": address_or_domain.strip().lower(),
    }).execute()
    return {"status": "ok", "message": f"Added {address_or_domain} to whitelist"}


@router.delete("/whitelist/{entry_id}")
async def remove_whitelist_entry(
    entry_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Remove an entry from the sender whitelist."""
    sb = get_supabase_admin()
    sb.table("email_sender_whitelist").delete().eq(
        "id", entry_id
    ).eq(
        "organisation_id", current_user["organisation_id"]
    ).execute()
    return {"status": "ok", "message": "Whitelist entry removed"}


# ---------------------------------------------------------------------------
# Nylas OAuth flow
# ---------------------------------------------------------------------------

@router.get("/nylas/connect")
async def nylas_connect(
    provider: str = Query(..., regex="^(google|microsoft)$"),
    current_user: dict = Depends(get_current_user),
):
    """Start the Nylas OAuth flow. Redirects to Nylas hosted auth page."""
    from fastapi.responses import RedirectResponse

    org_id = current_user["organisation_id"]
    user_id = current_user["id"]
    callback_uri = settings.NYLAS_CALLBACK_URI or f"{settings.APP_URL.rstrip('/')}/api/email/nylas/callback"
    auth_url = get_nylas_auth_url(provider, org_id, callback_uri, user_id=user_id)
    return RedirectResponse(url=auth_url, status_code=302)


@router.get("/nylas/callback", include_in_schema=False)
async def nylas_callback(
    request: Request,
    code: str = Query(""),
    state: str = Query(""),
):
    """Handle Nylas OAuth callback after user authorizes.

    The state parameter contains the organisation ID.
    """
    from fastapi.responses import RedirectResponse
    import urllib.parse

    # Log full callback URL and all query parameters
    logger.error(
        "Nylas callback received -- URL: %s, Query params: %s",
        str(request.url),
        dict(request.query_params),
    )

    # Check for error parameter from Nylas
    nylas_error = request.query_params.get("error", "")
    nylas_error_description = request.query_params.get("error_description", "")
    if nylas_error:
        logger.error(
            "Nylas OAuth error: %s, description: %s",
            nylas_error,
            nylas_error_description,
        )

    if not code:
        error_msg = nylas_error_description or nylas_error or "no_code"
        logger.error("Nylas callback received without code, redirecting with error: %s", error_msg)
        return RedirectResponse(
            url="/api/email/settings-page?error=" + urllib.parse.quote(error_msg),
            status_code=302,
        )

    # Parse state: "org_id:user_id" or legacy "org_id"
    state_parts = state.split(":", 1) if state else []
    org_id = state_parts[0] if state_parts else ""
    user_id = state_parts[1] if len(state_parts) > 1 else ""

    if not org_id:
        logger.warning("Nylas callback received without state (org_id)")
        return RedirectResponse(url="/api/email/settings-page?error=no_state", status_code=302)

    try:
        grant_data = await exchange_code_for_grant(code)
    except Exception as e:
        logger.error("Nylas token exchange failed: %s", str(e))
        return RedirectResponse(url="/api/email/settings-page?error=token_exchange", status_code=302)

    grant_id = grant_data.get("grant_id", "")
    email_address = grant_data.get("email", "")
    provider = grant_data.get("provider", "unknown")

    if not grant_id:
        logger.error("Nylas grant response missing grant_id: %s", grant_data)
        return RedirectResponse(url="/api/email/settings-page?error=no_grant", status_code=302)

    sb = get_supabase_admin()

    # Store grant on the user record (per-user email connection)
    if user_id:
        sb.table("users").update({
            "nylas_grant_id": grant_id,
            "nylas_email": email_address,
            "email_connected": True,
            "email_connected_at": datetime.utcnow().isoformat(),
        }).eq("id", user_id).execute()

    # Also maintain org-level email_connections for backward compatibility
    existing = (
        sb.table("email_connections")
        .select("id")
        .eq("organisation_id", org_id)
        .eq("email_address", email_address)
        .limit(1)
        .execute()
    )

    if existing.data:
        sb.table("email_connections").update({
            "grant_id": grant_id,
            "provider": provider,
            "status": "active",
            "error_message": None,
            "updated_at": datetime.utcnow().isoformat(),
        }).eq("id", existing.data[0]["id"]).execute()
    else:
        sb.table("email_connections").insert({
            "id": str(uuid.uuid4()),
            "organisation_id": org_id,
            "provider": provider,
            "email_address": email_address,
            "grant_id": grant_id,
            "status": "active",
        }).execute()

    # Audit log
    from app.audit import log_action
    await log_action(org_id, user_id or None, "email.connected", "user", user_id or None, {
        "email": email_address, "provider": provider,
    })

    return RedirectResponse(url="/api/email/settings-page?connected=1", status_code=302)


# ---------------------------------------------------------------------------
# Nylas webhook receiver
# ---------------------------------------------------------------------------

@router.get("/nylas/webhook")
async def nylas_webhook_challenge(challenge: Optional[str] = Query(None)):
    """Respond to Nylas webhook challenge for verification."""
    if challenge is None:
        return {"status": "ok"}
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(content=challenge)


@router.post("/nylas/webhook")
async def nylas_webhook_receiver(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Receive Nylas webhook notifications for new messages.

    Always returns 200 to acknowledge receipt.
    """
    raw_body = await request.body()

    # Verify signature
    signature = request.headers.get("x-nylas-signature", "")
    if settings.NYLAS_WEBHOOK_SECRET and not verify_webhook_signature(raw_body, signature):
        logger.warning("Invalid Nylas webhook signature")
        return {"status": "ok"}

    try:
        payload = json.loads(raw_body)
    except Exception:
        return {"status": "ok"}

    # Handle message.created events
    webhook_type = payload.get("type", "")
    if webhook_type != "message.created":
        return {"status": "ok"}

    data = payload.get("data", {})
    if not data:
        return {"status": "ok"}

    grant_id = data.get("grant_id", "") or payload.get("data", {}).get("object", {}).get("grant_id", "")
    message_data = data.get("object", data)
    message_id = message_data.get("id", "")

    if not grant_id or not message_id:
        logger.warning("Nylas webhook missing grant_id or message_id")
        return {"status": "ok"}

    sb = get_supabase_admin()

    # Look up the connection by grant_id
    try:
        conn_result = (
            sb.table("email_connections")
            .select("id, organisation_id, status")
            .eq("grant_id", grant_id)
            .limit(1)
            .execute()
        )
    except Exception as e:
        logger.error("Failed to look up connection for grant %s: %s", grant_id, str(e))
        return {"status": "ok"}

    if not conn_result.data:
        logger.warning("No connection found for grant_id %s", grant_id)
        return {"status": "ok"}

    connection = conn_result.data[0]

    # Skip if connection is paused or disconnected
    if connection["status"] != "active":
        return {"status": "ok"}

    org_id = connection["organisation_id"]
    connection_id = connection["id"]

    # Look up which user owns this grant (per-user email)
    user_id = None
    try:
        user_result = (
            sb.table("users")
            .select("id")
            .eq("nylas_grant_id", grant_id)
            .limit(1)
            .execute()
        )
        if user_result.data:
            user_id = user_result.data[0]["id"]
    except Exception:
        pass

    # Process in background
    background_tasks.add_task(
        process_nylas_message,
        grant_id=grant_id,
        message_id=message_id,
        org_id=org_id,
        connection_id=connection_id,
        user_id=user_id,
    )

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

@router.get("/connections")
async def list_connections(current_user: dict = Depends(get_current_user)):
    """List all email connections for the user's organisation."""
    sb = get_supabase_admin()
    result = (
        sb.table("email_connections")
        .select("*")
        .eq("organisation_id", current_user["organisation_id"])
        .order("created_at")
        .execute()
    )
    return {"connections": result.data}


@router.post("/connections/{connection_id}/pause")
async def pause_connection(
    connection_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Pause an email connection (stop processing new messages)."""
    sb = get_supabase_admin()
    result = (
        sb.table("email_connections")
        .select("id")
        .eq("id", connection_id)
        .eq("organisation_id", current_user["organisation_id"])
        .limit(1)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Connection not found")

    sb.table("email_connections").update({
        "status": "paused",
        "updated_at": datetime.utcnow().isoformat(),
    }).eq("id", connection_id).execute()
    return {"status": "ok", "message": "Connection paused"}


@router.post("/connections/{connection_id}/resume")
async def resume_connection(
    connection_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Resume a paused email connection."""
    sb = get_supabase_admin()
    result = (
        sb.table("email_connections")
        .select("id")
        .eq("id", connection_id)
        .eq("organisation_id", current_user["organisation_id"])
        .limit(1)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Connection not found")

    sb.table("email_connections").update({
        "status": "active",
        "error_message": None,
        "updated_at": datetime.utcnow().isoformat(),
    }).eq("id", connection_id).execute()
    return {"status": "ok", "message": "Connection resumed"}


@router.delete("/connections/{connection_id}")
async def disconnect_connection(
    connection_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Disconnect and remove an email connection."""
    sb = get_supabase_admin()
    result = (
        sb.table("email_connections")
        .select("id")
        .eq("id", connection_id)
        .eq("organisation_id", current_user["organisation_id"])
        .limit(1)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Connection not found")

    sb.table("email_connections").delete().eq("id", connection_id).execute()
    return {"status": "ok", "message": "Connection removed"}


# ---------------------------------------------------------------------------
# Smart attachment filters
# ---------------------------------------------------------------------------

@router.get("/filters")
async def list_filters(current_user: dict = Depends(get_current_user)):
    """List attachment filter rules for the user's organisation."""
    sb = get_supabase_admin()
    result = (
        sb.table("attachment_filters")
        .select("*")
        .eq("organisation_id", current_user["organisation_id"])
        .order("created_at")
        .execute()
    )
    return {"filters": result.data}


@router.post("/filters")
async def add_filter(
    filter_type: str = Form(...),
    filter_value: str = Form(...),
    current_user: dict = Depends(get_current_user),
):
    """Add a new attachment filter rule."""
    valid_types = {"skip_content_type", "skip_filename_pattern", "skip_size_under", "skip_size_over"}
    if filter_type not in valid_types:
        raise HTTPException(status_code=400, detail=f"Invalid filter_type. Must be one of: {', '.join(valid_types)}")

    sb = get_supabase_admin()
    sb.table("attachment_filters").insert({
        "id": str(uuid.uuid4()),
        "organisation_id": current_user["organisation_id"],
        "filter_type": filter_type,
        "filter_value": filter_value.strip(),
    }).execute()
    return {"status": "ok", "message": "Filter added"}


# ---------------------------------------------------------------------------
# Daily summary
# ---------------------------------------------------------------------------

@router.get("/daily-summary")
async def get_daily_summary(current_user: dict = Depends(get_current_user)):
    """Get today's morning email summary and to-do list (per-user)."""
    from app.email.daily_summary import generate_daily_summary, save_daily_summary

    org_id = current_user["organisation_id"]
    user_id = current_user["id"]
    sb = get_supabase_admin()
    today = datetime.utcnow().strftime("%Y-%m-%d")

    # Return cached summary for THIS USER today
    existing = (
        sb.table("daily_summaries")
        .select("summary_data")
        .eq("user_id", user_id)
        .eq("summary_date", today)
        .limit(1)
        .execute()
    )

    if existing.data:
        return existing.data[0]["summary_data"]

    # Generate fresh summary scoped to this user
    summary = await generate_daily_summary(org_id, user_id=user_id)
    await save_daily_summary(org_id, user_id, summary)

    from app.audit import log_action
    await log_action(org_id, user_id, "summary.generated", "summary", None, {
        "email_count": summary.get("email_count", 0),
        "action_items": summary.get("stats", {}).get("action_items_found", 0),
    })

    return summary


@router.post("/daily-summary/refresh")
async def refresh_daily_summary(current_user: dict = Depends(get_current_user)):
    """Force regenerate today's summary (per-user)."""
    from app.email.daily_summary import generate_daily_summary, save_daily_summary

    org_id = current_user["organisation_id"]
    user_id = current_user["id"]
    sb = get_supabase_admin()
    today = datetime.utcnow().strftime("%Y-%m-%d")

    # Delete existing for this user
    sb.table("daily_summaries").delete().eq(
        "user_id", user_id
    ).eq("summary_date", today).execute()

    # Also clean up previous todos from today's summary
    sb.table("user_todos").delete().eq(
        "user_id", user_id
    ).eq("completed", False).execute()

    # Regenerate
    summary = await generate_daily_summary(org_id, user_id=user_id)
    await save_daily_summary(org_id, user_id, summary)

    from app.audit import log_action
    await log_action(org_id, user_id, "summary.refreshed", "summary", None, {
        "email_count": summary.get("email_count", 0),
    })

    return summary


@router.get("/connection-status")
async def email_connection_status(current_user: dict = Depends(get_current_user)):
    """Check if the current user has connected their email."""
    return {
        "connected": current_user.get("email_connected", False),
        "email": current_user.get("nylas_email"),
        "connected_at": current_user.get("email_connected_at"),
    }


@router.delete("/filters/{filter_id}")
async def remove_filter(
    filter_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Remove an attachment filter rule."""
    sb = get_supabase_admin()
    sb.table("attachment_filters").delete().eq(
        "id", filter_id
    ).eq(
        "organisation_id", current_user["organisation_id"]
    ).execute()
    return {"status": "ok", "message": "Filter removed"}


@router.post("/filters/{filter_id}/toggle")
async def toggle_filter(
    filter_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Toggle a filter rule on/off."""
    sb = get_supabase_admin()
    result = (
        sb.table("attachment_filters")
        .select("id, is_active")
        .eq("id", filter_id)
        .eq("organisation_id", current_user["organisation_id"])
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Filter not found")

    new_state = not result.data["is_active"]
    sb.table("attachment_filters").update({
        "is_active": new_state,
    }).eq("id", filter_id).execute()
    return {"status": "ok", "is_active": new_state}


# ---------------------------------------------------------------------------
# To-do management (per-user, backed by user_todos table)
# ---------------------------------------------------------------------------

@router.get("/todos")
async def get_todos(
    completed: bool = Query(False),
    current_user: dict = Depends(get_current_user),
):
    """Get current user's to-do items."""
    sb = get_supabase_admin()
    result = (
        sb.table("user_todos")
        .select("*")
        .eq("user_id", current_user["id"])
        .eq("completed", completed)
        .order("created_at", desc=True)
        .execute()
    )
    return {"todos": result.data or []}


@router.post("/todos")
async def create_todo(
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Manually create a to-do item."""
    body = await request.json()
    sb = get_supabase_admin()
    user_id = current_user["id"]
    org_id = current_user["organisation_id"]

    todo_id = str(uuid.uuid4())
    sb.table("user_todos").insert({
        "id": todo_id,
        "user_id": user_id,
        "organisation_id": org_id,
        "task": body["task"],
        "priority": body.get("priority", "medium"),
        "due_hint": body.get("due_hint"),
    }).execute()

    from app.audit import log_action
    await log_action(org_id, user_id, "todo.created", "todo", todo_id, {
        "task": body["task"][:200],
    })

    return {"id": todo_id, "status": "ok"}


@router.patch("/todos/{todo_id}")
async def update_todo(
    todo_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Mark a to-do as complete/incomplete."""
    body = await request.json()
    sb = get_supabase_admin()
    user_id = current_user["id"]

    update_data = {"completed": body.get("completed", False)}
    if update_data["completed"]:
        update_data["completed_at"] = datetime.utcnow().isoformat()
    else:
        update_data["completed_at"] = None

    sb.table("user_todos").update(update_data).eq(
        "id", todo_id
    ).eq("user_id", user_id).execute()

    if update_data["completed"]:
        from app.audit import log_action
        await log_action(
            current_user["organisation_id"], user_id,
            "todo.completed", "todo", todo_id,
        )

    return {"status": "ok"}


@router.delete("/todos/{todo_id}")
async def delete_todo(
    todo_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Delete a to-do item."""
    sb = get_supabase_admin()
    sb.table("user_todos").delete().eq(
        "id", todo_id
    ).eq("user_id", current_user["id"]).execute()
    return {"status": "ok"}
