-- Migration 003: Add content_hash for hash-based duplicate detection

-- Add content_hash column to documents
ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_hash TEXT;

-- Index for fast duplicate lookups by hash within an organisation
CREATE INDEX IF NOT EXISTS idx_documents_content_hash ON documents(organisation_id, content_hash);

-- Drop the old filename+size index (replaced by hash-based detection)
DROP INDEX IF EXISTS idx_documents_duplicate;
