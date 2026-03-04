import logging
import os
import re
from email.header import decode_header as _decode_header
from typing import Optional

from app.config import settings
from app.database import get_supabase_admin

logger = logging.getLogger(__name__)

# Fernet cipher for IMAP password encryption (lazy loaded)
_fernet = None


def _get_fernet():
    global _fernet
    if _fernet is None:
        from cryptography.fernet import Fernet
        key = getattr(settings, "ENCRYPTION_KEY", "") or ""
        if not key:
            raise ValueError("ENCRYPTION_KEY is not configured")
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt_password(password: str) -> str:
    """Encrypt a plaintext password for storage."""
    return _get_fernet().encrypt(password.encode()).decode()


def decrypt_password(encrypted: str) -> str:
    """Decrypt a stored password."""
    return _get_fernet().decrypt(encrypted.encode()).decode()


def parse_email_address(raw: str) -> tuple[str, str]:
    """Parse 'John Smith <john@example.com>' into ('John Smith', 'john@example.com').

    Also handles plain email addresses like 'john@example.com'.
    """
    if not raw:
        return ("", "")

    # Try "Name <email>" format
    match = re.match(r'^"?([^"<]*)"?\s*<([^>]+)>', raw.strip())
    if match:
        name = match.group(1).strip().strip('"')
        email_addr = match.group(2).strip()
        return (name, email_addr)

    # Plain email
    stripped = raw.strip().strip("<>")
    if "@" in stripped:
        return ("", stripped)

    return ("", raw.strip())


def extract_header(raw_headers: str, header_name: str) -> Optional[str]:
    """Extract a specific header value from raw email headers text."""
    if not raw_headers:
        return None
    pattern = re.compile(
        rf"^{re.escape(header_name)}\s*:\s*(.+?)$",
        re.IGNORECASE | re.MULTILINE,
    )
    match = pattern.search(raw_headers)
    if match:
        return match.group(1).strip()
    return None


def decode_email_header(header: str) -> str:
    """Decode RFC 2047 encoded email headers."""
    if not header:
        return ""
    try:
        parts = _decode_header(header)
        decoded = []
        for content, charset in parts:
            if isinstance(content, bytes):
                decoded.append(content.decode(charset or "utf-8", errors="replace"))
            else:
                decoded.append(str(content))
        return " ".join(decoded)
    except Exception:
        return str(header)


def get_file_extension(filename: str) -> str:
    """Extract file extension from filename (including the dot)."""
    if "." in filename:
        return "." + filename.rsplit(".", 1)[-1]
    return ""


async def resolve_organisation(to_address: str, sender_email: str) -> Optional[str]:
    """Determine which organisation an inbound email belongs to.

    Strategy:
    1. Parse TO address for org slug (e.g. clientname@parse.fullcapacity.ai)
    2. Look up sender in whitelist
    3. Look up sender as a known user
    """
    sb = get_supabase_admin()

    # Strategy 1: Parse TO address for org slug
    if to_address:
        local_part = to_address.split("@")[0].lower() if "@" in to_address else ""
        if local_part and local_part != "docs":
            # Try matching org by name/slug
            try:
                result = (
                    sb.table("organisations")
                    .select("id")
                    .ilike("name", f"%{local_part}%")
                    .limit(1)
                    .execute()
                )
                if result.data:
                    return result.data[0]["id"]
            except Exception:
                pass

    # Strategy 2: Check sender whitelist
    if sender_email:
        try:
            sender_domain = sender_email.split("@")[1] if "@" in sender_email else ""
            result = (
                sb.table("email_sender_whitelist")
                .select("organisation_id")
                .or_(
                    f"address_or_domain.eq.{sender_email},"
                    f"address_or_domain.eq.{sender_domain}"
                )
                .limit(1)
                .execute()
            )
            if result.data:
                return result.data[0]["organisation_id"]
        except Exception:
            pass

    # Strategy 3: Look up sender as a known user
    if sender_email:
        try:
            result = (
                sb.table("users")
                .select("organisation_id")
                .eq("email", sender_email)
                .limit(1)
                .execute()
            )
            if result.data:
                return result.data[0]["organisation_id"]
        except Exception:
            pass

    return None


async def check_duplicate_email(message_id: str, org_id: str) -> bool:
    """Check if we have already processed an email with this Message-ID."""
    if not message_id:
        return False
    sb = get_supabase_admin()
    try:
        result = (
            sb.table("email_ingestions")
            .select("id")
            .eq("organisation_id", org_id)
            .eq("message_id", message_id)
            .limit(1)
            .execute()
        )
        return bool(result.data)
    except Exception:
        return False
