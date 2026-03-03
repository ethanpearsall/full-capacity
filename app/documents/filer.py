import re
import os
import logging
from datetime import date
from typing import Optional

from app.documents.models import ClassificationResult

logger = logging.getLogger(__name__)


def sanitise_path_segment(segment: str) -> str:
    """Sanitise a string for use in file paths. Replace spaces with underscores,
    remove special characters."""
    if not segment:
        return ""
    # Replace spaces and common separators with underscores
    segment = re.sub(r"[\s\-]+", "_", segment)
    # Remove anything that isn't alphanumeric, underscore, or period
    segment = re.sub(r"[^\w.]", "", segment)
    # Collapse multiple underscores
    segment = re.sub(r"_+", "_", segment)
    # Strip leading/trailing underscores
    segment = segment.strip("_")
    return segment


def generate_filed_path(
    classification: ClassificationResult,
    org_name: str,
    original_filename: str,
    upload_date: Optional[date] = None,
) -> tuple[str, str]:
    """
    Generate the folder path and filename for filing.

    Returns (folder_path, filed_filename).

    Folder hierarchy:
    - If client_name found: /{org}/{client_name}/{doc_type}/{year}/
    - If client_name null but counterparty exists: /{org}/{counterparty}/{doc_type}/{year}/
    - If both null: /{org}/_Unfiled/{doc_type}/{year}/
    """
    # Get file extension from original filename
    _, ext = os.path.splitext(original_filename)
    if not ext:
        ext = ".pdf"
    ext = ext.lower()

    # Determine components
    safe_org = sanitise_path_segment(org_name) or "Default_Org"
    safe_doc_type = sanitise_path_segment(classification.document_type) if classification.document_type else "Other"

    # Capitalise document type for folder name
    safe_doc_type_folder = safe_doc_type.replace("_", " ").title().replace(" ", "_")

    # Determine the entity name with fallback hierarchy
    client_name = classification.client_name
    counterparty = classification.counterparty
    entity_name = None

    if client_name:
        entity_name = sanitise_path_segment(client_name)
    elif counterparty:
        entity_name = sanitise_path_segment(counterparty)

    folder_entity = entity_name or "_Unfiled"

    # Determine date
    doc_date = classification.document_date
    if doc_date is None:
        doc_date = upload_date or date.today()
    elif isinstance(doc_date, str):
        try:
            doc_date = date.fromisoformat(doc_date)
        except ValueError:
            doc_date = upload_date or date.today()

    year = str(doc_date.year)
    date_str = doc_date.isoformat()

    # Build folder path
    folder_path = f"/{safe_org}/{folder_entity}/{safe_doc_type_folder}/{year}"

    # Build filename — never put "Uncategorised" or "None" in the filename
    name_part = entity_name or sanitise_path_segment(os.path.splitext(original_filename)[0])
    parts = [name_part, safe_doc_type, date_str]

    if classification.matter_reference:
        safe_ref = sanitise_path_segment(classification.matter_reference)
        if safe_ref:
            parts.append(safe_ref)

    filed_filename = "_".join(p for p in parts if p) + ext

    logger.info("Generated filed path: %s/%s", folder_path, filed_filename)
    return folder_path, filed_filename
