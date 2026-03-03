import logging
from fastapi import FastAPI, Request, Query, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse

from app.auth.router import router as auth_router
from app.auth.dependencies import get_current_user
from app.documents.router import router as documents_router
from app.dashboard.router import router as dashboard_router
from app.database import get_supabase_admin

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Full Capacity", description="AI Document Processing & Filing System")

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Include routers
app.include_router(auth_router)
app.include_router(documents_router)
app.include_router(dashboard_router)


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
