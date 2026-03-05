"""Daily email summary and to-do list generator.

Generates per-user morning summaries by collecting the user's emails
from the last 24 hours, using Claude AI to summarise each email and
extract prioritised action items.  Action items are persisted to the
``user_todos`` table for proper per-user tracking.
"""

import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Optional

import anthropic

from app.config import settings
from app.database import get_supabase_admin

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-20250514"
MAX_EMAILS_FOR_SUMMARY = 50


async def generate_daily_summary(org_id: str, user_id: Optional[str] = None) -> dict:
    """Generate a morning summary of emails received in the last 24 hours.

    When *user_id* is provided the summary is scoped to that user's
    emails only (per-user PA).  Falls back to org-wide if no user_id.
    """
    sb = get_supabase_admin()

    since = (datetime.utcnow() - timedelta(hours=24)).isoformat()

    query = (
        sb.table("email_ingestions")
        .select("*")
        .eq("organisation_id", org_id)
        .gte("received_at", since)
        .order("received_at", desc=True)
        .limit(MAX_EMAILS_FOR_SUMMARY)
    )

    if user_id:
        query = query.eq("user_id", user_id)

    result = query.execute()
    emails = result.data or []

    if not emails:
        return _empty_summary()

    # Enrich each email with attachment/document info
    email_data = []
    for email in emails:
        att_result = (
            sb.table("email_attachments")
            .select("*, documents(document_type, summary, extracted_text)")
            .eq("email_ingestion_id", email["id"])
            .execute()
        )

        attachments = att_result.data or []

        email_data.append(
            {
                "from_address": email.get("from_address", ""),
                "from_name": email.get("from_name", ""),
                "subject": email.get("subject", ""),
                "body_preview": email.get("body_preview", ""),
                "received_at": email.get("received_at", ""),
                "attachment_count": len(attachments),
                "has_attachments": len(attachments) > 0,
                "documents": [
                    {
                        "filename": a.get("original_filename", ""),
                        "type": (
                            a.get("documents", {}).get("document_type", "")
                            if a.get("documents")
                            else ""
                        ),
                        "summary": (
                            a.get("documents", {}).get("summary", "")
                            if a.get("documents")
                            else ""
                        ),
                    }
                    for a in attachments
                    if a.get("document_id")
                ],
            }
        )

    summary_result = await _ai_summarise_emails(email_data)
    return summary_result


def _empty_summary() -> dict:
    return {
        "summary_date": datetime.utcnow().strftime("%Y-%m-%d"),
        "email_count": 0,
        "emails": [],
        "todo_list": [],
        "stats": {
            "total_emails": 0,
            "with_attachments": 0,
            "documents_filed": 0,
            "action_items_found": 0,
            "high_priority": 0,
            "medium_priority": 0,
            "low_priority": 0,
        },
    }


async def _ai_summarise_emails(email_data: list) -> dict:
    """Use Claude to summarise emails and extract action items."""
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    email_text = ""
    for i, email in enumerate(email_data, 1):
        email_text += f"\n--- EMAIL {i} ---\n"
        email_text += f"From: {email['from_name']} <{email['from_address']}>\n"
        email_text += f"Subject: {email['subject']}\n"
        email_text += f"Received: {email['received_at']}\n"
        email_text += f"Preview: {email['body_preview']}\n"
        if email["documents"]:
            email_text += "Attachments filed:\n"
            for doc in email["documents"]:
                email_text += (
                    f"  - {doc['filename']} ({doc['type']}): {doc['summary']}\n"
                )
        email_text += "\n"

    prompt = f"""You are an executive assistant summarising the day's emails for a busy professional.

Here are the emails received in the last 24 hours:

{email_text}

Generate a JSON response with this exact structure:
{{
    "emails": [
        {{
            "from": "sender name or email",
            "subject": "email subject",
            "summary": "1-2 sentence summary of what this email is about and what it needs",
            "action_items": ["specific task extracted from this email"],
            "priority": "high/medium/low"
        }}
    ],
    "todo_list": [
        {{
            "task": "clear, actionable task description",
            "source_email": "subject of the email this came from",
            "from": "who sent it",
            "priority": "high/medium/low",
            "due_hint": "any deadline mentioned, or null"
        }}
    ]
}}

Rules:
- Summarise each email concisely -- the user wants to scan quickly
- Extract EVERY action item, no matter how small
- Priority: high = deadline/urgent/money, medium = needs response, low = FYI only
- Sort todo_list by priority (high first)
- If no action needed from an email, still summarise it but leave action_items empty
- due_hint should be the actual date/timeframe mentioned, or null if none

Return ONLY valid JSON, no markdown formatting."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = response.content[0].text
    result = _parse_json_response(response_text)

    if result is None:
        result = {"emails": [], "todo_list": []}

    # Add stats
    total = len(email_data)
    with_att = sum(1 for e in email_data if e["has_attachments"])
    docs_filed = sum(len(e["documents"]) for e in email_data)
    action_items = result.get("todo_list", [])

    result["summary_date"] = datetime.utcnow().strftime("%Y-%m-%d")
    result["email_count"] = total
    result["stats"] = {
        "total_emails": total,
        "with_attachments": with_att,
        "documents_filed": docs_filed,
        "action_items_found": len(action_items),
        "high_priority": sum(
            1 for a in action_items if a.get("priority") == "high"
        ),
        "medium_priority": sum(
            1 for a in action_items if a.get("priority") == "medium"
        ),
        "low_priority": sum(
            1 for a in action_items if a.get("priority") == "low"
        ),
    }

    return result


def _parse_json_response(response_text: str) -> Optional[dict]:
    """Parse JSON from Claude response, handling markdown code blocks."""
    cleaned = response_text.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    patterns = [
        r"```json\s*\n?(.*?)\n?\s*```",
        r"```\s*\n?(.*?)\n?\s*```",
        r"\{.*\}",
    ]

    for pattern in patterns:
        match = re.search(pattern, cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(
                    match.group(1) if match.lastindex else match.group(0)
                )
            except json.JSONDecodeError:
                continue

    logger.error("Could not parse JSON from summary response: %s", cleaned[:500])
    return None


async def save_daily_summary(
    org_id: str, user_id: str, summary: dict
) -> str:
    """Save the daily summary and persist to-do items to user_todos."""
    sb = get_supabase_admin()

    summary_id = str(uuid.uuid4())

    sb.table("daily_summaries").insert(
        {
            "id": summary_id,
            "organisation_id": org_id,
            "generated_by": user_id,
            "user_id": user_id,
            "summary_date": summary["summary_date"],
            "email_count": summary["email_count"],
            "todo_count": len(summary.get("todo_list", [])),
            "summary_data": summary,
        }
    ).execute()

    # Persist action items to user_todos table
    for item in summary.get("todo_list", []):
        try:
            sb.table("user_todos").insert(
                {
                    "id": str(uuid.uuid4()),
                    "user_id": user_id,
                    "organisation_id": org_id,
                    "summary_id": summary_id,
                    "task": item.get("task", ""),
                    "source_email_subject": item.get("source_email"),
                    "source_email_from": item.get("from"),
                    "priority": item.get("priority", "medium"),
                    "due_hint": item.get("due_hint"),
                }
            ).execute()
        except Exception as e:
            logger.error("Failed to create user_todo: %s", e)

    return summary_id
