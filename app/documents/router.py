import hashlib
import logging
import uuid
from datetime import datetime, date
from typing import Optional

from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Query

from app.auth.dependencies import get_current_user
from app.database import get_supabase_admin
from app.storage import upload_file, download_file, move_file, delete_file
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

    # Read file bytes and compute content hash
    file_bytes = await file.read()
    file_size = len(file_bytes)
    original_filename = file.filename or "unnamed_file"
    content_hash = hashlib.sha256(file_bytes).hexdigest()

    # Upload file and create document record
    doc_id = str(uuid.uuid4())
    ext = original_filename.rsplit(".", 1)[-1] if "." in original_filename else "pdf"
    original_storage_path = f"originals/{org_id}/{doc_id}.{ext}"
    upload_file(original_storage_path, file_bytes, mime_type)

    # Check for duplicates by hash and filename
    duplicate_status, duplicate_info = _check_duplicate(sb, org_id, content_hash, original_filename)

    if duplicate_status:
        # Store the document but flag it with the appropriate duplicate status
        sb.table("documents").insert({
            "id": doc_id,
            "organisation_id": org_id,
            "uploaded_by": user_id,
            "original_filename": original_filename,
            "file_size_bytes": file_size,
            "mime_type": mime_type,
            "original_storage_path": original_storage_path,
            "content_hash": content_hash,
            "status": duplicate_status,
            "duplicate_of": duplicate_info["existing_id"],
        }).execute()
        _log_activity(sb, org_id, user_id, doc_id, "duplicate_detected", {
            "duplicate_of": duplicate_info["existing_id"],
            "match_type": duplicate_info["match_type"],
        })
        return {
            "id": doc_id,
            "filename": original_filename,
            "status": duplicate_status,
            "duplicate_info": duplicate_info,
        }

    # No duplicate — process normally
    doc_record = {
        "id": doc_id,
        "organisation_id": org_id,
        "uploaded_by": user_id,
        "original_filename": original_filename,
        "file_size_bytes": file_size,
        "mime_type": mime_type,
        "original_storage_path": original_storage_path,
        "content_hash": content_hash,
        "status": "processing",
    }
    sb.table("documents").insert(doc_record).execute()

    # Log activity
    _log_activity(sb, org_id, user_id, doc_id, "uploaded", {"filename": original_filename})

    return _run_processing_pipeline(sb, doc_id, org_id, user_id, org_name, file_bytes, mime_type, original_filename, original_storage_path)


def _run_processing_pipeline(
    sb, doc_id: str, org_id: str, user_id: str, org_name: str,
    file_bytes: bytes, mime_type: str, original_filename: str,
    original_storage_path: str,
) -> dict:
    """Run text extraction, classification, and filing for a document."""
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


def _check_duplicate(
    sb, org_id: str, content_hash: str, original_filename: str
) -> tuple[Optional[str], Optional[dict]]:
    """Check for duplicates by content hash and filename.

    Returns (duplicate_status, duplicate_info) or (None, None) if no match.
    """
    try:
        # Find existing docs with same hash OR same filename in this org
        hash_result = (
            sb.table("documents")
            .select("id, original_filename, filed_name, uploaded_at, content_hash")
            .eq("organisation_id", org_id)
            .eq("content_hash", content_hash)
            .not_.in_("status", ["duplicate_exact", "duplicate_name", "duplicate_content", "duplicate", "replaced"])
            .limit(1)
            .execute()
        )

        name_result = (
            sb.table("documents")
            .select("id, original_filename, filed_name, uploaded_at, content_hash")
            .eq("organisation_id", org_id)
            .eq("original_filename", original_filename)
            .not_.in_("status", ["duplicate_exact", "duplicate_name", "duplicate_content", "duplicate", "replaced"])
            .limit(1)
            .execute()
        )

        hash_match = hash_result.data[0] if hash_result.data else None
        name_match = name_result.data[0] if name_result.data else None

        if hash_match and name_match and hash_match["id"] == name_match["id"]:
            # Same hash AND same filename — exact duplicate
            return "duplicate_exact", {
                "existing_id": hash_match["id"],
                "existing_filename": hash_match.get("filed_name") or hash_match["original_filename"],
                "existing_uploaded_at": hash_match.get("uploaded_at"),
                "match_type": "exact",
            }

        if name_match and (not hash_match or hash_match["id"] != name_match["id"]):
            # Same filename but different content
            return "duplicate_name", {
                "existing_id": name_match["id"],
                "existing_filename": name_match.get("filed_name") or name_match["original_filename"],
                "existing_uploaded_at": name_match.get("uploaded_at"),
                "match_type": "same_name",
            }

        if hash_match:
            # Different filename but same content
            return "duplicate_content", {
                "existing_id": hash_match["id"],
                "existing_filename": hash_match.get("filed_name") or hash_match["original_filename"],
                "existing_uploaded_at": hash_match.get("uploaded_at"),
                "match_type": "same_content",
            }

    except Exception:
        logger.exception("Duplicate check failed")

    return None, None


