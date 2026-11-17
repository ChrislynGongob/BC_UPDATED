"""Payment recording and document release authorization coverage."""
import contextlib
import io
import os
import re
import secrets
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db as dbh  # noqa: E402
_tmpdir = tempfile.mkdtemp()
dbh.DB_PATH = os.path.join(_tmpdir, "payments.db")
import app as A  # noqa: E402
import seed  # noqa: E402
from tests.session_helpers import authenticate  # noqa: E402


class PaymentReleaseTests(unittest.TestCase):
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
        cls.request_id = conn.execute(
            "SELECT id FROM requests WHERE request_type='Barangay Clearance' LIMIT 1"
        ).fetchone()["id"]
        cls.residency_id = conn.execute(
            "SELECT id FROM requests WHERE request_type='Certificate of Residency' LIMIT 1"
        ).fetchone()["id"]
        cls.indigency_id = conn.execute(
            "SELECT id FROM requests WHERE request_type='Certificate of Indigency' LIMIT 1"
        ).fetchone()["id"]
        cls.indigency_reference = conn.execute(
            "SELECT reference_no FROM requests WHERE id=?", (cls.indigency_id,)
        ).fetchone()["reference_no"]
        cls.residency_reference = conn.execute(
            "SELECT reference_no FROM requests WHERE id=?", (cls.residency_id,)
        ).fetchone()["reference_no"]
        cls.extra_request_state = {}
        for extra_id in (cls.residency_id, cls.indigency_id):
            state = conn.execute("SELECT * FROM requests WHERE id=?", (extra_id,)).fetchone()
            cls.extra_request_state[extra_id] = state
        cls.resident_id = conn.execute(
            "SELECT resident_id FROM requests WHERE id=?", (cls.request_id,)
        ).fetchone()["resident_id"]
        cls.treasurer_id = conn.execute(
            "SELECT id FROM users WHERE role='staff' AND office='treasurer' LIMIT 1"
        ).fetchone()["id"]
        cls.secretary_id = conn.execute(
            "SELECT id FROM users WHERE role='staff' AND office='secretary' LIMIT 1"
        ).fetchone()["id"]
        cls.bpso_id = conn.execute(
            "SELECT id FROM users WHERE role='staff' AND office='bpso' LIMIT 1"
        ).fetchone()["id"]
        cls.admin_id = conn.execute(
            "SELECT id FROM users WHERE role='admin' LIMIT 1"
        ).fetchone()["id"]
        conn.close()

    def setUp(self):
        conn = dbh.get_db()
        reference = conn.execute("SELECT reference_no FROM requests WHERE id=?", (self.request_id,)).fetchone()[0]
        conn.execute(
            "DELETE FROM audit_logs WHERE target=? AND action IN "
            "('Recorded in-person document payment','Released document request')", (reference,),
        )
        conn.execute(
            "UPDATE requests SET computed_fee=100, status='Approved', payment_status='Unpaid', "
            "amount_paid=NULL, amount_received=NULL, change_given=0, payment_at=NULL, payment_recorded_by=NULL, official_receipt_no=NULL, "
            "released_at=NULL, released_by=NULL WHERE id=?", (self.request_id,),
        )
        for extra_id, original in self.extra_request_state.items():
            conn.execute(
                "UPDATE requests SET computed_fee=?, status=?, payment_status=?, amount_paid=?, amount_received=?, "
                "change_given=?, payment_at=?, payment_recorded_by=?, official_receipt_no=?, released_at=?, released_by=? "
                "WHERE id=?",
                (original["computed_fee"], original["status"], original["payment_status"], original["amount_paid"],
                 original["amount_received"], original["change_given"], original["payment_at"],
                 original["payment_recorded_by"], original["official_receipt_no"], original["released_at"],
                 original["released_by"], extra_id),
            )
        conn.commit()
        conn.close()

    def client(self, user_id):
        return authenticate(A.app.test_client(), user_id)

    def token(self, client):
        with client.session_transaction() as state:
            if not state.get("_csrf_token"):
                state["_csrf_token"] = "test-csrf-token"
            return state["_csrf_token"]

    @staticmethod
    def local_payment_at():
        return A.datetime.now(A.BARANGAY_TIMEZONE).strftime("%Y-%m-%dT%H:%M")

    def test_treasurer_queue_shows_approved_unpaid_request_details(self):
        client = self.client(self.treasurer_id)
        response = client.get("/staff/payments")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Record Payment", html)
        self.assertIn("Ready to Pay", html)
        self.assertIn("Approved", html)
        self.assertIn("Barangay Clearance", html)

    def test_treasurer_payment_table_includes_treasurer_and_payment_owned_types_only(self):
        html = self.client(self.treasurer_id).get("/staff/payments").get_data(as_text=True)
        self.assertIn(self.residency_reference, html)
        self.assertNotIn(self.indigency_reference, html)
        self.assertNotIn("Certificate of Indigency", html)

    def test_treasurer_cannot_record_secretary_only_request_by_changing_id(self):
        conn = dbh.get_db()
        conn.execute(
            "UPDATE requests SET status='Approved', computed_fee=500, payment_status='Unpaid' WHERE id=?",
            (self.indigency_id,),
        )
        conn.commit()
        conn.close()
        client = self.client(self.treasurer_id)
        client.get("/staff/payments")
        response = client.post("/staff/payments", data={
            "_csrf_token": self.token(client), "request_id": self.indigency_id,
            "amount_received": "500", "payment_at": self.local_payment_at(),
        })
        self.assertEqual(response.status_code, 404)
        conn = dbh.get_db()
        row = conn.execute("SELECT payment_status, amount_received FROM requests WHERE id=?", (self.indigency_id,)).fetchone()
        conn.close()
        self.assertEqual((row["payment_status"], row["amount_received"]), ("Unpaid", None))

    def test_treasurer_summaries_exclude_secretary_only_requests(self):
        client = self.client(self.treasurer_id)
        baseline = client.get("/staff/payments").get_data(as_text=True)
        before = tuple(re.search(pattern, baseline).group(1) for pattern in (
            r"Awaiting Payment</span><strong>(.*?)</strong>",
            r"Collected Today</span><strong>(.*?)</strong>",
            r"Total Requests</span><strong>(.*?)</strong>",
        ))
        conn = dbh.get_db()
        conn.execute(
            "UPDATE requests SET status='Approved', computed_fee=999, payment_status='Paid', amount_paid=999, "
            "amount_received=1000, change_given=1, payment_at=? WHERE id=?",
            (self.local_payment_at().replace("T", " ") + ":00", self.indigency_id),
        )
        conn.commit()
        conn.close()
        after = client.get("/staff/payments").get_data(as_text=True)
        values_after = tuple(re.search(pattern, after).group(1) for pattern in (
            r"Awaiting Payment</span><strong>(.*?)</strong>",
            r"Collected Today</span><strong>(.*?)</strong>",
            r"Total Requests</span><strong>(.*?)</strong>",
        ))
        self.assertEqual(values_after, before)

    def test_admin_monitoring_keeps_secretary_only_payment_records_visible(self):
        html = self.client(self.admin_id).get("/admin/requests").get_data(as_text=True)
        self.assertIn(self.indigency_reference, html)
        self.assertIn("Certificate of Indigency", html)

    def test_treasurer_records_payment_without_changing_approval(self):
        client = self.client(self.treasurer_id)
        client.get("/staff/payments")
        response = client.post("/staff/payments", data={
            "_csrf_token": self.token(client), "request_id": self.request_id,
            "amount_received": "100", "payment_at": self.local_payment_at(), "official_receipt_no": "OR-1001",
        })
        self.assertEqual(response.status_code, 302)
        conn = dbh.get_db()
        row = conn.execute("SELECT * FROM requests WHERE id=?", (self.request_id,)).fetchone()
        audit = conn.execute(
            "SELECT actor_id, action FROM audit_logs WHERE target=? AND action='Recorded in-person document payment'",
            (row["reference_no"],),
        ).fetchone()
        conn.close()
        self.assertEqual((row["payment_status"], row["status"], row["amount_paid"], row["amount_received"],
                          row["change_given"], row["official_receipt_no"]),
                         ("Paid", "Approved", 100, 100, 0, "OR-1001"))
        self.assertTrue(row["payment_at"])
        self.assertEqual(row["payment_recorded_by"], self.treasurer_id)
        self.assertEqual(audit["actor_id"], self.treasurer_id)

    def test_duplicate_payment_is_rejected(self):
        client = self.client(self.treasurer_id)
        client.get("/staff/payments")
        data = {"_csrf_token": self.token(client), "request_id": self.request_id,
                "amount_received": "100", "payment_at": self.local_payment_at()}
        client.post("/staff/payments", data=data)
        client.post("/staff/payments", data=data)
        conn = dbh.get_db()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE target=(SELECT reference_no FROM requests WHERE id=?) "
            "AND action='Recorded in-person document payment'", (self.request_id,),
        ).fetchone()[0], 1)
        conn.close()

    def test_non_treasurer_staff_cannot_record_payment(self):
        client = self.client(self.secretary_id)
        response = client.post("/staff/payments", data={
            "_csrf_token": self.token(client), "request_id": self.request_id, "amount_received": "100",
        })
        self.assertEqual(response.status_code, 403)

    def test_resident_cannot_record_payment(self):
        client = self.client(self.resident_id)
        client.get(f"/requests/{self.request_id}")
        response = client.post("/staff/payments", data={
            "_csrf_token": self.token(client), "request_id": self.request_id, "amount_received": "100",
        })
        self.assertEqual(response.status_code, 403)

    def test_admin_cannot_confirm_payment(self):
        client = self.client(self.admin_id)
        client.get("/admin/requests")
        response = client.post("/staff/payments", data={
            "_csrf_token": self.token(client), "request_id": self.request_id, "amount_received": "100",
        })
        self.assertEqual(response.status_code, 403)

    def test_invalid_amount_is_rejected(self):
        client = self.client(self.treasurer_id)
        client.get("/staff/payments")
        response = client.post("/staff/payments", data={
            "_csrf_token": self.token(client), "request_id": self.request_id, "amount_received": "-1",
        })
        self.assertEqual(response.status_code, 302)
        conn = dbh.get_db()
        self.assertEqual(conn.execute("SELECT payment_status FROM requests WHERE id=?", (self.request_id,)).fetchone()[0], "Unpaid")
        conn.close()

    def test_insufficient_payment_is_rejected(self):
        client = self.client(self.treasurer_id)
        client.get("/staff/payments")
        response = client.post("/staff/payments", data={
            "_csrf_token": self.token(client), "request_id": self.request_id,
            "amount_received": "99.99", "payment_at": self.local_payment_at(),
        })
        self.assertEqual(response.status_code, 302)
        conn = dbh.get_db()
        row = conn.execute("SELECT payment_status, amount_received FROM requests WHERE id=?", (self.request_id,)).fetchone()
        conn.close()
        self.assertEqual((row["payment_status"], row["amount_received"]), ("Unpaid", None))

    def test_rejected_request_cannot_be_paid(self):
        conn = dbh.get_db()
        conn.execute("UPDATE requests SET status='Rejected' WHERE id=?", (self.request_id,))
        conn.commit()
        conn.close()
        client = self.client(self.treasurer_id)
        client.get("/staff/payments")
        response = client.post("/staff/payments", data={
            "_csrf_token": self.token(client), "request_id": self.request_id,
            "amount_received": "100", "payment_at": self.local_payment_at(),
        })
        self.assertEqual(response.status_code, 302)
        conn = dbh.get_db()
        row = conn.execute("SELECT status, payment_status FROM requests WHERE id=?", (self.request_id,)).fetchone()
        conn.close()
        self.assertEqual(tuple(row), ("Rejected", "Unpaid"))

    def test_overpayment_records_change_separately(self):
        client = self.client(self.treasurer_id)
        client.get("/staff/payments")
        response = client.post("/staff/payments", data={
            "_csrf_token": self.token(client), "request_id": self.request_id,
            "amount_received": "120", "payment_at": self.local_payment_at(),
        })
        self.assertEqual(response.status_code, 302)
        conn = dbh.get_db()
        row = conn.execute(
            "SELECT payment_status, amount_paid, amount_received, change_given FROM requests WHERE id=?",
            (self.request_id,),
        ).fetchone()
        conn.close()
        self.assertEqual(tuple(row), ("Paid", 100, 120, 20))
        html = client.get("/staff/payments").get_data(as_text=True)
        self.assertRegex(html, r"Collected Today</span><strong>₱100\.00</strong>")

    def test_unpaid_request_cannot_be_released(self):
        client = self.client(self.treasurer_id)
        client.get(f"/requests/{self.request_id}")
        response = client.post(f"/requests/{self.request_id}/update", data={
            "_csrf_token": self.token(client), "status": "Completed",
        })
        self.assertEqual(response.status_code, 302)
        conn = dbh.get_db()
        row = conn.execute("SELECT status, released_at FROM requests WHERE id=?", (self.request_id,)).fetchone()
        conn.close()
        self.assertEqual((row["status"], row["released_at"]), ("Approved", None))

    def test_paid_request_can_be_released(self):
        conn = dbh.get_db()
        conn.execute("UPDATE requests SET payment_status='Paid', amount_paid=100 WHERE id=?", (self.request_id,))
        conn.commit()
        conn.close()
        client = self.client(self.treasurer_id)
        client.get(f"/requests/{self.request_id}")
        response = client.post(f"/requests/{self.request_id}/update", data={
            "_csrf_token": self.token(client), "status": "Completed",
        })
        self.assertEqual(response.status_code, 302)
        conn = dbh.get_db()
        row = conn.execute("SELECT status, released_at, released_by FROM requests WHERE id=?", (self.request_id,)).fetchone()
        audit = conn.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action='Released document request' AND target=(SELECT reference_no FROM requests WHERE id=?)",
            (self.request_id,),
        ).fetchone()[0]
        conn.close()
        self.assertEqual(row["status"], "Completed")
        self.assertTrue(row["released_at"])
        self.assertEqual(row["released_by"], self.treasurer_id)
        self.assertEqual(audit, 1)

    def test_duplicate_release_is_rejected(self):
        conn = dbh.get_db()
        conn.execute(
            "UPDATE requests SET payment_status='Paid', amount_paid=100, status='Completed', "
            "released_at='2026-01-02 03:04:05', released_by=? WHERE id=?",
            (self.treasurer_id, self.request_id),
        )
        conn.commit()
        conn.close()
        client = self.client(self.treasurer_id)
        client.get(f"/requests/{self.request_id}")
        response = client.post(f"/requests/{self.request_id}/update", data={
            "_csrf_token": self.token(client), "status": "Completed",
        })
        self.assertEqual(response.status_code, 302)
        conn = dbh.get_db()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE target=(SELECT reference_no FROM requests WHERE id=?) "
            "AND action='Released document request'", (self.request_id,),
        ).fetchone()[0], 0)
        conn.close()

    def test_free_request_can_be_released_without_payment(self):
        conn = dbh.get_db()
        conn.execute("UPDATE requests SET computed_fee=0, payment_status='Not Required' WHERE id=?", (self.request_id,))
        conn.commit()
        conn.close()
        client = self.client(self.treasurer_id)
        client.get(f"/requests/{self.request_id}")
        response = client.post(f"/requests/{self.request_id}/update", data={
            "_csrf_token": self.token(client), "status": "Completed",
        })
        self.assertEqual(response.status_code, 302)
        conn = dbh.get_db()
        row = conn.execute("SELECT status, payment_status, released_at FROM requests WHERE id=?", (self.request_id,)).fetchone()
        conn.close()
        self.assertEqual((row["status"], row["payment_status"]), ("Completed", "Not Required"))
        self.assertTrue(row["released_at"])

    def test_claim_slip_exists_before_payment(self):
        client = self.client(self.resident_id)
        response = client.get(f"/requests/{self.request_id}")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Claim Slip", html)
        self.assertIn("100", html)
        self.assertIn("Unpaid", html)

    def test_admin_can_monitor_payment_read_only(self):
        conn = dbh.get_db()
        conn.execute(
            "UPDATE requests SET payment_status='Paid', amount_paid=100, payment_at='2026-01-02 03:04:05', "
            "payment_recorded_by=? WHERE id=?", (self.treasurer_id, self.request_id),
        )
        conn.commit()
        conn.close()
        client = self.client(self.admin_id)
        response = client.get("/admin/requests")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Payment", html)
        self.assertIn("Paid", html)
        detail = client.get(f"/requests/{self.request_id}").get_data(as_text=True)
        self.assertIn("payment date", detail.lower())
        self.assertIn("Recorded by", detail)
        self.assertNotIn("Record Payment", html + detail)

    def test_search_sections_follow_role_permissions(self):
        treasurer_html = self.client(self.treasurer_id).get("/search?q=paid").get_data(as_text=True)
        self.assertIn("<h3>Requests", treasurer_html)
        self.assertNotIn("<h3>Complaints", treasurer_html)

        bpso_html = self.client(self.bpso_id).get("/search?q=paid").get_data(as_text=True)
        self.assertNotIn("<h3>Requests", bpso_html)
        self.assertIn("<h3>Complaints", bpso_html)

        admin_html = self.client(self.admin_id).get("/search?q=paid").get_data(as_text=True)
        self.assertIn("<h3>Requests", admin_html)
        self.assertIn("<h3>Complaints", admin_html)

        resident_html = self.client(self.resident_id).get("/search?q=paid").get_data(as_text=True)
        self.assertIn("<h3>Requests", resident_html)
        self.assertIn("<h3>Complaints", resident_html)

    def test_request_claim_slip_and_existing_status_workflow(self):
        client = self.client(self.treasurer_id)
        client.get(f"/requests/{self.request_id}")
        response = client.post(f"/requests/{self.request_id}/update", data={
            "_csrf_token": self.token(client), "status": "Under Review",
        })
        self.assertEqual(response.status_code, 302)
        conn = dbh.get_db()
        row = conn.execute("SELECT status, payment_status FROM requests WHERE id=?", (self.request_id,)).fetchone()
        conn.close()
        self.assertEqual((row["status"], row["payment_status"]), ("Under Review", "Unpaid"))


if __name__ == "__main__":
    unittest.main()
