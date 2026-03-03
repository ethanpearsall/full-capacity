import re
import os
import logging
from datetime import date, datetime
from typing import Optional

from app.documents.models import ClassificationResult

logger = logging.getLogger(__name__)


def sanitise_path_segment(segment: str) -> str:
    """Sanitise a string for use in file paths. Replace spaces with underscores,
    remove special characters, lowercase."""
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

    Folder structure: /{org_name}/{client_name}/{document_type}/{year}/
    Filename format: {client_name}_{document_type}_{date}_{reference}.{ext}
    """
    # Get file extension from original filename
    _, ext = os.path.splitext(original_filename)
    if not ext:
        ext = ".pdf"
    ext = ext.lower()

    # Determine components
    safe_org = sanitise_path_segment(org_name) or "Default_Org"
    safe_client = sanitise_path_segment(classification.client_name) if classification.client_name else "Uncategorised"
    safe_doc_type = sanitise_path_segment(classification.document_type) if classification.document_type else "Other"

    # Capitalise document type for folder name
    safe_doc_type_folder = safe_doc_type.replace("_", " ").title().replace(" ", "_")

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
    folder_path = f"/{safe_org}/{safe_client}/{safe_doc_type_folder}/{year}"

    # Build filename
    parts = [safe_client, safe_doc_type, date_str]
    if classification.matter_reference:
        safe_ref = sanitise_path_segment(classification.matter_reference)
        if safe_ref:
            parts.append(safe_ref)

    filed_filename = "_".join(parts) + ext

    logger.info("Generated filed path: %s/%s", folder_path, filed_filename)
    return folder_path, filed_filename
