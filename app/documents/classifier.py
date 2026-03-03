import json
import logging
import re
from typing import Optional

import anthropic

from app.config import settings
from app.documents.models import ClassificationResult

logger = logging.getLogger(__name__)

CLASSIFICATION_PROMPT = """You are a document classification and metadata extraction system for professional services firms (law firms, accountancies, bookkeepers, tax consultants).

Analyse the following document text and return a JSON response with:

{
    "document_type": "one of: invoice, contract, letter, tax_return, receipt, bank_statement, filing, correspondence, report, annual_accounts, payslip, hmrc_notice, court_document, deed, lease, will, company_filing, vat_return, management_accounts, engagement_letter, other",
    "confidence": 0.0-1.0,
    "client_name": "the person or company this document relates to (the client of the firm)",
    "counterparty": "the other party involved if applicable (e.g., HMRC, opposing solicitor, supplier)",
    "document_date": "YYYY-MM-DD format, the date ON the document (not today)",
    "matter_reference": "any case number, matter reference, job number, file reference found",
    "amount": null or numeric value if financial document,
    "currency": "GBP/USD/EUR",
    "summary": "2-3 sentence plain English summary of what this document is",
    "tags": ["tag1", "tag2", "tag3"],
    "suggested_filename": "ClientName_DocType_YYYY-MM-DD_Reference.pdf"
}

Important rules:
- If you can't determine a field, set it to null
- For client_name, look for the addressee, the "RE:" line, the account holder, or the subject
- For dates, look for document date, invoice date, letter date — NOT any random date in the body
- For matter_reference, look for patterns like "Ref:", "Our Ref:", "Your Ref:", "Matter:", "Case No:", "File:"
- Be conservative with confidence — only go above 0.9 if you're very certain
- Tags should be useful for searching later
- Return ONLY valid JSON, no additional text

DOCUMENT TEXT:
__DOCUMENT_TEXT__"""

MAX_TEXT_LENGTH = 15000  # Limit text sent to Claude to control costs


def classify_document(text: str, original_filename: str = "") -> ClassificationResult:
    """
    Classify a document using Claude AI and extract metadata.

    Returns a ClassificationResult with document type, metadata, and suggested filename.
    """
    if not text or not text.strip():
        logger.warning("Empty text provided for classification")
        return ClassificationResult(
            document_type="other",
            confidence=0.0,
            summary="No text could be extracted from this document.",
        )

    # Truncate text if too long
    truncated_text = text[:MAX_TEXT_LENGTH]
    if len(text) > MAX_TEXT_LENGTH:
        truncated_text += "\n\n[... text truncated ...]"

    prompt = CLASSIFICATION_PROMPT.replace("__DOCUMENT_TEXT__", truncated_text)

    try:
        result = _call_claude(prompt)
        if result:
            return result
        logger.warning("First classification attempt returned None (JSON parse failure)")
    except Exception:
        logger.exception("First classification attempt failed")

    # Retry once on failure
    try:
        logger.info("Retrying classification...")
        result = _call_claude(prompt)
        if result:
            return result
        logger.warning("Second classification attempt returned None (JSON parse failure)")
    except Exception:
        logger.exception("Second classification attempt failed")

    # Fallback — return basic classification
    logger.warning("All classification attempts failed, returning fallback result")
    return ClassificationResult(
        document_type="other",
        confidence=0.1,
        summary="Automatic classification was unable to process this document.",
    )


def _call_claude(prompt: str) -> Optional[ClassificationResult]:
    """Make API call to Claude and parse the response."""
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    logger.info("Calling Claude API with model claude-3-5-haiku-latest")
    message = client.messages.create(
        model="claude-3-5-haiku-latest",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = message.content[0].text
    logger.info("Claude API response received (%d chars)", len(response_text))
    parsed = _parse_json_response(response_text)

    if parsed is None:
        logger.error("Failed to parse JSON from Claude response: %.500s", response_text)
        return None

    return _build_result(parsed)


def _parse_json_response(response_text: str) -> Optional[dict]:
    """Parse JSON from Claude response, handling markdown code blocks."""
    # Try direct JSON parse first
    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        pass

    # Try to extract JSON from markdown code blocks
    patterns = [
        r"```json\s*\n?(.*?)\n?\s*```",
        r"```\s*\n?(.*?)\n?\s*```",
        r"\{.*\}",
    ]

    for pattern in patterns:
        match = re.search(pattern, response_text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1) if match.lastindex else match.group(0))
            except json.JSONDecodeError:
                continue

    logger.error("Could not parse JSON from response: %s", response_text[:500])
    return None


def _build_result(data: dict) -> ClassificationResult:
    """Build ClassificationResult from parsed JSON dict, handling missing/bad fields."""
    try:
        return ClassificationResult(
            document_type=data.get("document_type", "other"),
            confidence=float(data.get("confidence", 0.5)),
            client_name=data.get("client_name"),
            counterparty=data.get("counterparty"),
            document_date=data.get("document_date"),
            matter_reference=data.get("matter_reference"),
            amount=data.get("amount"),
            currency=data.get("currency", "GBP"),
            summary=data.get("summary"),
            tags=data.get("tags", []),
            suggested_filename=data.get("suggested_filename"),
        )
    except Exception:
        logger.exception("Error building ClassificationResult from data: %s", data)
        return ClassificationResult(
            document_type=data.get("document_type", "other"),
            confidence=float(data.get("confidence", 0.3)),
            summary=data.get("summary"),
        )
