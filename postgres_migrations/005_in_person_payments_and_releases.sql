-- Manually reviewed, additive migration for in-person payment and release tracking.
-- Apply through the Supabase SQL Editor as the database owner after reviewing.
-- Existing computed_fee and request status values are left unchanged.
ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS payment_status TEXT NOT NULL DEFAULT 'Unpaid',
    ADD COLUMN IF NOT EXISTS amount_paid NUMERIC(12,2),
    ADD COLUMN IF NOT EXISTS payment_at TEXT,
    ADD COLUMN IF NOT EXISTS payment_recorded_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS official_receipt_no TEXT,
    ADD COLUMN IF NOT EXISTS released_at TEXT,
    ADD COLUMN IF NOT EXISTS released_by INTEGER REFERENCES users(id) ON DELETE SET NULL;

UPDATE requests
SET payment_status = 'Not Required'
WHERE computed_fee = 0 AND payment_status = 'Unpaid';

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'requests_payment_status_allowed'
    ) THEN
        ALTER TABLE requests
            ADD CONSTRAINT requests_payment_status_allowed
            CHECK (payment_status IN ('Unpaid', 'Paid', 'Not Required'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_requests_payment_release
    ON requests (payment_status, status);
