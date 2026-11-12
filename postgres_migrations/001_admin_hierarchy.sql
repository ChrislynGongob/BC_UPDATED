-- BRGYCONNECT Administrator Hierarchy, legacy PostgreSQL -> hierarchy schema.
-- Review and run manually as a database owner during a maintenance window.
-- This assigns Founding Admin to Ralph (ID 17) only; it does not alter his
-- Super Admin flag, status, credentials, or any related account records.
BEGIN;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS is_founding_admin INTEGER NOT NULL DEFAULT 0;

DO $$
DECLARE
    target_count INTEGER;
    current_founders INTEGER;
BEGIN
    SELECT COUNT(*) INTO target_count
      FROM users
     WHERE id = 17 AND role = 'admin' AND is_super_admin = 1 AND status = 'active';
    IF target_count <> 1 THEN
        RAISE EXCEPTION 'Expected active Super Admin account ID 17; migration stopped without transfer';
    END IF;

    SELECT COUNT(*) INTO current_founders
      FROM users
     WHERE role = 'admin' AND is_founding_admin = 1;
    IF current_founders > 0 THEN
        RAISE EXCEPTION 'Founding Admin already designated; inspect current data before migration';
    END IF;

    UPDATE users SET is_founding_admin = 1 WHERE id = 17;
END $$;

-- Remove the legacy index that incorrectly made Super Admin a singleton.
DROP INDEX IF EXISTS idx_users_single_founding_admin;
CREATE UNIQUE INDEX idx_users_single_founding_admin
    ON users(is_founding_admin)
    WHERE role = 'admin' AND is_founding_admin = 1;

DO $$
DECLARE
    founder_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO founder_count
      FROM users WHERE role = 'admin' AND is_founding_admin = 1;
    IF founder_count <> 1 THEN
        RAISE EXCEPTION 'Expected exactly one Founding Admin after migration';
    END IF;
END $$;

COMMIT;
