import json
import logging
import re
import traceback
from typing import Optional

import anthropic

from app.config import settings
from app.documents.models import ClassificationResult

logger = logging.getLogger(__name__)


def _log(msg: str) -> None:
    """Print to stdout (always visible in Railway) and also log."""
    print(f"[CLASSIFIER] {msg}", flush=True)
    logger.info(msg)

CLASSIFICATION_PROMPT = """You are an expert document analyst for UK professional services firms (law firms, accountancies, bookkeepers, tax consultants, quantity surveyors).

Your job is to classify this document AND extract every possible metadata field. Be thorough — clients pay for accuracy.

Analyse the document text below and return ONLY a JSON object (no other text, no markdown) with these fields:

{
    "document_type": "invoice|contract|letter|tax_return|receipt|bank_statement|filing|correspondence|report|annual_accounts|payslip|hmrc_notice|court_document|deed|lease|will|company_filing|vat_return|management_accounts|engagement_letter|other",
    "confidence": 0.0-1.0,
    "client_name": "REQUIRED — the person or company this document is about. Look for: the addressee, the 'RE:' line, the account holder, the billable party, the subject of the document. For employment contracts, this is the EMPLOYER. For invoices, this is who is being BILLED. For HMRC notices, this is the COMPANY named. Never return null if there is any name in the document.",
    "counterparty": "The other party — for invoices: the firm issuing it. For contracts: the other signatory. For HMRC letters: HMRC. For court docs: opposing party.",
    "document_date": "YYYY-MM-DD — look for: 'Date:', 'Dated:', 'Invoice Date:', 'Date of Agreement:', letter date at top. Use the document's own date, NOT today. If multiple dates, use the primary/header date.",
    "matter_reference": "Look for ANY of: 'Ref:', 'Our Ref:', 'Your Ref:', 'Reference:', 'Matter:', 'Case No:', 'File:', 'Job No:', 'Invoice No:', 'Claim No:', 'UTR:', 'Account No:'. Extract ALL references found, separated by comma.",
    "amount": "numeric value only (no currency symbol). For invoices: the TOTAL amount. For contracts: salary or contract value. For tax: tax amount due. null if not financial.",
    "currency": "GBP|USD|EUR — infer from currency signs or context. Default GBP for UK documents.",
    "summary": "2-3 sentences. Be specific — include names, amounts, dates, and what action the document requires.",
    "tags": ["at least 3 relevant tags for searching"],
    "suggested_filename": "ClientName_DocType_YYYY-MM-DD_Reference.ext — use the actual client name and reference you extracted"
}

CRITICAL RULES:
1. NEVER return null for client_name if ANY person or company name appears in the document
2. NEVER return null for document_date if ANY date appears in the document header/metadata
3. NEVER return null for matter_reference if ANY reference number pattern appears
4. For the summary, always mention specific names, amounts, and dates — not generic descriptions
5. Return ONLY the JSON object. No explanation. No markdown code blocks. Just the raw JSON.

DOCUMENT TEXT:
"""

EXTRACTION_PROMPT = """The following document was classified as {document_type} but metadata extraction was incomplete.

Please re-examine the text carefully and extract ONLY these fields as a JSON object (no other text, no markdown):

{{
    "client_name": "the main person or company this document is about",
    "counterparty": "the other party involved",
    "document_date": "YYYY-MM-DD",
    "matter_reference": "any reference numbers found",
    "amount": null,
    "currency": "GBP"
}}

Look VERY carefully at the document header, addressee lines, RE: lines, and any reference numbers.

DOCUMENT TEXT:
{text}"""

MAX_TEXT_LENGTH = 15000  # Limit text sent to Claude to control costs

MODEL = "claude-sonnet-4-20250514"


