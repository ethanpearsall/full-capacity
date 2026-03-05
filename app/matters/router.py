"""Client matters API -- organise documents by client and matter."""

import logging
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.audit import log_action
from app.auth.dependencies import get_current_user, get_optional_user
from app.database import get_supabase_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/matters", tags=["matters"])
templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------------------------------
# Page route
# ---------------------------------------------------------------------------

@router.get("/page", response_class=HTMLResponse, include_in_schema=False)
async def matters_page(request: Request):
    """Render the client matters page."""
    user = await get_optional_user(request)
    if not user:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(
        "matters.html", {"request": request, "user": user}
    )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@router.get("")
async def list_matters(
    status: str = Query("active"),
    current_user: dict = Depends(get_current_user),
):
    """List all client matters for the organisation."""
    sb = get_supabase_admin()
    org_id = current_user["organisation_id"]

    query = (
        sb.table("client_matters")
        .select("*")
        .eq("organisation_id", org_id)
        .order("updated_at", desc=True)
    )

    if status != "all":
        query = query.eq("status", status)

    result = query.execute()
    matters = result.data or []

    # Get document counts per matter
    for matter in matters:
        doc_result = (
            sb.table("documents")
            .select("id")
            .eq("client_matter_id", matter["id"])
            .execute()
        )
        matter["document_count"] = len(doc_result.data) if doc_result.data else 0

    return {"matters": matters}


@router.post("")
async def create_matter(
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Create a new client matter."""
    body = await request.json()
    sb = get_supabase_admin()
    org_id = current_user["organisation_id"]
    user_id = current_user["id"]

    matter_id = str(uuid.uuid4())
    sb.table("client_matters").insert({
        "id": matter_id,
        "organisation_id": org_id,
        "client_name": body["client_name"],
        "matter_name": body.get("matter_name"),
        "matter_reference": body.get("matter_reference"),
        "created_by": user_id,
    }).execute()

    await log_action(org_id, user_id, "matter.created", "matter", matter_id, {
        "client_name": body["client_name"],
        "matter_reference": body.get("matter_reference"),
    }, request=request)

    return {"id": matter_id, "status": "ok"}


@router.get("/{matter_id}")
async def get_matter(
    matter_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Get a client matter with its documents."""
    sb = get_supabase_admin()
    org_id = current_user["organisation_id"]

    matter = (
        sb.table("client_matters")
        .select("*")
        .eq("id", matter_id)
        .eq("organisation_id", org_id)
        .single()
        .execute()
    )

    if not matter.data:
        raise HTTPException(status_code=404, detail="Matter not found")

    docs = (
        sb.table("documents")
        .select("*")
        .eq("client_matter_id", matter_id)
        .order("uploaded_at", desc=True)
        .execute()
    )

    result = matter.data
    result["documents"] = docs.data or []
    return result


@router.patch("/{matter_id}")
async def update_matter(
    matter_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Update a client matter."""
    body = await request.json()
    sb = get_supabase_admin()
    org_id = current_user["organisation_id"]

    existing = (
        sb.table("client_matters")
        .select("id")
        .eq("id", matter_id)
        .eq("organisation_id", org_id)
        .limit(1)
        .execute()
    )
    if not existing.data:
        raise HTTPException(status_code=404, detail="Matter not found")

    allowed = {"client_name", "matter_name", "matter_reference", "status"}
    update_data = {k: v for k, v in body.items() if k in allowed}
    update_data["updated_at"] = datetime.utcnow().isoformat()

    sb.table("client_matters").update(update_data).eq("id", matter_id).execute()

    action = "matter.archived" if update_data.get("status") == "archived" else "matter.updated"
    await log_action(org_id, current_user["id"], action, "matter", matter_id, {
        "changes": update_data,
    }, request=request)

    return {"status": "ok"}


@router.delete("/{matter_id}")
async def delete_matter(
    matter_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Delete a client matter (unlinks documents, does not delete them)."""
    sb = get_supabase_admin()
    org_id = current_user["organisation_id"]

    existing = (
        sb.table("client_matters")
        .select("id")
        .eq("id", matter_id)
        .eq("organisation_id", org_id)
        .limit(1)
        .execute()
    )
    if not existing.data:
        raise HTTPException(status_code=404, detail="Matter not found")

    # Unlink documents
    sb.table("documents").update({"client_matter_id": None}).eq(
        "client_matter_id", matter_id
    ).execute()

    sb.table("client_matters").delete().eq("id", matter_id).execute()
    return {"status": "ok"}
