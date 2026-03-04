import json
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

import anthropic

from app.config import settings
from app.database import get_supabase_admin

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-20250514"

EXCLUDED_STATUSES = ["duplicate", "duplicate_exact", "duplicate_name", "duplicate_content", "replaced"]

INTERPRET_PROMPT = """You are a search query interpreter for a document filing system at a professional services firm.

Given the user's natural language query, extract the key search terms and any time filters.

Return ONLY a JSON object (no markdown, no explanation):
{{
    "search_terms": ["term1", "term2"],
    "time_filter": null,
    "date_from": null,
    "date_to": null,
    "limit": 10
}}

Rules:
- search_terms: Extract the meaningful keywords the user is looking for. Names, document types, companies, amounts, etc. Strip filler words. If the query is about "recent docs" or "what was filed today", leave search_terms empty.
- time_filter: One of "today", "this_week", "this_month", "last_month", "recent", or null. Use "recent" for vague recency requests.
- date_from / date_to: If the user mentions specific dates or ranges, calculate them. Today is {today}.
- limit: How many results to return. Default 10. Use 5 for "latest" or "most recent" queries. Use 20 for broad queries like "all invoices".

Examples:
- "what's going on with Thornton" -> {{"search_terms": ["Thornton"], "time_filter": null, "date_from": null, "date_to": null, "limit": 10}}
- "recent docs" -> {{"search_terms": [], "time_filter": "recent", "date_from": null, "date_to": null, "limit": 5}}
- "VAT returns from January" -> {{"search_terms": ["VAT", "vat_return"], "time_filter": null, "date_from": "2026-01-01", "date_to": "2026-01-31", "limit": 10}}
- "Henderson engagement letter" -> {{"search_terms": ["Henderson", "engagement"], "time_filter": null, "date_from": null, "date_to": null, "limit": 10}}
- "how much have we spent on office supplies" -> {{"search_terms": ["office supplies", "receipt", "invoice"], "time_filter": null, "date_from": null, "date_to": null, "limit": 20}}
- "anything from HMRC" -> {{"search_terms": ["HMRC"], "time_filter": null, "date_from": null, "date_to": null, "limit": 10}}

USER QUERY: {query}"""

SYNTHESISE_PROMPT = """CRITICAL FORMATTING RULE: Your response must be plain text ONLY. Never use markdown. Never use ** or * for bold/italic. Never use # for headers. Never use bullet points with * or •. Use dashes (-) for lists. Use line breaks for separation. This is displayed in a small chat widget that does not render markdown.

You are a document assistant for a professional services firm. You have access to the firm's document filing system.

The user asked: "{question}"

Here are the relevant documents found:

{document_summaries}

Respond conversationally and helpfully. Be specific — mention names, dates, amounts, and references.
If multiple documents match, give a brief overview of each.
If the user asks about a situation or status, synthesise the information across all matching documents into a coherent narrative.
If no documents were found, say so and suggest they try different search terms.
Keep responses concise but complete. Use natural language, not raw data dumps.
When mentioning monetary amounts, use the appropriate currency symbol (£, $, €)."""


async def process_query(query: str, org_id: str) -> tuple[str, list[str]]:
    """Process a natural language query and return (response_text, document_ids)."""
    # Stage 1: Interpret the query to extract search terms
    search_plan = _interpret_query(query)

    # Stage 2: Smart search across multiple fields
    documents = _smart_search(org_id, search_plan)
    doc_ids = [d["id"] for d in documents]

    if not documents:
        return "I couldn't find any documents matching your query. Try different search terms or ask about a specific client or document type.", []

    # Stage 3: Use Claude to synthesise a conversational response
    response = _synthesise_response(query, documents)
    return response, doc_ids


def _interpret_query(query: str) -> dict:
    """Use Claude to extract search terms and filters from the user's query."""
    try:
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        today = datetime.utcnow().strftime("%Y-%m-%d")

        message = client.messages.create(
            model=MODEL,
            max_tokens=256,
            messages=[{"role": "user", "content": INTERPRET_PROMPT.format(today=today, query=query)}],
        )

        text = message.content[0].text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                return json.loads(match.group(0))
    except Exception:
        logger.exception("Query interpretation failed")

    # Fallback: use the raw query as a search term
    return {"search_terms": [query], "time_filter": None, "date_from": None, "date_to": None, "limit": 10}


