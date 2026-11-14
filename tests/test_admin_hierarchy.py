"""Administrator hierarchy permissions and ownership transfer safety."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from flask import session

from werkzeug.security import generate_password_hash

import app as A
import db as dbh


class AdminHierarchyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patchers = [
            patch.dict(os.environ, {"DB_BACKEND": "sqlite"}),
            patch.object(dbh, "DB_PATH", os.path.join(self.temp.name, "hierarchy.db")),
            patch.object(dbh, "auto_backup_if_due"),
            patch.object(A, "_db_ready", True),
            patch.dict(A.app.config, {"TESTING": True}),
            patch.object(A.email_service, "send_email"),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        dbh.init_db()
        self.founder_id = self.insert_user(
            "Ralph Patrick Ga", "ralphpatrickga@gmail.com", super_admin=1, founding_admin=0
        )
        self.super_id = self.insert_user("Super", "super@example.test", super_admin=1)
        self.regular_id = self.insert_user("Regular", "regular@example.test")
        self.client = A.app.test_client()

    def insert_user(self, name, email, super_admin=0, founding_admin=0):
        conn = dbh.get_db()
        try:
            cur = conn.execute(
                "INSERT INTO users(full_name,email,password_hash,role,is_super_admin,is_founding_admin) "
                "VALUES (?,?,?,'admin',?,?)",
                (name, email, generate_password_hash("SafePassword123"), super_admin, founding_admin),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def insert_bpso_staff(self):
        conn = dbh.get_db()
        try:
            cursor = conn.execute(
                "INSERT INTO users(full_name,email,password_hash,role,office,status) "
                "VALUES (?,?,?,'staff','bpso','active')",
                ("BPSO Staff", "bpso@example.test", generate_password_hash("SafePassword123")),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def set_session(self, user_id, mfa=False):
        with self.client.session_transaction() as state:
            state.clear()
            state["user_id"] = user_id
            state["role"] = "admin"
            if mfa:
                conn = dbh.get_db()
                user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
                conn.close()
                import hashlib, hmac
                state["admin_mfa_verified_at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                state["admin_mfa_proof"] = hmac.new(
                    A.app.secret_key.encode(),
                    f"admin-credentials:{user_id}:{user['password_hash']}:{user['email']}".encode(),
                    hashlib.sha256,
                ).hexdigest()
                state["founder_transfer_csrf"] = "csrf-test-token"

    def create_admin(self, user_id):
        self.set_session(user_id)
        return self.client.post("/admin/administrators", data={
            "full_name": "New Admin", "email": "new-admin@example.test",
            "recovery_email": "recovery@example.test", "password": "Password123",
        })

    def test_only_legacy_founder_can_create_regular_admins(self):
        response = self.create_admin(self.founder_id)
        self.assertEqual(response.status_code, 302)
        conn = dbh.get_db()
        created = conn.execute(
            "SELECT is_super_admin,is_founding_admin FROM users WHERE email=?",
            ("new-admin@example.test",),
        ).fetchone()
        self.assertEqual((created["is_super_admin"], created["is_founding_admin"]), (0, 0))
        conn.close()
        self.assertEqual(self.create_admin(self.super_id).status_code, 403)

    def test_only_founder_can_open_management_page(self):
        self.set_session(self.founder_id)
        self.assertEqual(self.client.get("/admin/administrators").status_code, 200)
        self.set_session(self.regular_id)
        self.assertEqual(self.client.get("/admin/administrators").status_code, 403)

    def test_current_user_is_loaded_once_per_request(self):
        with A.app.test_request_context("/admin/administrators"):
            session["user_id"] = self.founder_id
            with patch.object(A.dbh, "get_db", wraps=dbh.get_db) as open_connection:
                first = A.current_user()
                second = A.current_user()
            self.assertEqual(first["id"], self.founder_id)
            self.assertEqual(second["id"], self.founder_id)
            self.assertEqual(open_connection.call_count, 1)

    def test_staff_dashboard_and_complaints_support_partial_and_full_responses(self):
        staff_id = self.insert_bpso_staff()
        self.set_session(staff_id)

        dashboard = self.client.get("/staff/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"class=\"sidebar\"", dashboard.data)
        self.assertEqual(dashboard.headers["Cache-Control"], "private, no-store")

        dashboard_partial = self.client.get(
            "/staff/dashboard", headers={"X-BC-Partial": "1"}
        )
        self.assertEqual(dashboard_partial.status_code, 200)
        self.assertEqual(dashboard_partial.headers["X-BC-Partial"], "1")
        self.assertIn(b"Staff dashboard overview", dashboard_partial.data)
        self.assertNotIn(b"class=\"sidebar\"", dashboard_partial.data)

        complaints_partial = self.client.get(
            "/staff/complaints?priority=High", headers={"X-BC-Partial": "1"}
        )
        self.assertEqual(complaints_partial.status_code, 200)
        self.assertIn(b"name=\"status\"", complaints_partial.data)
        self.assertIn(b"name=\"priority\" value=\"High\"", complaints_partial.data)
        self.assertNotIn(b"class=\"sidebar\"", complaints_partial.data)

        failed_post = self.client.post(
            "/staff/complaints", headers={"X-BC-Partial": "1"}
        )
        self.assertEqual(failed_post.status_code, 405)

    def test_admin_and_resident_pages_support_partial_responses(self):
        self.set_session(self.founder_id)
        admin_page = self.client.get(
            "/admin/announcements", headers={"X-BC-Partial": "1"}
        )
        self.assertEqual(admin_page.status_code, 200)
        self.assertEqual(admin_page.headers["X-BC-Partial"], "1")
        self.assertIn(b"Post a New Announcement", admin_page.data)

        conn = dbh.get_db()
        try:
            cursor = conn.execute(
                "INSERT INTO users(full_name,email,password_hash,role,status) "
                "VALUES (?,?,?,'resident','active')",
                ("Test Resident", "resident@example.test", generate_password_hash("SafePassword123")),
            )
            conn.commit()
            resident_id = cursor.lastrowid
        finally:
            conn.close()
        self.set_session(resident_id)
        resident_page = self.client.get("/dashboard", headers={"X-BC-Partial": "1"})
        self.assertEqual(resident_page.status_code, 200)
        self.assertEqual(resident_page.headers["X-BC-Partial"], "1")
        self.assertIn(b"My Dashboard", resident_page.data)

        denied = self.client.get(
            "/staff/dashboard", headers={"X-BC-Partial": "1"}
        )
        self.assertEqual(denied.status_code, 403)
        self.assertNotIn("X-BC-Partial", denied.headers)

    def test_static_navigation_asset_is_cacheable_but_html_is_not(self):
        page = self.client.get("/login")
        self.assertIn("no-store", page.headers["Cache-Control"])
        asset = self.client.get("/static/js/partial-navigation.js")
        self.assertIn("public", asset.headers["Cache-Control"])
        self.assertIn("max-age=300", asset.headers["Cache-Control"])

    def test_regular_admin_cannot_create_accounts_directly(self):
        self.assertEqual(self.create_admin(self.regular_id).status_code, 403)

    def test_duplicate_email_is_not_created(self):
        self.set_session(self.founder_id)
        response = self.client.post("/admin/administrators", data={
            "full_name": "Duplicate", "email": "ralphpatrickga@gmail.com",
            "recovery_email": "recovery@example.test", "password": "Password123",
        })
        conn = dbh.get_db()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM users WHERE lower(email)='ralphpatrickga@gmail.com'"
        ).fetchone()[0], 1)
        conn.close()
        self.assertEqual(response.status_code, 302)

    def test_resident_crud_archive_and_restore_preserve_history(self):
        self.set_session(self.founder_id)
        created = self.client.post("/admin/residents/new", data={
            "full_name": "Archive Resident", "email": "archive@example.test",
            "password": "Resident123", "address": "Purok 2", "contact_number": "09171234567",
        })
        self.assertEqual(created.status_code, 302)
        conn = dbh.get_db()
        resident = conn.execute("SELECT * FROM users WHERE email='archive@example.test'").fetchone()
        conn.execute(
            "INSERT INTO requests(reference_no,resident_id,request_type) VALUES('REQ-ARCHIVE-1',?,'Barangay Clearance')",
            (resident["id"],),
        )
        conn.commit()
        conn.close()
        self.assertEqual(self.client.get(f"/admin/residents/{resident['id']}").status_code, 200)
        updated = self.client.post(f"/admin/residents/{resident['id']}/edit", data={
            "full_name": "Archive Resident Updated", "email": "archive@example.test",
            "address": "Purok 3", "contact_number": "09171234567",
        })
        self.assertEqual(updated.status_code, 302)
        self.assertEqual(self.client.post(f"/admin/residents/{resident['id']}/archive").status_code, 302)
        conn = dbh.get_db()
        archived = conn.execute("SELECT status,is_archived FROM users WHERE id=?", (resident["id"],)).fetchone()
        history_count = conn.execute("SELECT COUNT(*) FROM requests WHERE resident_id=?", (resident["id"],)).fetchone()[0]
        conn.close()
        self.assertEqual((archived["status"], archived["is_archived"], history_count), ("inactive", 1, 1))
        self.assertEqual(self.client.post(f"/admin/residents/{resident['id']}/restore").status_code, 302)
        conn = dbh.get_db()
        restored = conn.execute("SELECT status,is_archived FROM users WHERE id=?", (resident["id"],)).fetchone()
        audit = conn.execute("SELECT module,result FROM audit_logs WHERE target='archive@example.test' ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        self.assertEqual((restored["status"], restored["is_archived"]), ("inactive", 0))
        self.assertEqual((audit["module"], audit["result"]), ("accounts", "Success"))

    def test_regular_admin_cannot_change_roles_or_modify_founder(self):
        conn = dbh.get_db()
        cur = conn.execute(
            "INSERT INTO users(full_name,email,password_hash,role,status) VALUES('Role Test','role@example.test',?,'resident','active')",
            (generate_password_hash("Resident123"),),
        )
        resident_id = cur.lastrowid
        conn.commit()
        conn.close()
        self.set_session(self.regular_id)
        self.assertEqual(self.client.post("/admin/settings/users", data={
            "account_id": resident_id, "operation": "role", "role": "admin",
        }).status_code, 400)
        self.assertEqual(self.client.post("/admin/settings/users", data={
            "account_id": self.founder_id, "operation": "status",
        }).status_code, 403)
        conn = dbh.get_db()
        self.assertEqual(conn.execute("SELECT role FROM users WHERE id=?", (resident_id,)).fetchone()[0], "resident")
        conn.close()

    def test_complaint_acknowledgement_is_private_and_does_not_resolve(self):
        conn = dbh.get_db()
        cur = conn.execute(
            "INSERT INTO users(full_name,email,password_hash,role,status) VALUES('Concern Owner','owner@example.test',?,'resident','active')",
            (generate_password_hash("Resident123"),),
        )
        resident_id = cur.lastrowid
        complaint = conn.execute(
            "INSERT INTO complaints(reference_no,resident_id,category,description) VALUES('CMP-ACK-1',?,'Others','Private concern summary')",
            (resident_id,),
        )
        complaint_id = complaint.lastrowid
        conn.commit()
        conn.close()
        self.set_session(resident_id)
        pdf = self.client.get(f"/complaints/{complaint_id}/acknowledgement.pdf")
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.mimetype, "application/pdf")
        conn = dbh.get_db()
        status = conn.execute("SELECT status FROM complaints WHERE id=?", (complaint_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(status, "Pending")
        conn = dbh.get_db()
        cursor = conn.execute(
            "INSERT INTO users(full_name,email,password_hash,role,office,status) VALUES('Treasurer Staff','treasurer@example.test',?,'staff','treasurer','active')",
            (generate_password_hash("SafePassword123"),),
        )
        conn.commit()
        treasurer_id = cursor.lastrowid
        conn.close()
        self.set_session(treasurer_id)
        self.assertEqual(self.client.get(f"/complaints/{complaint_id}/acknowledgement.pdf").status_code, 403)

    def test_authentication_events_are_audited_without_credentials(self):
        conn = dbh.get_db()
        cursor = conn.execute(
            "INSERT INTO users(full_name,email,password_hash,role,status) VALUES('Login Resident','login@example.test',?,'resident','active')",
            (generate_password_hash("LoginPassword123"),),
        )
        conn.commit()
        conn.close()
        client = A.app.test_client()
        client.post("/login", data={"email": "login@example.test", "password": "wrong"})
        client.post("/login", data={"email": "login@example.test", "password": "LoginPassword123"})
        client.get("/logout")
        conn = dbh.get_db()
        rows = conn.execute(
            "SELECT action,actor_role,module,result,details FROM audit_logs "
            "WHERE target='login@example.test' ORDER BY id"
        ).fetchall()
        conn.close()
        self.assertEqual([row["action"] for row in rows], ["Failed login", "Successful login", "Logout"])
        self.assertTrue(all(row["actor_role"] == "resident" and row["module"] == "authentication" for row in rows))
        self.assertTrue(all("LoginPassword123" not in (row["details"] or "") for row in rows))



if __name__ == "__main__":
    unittest.main()