@router.post("/{doc_id}/force-file")
async def force_file_duplicate(doc_id: str, user: dict = Depends(get_current_user)):
    """Force-file a document that was flagged as duplicate (upload anyway)."""
    sb = get_supabase_admin()
    org_id = user["organisation_id"]
    org_name = user.get("organisations", {}).get("name", "Default") if isinstance(user.get("organisations"), dict) else "Default"

    result = (
        sb.table("documents")
        .select("*")
        .eq("id", doc_id)
        .eq("organisation_id", org_id)
        .in_("status", ["duplicate_exact", "duplicate_name", "duplicate_content", "duplicate"])
        .single()
        .execute()
    )

    if not result.data:
        raise HTTPException(status_code=404, detail="Document not found or not a duplicate")

    doc = result.data
    original_storage_path = doc["original_storage_path"]
    file_bytes = download_file(original_storage_path)

    # Clear duplicate status and process
    sb.table("documents").update({
        "status": "processing",
        "duplicate_of": None,
    }).eq("id", doc_id).execute()

    pipeline_result = _run_processing_pipeline(
        sb, doc_id, org_id, user["id"], org_name,
        file_bytes, doc["mime_type"], doc["original_filename"], original_storage_path,
    )

    _log_activity(sb, org_id, user["id"], doc_id, "force_filed", {})
    return pipeline_result


@router.patch("/{existing_id}/overwrite")
async def overwrite_document(existing_id: str, doc_id: str = Query(...), user: dict = Depends(get_current_user)):
    """Overwrite an existing document with a new version.

    The new document (doc_id) replaces the existing document (existing_id).
    The old version's storage files are removed and its record is marked as replaced.
    """
    sb = get_supabase_admin()
    org_id = user["organisation_id"]
    org_name = user.get("organisations", {}).get("name", "Default") if isinstance(user.get("organisations"), dict) else "Default"

    # Fetch the new (duplicate-flagged) document
    new_result = (
        sb.table("documents")
        .select("*")
        .eq("id", doc_id)
        .eq("organisation_id", org_id)
        .in_("status", ["duplicate_exact", "duplicate_name", "duplicate_content", "duplicate"])
        .single()
        .execute()
    )
    if not new_result.data:
        raise HTTPException(status_code=404, detail="New document not found or not a duplicate")

    # Fetch the existing document to be replaced
    old_result = (
        sb.table("documents")
        .select("*")
        .eq("id", existing_id)
        .eq("organisation_id", org_id)
        .single()
        .execute()
    )
    if not old_result.data:
        raise HTTPException(status_code=404, detail="Existing document not found")

    old_doc = old_result.data
    new_doc = new_result.data

    # Delete old storage files
    for path_key in ("original_storage_path", "filed_storage_path"):
        old_path = old_doc.get(path_key)
        if old_path:
            try:
                delete_file(old_path)
            except Exception:
                logger.warning("Could not delete old file: %s", old_path)

    # Mark the old document as replaced
    sb.table("documents").update({
        "status": "replaced",
        "error_message": f"Replaced by document {doc_id}",
    }).eq("id", existing_id).execute()

    # Process the new document through the full pipeline
    file_bytes = download_file(new_doc["original_storage_path"])

    sb.table("documents").update({
        "status": "processing",
        "duplicate_of": None,
    }).eq("id", doc_id).execute()

    pipeline_result = _run_processing_pipeline(
        sb, doc_id, org_id, user["id"], org_name,
        file_bytes, new_doc["mime_type"], new_doc["original_filename"],
        new_doc["original_storage_path"],
    )

    _log_activity(sb, org_id, user["id"], doc_id, "overwrote_document", {
        "replaced_id": existing_id,
        "replaced_filename": old_doc.get("original_filename"),
    })
    return pipeline_result


@router.delete("/{doc_id}")
async def delete_document(doc_id: str, user: dict = Depends(get_current_user)):
    """Delete a document (used to skip/cancel a duplicate)."""
    sb = get_supabase_admin()
    org_id = user["organisation_id"]

    result = (
        sb.table("documents")
        .select("original_storage_path, filed_storage_path, status")
        .eq("id", doc_id)
        .eq("organisation_id", org_id)
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Document not found")

    doc = result.data

    # Delete storage files
    for path_key in ("original_storage_path", "filed_storage_path"):
        path = doc.get(path_key)
        if path:
            try:
                delete_file(path)
            except Exception:
                logger.warning("Could not delete file: %s", path)

    sb.table("documents").delete().eq("id", doc_id).eq("organisation_id", org_id).execute()
    _log_activity(sb, org_id, user["id"], doc_id, "deleted", {})
    return {"id": doc_id, "status": "deleted"}


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
