import logging
from fastapi import APIRouter, Request, Response, HTTPException
from pydantic import BaseModel

from app.database import get_supabase, get_supabase_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


class SignupRequest(BaseModel):
    email: str
    password: str
    full_name: str
    org_name: str


class LoginRequest(BaseModel):
    email: str
    password: str


@router.post("/signup")
async def signup(data: SignupRequest, response: Response):
    """Create a new account with organisation."""
    try:
        sb = get_supabase()
        sb_admin = get_supabase_admin()

        # Create auth user in Supabase Auth
        auth_response = sb.auth.sign_up(
            {"email": data.email, "password": data.password}
        )

        if not auth_response.user:
            raise HTTPException(status_code=400, detail="Failed to create account")

        user_id = auth_response.user.id

        # Create organisation
        org_result = (
            sb_admin.table("organisations")
            .insert({"name": data.org_name})
            .execute()
        )
        org_id = org_result.data[0]["id"]

        # Create user profile
        sb_admin.table("users").insert(
            {
                "id": user_id,
                "email": data.email,
                "full_name": data.full_name,
                "organisation_id": org_id,
                "role": "admin",
            }
        ).execute()

        # Set session cookies if we have a session
        if auth_response.session:
            response.set_cookie(
                "access_token",
                auth_response.session.access_token,
                httponly=True,
                samesite="lax",
                max_age=3600,
            )
            response.set_cookie(
                "refresh_token",
                auth_response.session.refresh_token,
                httponly=True,
                samesite="lax",
                max_age=604800,
            )

        return {"message": "Account created successfully", "user_id": user_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Signup error: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/login")
async def login(data: LoginRequest, response: Response):
    """Login with email and password."""
    try:
        sb = get_supabase()
        auth_response = sb.auth.sign_in_with_password(
            {"email": data.email, "password": data.password}
        )

        if not auth_response.session:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        response.set_cookie(
            "access_token",
            auth_response.session.access_token,
            httponly=True,
            samesite="lax",
            max_age=3600,
        )
        response.set_cookie(
            "refresh_token",
            auth_response.session.refresh_token,
            httponly=True,
            samesite="lax",
            max_age=604800,
        )

        return {"message": "Login successful"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Login error: %s", str(e))
        raise HTTPException(status_code=401, detail="Invalid credentials")


@router.post("/logout")
async def logout(response: Response):
    """Logout — clear session cookies."""
    response.delete_cookie("access_token")
    response.delete_cookie("refresh_token")
    return {"message": "Logged out"}
