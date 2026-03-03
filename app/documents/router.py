import logging
import uuid
from datetime import datetime, date
from typing import Optional

from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Query

from app.auth.dependencies import get_current_user
from app.database import get_supabase_admin
from app.storage import upload_file, download_file, move_file
from app.documents.processor import extract_text
from app.documents.classifier import classify_document
from app.documents.filer import generate_filed_path
from app.documents.models import DocumentResponse, DocumentStats

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/documents", tags=["documents"])

ALLOWED_MIME_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/tiff",
    "image/bmp",
    "image/gif",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
}


@router.post("/upload")
async def upload_documents(
    files: list[UploadFile] = File(...),
    user: dict = Depends(get_current_user),
):
    """Upload one or more documents for processing."""
    results = []
    org_id = user["organisation_id"]
    user_id = user["id"]
    org_name = user.get("organisations", {}).get("name", "Default") if isinstance(user.get("organisations"), dict) else "Default"

    for file in files:
        try:
            result = await _process_single_file(file, org_id, user_id, org_name)
            results.append(result)
        except Exception as e:
            logger.error("Error processing file %s: %s", file.filename, str(e))
            results.append({
                "filename": file.filename,
                "status": "error",
                "error": str(e),
            })

    return {"uploaded": len(results), "results": results}


async def _process_single_file(
    file: UploadFile, org_id: str, user_id: str, org_name: str
) -> dict:
    """Process a single uploaded file through the full pipeline."""
    sb = get_supabase_admin()

    # Validate mime type
    mime_type = file.content_type or "application/octet-stream"
    if mime_type not in ALLOWED_MIME_TYPES:
        raise ValueError(f"Unsupported file type: {mime_type}")

    # Read file bytes
    file_bytes = await file.read()
    file_size = len(file_bytes)
    original_filename = file.filename or "unnamed_file"

    # Generate a unique storage path for the original
    doc_id = str(uuid.uuid4())
    ext = original_filename.rsplit(".", 1)[-1] if "." in original_filename else "pdf"
    original_storage_path = f"originals/{org_id}/{doc_id}.{ext}"

    # Upload original to storage
    upload_file(original_storage_path, file_bytes, mime_type)

    # Create document record with 'processing' status
    doc_record = {
        "id": doc_id,
        "organisation_id": org_id,
        "uploaded_by": user_id,
        "original_filename": original_filename,
        "file_size_bytes": file_size,
        "mime_type": mime_type,
        "original_storage_path": original_storage_path,
        "status": "processing",
    }
    sb.table("documents").insert(doc_record).execute()

    # Log activity
    _log_activity(sb, org_id, user_id, doc_id, "uploaded", {"filename": original_filename})

    # Step 1: Extract text
    try:
        extracted_text = extract_text(file_bytes, mime_type)
        sb.table("documents").update(
            {"extracted_text": extracted_text}
        ).eq("id", doc_id).execute()
    except Exception as e:
        logger.error("Text extraction failed for %s: %s", doc_id, str(e))
        sb.table("documents").update(
            {"status": "error", "error_message": f"Text extraction failed: {str(e)}"}
        ).eq("id", doc_id).execute()
        _log_activity(sb, org_id, user_id, doc_id, "error", {"error": str(e)})
        return {"id": doc_id, "filename": original_filename, "status": "error", "error": str(e)}

    # Step 2: Classify with AI
    try:
        classification = classify_document(extracted_text, original_filename)
        update_data = {
            "document_type": classification.document_type,
            "confidence_score": classification.confidence,
            "client_name": classification.client_name,
            "counterparty": classification.counterparty,
            "document_date": classification.document_date.isoformat() if classification.document_date else None,
            "matter_reference": classification.matter_reference,
            "amount": classification.amount,
            "currency": classification.currency,
            "summary": classification.summary,
            "tags": classification.tags,
            "status": "classified",
            "processed_at": datetime.utcnow().isoformat(),
        }
        sb.table("documents").update(update_data).eq("id", doc_id).execute()
        _log_activity(sb, org_id, user_id, doc_id, "classified", {
            "document_type": classification.document_type,
            "confidence": classification.confidence,
        })
    except Exception as e:
        logger.error("Classification failed for %s: %s", doc_id, str(e))
        sb.table("documents").update(
            {"status": "error", "error_message": f"Classification failed: {str(e)}"}
        ).eq("id", doc_id).execute()
        _log_activity(sb, org_id, user_id, doc_id, "error", {"error": str(e)})
        return {"id": doc_id, "filename": original_filename, "status": "error", "error": str(e)}

    # Step 3: File the document
    try:
        folder_path, filed_name = generate_filed_path(
            classification, org_name, original_filename
        )
        filed_storage_path = f"filed{folder_path}/{filed_name}"

        # Move file in storage
        move_file(original_storage_path, filed_storage_path)

        sb.table("documents").update({
            "filed_storage_path": filed_storage_path,
            "filed_name": filed_name,
            "folder_path": folder_path,
            "status": "filed",
            "filed_at": datetime.utcnow().isoformat(),
        }).eq("id", doc_id).execute()

        _log_activity(sb, org_id, user_id, doc_id, "filed", {
            "folder_path": folder_path,
            "filed_name": filed_name,
        })
    except Exception as e:
        logger.error("Filing failed for %s: %s", doc_id, str(e))
        # Document is classified but filing failed — not critical
        sb.table("documents").update({
            "status": "classified",
            "error_message": f"Filing failed: {str(e)}",
        }).eq("id", doc_id).execute()

    return {
        "id": doc_id,
        "filename": original_filename,
        "status": "filed",
        "document_type": classification.document_type,
        "client_name": classification.client_name,
        "folder_path": folder_path,
        "filed_name": filed_name,
    }


