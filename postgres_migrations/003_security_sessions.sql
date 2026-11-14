-- Add account session-versioning and the index used by database-backed login throttling.
-- Apply manually after migrations 001 and 002. Existing accounts and password hashes are preserved.
BEGIN;

ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_version INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_audit_logs_ip_action_created
    ON audit_logs(ip_address, action, created_at);

COMMIT;
