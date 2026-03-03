import logging
from typing import Optional
from fastapi import Request, HTTPException

from app.database import get_supabase, get_supabase_admin

logger = logging.getLogger(__name__)


async def get_current_user(request: Request) -> dict:
    """
    Extract and validate the current user from the session cookie.

    Returns a dict with user info including id, email, organisation_id, role.
    Raises HTTPException 401 if not authenticated.
    """
    access_token = request.cookies.get("access_token")
    refresh_token = request.cookies.get("refresh_token")

    if not access_token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        sb = get_supabase()
        # Set the session using the tokens from cookies
        session = sb.auth.set_session(access_token, refresh_token or "")
        user = session.user

        if not user:
            raise HTTPException(status_code=401, detail="Invalid session")

        # Fetch user profile from our users table
        sb_admin = get_supabase_admin()
        result = (
            sb_admin.table("users")
            .select("*, organisations(name)")
            .eq("id", user.id)
            .single()
            .execute()
        )

        if not result.data:
            raise HTTPException(status_code=401, detail="User profile not found")

        user_data = result.data
        user_data["access_token"] = access_token
        user_data["refresh_token"] = refresh_token
        return user_data

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Auth error: %s", str(e))
        raise HTTPException(status_code=401, detail="Authentication failed")


async def get_optional_user(request: Request) -> Optional[dict]:
    """Get current user if logged in, or None."""
    try:
        return await get_current_user(request)
    except HTTPException:
        return None