@router.get("")
async def list_documents(
    user: dict = Depends(get_current_user),
    document_type: Optional[str] = Query(None),
    client_name: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List all documents for the user's organisation with optional filters."""
    sb = get_supabase_admin()
    query = (
        sb.table("documents")
        .select("*")
        .eq("organisation_id", user["organisation_id"])
        .order("uploaded_at", desc=True)
        .range(offset, offset + limit - 1)
    )

    if document_type:
        query = query.eq("document_type", document_type)
    if client_name:
        query = query.ilike("client_name", f"%{client_name}%")
    if status:
        query = query.eq("status", status)
    if date_from:
        query = query.gte("document_date", date_from)
    if date_to:
        query = query.lte("document_date", date_to)

    result = query.execute()
    return {"documents": result.data, "count": len(result.data)}


@router.get("/search")
async def search_documents(
    q: str = Query(..., min_length=1),
    user: dict = Depends(get_current_user),
    limit: int = Query(50, ge=1, le=200),
):
    """Full-text search across all documents."""
    sb = get_supabase_admin()
    org_id = user["organisation_id"]

    # Use PostgreSQL full-text search
    result = (
        sb.table("documents")
        .select("*")
        .eq("organisation_id", org_id)
        .text_search("fts", q)
        .order("uploaded_at", desc=True)
        .limit(limit)
        .execute()
    )

    _log_activity(sb, org_id, user["id"], None, "searched", {"query": q})
    return {"documents": result.data, "count": len(result.data), "query": q}


@router.get("/stats")
async def document_stats(user: dict = Depends(get_current_user)):
    """Get dashboard statistics."""
    sb = get_supabase_admin()
    org_id = user["organisation_id"]

    # Total documents
    all_docs = (
        sb.table("documents")
        .select("id, document_type, status, uploaded_at")
        .eq("organisation_id", org_id)
        .execute()
    )

    total = len(all_docs.data)
    today = date.today().isoformat()
    processed_today = sum(
        1 for d in all_docs.data
        if d.get("uploaded_at", "").startswith(today)
    )

    by_type: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for doc in all_docs.data:
        doc_type = doc.get("document_type") or "unknown"
        by_type[doc_type] = by_type.get(doc_type, 0) + 1
        doc_status = doc.get("status") or "unknown"
        by_status[doc_status] = by_status.get(doc_status, 0) + 1

    return {
        "total_documents": total,
        "processed_today": processed_today,
        "by_type": by_type,
        "by_status": by_status,
    }


@router.get("/{doc_id}")
async def get_document(doc_id: str, user: dict = Depends(get_current_user)):
    """Get a single document's details."""
    sb = get_supabase_admin()
    result = (
        sb.table("documents")
        .select("*")
        .eq("id", doc_id)
        .eq("organisation_id", user["organisation_id"])
        .single()
        .execute()
    )

    if not result.data:
        raise HTTPException(status_code=404, detail="Document not found")

    return result.data


@router.get("/{doc_id}/download")
async def download_document(doc_id: str, user: dict = Depends(get_current_user)):
    """Download the original document file."""
    from fastapi.responses import StreamingResponse
    import io

    sb = get_supabase_admin()
    result = (
        sb.table("documents")
        .select("original_storage_path, filed_storage_path, original_filename, mime_type")
        .eq("id", doc_id)
        .eq("organisation_id", user["organisation_id"])
        .single()
        .execute()
    )

    if not result.data:
        raise HTTPException(status_code=404, detail="Document not found")

    # Try filed path first, fall back to original
    storage_path = result.data.get("filed_storage_path") or result.data["original_storage_path"]
    file_bytes = download_file(storage_path)

    _log_activity(sb, user["organisation_id"], user["id"], doc_id, "downloaded", {})

    return StreamingResponse(
        io.BytesIO(file_bytes),
        media_type=result.data.get("mime_type", "application/octet-stream"),
        headers={
            "Content-Disposition": f'attachment; filename="{result.data["original_filename"]}"'
        },
    )


def _log_activity(
    sb, org_id: str, user_id: str, doc_id: Optional[str], action: str, details: dict
):
    """Log an activity event."""
    try:
        sb.table("activity_log").insert({
            "organisation_id": org_id,
            "user_id": user_id,
            "document_id": doc_id,
            "action": action,
            "details": details,
        }).execute()
    except Exception as e:
        logger.error("Failed to log activity: %s", str(e))
