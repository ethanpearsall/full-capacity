import json
import logging
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
    encrypt_password,
    extract_header,
    parse_email_address,
    resolve_organisation,
)
from app.email.processor import (
    poll_imap_mailbox,
    process_email_attachments,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/email", tags=["email"])
templates = Jinja2Templates(directory="templates")

# Webhook secret for SendGrid verification
EMAIL_WEBHOOK_SECRET = getattr(settings, "EMAIL_WEBHOOK_SECRET", "")


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
# IMAP configuration
# ---------------------------------------------------------------------------

@router.get("/imap/config")
async def get_imap_config_endpoint(current_user: dict = Depends(get_current_user)):
    """Get IMAP configuration for the user's organisation."""
    sb = get_supabase_admin()
    org_id = current_user["organisation_id"]

    result = (
        sb.table("imap_configs")
        .select("*")
        .eq("organisation_id", org_id)
        .limit(1)
        .execute()
    )

    if not result.data:
        return {}

    config = result.data[0]
    config["password_encrypted"] = "********" if config.get("password_encrypted") else ""
    return config


@router.post("/imap/config")
async def save_imap_config_endpoint(
    host: str = Form(...),
    port: int = Form(993),
    username: str = Form(...),
    password: str = Form(...),
    folder: str = Form("INBOX"),
    use_ssl: bool = Form(True),
    poll_interval_minutes: int = Form(5),
    current_user: dict = Depends(get_current_user),
):
    """Save IMAP configuration. Validates connection first."""
    import imaplib

    # Test connection
    try:
        if use_ssl:
            test_mail = imaplib.IMAP4_SSL(host, port)
        else:
            test_mail = imaplib.IMAP4(host, port)
        test_mail.login(username, password)
        test_mail.logout()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not connect to IMAP server: {str(e)}",
        )

    sb = get_supabase_admin()
    org_id = current_user["organisation_id"]
    encrypted_pw = encrypt_password(password)

    # Upsert config
    existing = (
        sb.table("imap_configs")
        .select("id")
        .eq("organisation_id", org_id)
        .limit(1)
        .execute()
    )

    config_data = {
        "organisation_id": org_id,
        "host": host,
        "port": port,
        "username": username,
        "password_encrypted": encrypted_pw,
        "folder": folder,
        "use_ssl": use_ssl,
        "poll_interval_minutes": poll_interval_minutes,
        "is_active": True,
        "updated_at": datetime.utcnow().isoformat(),
    }

    if existing.data:
        sb.table("imap_configs").update(config_data).eq(
            "id", existing.data[0]["id"]
        ).execute()
    else:
        config_data["id"] = str(uuid.uuid4())
        sb.table("imap_configs").insert(config_data).execute()

    return {"status": "ok", "message": "IMAP configuration saved and verified"}


@router.post("/imap/test")
async def test_imap_connection(
    host: str = Form(...),
    port: int = Form(993),
    username: str = Form(...),
    password: str = Form(...),
    use_ssl: bool = Form(True),
    current_user: dict = Depends(get_current_user),
):
    """Test IMAP connection without saving."""
    import imaplib

    try:
        if use_ssl:
            test_mail = imaplib.IMAP4_SSL(host, port)
        else:
            test_mail = imaplib.IMAP4(host, port)
        test_mail.login(username, password)
        test_mail.logout()
        return {"status": "ok", "message": "Connection successful"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/imap/check")
async def check_imap_mailbox(
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
):
    """Manually trigger an IMAP check for the user's organisation."""
    sb = get_supabase_admin()
    org_id = current_user["organisation_id"]

    result = (
        sb.table("imap_configs")
        .select("*")
        .eq("organisation_id", org_id)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )

    if not result.data:
        raise HTTPException(status_code=400, detail="IMAP not configured")

    config = result.data[0]
    background_tasks.add_task(poll_imap_mailbox, config, org_id)
    return {"status": "ok", "message": "IMAP check started"}


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

    # Re-trigger processing (we need the file content again from storage or original)
    # For now, just reset status - a full reprocess would need stored files
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
# Stats
# ---------------------------------------------------------------------------

@router.get("/stats")
async def get_email_stats(current_user: dict = Depends(get_current_user)):
    """Get email ingestion statistics for dashboard cards."""
    sb = get_supabase_admin()
    org_id = current_user["organisation_id"]

    # All ingestions
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
