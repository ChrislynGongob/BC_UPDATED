-- Track whether each authorized staff account has opened each document request.
-- Apply manually in Supabase as the database owner before deploying the app.
CREATE TABLE IF NOT EXISTS staff_request_reads (
    request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    staff_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    is_read INTEGER NOT NULL DEFAULT 0 CHECK (is_read IN (0,1)),
    read_at TEXT,
    PRIMARY KEY (request_id, staff_id)
);

CREATE INDEX IF NOT EXISTS idx_staff_request_reads_staff_unread
    ON staff_request_reads(staff_id, is_read);

-- Preserve outstanding request notifications for existing staff. Older requests
-- without an unread "New ... request ... was submitted" notification are treated
-- as already viewed so deployment does not create an unread backlog.
INSERT INTO staff_request_reads (request_id, staff_id, is_read, read_at)
SELECT r.id,
       u.id,
       CASE WHEN EXISTS (
           SELECT 1 FROM notifications n
           WHERE n.user_id = u.id
             AND n.link = '/requests/' || r.id::TEXT
             AND n.message = 'New ' || r.request_type || ' request ' || r.reference_no || ' was submitted.'
             AND n.is_read = 0
       ) THEN 0 ELSE 1 END,
       CASE WHEN EXISTS (
           SELECT 1 FROM notifications n
           WHERE n.user_id = u.id
             AND n.link = '/requests/' || r.id::TEXT
             AND n.message = 'New ' || r.request_type || ' request ' || r.reference_no || ' was submitted.'
             AND n.is_read = 0
       ) THEN NULL ELSE COALESCE(r.created_at, to_char(CURRENT_TIMESTAMP, 'YYYY-MM-DD HH24:MI:SS')) END
FROM requests r
JOIN users u ON u.role='staff' AND u.status='active'
WHERE (r.request_type='Barangay Clearance' AND u.office='treasurer')
   OR (r.request_type IN ('Certificate of Residency','Certificate of Indigency') AND u.office='secretary')
ON CONFLICT (request_id, staff_id) DO NOTHING;