def classify_document(text: str, original_filename: str = "") -> ClassificationResult:
    """
    Classify a document using Claude AI and extract metadata.

    Uses a two-pass approach: first classifies and extracts metadata,
    then does a focused extraction pass if confidence is low or client_name is missing.
    """
    _log(f"classify_document called: filename={original_filename}, text_length={len(text) if text else 0}")

    if not text or not text.strip():
        _log("Empty text provided — returning empty fallback")
        return ClassificationResult(
            document_type="other",
            confidence=0.0,
            summary="No text could be extracted from this document.",
        )

    # Check API key is available
    api_key = settings.ANTHROPIC_API_KEY
    if not api_key:
        _log("ERROR: ANTHROPIC_API_KEY is empty! Cannot classify.")
        return ClassificationResult(
            document_type="other",
            confidence=0.1,
            summary="Classification unavailable: API key not configured.",
        )
    _log(f"API key present ({len(api_key)} chars)")

    # Truncate text if too long
    truncated_text = text[:MAX_TEXT_LENGTH]
    if len(text) > MAX_TEXT_LENGTH:
        truncated_text += "\n\n[... text truncated ...]"

    prompt = CLASSIFICATION_PROMPT + truncated_text
    _log(f"Prompt built: {len(prompt)} chars, model={MODEL}")

    # First pass: full classification
    result = None
    try:
        result = _call_claude(prompt)
        if result:
            _log(f"First pass SUCCESS: type={result.document_type} confidence={result.confidence:.2f} client={result.client_name}")
        else:
            _log("First pass returned None (JSON parse failure)")
    except Exception as e:
        _log(f"First pass EXCEPTION: {type(e).__name__}: {e}")
        print(f"[CLASSIFIER] First pass traceback:\n{traceback.format_exc()}", flush=True)

    if result is None:
        # Retry once on failure
        try:
            _log("Retrying classification (attempt 2)...")
            result = _call_claude(prompt)
            if result:
                _log(f"Retry SUCCESS: type={result.document_type}")
            else:
                _log("Retry returned None")
        except Exception as e:
            _log(f"Retry EXCEPTION: {type(e).__name__}: {e}")
            print(f"[CLASSIFIER] Retry traceback:\n{traceback.format_exc()}", flush=True)

    if result is None:
        _log("ALL classification attempts failed — returning fallback")
        return ClassificationResult(
            document_type="other",
            confidence=0.1,
            summary="Automatic classification was unable to process this document.",
        )

    # Second pass: focused extraction if metadata is incomplete
    needs_second_pass = (
        result.confidence < 0.7
        or (result.client_name is None and len(text.strip()) > 200)
    )

    if needs_second_pass:
        _log(f"Running second-pass extraction (confidence={result.confidence:.2f}, client={result.client_name})")
        try:
            extraction = _run_extraction_pass(result.document_type, truncated_text)
            if extraction:
                result = _merge_extraction(result, extraction)
                _log(f"Second pass merged: client={result.client_name}, date={result.document_date}, ref={result.matter_reference}")
        except Exception as e:
            _log(f"Second-pass extraction EXCEPTION: {type(e).__name__}: {e}")
            print(f"[CLASSIFIER] Second-pass traceback:\n{traceback.format_exc()}", flush=True)

    return result


def _run_extraction_pass(document_type: str, text: str) -> Optional[dict]:
    """Run a focused extraction pass to fill in missing metadata."""
    prompt = EXTRACTION_PROMPT.format(document_type=document_type, text=text)
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    logger.info("Calling Claude API for extraction pass with model %s", MODEL)
    message = client.messages.create(
        model=MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = message.content[0].text
    return _parse_json_response(response_text)


def _merge_extraction(result: ClassificationResult, extraction: dict) -> ClassificationResult:
    """Merge second-pass extraction into the classification result, filling nulls."""
    data = result.model_dump()

    for field in ("client_name", "counterparty", "document_date", "matter_reference", "amount", "currency"):
        if data.get(field) is None and extraction.get(field) is not None:
            data[field] = extraction[field]

    return _build_result(data)


def _call_claude(prompt: str) -> Optional[ClassificationResult]:
    """Make API call to Claude and parse the response."""
    _log(f"Creating Anthropic client...")
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    _log(f"Sending request to Claude API (model={MODEL})...")
    message = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = message.content[0].text
    _log(f"Claude API response received ({len(response_text)} chars)")
    parsed = _parse_json_response(response_text)

    if parsed is None:
        _log(f"JSON parse FAILED. Response preview: {response_text[:300]}")
        return None

    return _build_result(parsed)


def _parse_json_response(response_text: str) -> Optional[dict]:
    """Parse JSON from Claude response, handling markdown code blocks."""
    # Try stripping whitespace first
    cleaned = response_text.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Try to extract JSON from markdown code blocks
    patterns = [
        r"```json\s*\n?(.*?)\n?\s*```",
        r"```\s*\n?(.*?)\n?\s*```",
        r"\{.*\}",
    ]

    for pattern in patterns:
        match = re.search(pattern, cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1) if match.lastindex else match.group(0))
            except json.JSONDecodeError:
                continue

    logger.error("Could not parse JSON from response: %s", cleaned[:500])
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
