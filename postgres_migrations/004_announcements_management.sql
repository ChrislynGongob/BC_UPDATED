-- Manually reviewed migration for announcement publishing, audience, images, and soft archive.
-- Do not run automatically against production.
ALTER TABLE announcements
    ADD COLUMN IF NOT EXISTS is_published INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS audience TEXT NOT NULL DEFAULT 'all',
    ADD COLUMN IF NOT EXISTS is_archived INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS archived_at TEXT,
    ADD COLUMN IF NOT EXISTS image_filename TEXT,
    ADD COLUMN IF NOT EXISTS image_stored_name TEXT;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'announcements_audience_allowed'
    ) THEN
        ALTER TABLE announcements
            ADD CONSTRAINT announcements_audience_allowed
            CHECK (audience IN ('all', 'resident', 'staff'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_announcements_active_feed
    ON announcements (is_published, is_archived, audience, created_at DESC);
