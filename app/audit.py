"""Audit trail logging service.

All significant user and system actions are logged here for compliance,
accountability, and client billing purposes.  Audit logs are INSERT-ONLY
-- they must never be updated or deleted.
"""

import logging
from typing import Optional

from app.database import get_supabase_admin

logger = logging.getLogger(__name__)


async def log_action(
    org_id: str,
    user_id: Optional[str],
    action: str,
    entity_type: str,
    entity_id: Optional[str] = None,
    details: Optional[dict] = None,
    request=None,
):
    """Log an action to the audit trail.

    Actions follow the pattern ``entity.verb``, for example:
    document.uploaded, document.classified, email.connected,
    matter.created, todo.completed, user.logged_in, summary.generated.
    """
    try:
        sb = get_supabase_admin()

        ip_address = None
        user_agent = None
        if request:
            ip_address = request.client.host if request.client else None
            user_agent = (request.headers.get("user-agent") or "")[:500]

        sb.table("audit_log").insert(
            {
                "organisation_id": org_id,
                "user_id": user_id,
                "action": action,
                "entity_type": entity_type,
                "entity_id": str(entity_id) if entity_id else None,
                "details": details or {},
                "ip_address": ip_address,
                "user_agent": user_agent,
            }
        ).execute()

    except Exception as e:
        # Never let audit logging break the main flow
        logger.error("Audit log failed: %s", e)
