-- Add actual cash tendered and change returned to the in-person payment record.
-- Apply manually in the Supabase SQL Editor as the database owner.
-- amount_paid remains the document fee collected; request computed_fee is untouched.
ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS amount_received NUMERIC(12,2),
    ADD COLUMN IF NOT EXISTS change_given NUMERIC(12,2) NOT NULL DEFAULT 0;

-- Earlier payment records required exact cash, so the received amount equals the fee.
UPDATE requests
SET amount_received = amount_paid
WHERE payment_status = 'Paid' AND amount_received IS NULL;
