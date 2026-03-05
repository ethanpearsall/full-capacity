-- Daily email summaries table
CREATE TABLE IF NOT EXISTS daily_summaries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organisation_id UUID NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
    generated_by UUID REFERENCES users(id) ON DELETE SET NULL,
    summary_date DATE NOT NULL,
    email_count INTEGER DEFAULT 0,
    todo_count INTEGER DEFAULT 0,
    summary_data JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(organisation_id, summary_date)
);

CREATE INDEX idx_daily_summaries_org_date
    ON daily_summaries(organisation_id, summary_date);

-- Enable RLS
ALTER TABLE daily_summaries ENABLE ROW LEVEL SECURITY;

-- Policy: users can see their org's summaries
CREATE POLICY "Users can view own org summaries" ON daily_summaries
    FOR SELECT USING (
        organisation_id IN (
            SELECT organisation_id FROM users WHERE id = auth.uid()
        )
    );
