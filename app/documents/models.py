from pydantic import BaseModel
from datetime import date, datetime
from typing import Optional


class DocumentCreate(BaseModel):
    organisation_id: str
    uploaded_by: str
    original_filename: str
    file_size_bytes: int
    mime_type: str
    original_storage_path: str


class ClassificationResult(BaseModel):
    document_type: str = "other"
    confidence: float = 0.0
    client_name: Optional[str] = None
    counterparty: Optional[str] = None
    document_date: Optional[date] = None
    matter_reference: Optional[str] = None
    amount: Optional[float] = None
    currency: str = "GBP"
    summary: Optional[str] = None
    tags: list[str] = []
    suggested_filename: Optional[str] = None


class DocumentResponse(BaseModel):
    id: str
    organisation_id: str
    original_filename: str
    file_size_bytes: Optional[int] = None
    mime_type: Optional[str] = None
    document_type: Optional[str] = None
    confidence_score: Optional[float] = None
    client_name: Optional[str] = None
    counterparty: Optional[str] = None
    document_date: Optional[str] = None
    matter_reference: Optional[str] = None
    amount: Optional[float] = None
    currency: Optional[str] = "GBP"
    summary: Optional[str] = None
    tags: Optional[list[str]] = None
    filed_name: Optional[str] = None
    folder_path: Optional[str] = None
    status: str = "processing"
    error_message: Optional[str] = None
    uploaded_at: Optional[str] = None
    processed_at: Optional[str] = None
    filed_at: Optional[str] = None
    content_hash: Optional[str] = None
    duplicate_of: Optional[str] = None


class DocumentStats(BaseModel):
    total_documents: int = 0
    processed_today: int = 0
    by_type: dict[str, int] = {}
    by_status: dict[str, int] = {}


class SearchQuery(BaseModel):
    q: str
    document_type: Optional[str] = None
    client_name: Optional[str] = None
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    limit: int = 50
    offset: int = 0
