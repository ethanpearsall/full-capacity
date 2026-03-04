import hashlib
import imaplib
import logging
import uuid
from datetime import datetime
from email import message_from_bytes
from typing import Optional

from app.database import get_supabase_admin
from app.storage import upload_file
from app.documents.processor import extract_text
from app.documents.classifier import classify_document
from app.documents.filer import generate_filed_path
from app.storage import move_file

from app.email.helpers import (
    decode_email_header,
    decrypt_password,
    get_file_extension,
    parse_email_address,
    check_duplicate_email,
)

logger = logging.getLogger(__name__)

# File types the document pipeline can process
PROCESSABLE_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/tiff",
    "image/bmp",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
}

PROCESSABLE_EXTENSIONS = {
    ".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".bmp",
    ".doc", ".docx", ".xls", ".xlsx",
}

MAX_ATTACHMENT_SIZE = 25 * 1024 * 1024  # 25 MB


async def process_email_attachments(
    ingestion_id: str,
    org_id: str,
    attachments: list,
    email_from: str,
    email_subject: str,
) -> None:
    """Process each attachment through the existing document pipeline.

    Runs in the background after the webhook/IMAP handler returns.
    """
    sb = get_supabase_admin()

    # Mark ingestion as processing
    sb.table("email_ingestions").update(
        {"status": "processing"}
    ).eq("id", ingestion_id).execute()

    # Resolve org name for filing
    org_result = sb.table("organisations").select("name").eq("id", org_id).single().execute()
    org_name = org_result.data.get("name", "Default") if org_result.data else "Default"

    processed = 0
    failed = 0

    for attachment in attachments:
        filename = attachment["filename"]
        content_type = attachment["content_type"] or ""
        content = attachment["content"]
        file_size = attachment["size"]

        # Create email_attachment record
        att_id = str(uuid.uuid4())
        sb.table("email_attachments").insert({
            "id": att_id,
            "email_ingestion_id": ingestion_id,
            "original_filename": filename,
            "content_type": content_type,
            "file_size_bytes": file_size,
            "processing_status": "pending",
        }).execute()

        # Size check
        if file_size > MAX_ATTACHMENT_SIZE:
            sb.table("email_attachments").update({
                "processing_status": "skipped",
                "skip_reason": f"File too large ({file_size} bytes, max {MAX_ATTACHMENT_SIZE})",
            }).eq("id", att_id).execute()
            continue

        # Type check
        ext = get_file_extension(filename).lower()
        if content_type not in PROCESSABLE_TYPES and ext not in PROCESSABLE_EXTENSIONS:
            sb.table("email_attachments").update({
                "processing_status": "skipped",
                "skip_reason": f"Unsupported file type: {content_type or ext}",
            }).eq("id", att_id).execute()
            continue

        try:
            sb.table("email_attachments").update(
                {"processing_status": "processing"}
            ).eq("id", att_id).execute()

            # Determine mime type for pipeline
            mime_type = content_type
            if not mime_type or mime_type == "application/octet-stream":
                mime_type = _guess_mime_from_ext(ext)

            doc_id = str(uuid.uuid4())
            content_hash = hashlib.sha256(content).hexdigest()
            safe_ext = ext.lstrip(".") if ext else "pdf"
            original_storage_path = f"originals/{org_id}/{doc_id}.{safe_ext}"

            # Upload to Supabase Storage
            upload_file(original_storage_path, content, mime_type)

            # Create document record with email metadata
            sb.table("documents").insert({
                "id": doc_id,
                "organisation_id": org_id,
                "uploaded_by": None,
                "original_filename": filename,
                "file_size_bytes": file_size,
                "mime_type": mime_type,
                "original_storage_path": original_storage_path,
                "content_hash": content_hash,
                "status": "processing",
                "email_ingestion_id": ingestion_id,
                "email_from": email_from,
                "email_subject": email_subject,
            }).execute()

            # Run the document processing pipeline (same as upload)
            _run_email_processing_pipeline(
                sb, doc_id, org_id, org_name,
                content, mime_type, filename, original_storage_path,
            )

            # Link attachment to document
            sb.table("email_attachments").update({
                "processing_status": "completed",
                "document_id": doc_id,
            }).eq("id", att_id).execute()

            processed += 1

        except Exception as e:
            logger.error("Failed to process email attachment %s: %s", filename, str(e))
            sb.table("email_attachments").update({
                "processing_status": "failed",
                "skip_reason": str(e)[:500],
            }).eq("id", att_id).execute()
            failed += 1

    # Update ingestion status
    if failed == 0:
        final_status = "completed"
    elif processed > 0:
        final_status = "partial"
    else:
        final_status = "failed"

    update_data = {
        "status": final_status,
        "processed_count": processed,
    }
    if failed > 0:
        update_data["error_message"] = f"{failed} attachment(s) failed"

    sb.table("email_ingestions").update(update_data).eq("id", ingestion_id).execute()