def _smart_search(org_id: str, plan: dict) -> list[dict]:
    """Search across multiple document fields using OR conditions."""
    sb = get_supabase_admin()
    search_terms = plan.get("search_terms") or []
    time_filter = plan.get("time_filter")
    date_from = plan.get("date_from")
    date_to = plan.get("date_to")
    limit = min(plan.get("limit", 10), 20)

    select_fields = (
        "id, original_filename, filed_name, document_type, client_name, "
        "counterparty, document_date, matter_reference, amount, currency, "
        "summary, folder_path, status, tags, uploaded_at, extracted_text"
    )

    query = (
        sb.table("documents")
        .select(select_fields)
        .eq("organisation_id", org_id)
        .not_.in_("status", EXCLUDED_STATUSES)
        .order("uploaded_at", desc=True)
        .limit(limit)
    )

    # Apply time filters
    time_cutoff = _resolve_time_filter(time_filter)
    if time_cutoff:
        query = query.gte("uploaded_at", time_cutoff)
    if date_from:
        query = query.gte("document_date", date_from)
    if date_to:
        query = query.lte("document_date", date_to)

    # If no search terms, just return by recency (for "recent docs", "what was filed today")
    if not search_terms:
        try:
            result = query.execute()
            return result.data or []
        except Exception:
            logger.exception("Search execution failed")
            return []

    # Build OR filter across multiple fields for each search term
    or_conditions = []
    for term in search_terms:
        escaped = term.replace("%", "").replace("_", "\\_")
        pattern = f"%{escaped}%"
        or_conditions.extend([
            f"client_name.ilike.{pattern}",
            f"counterparty.ilike.{pattern}",
            f"summary.ilike.{pattern}",
            f"document_type.ilike.{pattern}",
            f"matter_reference.ilike.{pattern}",
            f"original_filename.ilike.{pattern}",
            f"filed_name.ilike.{pattern}",
            f"extracted_text.ilike.{pattern}",
        ])

    query = query.or_(",".join(or_conditions))

    try:
        result = query.execute()
        return result.data or []
    except Exception:
        logger.exception("Smart search failed")
        return []


def _resolve_time_filter(time_filter: Optional[str]) -> Optional[str]:
    """Convert a time filter keyword to an ISO datetime string."""
    if not time_filter:
        return None

    now = datetime.utcnow()
    if time_filter == "today":
        cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif time_filter == "this_week":
        cutoff = now - timedelta(days=now.weekday())
        cutoff = cutoff.replace(hour=0, minute=0, second=0, microsecond=0)
    elif time_filter == "this_month":
        cutoff = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif time_filter == "last_month":
        first_of_month = now.replace(day=1)
        cutoff = (first_of_month - timedelta(days=1)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif time_filter == "recent":
        cutoff = now - timedelta(days=7)
    else:
        return None

    return cutoff.isoformat()


def _synthesise_response(question: str, documents: list[dict]) -> str:
    """Use Claude to generate a conversational response from matching documents."""
    # Build document summaries for the prompt
    summaries = []
    for i, doc in enumerate(documents, 1):
        client = doc.get("client_name") or "Unknown"
        doc_type = (doc.get("document_type") or "other").replace("_", " ").title()
        doc_date = doc.get("document_date") or "no date"
        amount = doc.get("amount")
        currency = doc.get("currency") or "GBP"
        ref = doc.get("matter_reference") or ""
        summary = doc.get("summary") or ""
        counterparty = doc.get("counterparty") or ""
        filename = doc.get("filed_name") or doc.get("original_filename") or ""
        uploaded = doc.get("uploaded_at") or ""
        extracted = doc.get("extracted_text") or ""

        currency_symbols = {"GBP": "\u00a3", "USD": "$", "EUR": "\u20ac"}
        symbol = currency_symbols.get(currency, currency + " ")

        entry = f"Document {i}:\n"
        entry += f"  Filename: {filename}\n"
        entry += f"  Type: {doc_type}\n"
        entry += f"  Client: {client}\n"
        if counterparty:
            entry += f"  Counterparty: {counterparty}\n"
        entry += f"  Date: {doc_date}\n"
        if amount is not None:
            entry += f"  Amount: {symbol}{amount:,.2f}\n"
        if ref:
            entry += f"  Reference: {ref}\n"
        if summary:
            entry += f"  Summary: {summary}\n"
        if uploaded:
            entry += f"  Uploaded: {uploaded}\n"
        # Include extracted text (truncated) for deeper answers
        if extracted:
            entry += f"  Full text excerpt: {extracted[:3000]}\n"

        summaries.append(entry)

    doc_summaries = "\n".join(summaries)

    try:
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

        message = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": SYNTHESISE_PROMPT.format(
                question=question,
                document_summaries=doc_summaries,
            )}],
        )

        claude_response = message.content[0].text.strip()
    except Exception:
        logger.exception("Response synthesis failed")
        claude_response = _fallback_format(documents)

    # Append clickable document reference links
    claude_response += _build_document_links(documents)
    return claude_response


def _build_document_links(documents: list[dict]) -> str:
    """Build an HTML links section for referenced documents."""
    if not documents:
        return ""

    links = "\n\n---\nReferenced documents:"
    for doc in documents:
        name = doc.get("filed_name") or doc.get("original_filename") or "Unknown"
        doc_type = (doc.get("document_type") or "").replace("_", " ").title()
        doc_date = doc.get("document_date") or ""
        doc_id = doc["id"]
        label = f"{doc_type} — {name}" if doc_type else name
        links += f'\n<a href="/document/{doc_id}" target="_blank">{label}</a>'
        if doc_date:
            links += f" ({doc_date})"
    return links


def _fallback_format(documents: list[dict]) -> str:
    """Simple fallback formatting if Claude synthesis fails."""
    lines = []
    for doc in documents:
        client = doc.get("client_name") or "Unknown"
        doc_type = (doc.get("document_type") or "other").replace("_", " ").title()
        summary = doc.get("summary") or ""
        name = doc.get("filed_name") or doc.get("original_filename") or ""
        line = f"{doc_type} - {client}"
        if name:
            line = f"{name}\n  {line}"
        if summary:
            line += f"\n  {summary}"
        lines.append(line)
    return "\n\n".join(lines)
