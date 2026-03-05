-- Migration 007: Per-user email, client matters, audit trail
-- Run this in Supabase SQL editor BEFORE deploying code changes

-- 1. Add email connection fields to users
ALTER TABLE users ADD COLUMN IF NOT EXISTS nylas_grant_id TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS nylas_email TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS email_connected BOOLEAN DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS email_connected_at TIMESTAMPTZ;

-- 2. Add user_id to email_ingestions
ALTER TABLE email_ingestions ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id);

-- 3. Update daily_summaries to be per-user
ALTER TABLE daily_summaries ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id);
ALTER TABLE daily_summaries DROP CONSTRAINT IF EXISTS daily_summaries_organisation_id_summary_date_key;
ALTER TABLE daily_summaries ADD CONSTRAINT daily_summaries_user_date_key UNIQUE(user_id, summary_date);

-- 4. Create user_todos table
CREATE TABLE IF NOT EXISTS user_todos (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    organisation_id UUID NOT NULL REFERENCES organisations(id),
    summary_id UUID REFERENCES daily_summaries(id),
    task TEXT NOT NULL,
    source_email_subject TEXT,
    source_email_from TEXT,
    priority TEXT DEFAULT 'medium' CHECK (priority IN ('high', 'medium', 'low')),
    due_hint TEXT,
    completed BOOLEAN DEFAULT FALSE,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE user_todos ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users can manage own todos" ON user_todos
    FOR ALL USING (user_id = auth.uid());

-- 5. Create client_matters table
CREATE TABLE IF NOT EXISTS client_matters (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organisation_id UUID NOT NULL REFERENCES organisations(id),
    client_name TEXT NOT NULL,
    matter_name TEXT,
    matter_reference TEXT,
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'archived', 'closed')),
    created_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(organisation_id, matter_reference)
);

ALTER TABLE client_matters ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users can view own org matters" ON client_matters
    FOR SELECT USING (
        organisation_id IN (
            SELECT organisation_id FROM users WHERE id = auth.uid()
        )
    );
CREATE POLICY "Users can create matters in own org" ON client_matters
    FOR INSERT WITH CHECK (
        organisation_id IN (
            SELECT organisation_id FROM users WHERE id = auth.uid()
        )
    );
CREATE POLICY "Users can update own org matters" ON client_matters
    FOR UPDATE USING (
        organisation_id IN (
            SELECT organisation_id FROM users WHERE id = auth.uid()
        )
    );

-- 6. Add client_matter_id to documents
ALTER TABLE documents ADD COLUMN IF NOT EXISTS client_matter_id UUID REFERENCES client_matters(id);

-- 7. Create audit_log table
CREATE TABLE IF NOT EXISTS audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organisation_id UUID NOT NULL REFERENCES organisations(id),
    user_id UUID REFERENCES users(id),
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id UUID,
    details JSONB DEFAULT '{}',
    ip_address TEXT,
    user_agent TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_log_org_created ON audit_log(organisation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_entity ON audit_log(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_user ON audit_log(user_id, created_at DESC);

ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users can view own org audit log" ON audit_log
    FOR SELECT USING (
        organisation_id IN (
            SELECT organisation_id FROM users WHERE id = auth.uid()
        )
    );
