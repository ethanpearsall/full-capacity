import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Query, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse

from app.auth.router import router as auth_router
from app.auth.dependencies import get_current_user
from app.documents.router import router as documents_router
from app.dashboard.router import router as dashboard_router
from app.chat.router import router as chat_router
from app.email.router import router as email_router
from app.matters.router import router as matters_router
from app.audit_router import router as audit_router
from app.database import get_supabase_admin
from app.config import settings

# Force logging configuration — force=True ensures this works even if imported
# libraries (supabase, anthropic, etc.) already configured the root logger,
# which would cause basicConfig() without force to silently no-op.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """App startup/shutdown lifecycle."""
    # Register Telegram webhook on startup if token is configured
    if settings.TELEGRAM_BOT_TOKEN:
        try:
            from app.chat.bot import set_webhook
            webhook_url = f"{settings.APP_URL.rstrip('/')}/webhook/telegram"
            result = await set_webhook(webhook_url)
            if result:
                print(f"[STARTUP] Telegram webhook registered: {webhook_url}", flush=True)
            else:
                print("[STARTUP] Failed to register Telegram webhook", flush=True)
        except Exception as e:
            print(f"[STARTUP] Telegram webhook setup error: {e}", flush=True)
    else:
        print("[STARTUP] TELEGRAM_BOT_TOKEN not set, skipping webhook registration", flush=True)

    # Log API key status (not the key itself)
    api_key = settings.ANTHROPIC_API_KEY
    if api_key:
        print(f"[STARTUP] ANTHROPIC_API_KEY loaded ({len(api_key)} chars, starts with {api_key[:7]}...)", flush=True)
    else:
        print("[STARTUP] WARNING: ANTHROPIC_API_KEY is empty!", flush=True)

    # IMAP polling disabled -- forwarding-based ingestion is primary.
    # Keeping code for future OAuth-based IMAP support.
    # import asyncio
    # from app.email.imap_scheduler import imap_polling_loop
    # polling_task = asyncio.create_task(imap_polling_loop())
    # print("[STARTUP] IMAP polling background task started", flush=True)

    yield


app = FastAPI(
    title="Full Capacity",
    description="AI Document Processing & Filing System",
    lifespan=lifespan,
)

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Include routers
app.include_router(auth_router)
app.include_router(documents_router)
app.include_router(dashboard_router)
app.include_router(chat_router)
app.include_router(email_router)
app.include_router(matters_router)
app.include_router(audit_router)


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "full-capacity"}


@app.get("/api/folders")
async def get_folder_tree(user: dict = Depends(get_current_user)):
    """Get the folder tree structure from document folder_path values."""
    sb = get_supabase_admin()
    result = (
        sb.table("documents")
        .select("folder_path, id")
        .eq("organisation_id", user["organisation_id"])
        .not_.is_("folder_path", "null")
        .execute()
    )

    # Build tree from folder paths
    tree: dict = {}
    for doc in result.data:
        path = doc["folder_path"]
        if not path:
            continue
        parts = [p for p in path.strip("/").split("/") if p]
        current = tree
        for part in parts:
            if part not in current:
                current[part] = {}
            current = current[part]

    return {"tree": tree}


@app.get("/api/folders/{path:path}")
async def get_folder_contents(
    path: str,
    user: dict = Depends(get_current_user),
):
    """Get documents in a specific folder."""
    sb = get_supabase_admin()
    folder_path = "/" + path.strip("/")

    result = (
        sb.table("documents")
        .select("*")
        .eq("organisation_id", user["organisation_id"])
        .eq("folder_path", folder_path)
        .order("uploaded_at", desc=True)
        .execute()
    )

    return {"folder_path": folder_path, "documents": result.data}


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Global error handler to prevent crashes."""
    logger.error("Unhandled error: %s", str(exc), exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal error occurred. Please try again."},
    )
