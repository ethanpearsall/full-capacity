-- Email Ingestion System tables
-- Migration 004: email_ingestions, email_attachments, imap_configs

-- Email ingestion records
CREATE TABLE email_ingestions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organisation_id UUID REFERENCES organisations(id),
    message_id TEXT,
    from_address TEXT NOT NULL,
    from_name TEXT,
    to_address TEXT NOT NULL,
    subject TEXT,
    body_preview TEXT,
    received_at TIMESTAMPTZ DEFAULT NOW(),
    source TEXT NOT NULL,  -- 'webhook' or 'imap'
    attachment_count INTEGER DEFAULT 0,
    processed_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'received',  -- received, processing, completed, failed, partial
    error_message TEXT,
    raw_headers JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_email_ingestions_org ON email_ingestions(organisation_id);
CREATE INDEX idx_email_ingestions_message_id ON email_ingestions(message_id);
CREATE INDEX idx_email_ingestions_from ON email_ingestions(from_address);
CREATE INDEX idx_email_ingestions_status ON email_ingestions(status);

-- Email attachment tracking
CREATE TABLE email_attachments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email_ingestion_id UUID REFERENCES email_ingestions(id) ON DELETE CASCADE,
    document_id UUID REFERENCES documents(id),
    original_filename TEXT NOT NULL,
    content_type TEXT,
    file_size_bytes INTEGER,
    processing_status TEXT DEFAULT 'pending',  -- pending, processing, completed, failed, skipped
    skip_reason TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_email_attachments_ingestion ON email_attachments(email_ingestion_id);
CREATE INDEX idx_email_attachments_document ON email_attachments(document_id);

-- IMAP configuration per organisation
CREATE TABLE imap_configs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organisation_id UUID REFERENCES organisations(id) UNIQUE,
    host TEXT NOT NULL,
    port INTEGER DEFAULT 993,
    username TEXT NOT NULL,
    password_encrypted TEXT NOT NULL,
    folder TEXT DEFAULT 'INBOX',
    use_ssl BOOLEAN DEFAULT TRUE,
    poll_interval_minutes INTEGER DEFAULT 5,
    last_polled_at TIMESTAMPTZ,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Add email source columns to existing documents table
ALTER TABLE documents ADD COLUMN IF NOT EXISTS email_ingestion_id UUID REFERENCES email_ingestions(id);
ALTER TABLE documents ADD COLUMN IF NOT EXISTS email_from TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS email_subject TEXT;

-- Sender whitelist table
CREATE TABLE email_sender_whitelist (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organisation_id UUID REFERENCES organisations(id),
    address_or_domain TEXT NOT NULL,  -- e.g. 'john@example.com' or 'example.com'
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_email_whitelist_org ON email_sender_whitelist(organisation_id);