def _run_email_processing_pipeline(
    sb, doc_id: str, org_id: str, org_name: str,
    file_bytes: bytes, mime_type: str, original_filename: str,
    original_storage_path: str,
) -> None:
    """Run the same extract/classify/file pipeline used by document upload."""
    # Step 1: Extract text
    try:
        extracted_text = extract_text(file_bytes, mime_type)
        sb.table("documents").update(
            {"extracted_text": extracted_text}
        ).eq("id", doc_id).execute()
    except Exception as e:
        logger.error("Text extraction failed for email doc %s: %s", doc_id, str(e))
        sb.table("documents").update({
            "status": "error",
            "error_message": f"Text extraction failed: {str(e)}",
        }).eq("id", doc_id).execute()
        raise

    # Step 2: Classify with AI
    try:
        classification = classify_document(extracted_text, original_filename)
        update_data = {
            "document_type": classification.document_type,
            "confidence_score": classification.confidence,
            "client_name": classification.client_name,
            "counterparty": classification.counterparty,
            "document_date": str(classification.document_date) if classification.document_date else None,
            "matter_reference": classification.matter_reference,
            "amount": classification.amount,
            "currency": classification.currency,
            "summary": classification.summary,
            "tags": classification.tags,
            "status": "classified",
            "processed_at": datetime.utcnow().isoformat(),
        }
        sb.table("documents").update(update_data).eq("id", doc_id).execute()
    except Exception as e:
        logger.error("Classification failed for email doc %s: %s", doc_id, str(e))
        sb.table("documents").update({
            "status": "error",
            "error_message": f"Classification failed: {str(e)}",
        }).eq("id", doc_id).execute()
        raise

    # Step 3: File the document
    try:
        folder_path, filed_name = generate_filed_path(
            classification, org_name, original_filename
        )
        filed_storage_path = f"filed{folder_path}/{filed_name}"
        move_file(original_storage_path, filed_storage_path)

        sb.table("documents").update({
            "filed_storage_path": filed_storage_path,
            "filed_name": filed_name,
            "folder_path": folder_path,
            "status": "filed",
            "filed_at": datetime.utcnow().isoformat(),
        }).eq("id", doc_id).execute()
    except Exception as e:
        logger.error("Filing failed for email doc %s: %s", doc_id, str(e))
        sb.table("documents").update({
            "status": "classified",
            "error_message": f"Filing failed: {str(e)}",
        }).eq("id", doc_id).execute()


def _guess_mime_from_ext(ext: str) -> str:
    """Guess MIME type from file extension."""
    ext_map = {
        ".pdf": "application/pdf",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".tiff": "image/tiff",
        ".bmp": "image/bmp",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xls": "application/vnd.ms-excel",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    return ext_map.get(ext.lower(), "application/octet-stream")


async def poll_imap_mailbox(config: dict, org_id: str) -> None:
    """Connect to IMAP server, fetch unread emails, process attachments."""
    host = config["host"]
    port = config.get("port", 993)
    username = config["username"]
    password = decrypt_password(config["password_encrypted"])
    folder = config.get("folder", "INBOX")
    use_ssl = config.get("use_ssl", True)

    mail = None
    try:
        if use_ssl:
            mail = imaplib.IMAP4_SSL(host, port)
        else:
            mail = imaplib.IMAP4(host, port)

        mail.login(username, password)
        mail.select(folder)

        status, messages = mail.search(None, "UNSEEN")
        if status != "OK":
            return

        email_ids = messages[0].split() if messages[0] else []

        for email_id in email_ids:
            try:
                await _process_imap_email(mail, email_id, org_id)
            except Exception as e:
                logger.error("Error processing IMAP email %s: %s", email_id, str(e))
                # Mark as seen so we don't retry endlessly
                mail.store(email_id, "+FLAGS", "\\Seen")

        mail.close()
        mail.logout()

    except Exception as e:
        logger.error("IMAP polling failed for org %s: %s", org_id, str(e))
        if mail:
            try:
                mail.logout()
            except Exception:
                pass


async def _process_imap_email(
    mail: imaplib.IMAP4_SSL, email_id: bytes, org_id: str
) -> None:
    """Process a single IMAP email."""
    sb = get_supabase_admin()

    status, msg_data = mail.fetch(email_id, "(RFC822)")
    if status != "OK":
        return

    raw_email = msg_data[0][1]
    msg = message_from_bytes(raw_email)

    subject = decode_email_header(msg.get("Subject", ""))
    from_addr = decode_email_header(msg.get("From", ""))
    to_addr = decode_email_header(msg.get("To", ""))
    message_id = msg.get("Message-ID", "")

    sender_name, sender_email = parse_email_address(from_addr)

    # Duplicate check
    if message_id:
        is_dup = await check_duplicate_email(message_id, org_id)
        if is_dup:
            mail.store(email_id, "+FLAGS", "\\Seen")
            return

    # Extract body preview
    body_text = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in disp:
                payload = part.get_payload(decode=True)
                if payload:
                    body_text = payload.decode(errors="replace")
                break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body_text = payload.decode(errors="replace")

    # Collect attachments
    attachments = []
    for part in msg.walk():
        content_disposition = str(part.get("Content-Disposition", ""))
        if "attachment" in content_disposition:
            filename = part.get_filename()
            if filename:
                filename = decode_email_header(filename)
                content = part.get_payload(decode=True)
                content_type = part.get_content_type()
                if content:
                    attachments.append({
                        "filename": filename,
                        "content_type": content_type,
                        "content": content,
                        "size": len(content),
                    })

    if not attachments:
        mail.store(email_id, "+FLAGS", "\\Seen")
        return

    # Create ingestion record
    ingestion_id = str(uuid.uuid4())
    sb.table("email_ingestions").insert({
        "id": ingestion_id,
        "organisation_id": org_id,
        "message_id": message_id,
        "from_address": sender_email,
        "from_name": sender_name,
        "to_address": to_addr,
        "subject": subject,
        "body_preview": body_text[:500] if body_text else "",
        "source": "imap",
        "attachment_count": len(attachments),
        "raw_headers": {"from": from_addr, "to": to_addr, "subject": subject},
    }).execute()

    # Process attachments
    await process_email_attachments(
        ingestion_id=ingestion_id,
        org_id=org_id,
        attachments=attachments,
        email_from=sender_email,
        email_subject=subject,
    )

    mail.store(email_id, "+FLAGS", "\\Seen")
