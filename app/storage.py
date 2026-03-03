import logging
from app.database import get_supabase_admin
from app.config import settings

logger = logging.getLogger(__name__)

BUCKET = settings.STORAGE_BUCKET


def upload_file(path: str, file_bytes: bytes, content_type: str) -> str:
    """Upload a file to Supabase Storage. Returns the storage path."""
    sb = get_supabase_admin()
    sb.storage.from_(BUCKET).upload(
        path,
        file_bytes,
        file_options={"content-type": content_type},
    )
    logger.info("Uploaded file to storage: %s", path)
    return path


def download_file(path: str) -> bytes:
    """Download a file from Supabase Storage."""
    sb = get_supabase_admin()
    data = sb.storage.from_(BUCKET).download(path)
    return data


def move_file(from_path: str, to_path: str) -> str:
    """Move/rename a file in Supabase Storage."""
    sb = get_supabase_admin()
    sb.storage.from_(BUCKET).move(from_path, to_path)
    logger.info("Moved file: %s -> %s", from_path, to_path)
    return to_path


def get_public_url(path: str) -> str:
    """Get the public URL for a file in storage."""
    sb = get_supabase_admin()
    result = sb.storage.from_(BUCKET).get_public_url(path)
    return result


def delete_file(path: str) -> None:
    """Delete a file from Supabase Storage."""
    sb = get_supabase_admin()
    sb.storage.from_(BUCKET).remove([path])
    logger.info("Deleted file from storage: %s", path)
