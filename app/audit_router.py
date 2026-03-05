"""Audit trail API and page route."""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth.dependencies import get_current_user, get_optional_user
from app.database import get_supabase_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/audit", tags=["audit"])
templates = Jinja2Templates(directory="templates")


@router.get("/page", response_class=HTMLResponse, include_in_schema=False)
async def audit_log_page(request: Request):
    """Render the activity log page."""
    user = await get_optional_user(request)
    if not user:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(
        "audit_log.html", {"request": request, "user": user}
    )


@router.get("/log")
async def get_audit_log(
    entity_type: Optional[str] = Query(None),
    entity_id: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_user),
):
    """Get the audit log for the organisation."""
    sb = get_supabase_admin()
    org_id = current_user["organisation_id"]

    query = (
        sb.table("audit_log")
        .select("*")
        .eq("organisation_id", org_id)
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
    )

    if entity_type:
        query = query.eq("entity_type", entity_type)
    if entity_id:
        query = query.eq("entity_id", entity_id)
    if user_id:
        query = query.eq("user_id", user_id)

    result = query.execute()
    return {"entries": result.data or [], "count": len(result.data or [])}


@router.get("/log/entity/{entity_type}/{entity_id}")
async def get_entity_audit_log(
    entity_type: str,
    entity_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Get all audit entries for a specific entity."""
    sb = get_supabase_admin()
    org_id = current_user["organisation_id"]

    result = (
        sb.table("audit_log")
        .select("*")
        .eq("organisation_id", org_id)
        .eq("entity_type", entity_type)
        .eq("entity_id", entity_id)
        .order("created_at", desc=True)
        .execute()
    )

    return {"entries": result.data or []}
