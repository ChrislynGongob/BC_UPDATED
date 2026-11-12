-- Add the Founding Admin marker without guessing which existing account owns it.
-- Review and run manually as a database owner during a maintenance window.
-- Provision the designated Founder separately with setup_founding_admin.py.
BEGIN;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS is_founding_admin INTEGER NOT NULL DEFAULT 0;

DO $$
DECLARE
    founder_count INTEGER;
    valid_founder_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO founder_count
      FROM users
     WHERE role = 'admin' AND is_founding_admin = 1;
    IF founder_count > 1 THEN
        RAISE EXCEPTION 'Multiple Founding Admin accounts exist; migration stopped';
    END IF;

    IF founder_count = 1 THEN
        SELECT COUNT(*) INTO valid_founder_count
          FROM users
         WHERE role = 'admin'
           AND is_founding_admin = 1
           AND is_super_admin = 1
           AND status = 'active';
        IF valid_founder_count <> 1 THEN
            RAISE EXCEPTION 'Existing Founding Admin is not an active Super Admin';
        END IF;
    END IF;
END $$;

DROP INDEX IF EXISTS idx_users_single_founding_admin;
CREATE UNIQUE INDEX idx_users_single_founding_admin
    ON users(is_founding_admin)
    WHERE role = 'admin' AND is_founding_admin = 1;

COMMIT;
