-- Additive metadata for account archiving and searchable security audit history.
-- Review and apply manually as a database owner; this migration preserves all rows.
BEGIN;

ALTER TABLE users ADD COLUMN IF NOT EXISTS is_archived INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS archived_at TEXT;

ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS actor_role TEXT;
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS module TEXT;
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS result TEXT NOT NULL DEFAULT 'Success';
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS ip_address TEXT;

CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_logs_actor ON audit_logs(actor_id);

COMMIT;
