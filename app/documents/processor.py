import io
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def extract_text(file_bytes: bytes, mime_type: str) -> str:
    """
    Extract text from uploaded document.

    Supports PDFs (text-based and scanned), images, and Word documents.
    """
    try:
        # Strip MIME parameters (e.g. 'application/pdf; name="file.pdf"' -> 'application/pdf')
        base_mime = mime_type.split(";")[0].strip().lower() if mime_type else ""
        if base_mime == "application/pdf":
            return _extract_from_pdf(file_bytes)
        elif base_mime in (
            "image/jpeg",
            "image/png",
            "image/tiff",
            "image/bmp",
            "image/gif",
        ):
            return _extract_from_image(file_bytes)
        elif base_mime in (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/msword",
        ):
            return _extract_from_docx(file_bytes)
        else:
            logger.warning("Unsupported mime type for text extraction: %s", mime_type)
            return ""
    except Exception as e:
        logger.error("Text extraction failed: %s", str(e))
        raise


def _extract_from_pdf(file_bytes: bytes) -> str:
    """Extract text from PDF — tries embedded text first, falls back to OCR."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(file_bytes))
    text_parts = []
    for page in reader.pages:
        page_text = page.extract_text() or ""
        text_parts.append(page_text)

    text = "\n".join(text_parts).strip()

    # If extracted text is too short, assume scanned PDF and use OCR
    if len(text) < 50:
        logger.info("PDF has little embedded text (%d chars), falling back to OCR", len(text))
        return _ocr_pdf(file_bytes)

    return text


def _ocr_pdf(file_bytes: bytes) -> str:
    """Convert PDF pages to images and OCR each page."""
    from pdf2image import convert_from_bytes
    import pytesseract

    images = convert_from_bytes(file_bytes)
    text_parts = []
    for i, image in enumerate(images):
        page_text = pytesseract.image_to_string(image)
        text_parts.append(page_text)
        logger.debug("OCR page %d: %d chars", i + 1, len(page_text))

    return "\n\n".join(text_parts).strip()


def _extract_from_image(file_bytes: bytes) -> str:
    """OCR an image file directly."""
    from PIL import Image
    import pytesseract

    image = Image.open(io.BytesIO(file_bytes))
    text = pytesseract.image_to_string(image)
    return text.strip()


def _extract_from_docx(file_bytes: bytes) -> str:
    """Extract text from a Word document."""
    from docx import Document

    doc = Document(io.BytesIO(file_bytes))
    text_parts = []
    for paragraph in doc.paragraphs:
        if paragraph.text.strip():
            text_parts.append(paragraph.text)

    # Also extract text from tables
    for table in doc.tables:
        for row in table.rows:
            row_text = "\t".join(cell.text.strip() for cell in row.cells if cell.text.strip())
            if row_text:
                text_parts.append(row_text)

    return "\n".join(text_parts).strip()
