-- Nylas Email Connections & Smart Attachment Filtering
-- Migration 005: email_connections, attachment_filters

-- Nylas-connected email accounts
CREATE TABLE email_connections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organisation_id UUID REFERENCES organisations(id),
    provider TEXT NOT NULL,  -- 'google', 'microsoft'
    email_address TEXT NOT NULL,
    grant_id TEXT NOT NULL,
    status TEXT DEFAULT 'active',  -- active, paused, disconnected, error
    last_sync_at TIMESTAMPTZ,
    error_message TEXT,
    connected_at TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_email_connections_org ON email_connections(organisation_id);
CREATE INDEX idx_email_connections_grant ON email_connections(grant_id);
CREATE UNIQUE INDEX idx_email_connections_email_org ON email_connections(email_address, organisation_id);

-- Smart attachment filter rules
CREATE TABLE attachment_filters (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organisation_id UUID REFERENCES organisations(id),
    filter_type TEXT NOT NULL,  -- 'skip_inline', 'skip_content_type', 'skip_filename_pattern', 'skip_size_under', 'skip_size_over'
    filter_value TEXT NOT NULL,  -- e.g. 'image/*', '*.png', '1024', '26214400'
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_attachment_filters_org ON attachment_filters(organisation_id);

-- Update email_ingestions source to support 'nylas'
-- (source column already accepts any TEXT, just documenting the new value)
COMMENT ON COLUMN email_ingestions.source IS 'webhook, imap, or nylas';

-- Add connection reference to email_ingestions
ALTER TABLE email_ingestions ADD COLUMN IF NOT EXISTS email_connection_id UUID REFERENCES email_connections(id);
