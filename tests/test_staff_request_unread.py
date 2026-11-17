"""Per-staff unread document-request indicators."""
import contextlib
import io
import os
import secrets
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db as dbh  # noqa: E402
_tmpdir = tempfile.mkdtemp()
dbh.DB_PATH = os.path.join(_tmpdir, "request_reads.db")
import app as A  # noqa: E402
import seed  # noqa: E402
from tests.session_helpers import authenticate  # noqa: E402


class StaffRequestUnreadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with contextlib.redirect_stdout(io.StringIO()):
            with patch.dict(os.environ, {
                "DEMO_ADMIN_PASSWORD": secrets.token_urlsafe(16) + "A1",
                "DEMO_STAFF_PASSWORD": "Staff@1234",
                "DEMO_RESIDENT_PASSWORD": "Resident@1234",
            }):
                seed.run()
        conn = dbh.get_db()
        cls.resident_id = conn.execute("SELECT id FROM users WHERE role='resident' LIMIT 1").fetchone()["id"]
        cls.treasurer_id = conn.execute(
            "SELECT id FROM users WHERE role='staff' AND office='treasurer' LIMIT 1"
        ).fetchone()["id"]
        cls.secretary_id = conn.execute(
            "SELECT id FROM users WHERE role='staff' AND office='secretary' LIMIT 1"
        ).fetchone()["id"]
        cursor = conn.execute(
            "INSERT INTO users(full_name,email,password_hash,role,office,status) "
            "VALUES('Second Treasurer','second-treasurer@test.invalid','unused','staff','treasurer','active')"
        )
        cls.second_treasurer_id = cursor.lastrowid
        conn.commit()
        conn.close()

    def setUp(self):
        conn = dbh.get_db()
        old_requests = conn.execute("SELECT id,request_type FROM requests").fetchall()
        for row in old_requests:
            for staff in A.active_staff_for_offices(conn, A.offices_for_request_type(row["request_type"])):
                conn.execute(
                    "INSERT INTO staff_request_reads(request_id,staff_id,is_read,read_at) "
                    "VALUES(?,?,1,datetime('now')) ON CONFLICT(request_id,staff_id) "
                    "DO UPDATE SET is_read=1, read_at=COALESCE(staff_request_reads.read_at,datetime('now'))",
                    (row["id"], staff["id"]),
                )

        self.clearance_ref = "REQ-UNREAD-" + secrets.token_hex(4).upper()
        self.indigency_ref = "REQ-UNREAD-" + secrets.token_hex(4).upper()
        self.clearance_id = self._insert_request(conn, self.clearance_ref, "Barangay Clearance")
        self.indigency_id = self._insert_request(conn, self.indigency_ref, "Certificate of Indigency")
        A.initialize_staff_request_reads(conn, self.clearance_id, "Barangay Clearance")
        A.initialize_staff_request_reads(conn, self.indigency_id, "Certificate of Indigency")
        conn.commit()
        conn.close()

    def _insert_request(self, conn, reference, request_type):
        return conn.execute(
            "INSERT INTO requests(reference_no,resident_id,request_type,computed_fee,status) "
            "VALUES(?,?,?,100,'Submitted')",
            (reference, self.resident_id, request_type),
        ).lastrowid

    @staticmethod
    def client(user_id):
        return authenticate(A.app.test_client(), user_id)

    @staticmethod
    def csrf_token(client):
        with client.session_transaction() as session:
            return session.get("_csrf_token")

    @staticmethod
    def unread_badge(html, href):
        import re
        match = re.search(rf'<a href="{href}"[^>]*>(.*?)</a>', html, re.DOTALL)
        if not match:
            return None
        badge = re.search(r'class="pill-badge"[^>]*>(\d+)</span>', match.group(1))
        return int(badge.group(1)) if badge else 0

    def test_new_request_shows_new_badge_dot_and_unread_row(self):
        html = self.client(self.treasurer_id).get("/staff/requests").get_data(as_text=True)
        self.assertIn(self.clearance_ref, html)
        self.assertIn('class="request-new-badge">NEW</span>', html)
        self.assertIn("request-unread-dot", html)
        self.assertIn("request-row-unread", html)
        self.assertEqual(self.unread_badge(html, "/staff/requests"), 1)

    def test_opening_request_marks_only_that_staff_read_and_preserves_status(self):
        client = self.client(self.treasurer_id)
        list_html = client.get("/staff/requests").get_data(as_text=True)
        self.assertEqual(self.unread_badge(list_html, "/staff/requests"), 1)
        self.assertIn(self.clearance_ref, list_html)

        details = client.get(f"/requests/{self.clearance_id}")
        detail_html = details.get_data(as_text=True)
        self.assertEqual(details.status_code, 200)
        self.assertNotIn('class="pill-badge" aria-label="1 new requests"', detail_html)
        conn = dbh.get_db()
        state = conn.execute(
            "SELECT is_read,read_at FROM staff_request_reads WHERE request_id=? AND staff_id=?",
            (self.clearance_id, self.treasurer_id),
        ).fetchone()
        req = conn.execute("SELECT status FROM requests WHERE id=?", (self.clearance_id,)).fetchone()
        conn.close()
        self.assertEqual((state["is_read"], req["status"]), (1, "Submitted"))
        self.assertTrue(state["read_at"])

        refreshed = client.get("/staff/requests").get_data(as_text=True)
        self.assertIn(self.clearance_ref, refreshed)
        self.assertNotIn('class="request-new-badge">NEW</span>', refreshed)
        self.assertEqual(self.unread_badge(refreshed, "/staff/requests"), 0)

    def test_opening_requests_list_does_not_mark_rows_read(self):
        client = self.client(self.treasurer_id)
        client.get("/staff/requests")
        conn = dbh.get_db()
        state = conn.execute(
            "SELECT is_read FROM staff_request_reads WHERE request_id=? AND staff_id=?",
            (self.clearance_id, self.treasurer_id),
        ).fetchone()
        conn.close()
        self.assertEqual(state["is_read"], 0)

    def test_other_staff_account_keeps_its_independent_unread_state(self):
        self.client(self.treasurer_id).get(f"/requests/{self.clearance_id}")
        second = self.client(self.second_treasurer_id)
        html = second.get("/staff/requests").get_data(as_text=True)
        self.assertIn(self.clearance_ref, html)
        self.assertIn('class="request-new-badge">NEW</span>', html)
        self.assertEqual(self.unread_badge(html, "/staff/requests"), 1)

    def test_unread_requests_follow_office_routing(self):
        treasurer_html = self.client(self.treasurer_id).get("/staff/requests").get_data(as_text=True)
        secretary_html = self.client(self.secretary_id).get("/staff/requests").get_data(as_text=True)
        self.assertIn(self.clearance_ref, treasurer_html)
        self.assertNotIn(self.indigency_ref, treasurer_html)
        self.assertIn(self.indigency_ref, secretary_html)
        self.assertNotIn(self.clearance_ref, secretary_html)
        self.assertEqual(self.unread_badge(treasurer_html, "/staff/requests"), 1)
        self.assertEqual(self.unread_badge(secretary_html, "/staff/requests"), 1)

    def test_newest_request_is_first_and_opening_does_not_change_status(self):
        conn = dbh.get_db()
        newer_ref = "REQ-UNREAD-" + secrets.token_hex(4).upper()
        newer_id = self._insert_request(conn, newer_ref, "Barangay Clearance")
        A.initialize_staff_request_reads(conn, newer_id, "Barangay Clearance")
        conn.commit()
        conn.close()
        html = self.client(self.treasurer_id).get("/staff/requests").get_data(as_text=True)
        self.assertLess(html.index(newer_ref), html.index(self.clearance_ref))

        self.client(self.treasurer_id).get(f"/requests/{newer_id}")
        conn = dbh.get_db()
        self.assertEqual(conn.execute("SELECT status FROM requests WHERE id=?", (newer_id,)).fetchone()[0], "Submitted")
        conn.close()


if __name__ == "__main__":
    unittest.main()
