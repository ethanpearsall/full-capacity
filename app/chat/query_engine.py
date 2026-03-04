import json
import logging
from datetime import datetime, timedelta
from typing import Optional

import anthropic

from app.config import settings
from app.database import get_supabase_admin

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-20250514"

QUERY_PROMPT = """You are a document search assistant for a professional services firm.
The user wants to find or ask about documents in their filing system.

Given the user's query, determine:
1. What they're looking for (document type, client name, date range, amount, etc.)
2. Generate a search strategy

Return a JSON object (no other text, no markdown):
{{
    "intent": "search|summary|count|compare|latest",
    "search_filters": {{
        "document_type": null,
        "client_name": null,
        "date_from": null,
        "date_to": null,
        "matter_reference": null,
        "text_search": null,
        "amount_min": null,
        "amount_max": null
    }},
    "response_type": "list|detail|number|summary",
    "limit": 5
}}

Today's date is {today}. When the user says "last month", "this week", etc., calculate the actual dates.

USER QUERY: {query}"""

ANSWER_PROMPT = """Based on the following document, answer the user's question accurately and concisely.

DOCUMENT: {document_title}
TYPE: {document_type}
CLIENT: {client_name}

FULL TEXT:
{extracted_text}

USER QUESTION: {question}

Answer in 1-3 sentences. Include specific numbers, dates, and names. If the answer isn't in the document, say so."""


async def process_query(query: str, org_id: str) -> tuple[str, list[str]]:
    """Process a natural language query and return (response_text, document_ids)."""
    # Step 1: Use Claude to understand the query and generate search filters
    search_plan = _understand_query(query)
    if search_plan is None:
        return "I couldn't understand your query. Could you rephrase it?", []

    intent = search_plan.get("intent", "search")
    filters = search_plan.get("search_filters", {})
    response_type = search_plan.get("response_type", "list")
    limit = min(search_plan.get("limit", 5), 20)

    # Step 2: Execute the search against the database
    documents = _execute_search(org_id, filters, limit)

    if not documents:
        return "No documents found matching your query. Try broadening your search.", []

    doc_ids = [d["id"] for d in documents]

    # Step 3: Format the response based on intent
    if intent == "count":
        return _format_count_response(documents, filters), doc_ids

    if intent == "summary" and len(documents) == 1:
        # For a single document summary, use Claude to answer from full text
        return _format_detail_response(documents[0], query), doc_ids

    if intent == "latest":
        return _format_document_list(documents[:1]), doc_ids[:1]

    # Default: list of documents
    return _format_document_list(documents), doc_ids


def _understand_query(query: str) -> Optional[dict]:
    """Use Claude to parse the user's natural language query into search filters."""
    try:
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        today = datetime.utcnow().strftime("%Y-%m-%d")
        prompt = QUERY_PROMPT.format(today=today, query=query)

        message = client.messages.create(
            model=MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )

        response_text = message.content[0].text.strip()
        try:
            return json.loads(response_text)
        except json.JSONDecodeError:
            # Try to extract JSON
            import re
            match = re.search(r"\{.*\}", response_text, re.DOTALL)
            if match:
                return json.loads(match.group(0))
            logger.error("Could not parse query understanding response: %.500s", response_text)
            return None
    except Exception:
        logger.exception("Query understanding failed")
        return None


def _execute_search(org_id: str, filters: dict, limit: int) -> list[dict]:
    """Execute a database search with the given filters. Always scoped to org_id."""
    sb = get_supabase_admin()

    query = (
        sb.table("documents")
        .select("id, original_filename, filed_name, document_type, client_name, "
                "counterparty, document_date, matter_reference, amount, currency, "
                "summary, confidence_score, folder_path, status, tags, uploaded_at, extracted_text")
        .eq("organisation_id", org_id)
        .not_.in_("status", ["duplicate", "duplicate_exact", "duplicate_name", "duplicate_content", "replaced"])
        .order("uploaded_at", desc=True)
        .limit(limit)
    )

    doc_type = filters.get("document_type")
    if doc_type:
        query = query.eq("document_type", doc_type)

    client_name = filters.get("client_name")
    if client_name:
        query = query.ilike("client_name", f"%{client_name}%")

    date_from = filters.get("date_from")
    if date_from:
        query = query.gte("document_date", date_from)

    date_to = filters.get("date_to")
    if date_to:
        query = query.lte("document_date", date_to)

    ref = filters.get("matter_reference")
    if ref:
        query = query.ilike("matter_reference", f"%{ref}%")

    amount_min = filters.get("amount_min")
    if amount_min is not None:
        query = query.gte("amount", amount_min)

    amount_max = filters.get("amount_max")
    if amount_max is not None:
        query = query.lte("amount", amount_max)

    text_search = filters.get("text_search")
    if text_search:
        query = query.text_search("fts", text_search)

    try:
        result = query.execute()
        return result.data or []
    except Exception:
        logger.exception("Search execution failed")
        return []


def _format_document_list(documents: list[dict]) -> str:
    """Format a list of documents as a Telegram-friendly response."""
    if not documents:
        return "No documents found."

    lines = []
    for doc in documents:
        client = doc.get("client_name") or "Unknown"
        doc_type = (doc.get("document_type") or "other").replace("_", " ").title()
        confidence = doc.get("confidence_score")
        conf_str = f"{int(confidence * 100)}%" if confidence else "—"
        doc_date = doc.get("document_date") or "—"
        amount = doc.get("amount")
        currency = doc.get("currency") or "GBP"
        ref = doc.get("matter_reference") or ""
        summary = doc.get("summary") or ""
        folder = doc.get("folder_path") or ""

        currency_symbols = {"GBP": "\u00a3", "USD": "$", "EUR": "\u20ac"}
        symbol = currency_symbols.get(currency, currency)

        entry = f"*{client}*\n"
        entry += f"Type: {doc_type} | Confidence: {conf_str}\n"
        if doc_date != "—":
            entry += f"Date: {doc_date}\n"
        if amount is not None:
            entry += f"Amount: {symbol}{amount:,.2f}\n"
        if ref:
            entry += f"Ref: {ref}\n"
        if summary:
            entry += f"_{summary}_\n"
        if folder:
            entry += f"Filed: {folder}\n"

        lines.append(entry)

    return "\n---\n".join(lines)


def _format_count_response(documents: list[dict], filters: dict) -> str:
    """Format a count-style response."""
    count = len(documents)
    parts = [f"Found *{count}* document(s)"]

    doc_type = filters.get("document_type")
    if doc_type:
        parts.append(f"of type _{doc_type}_")

    client = filters.get("client_name")
    if client:
        parts.append(f"for client _{client}_")

    return " ".join(parts) + "."


def _format_detail_response(doc: dict, question: str) -> str:
    """Use Claude to answer a specific question about a document."""
    extracted_text = doc.get("extracted_text", "")
    if not extracted_text:
        # Fall back to summary
        return _format_document_list([doc])

    try:
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        prompt = ANSWER_PROMPT.format(
            document_title=doc.get("filed_name") or doc.get("original_filename", ""),
            document_type=doc.get("document_type", ""),
            client_name=doc.get("client_name", ""),
            extracted_text=extracted_text[:10000],
            question=question,
        )

        message = client.messages.create(
            model=MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )

        answer = message.content[0].text.strip()
        header = _format_document_list([doc])
        return f"{header}\n\n*Answer:* {answer}"
    except Exception:
        logger.exception("Detail response generation failed")
        return _format_document_list([doc])
