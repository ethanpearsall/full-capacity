-- Migration 002: Add Telegram chat, account linking, and duplicate detection support

-- Duplicate detection: add duplicate_of column to documents
ALTER TABLE documents ADD COLUMN IF NOT EXISTS duplicate_of UUID REFERENCES documents(id);

-- Telegram account linking
CREATE TABLE IF NOT EXISTS telegram_links (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id UUID REFERENCES users(id) NOT NULL,
    organisation_id UUID REFERENCES organisations(id) NOT NULL,
    telegram_chat_id BIGINT UNIQUE NOT NULL,
    telegram_username TEXT,
    linked_at TIMESTAMPTZ DEFAULT NOW(),
    is_active BOOLEAN DEFAULT TRUE
);

-- Chat history (for context and audit trail)
CREATE TABLE IF NOT EXISTS chat_messages (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    telegram_chat_id BIGINT,
    organisation_id UUID REFERENCES organisations(id),
    role TEXT NOT NULL,  -- 'user' or 'assistant'
    content TEXT NOT NULL,
    documents_referenced UUID[],  -- document IDs mentioned in response
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_telegram_links_chat ON telegram_links(telegram_chat_id);
CREATE INDEX IF NOT EXISTS idx_telegram_links_active ON telegram_links(telegram_chat_id, is_active) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_chat_messages_chat ON chat_messages(telegram_chat_id);
CREATE INDEX IF NOT EXISTS idx_chat_messages_org ON chat_messages(organisation_id);
CREATE INDEX IF NOT EXISTS idx_documents_duplicate ON documents(organisation_id, original_filename, file_size_bytes);
