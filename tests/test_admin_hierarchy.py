"""Administrator hierarchy permissions and ownership transfer safety."""
import os
import sqlite3
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
        conn = dbh.get_db()
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        conn.close()
        with self.client.session_transaction() as state:
            state.clear()
            state["user_id"] = user_id
            state["role"] = user["role"]
            state["auth_version"] = int(user["auth_version"] or 0)
            if user["role"] == "admin":
                import hashlib, hmac
                state["admin_mfa_verified_at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                state["admin_mfa_proof"] = hmac.new(
                    A.app.secret_key.encode(),
                    f"admin-credentials:{user_id}:{user['password_hash']}:{user['email']}".encode(),
                    hashlib.sha256,
                ).hexdigest()
                state["admin_last_activity_at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                state["founder_transfer_csrf"] = "csrf-test-token"

    def create_admin(self, user_id):
        self.set_session(user_id)
        return self.client.post("/admin/administrators", data={
            "full_name": "New Admin", "email": "new-admin@example.test",
            "recovery_email": "recovery@example.test", "password": "StrongPassword123",
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

    def test_founder_check_falls_back_for_legacy_schema_without_marker(self):
        legacy_founder = {
            "role": "admin", "email": "ralphpatrickga@gmail.com", "is_super_admin": 1,
        }
        self.assertTrue(A.is_founding_admin(legacy_founder))
        self.assertFalse(A.is_founding_admin({
            "role": "admin", "email": "other@example.test", "is_super_admin": 1,
        }))

    def test_only_founder_can_open_management_page(self):
        self.set_session(self.founder_id)
        self.assertEqual(self.client.get("/admin/administrators").status_code, 200)
        self.set_session(self.regular_id)
        self.assertEqual(self.client.get("/admin/administrators").status_code, 403)

    def test_security_settings_displays_policy_and_supports_audited_unlock(self):
        until = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
        conn = dbh.get_db()
        conn.execute("UPDATE users SET locked_until=?,failed_logins=5 WHERE id=?", (until, self.regular_id))
        conn.commit()
        conn.close()
        self.set_session(self.founder_id)
        page = self.client.get("/admin/settings/security")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"12", page.data)
        self.assertIn(b"Role permission matrix", page.data)
        with self.client.session_transaction() as state:
            csrf_token = state["security_unlock_csrf"]
        response = self.client.post(
            f"/admin/settings/security/unlock/{self.regular_id}",
            data={"csrf_token": csrf_token},
        )
        self.assertEqual(response.status_code, 302)
        conn = dbh.get_db()
        user = conn.execute("SELECT locked_until,failed_logins FROM users WHERE id=?", (self.regular_id,)).fetchone()
        event = conn.execute(
            "SELECT action FROM audit_logs WHERE action='Account unlocked by administrator' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertIsNone(user["locked_until"])
        self.assertEqual(user["failed_logins"], 0)
        self.assertIsNotNone(event)

    def test_admin_session_expires_after_twenty_minutes_idle(self):
        self.set_session(self.founder_id)
        expired_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=21)
        with self.client.session_transaction() as state:
            state["admin_last_activity_at"] = expired_at.isoformat()
        response = self.client.get("/admin/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)
        conn = dbh.get_db()
        event = conn.execute(
            "SELECT action FROM audit_logs WHERE action='Admin session expired due to inactivity'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(event)

    def test_barangay_information_settings_preserve_hotline_values_and_legacy_route(self):
        self.set_session(self.founder_id)
        response = self.client.get("/admin/settings")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/admin/settings/barangay-information")
        page = self.client.get("/admin/settings/barangay-information")
        self.assertEqual(page.status_code, 200)
        for label in ("Barangay name", "Barangay address", "Main line", "100 Base", "Annex",
                      "Annex II", "Traffic", "BHERT", "MRCF", "Dogpound", "PNP", "BFP", "BDRRM"):
            self.assertIn(label, page.get_data(as_text=True))
        values = {
            "barangay_name": "Poblacion Test",
            "barangay_address": "Municipal Hall, Test City",
            "barangay_main_number": "09170000001",
            "barangay_base_number": "09170000002",
            "barangay_annex_number": "09170000003",
            "barangay_annex2_number": "09170000004",
            "traffic_number": "09170000005",
            "bhert_number": "09170000006",
            "mrcf_number": "09170000007",
            "dogpound_number": "09170000008",
            "pnp_number": "09170000009",
            "bfp_number": "09170000010",
            "bdrrm_number": "09170000011",
        }
        saved = self.client.post("/admin/settings/barangay-information", data=values)
        self.assertEqual(saved.status_code, 302)
        self.assertEqual(saved.location, "/admin/settings/barangay-information")
        hotlines = self.client.get("/hotlines").get_data(as_text=True)
        self.assertIn("09170000001", hotlines)
        conn = dbh.get_db()
        self.assertEqual(dbh.get_setting(conn, "barangay_name"), "Poblacion Test")
        conn.close()

    def test_settings_navigation_and_backend_permissions(self):
        self.set_session(self.founder_id)
        page = self.client.get("/admin/dashboard").get_data(as_text=True)
        for item in ("User Management", "Manage Administrators", "Barangay Information",
                     "Database Backup", "Security Settings"):
            self.assertIn(item, page)
        self.assertNotIn("System Settings", page)
        self.assertEqual(self.client.get("/admin/settings/backups").status_code, 200)
        self.set_session(self.regular_id)
        page = self.client.get("/admin/dashboard").get_data(as_text=True)
        self.assertIn("User Management", page)
        self.assertIn("Barangay Information", page)
        self.assertIn("Security Settings", page)
        self.assertNotIn("Manage Administrators", page)
        self.assertNotIn("Database Backup", page)
        self.assertEqual(self.client.get("/admin/settings/barangay-information").status_code, 200)
        self.assertEqual(self.client.get("/admin/settings/backups").status_code, 403)
        self.assertEqual(self.client.get("/admin/settings/security").status_code, 200)
        self.assertEqual(self.client.get("/admin/administrators").status_code, 403)
        staff_id = self.insert_bpso_staff()
        self.set_session(staff_id)
        self.assertEqual(self.client.get("/admin/settings/barangay-information").status_code, 403)

    def test_user_management_tabs_load_and_account_actions_preserve_records(self):
        self.set_session(self.founder_id)

        self.assertEqual(self.client.post("/admin/residents/new", data={
            "full_name": "Central Management Resident", "email": "central-resident@example.test",
            "password": "ResidentPassword123", "address": "Purok 4",
        }).status_code, 302)
        conn = dbh.get_db()
        resident_id = conn.execute("SELECT id FROM users WHERE email='central-resident@example.test'").fetchone()["id"]
        conn.close()

        response = self.client.get("/admin/settings/users")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Central Management Resident", response.data)
        for query in ("?role=admin&status=all", "?role=staff&status=all",
                      "?role=resident&status=all", "?status=archived"):
            self.assertEqual(self.client.get("/admin/settings/users" + query).status_code, 200)
        self.assertEqual(self.client.get(f"/admin/residents/{resident_id}").status_code, 200)
        self.assertEqual(self.client.get(f"/admin/residents/{resident_id}/edit").status_code, 200)
        self.assertEqual(self.client.post(f"/admin/residents/{resident_id}/edit", data={
            "full_name": "Updated Central Resident", "email": "central-resident@example.test",
            "address": "Purok 5", "contact_number": "",
        }).status_code, 302)

        self.assertEqual(self.client.post("/admin/settings/users", data={
            "account_id": resident_id, "operation": "status",
        }).status_code, 302)
        conn = dbh.get_db()
        self.assertEqual(conn.execute("SELECT status FROM users WHERE id=?", (resident_id,)).fetchone()["status"], "inactive")
        conn.close()

        self.assertEqual(self.client.post("/admin/settings/users", data={
            "account_id": resident_id, "operation": "archive",
        }).status_code, 302)
        conn = dbh.get_db()
        archived = conn.execute("SELECT is_archived,status FROM users WHERE id=?", (resident_id,)).fetchone()
        allowance = conn.execute("SELECT remaining FROM resident_concern_allowances WHERE user_id=?", (resident_id,)).fetchone()
        conn.close()
        self.assertEqual((archived["is_archived"], archived["status"]), (1, "inactive"))
        self.assertEqual(allowance["remaining"], 5)

        self.assertEqual(self.client.post("/admin/settings/users", data={
            "account_id": resident_id, "operation": "restore",
        }).status_code, 302)
        role_user_id = self.insert_bpso_staff()
        self.assertEqual(self.client.post("/admin/settings/users", data={
            "account_id": role_user_id, "operation": "role", "role": "resident", "office": "bpso",
        }).status_code, 302)
        conn = dbh.get_db()
        self.assertEqual(conn.execute("SELECT role,office FROM users WHERE id=?", (role_user_id,)).fetchone()[0:2], ("resident", None))
        conn.close()
        self.set_session(self.regular_id)
        self.assertEqual(self.client.get("/admin/settings/users").status_code, 200)
        self.assertEqual(self.client.get("/admin/settings/users?role=admin").status_code, 403)
        conn = dbh.get_db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM users WHERE email='central-resident@example.test'").fetchone()[0], 1)
        conn.close()
        regular_page = self.client.get("/admin/settings/users").get_data(as_text=True)
        self.assertNotIn("ralphpatrickga@gmail.com", regular_page)
        conn = dbh.get_db()
        staff_id = conn.execute(
            "INSERT INTO users(full_name,email,password_hash,role,office,status) "
            "VALUES('Another BPSO Staff','another-bpso@example.test',?,'staff','bpso','active')",
            (generate_password_hash("SafePassword123"),),
        ).lastrowid
        conn.commit()
        conn.close()
        self.set_session(staff_id)
        self.assertEqual(self.client.get("/admin/settings/users").status_code, 403)

    def test_user_management_redesign_search_counts_and_page_navigation(self):
        self.set_session(self.founder_id)
        conn = dbh.get_db()
        for index in range(30):
            conn.execute(
                "INSERT INTO users(full_name,email,password_hash,role,status,contact_number) "
                "VALUES(?,?,?,'resident',?,?)",
                (f"Paged Person {index:02d}", f"paged-{index:02d}@example.test",
                 generate_password_hash("SafePassword123"),
                 "inactive" if index == 0 else "active", f"0917000{index:04d}"),
            )
        conn.execute(
            "INSERT INTO users(full_name,email,password_hash,role,status,is_archived,archived_at) "
            "VALUES(?,?,?,'resident','inactive',1,datetime('now'))",
            ("Archived Person", "archived-person@example.test", generate_password_hash("SafePassword123")),
        )
        conn.commit()
        conn.close()

        response = self.client.get("/admin/settings/users?status=all")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Manage existing user accounts, roles, access permissions, and account status.", html)
        self.assertNotIn("Add User", html)
        self.assertNotIn("Add Resident", html)
        self.assertIn('aria-label="Go Back"', html)
        for heading in ("Name", "Email", "Mobile Number", "Role", "Status", "Registered", "Actions"):
            self.assertIn(f">{heading}</th>", html)
        self.assertIn("Total Users", html)
        self.assertIn("Active Accounts", html)
        self.assertIn("Inactive Accounts", html)
        self.assertIn("Archived Accounts", html)
        self.assertIn("Showing 1 to 25 of 33 users", html)
        self.assertNotIn('name="contact_number"', html)

        second = self.client.get("/admin/settings/users?role=resident&status=active&page=2")
        self.assertEqual(second.status_code, 200)
        second_html = second.get_data(as_text=True)
        self.assertIn("Paged Person 29", second_html)
        self.assertIn("role=resident&amp;status=active", second_html)
        self.assertIn("Showing 26 to 29 of 29 users", second_html)
        self.assertNotIn("Paged Person 00", second_html)

        filtered = self.client.get("/admin/settings/users?role=resident&status=active&q=paged-02")
        self.assertEqual(filtered.status_code, 200)
        self.assertIn(b"Paged Person 02", filtered.data)
        self.assertNotIn(b"Paged Person 12", filtered.data)
        archived = self.client.get("/admin/settings/users?status=archived")
        self.assertEqual(archived.status_code, 200)
        self.assertIn(b"Archived Person", archived.data)
        self.assertIn(b"Archived Accounts</span><strong>1</strong>", archived.data)

    def test_user_management_profile_image_fallback_and_admin_tab_permission(self):
        self.set_session(self.founder_id)
        conn = dbh.get_db()
        conn.execute(
            "INSERT INTO users(full_name,email,password_hash,role,profile_picture,contact_number) "
            "VALUES(?,?,?,'resident','missing-avatar.png','09171234567')",
            ("Fallback Avatar", "fallback@example.test", generate_password_hash("SafePassword123")),
        )
        conn.commit()
        conn.close()
        page = self.client.get("/admin/settings/users?role=resident&status=all").get_data(as_text=True)
        self.assertIn("/uploads/profiles/missing-avatar.png", page)
        self.assertIn("onerror=\"this.hidden=true\"", page)
        self.assertIn(">FA</span>", page)
        self.assertIn("09171234567", page)
        self.assertIn("/admin/settings/users?role=admin&amp;status=all", self.client.get("/admin/settings/users").get_data(as_text=True))
        self.set_session(self.regular_id)
        self.assertEqual(self.client.get("/admin/settings/users?role=admin").status_code, 403)

    def test_user_management_staff_view_and_edit_use_dedicated_form(self):
        self.set_session(self.founder_id)
        staff_id = self.insert_bpso_staff()
        detail = self.client.get(f"/admin/staff/{staff_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"BPSO Staff", detail.data)
        edit = self.client.get(f"/admin/staff/{staff_id}/edit")
        self.assertEqual(edit.status_code, 200)
        self.assertIn(b'name="contact_number"', edit.data)
        saved = self.client.post(f"/admin/staff/{staff_id}/edit", data={
            "full_name": "Updated BPSO Staff", "email": "updated-bpso@example.test",
            "contact_number": "09171234567", "office": "bpso",
        })
        self.assertEqual(saved.status_code, 302)
        self.assertIn("/admin/settings/users?role=staff", saved.location)
        conn = dbh.get_db()
        row = conn.execute("SELECT full_name,email,contact_number FROM users WHERE id=?", (staff_id,)).fetchone()
        conn.close()
        self.assertEqual(tuple(row), ("Updated BPSO Staff", "updated-bpso@example.test", "+639171234567"))

    def test_staff_tab_restores_staff_creation_coverage_and_existing_controls(self):
        self.set_session(self.founder_id)
        bpso_id = self.insert_bpso_staff()
        conn = dbh.get_db()
        conn.execute(
            "INSERT INTO users(full_name,email,password_hash,role,office,status) "
            "VALUES(?,?,?,'staff','treasurer','active')",
            ("Treasurer Staff", "treasurer-staff@example.test", generate_password_hash("SafePassword123")),
        )
        conn.commit()
        conn.close()

        all_users = self.client.get("/admin/settings/users?status=all").get_data(as_text=True)
        resident_tab = self.client.get("/admin/settings/users?role=resident&status=all").get_data(as_text=True)
        self.assertNotIn("Create Staff Account", all_users)
        self.assertNotIn("Create Staff Account", resident_tab)

        page = self.client.get("/admin/settings/users?role=staff&status=all")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        for label in ("Staff Management", "Create Staff Account", "Full Name", "Email",
                      "Philippine Mobile Number", "Assigned Office", "Office Coverage",
                      "Barangay Treasurer", "Barangay Secretary", "BPSO (Public Safety)",
                      "Barangay Clearance", "Certificate of Indigency", "Certificate of Residency",
                      "Complaints", "Active Workload", "Reset password"):
            self.assertIn(label, html)
        self.assertIn('data-office="treasurer" data-active-staff="1"', html)
        self.assertIn('data-office="bpso" data-active-staff="1"', html)
        self.assertIn("Treasurer Staff", html)
        self.assertIn("BPSO Staff", html)

        created = self.client.post("/admin/staff", data={
            "full_name": "Integrated Staff", "email": "integrated-staff@example.test",
            "contact_number": "09171230000", "office": "secretary",
        })
        self.assertEqual(created.status_code, 302)
        self.assertIn("/admin/settings/users?role=staff", created.location)
        conn = dbh.get_db()
        created_row = conn.execute(
            "SELECT id,office,position,contact_number,must_reset_pw,password_hash FROM users "
            "WHERE email='integrated-staff@example.test'"
        ).fetchone()
        conn.close()
        self.assertEqual((created_row["office"], created_row["position"], created_row["contact_number"]),
                         ("secretary", "Barangay Secretary", "+639171230000"))
        self.assertEqual(created_row["must_reset_pw"], 1)
        self.assertTrue(created_row["password_hash"])

        office_response = self.client.post(f"/admin/staff/{bpso_id}/office", data={"office": "treasurer"})
        self.assertEqual(office_response.status_code, 302)
        mobile_response = self.client.post(f"/admin/users/{bpso_id}/mobile", data={"contact_number": "09179998888"})
        self.assertEqual(mobile_response.status_code, 302)
        self.assertIn("/admin/settings/users?role=staff", mobile_response.location)
        conn = dbh.get_db()
        updated = conn.execute("SELECT office,contact_number FROM users WHERE id=?", (bpso_id,)).fetchone()
        conn.close()
        self.assertEqual((updated["office"], updated["contact_number"]), ("treasurer", "+639179998888"))

    def test_user_management_delete_preserves_audit_and_blocks_linked_service_history(self):
        self.set_session(self.founder_id)
        conn = dbh.get_db()
        deletable_id = conn.execute(
            "INSERT INTO users(full_name,email,password_hash,role,office) VALUES(?,?,?,'staff','bpso')",
            ("Delete Candidate", "delete-candidate@example.test", generate_password_hash("SafePassword123")),
        ).lastrowid
        actor = conn.execute("SELECT * FROM users WHERE id=?", (deletable_id,)).fetchone()
        dbh.log_action(conn, actor, "Test resident action", target="delete-candidate@example.test", module="accounts")
        dbh.notify(conn, deletable_id, "Resident notification")
        linked_resident_id = conn.execute(
            "INSERT INTO users(full_name,email,password_hash,role) VALUES(?,?,?,'resident')",
            ("Linked Resident", "linked-resident@example.test", generate_password_hash("SafePassword123")),
        ).lastrowid
        blocked_id = conn.execute(
            "INSERT INTO users(full_name,email,password_hash,role,office) VALUES(?,?,?,'staff','bpso')",
            ("Linked Staff", "linked-staff@example.test", generate_password_hash("SafePassword123")),
        ).lastrowid
        conn.execute(
            "INSERT INTO requests(reference_no,resident_id,request_type,assigned_staff_id) VALUES(?,?,?,?)",
            ("DELETE-GUARD-001", linked_resident_id, "Barangay Clearance", blocked_id),
        )
        conn.commit()
        conn.close()

        deleted = self.client.post("/admin/settings/users", data={
            "account_id": deletable_id, "operation": "delete",
        })
        self.assertEqual(deleted.status_code, 302)
        conn = dbh.get_db()
        self.assertIsNone(conn.execute("SELECT id FROM users WHERE id=?", (deletable_id,)).fetchone())
        self.assertEqual(conn.execute("SELECT COUNT(*) AS count FROM notifications WHERE user_id=?", (deletable_id,)).fetchone()["count"], 0)
        history = conn.execute(
            "SELECT actor_id,actor_name FROM audit_logs WHERE action='Test resident action' AND target=?",
            ("delete-candidate@example.test",),
        ).fetchone()
        conn.close()
        self.assertIsNone(history["actor_id"])
        self.assertEqual(history["actor_name"], "Delete Candidate")

        blocked = self.client.post("/admin/settings/users", data={
            "account_id": blocked_id, "operation": "delete",
        })
        self.assertEqual(blocked.status_code, 302)
        self.assertIn(b"cannot be deleted", self.client.get(blocked.location).data)
        conn = dbh.get_db()
        self.assertIsNotNone(conn.execute("SELECT id FROM users WHERE id=?", (blocked_id,)).fetchone())
        self.assertEqual(conn.execute("SELECT COUNT(*) AS count FROM requests WHERE assigned_staff_id=?", (blocked_id,)).fetchone()["count"], 1)
        conn.close()

        protected = self.client.post("/admin/settings/users", data={
            "account_id": self.founder_id, "operation": "delete",
        })
        self.assertEqual(protected.status_code, 403)

    def test_admin_sidebar_and_settings_dropdown_are_centralized(self):
        self.set_session(self.founder_id)
        response = self.client.get("/admin/dashboard")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        sidebar = html.split('<aside class="sidebar">', 1)[1].split("</aside>", 1)[0]
        for removed in ("Manage Residents", "Manage Staff", "Administrators", "System Settings"):
            self.assertNotIn(removed, sidebar)
        for item in ("User Management", "Manage Administrators", "Barangay Information",
                     "Database Backup", "Security Settings"):
            self.assertIn(item, html)
        for path in ("/admin/settings/users", "/admin/administrators",
                     "/admin/settings/barangay-information", "/admin/settings/backups",
                     "/admin/settings/security"):
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_sqlite_archive_migration_adds_columns_without_losing_accounts(self):
        legacy_path = os.path.join(self.temp.name, "legacy-users.db")
        with open(dbh.SCHEMA_PATH, encoding="utf-8") as schema_file:
            schema = schema_file.read()
        schema = schema.replace("    is_archived     INTEGER NOT NULL DEFAULT 0,\n", "")
        schema = schema.replace("    archived_at     TEXT,\n", "")
        schema = schema.replace("    auth_version    INTEGER NOT NULL DEFAULT 0,\n", "")
        conn = sqlite3.connect(legacy_path)
        conn.executescript(schema)
        conn.execute(
            "INSERT INTO users(full_name,email,password_hash,role) VALUES(?,?,?,'resident')",
            ("Preserved Resident", "preserved@example.test", generate_password_hash("ResidentPassword123")),
        )
        conn.commit()
        conn.close()
        with patch.object(dbh, "DB_PATH", legacy_path):
            dbh.migrate_db()
            conn = dbh.get_db()
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
            existing = conn.execute("SELECT full_name,email FROM users").fetchone()
            conn.close()
        self.assertTrue({"is_archived", "archived_at", "auth_version"}.issubset(columns))
        self.assertEqual(tuple(existing), ("Preserved Resident", "preserved@example.test"))

    def test_user_management_supports_legacy_schema_without_founder_marker(self):
        legacy_path = os.path.join(self.temp.name, "legacy-founder-marker.db")
        with open(dbh.SCHEMA_PATH, encoding="utf-8") as schema_file:
            schema = schema_file.read()
        schema = schema.replace("    is_founding_admin INTEGER NOT NULL DEFAULT 0, -- single system owner; also a Super Admin\n", "")
        schema = schema.replace(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_single_founding_admin\n"
            "    ON users(is_founding_admin)\n"
            "    WHERE role='admin' AND is_founding_admin=1;\n\n", "",
        )
        conn = sqlite3.connect(legacy_path)
        conn.executescript(schema)
        conn.execute(
            "INSERT INTO users(full_name,email,password_hash,role,is_super_admin) VALUES(?,?,?,'admin',1)",
            ("Legacy Founder", "ralphpatrickga@gmail.com", generate_password_hash("LegacyFounderPassword123")),
        )
        conn.commit()
        conn.close()
        client = A.app.test_client()
        with patch.object(dbh, "DB_PATH", legacy_path), patch.object(A, "_db_ready", True):
            conn = dbh.get_db()
            founder = conn.execute("SELECT * FROM users WHERE role='admin'").fetchone()
            conn.close()
            with client.session_transaction() as state:
                state["user_id"] = founder["id"]
                state["role"] = "admin"
                state["auth_version"] = founder["auth_version"]
                state["admin_mfa_verified_at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                state["admin_mfa_proof"] = A._admin_session_proof(founder)
                state["admin_last_activity_at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
            response = client.get("/admin/settings/users")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Legacy Founder", response.data)

    def test_current_user_is_loaded_once_per_request(self):
        with A.app.test_request_context("/admin/administrators"):
            session["user_id"] = self.founder_id
            session["role"] = "admin"
            conn = dbh.get_db()
            user = conn.execute("SELECT * FROM users WHERE id=?", (self.founder_id,)).fetchone()
            conn.close()
            session["auth_version"] = int(user["auth_version"] or 0)
            session["admin_mfa_verified_at"] = datetime.now().isoformat()
            session["admin_mfa_proof"] = A._admin_session_proof(user)
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
            "recovery_email": "recovery@example.test", "password": "StrongPassword123",
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
            "password": "ResidentPassword123", "address": "Purok 2", "contact_number": "09171234567",
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
        }).status_code, 403)
        conn = dbh.get_db()
        denied = conn.execute(
            "SELECT result FROM audit_logs WHERE action='Denied staff role change' "
            "AND target='role@example.test' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertEqual(denied["result"], "Denied")

        staff_id = self.insert_bpso_staff()
        self.assertEqual(self.client.post("/admin/settings/users", data={
            "account_id": staff_id, "operation": "role", "role": "resident", "office": "bpso",
        }).status_code, 403)
        conn = dbh.get_db()
        self.assertEqual(conn.execute("SELECT role FROM users WHERE id=?", (staff_id,)).fetchone()[0], "staff")
        conn.close()

        self.set_session(self.founder_id)
        denied_escalation = self.client.post("/admin/settings/users", data={
            "account_id": staff_id, "operation": "role", "role": "admin", "office": "bpso",
        })
        self.assertEqual(denied_escalation.status_code, 400)
        allowed_staff_change = self.client.post("/admin/settings/users", data={
            "account_id": staff_id, "operation": "role", "role": "staff", "office": "treasurer",
        })
        self.assertEqual(allowed_staff_change.status_code, 302)
        conn = dbh.get_db()
        changed = conn.execute("SELECT role,office FROM users WHERE id=?", (staff_id,)).fetchone()
        success_log = conn.execute(
            "SELECT result FROM audit_logs WHERE action='Changed user role' AND target='bpso@example.test' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertEqual(tuple(changed), ("staff", "treasurer"))
        self.assertEqual(success_log["result"], "Success")

        for admin_id in (self.regular_id, self.founder_id):
            self.assertEqual(self.client.post("/admin/settings/users", data={
                "account_id": admin_id, "operation": "role", "role": "staff", "office": "bpso",
            }).status_code, 403)
        conn = dbh.get_db()
        denied_admin_attempts = conn.execute(
            "SELECT COUNT(*) AS count FROM audit_logs WHERE action='Denied staff role change' "
            "AND result='Denied' AND target IN ('regular@example.test','ralphpatrickga@gmail.com')"
        ).fetchone()["count"]
        conn.close()
        self.assertEqual(denied_admin_attempts, 2)

        self.assertEqual(self.client.post("/admin/settings/users", data={
            "account_id": self.founder_id, "operation": "status",
        }).status_code, 403)
        conn = dbh.get_db()
        self.assertEqual(conn.execute("SELECT role FROM users WHERE id=?", (resident_id,)).fetchone()[0], "resident")
        conn.close()

    def test_resident_rows_have_no_role_controls_or_role_confirmation_modal(self):
        self.set_session(self.founder_id)
        resident_id = self.client.post("/admin/residents/new", data={
            "full_name": "No Role Control Resident", "email": "no-role-control@example.test",
            "password": "ResidentPassword123", "address": "Purok 7",
        })
        self.assertEqual(resident_id.status_code, 302)
        conn = dbh.get_db()
        resident_id = conn.execute("SELECT id FROM users WHERE email='no-role-control@example.test'").fetchone()["id"]
        conn.close()
        for url in ("/admin/settings/users?role=resident&status=all", "/admin/settings/users?status=all"):
            html = self.client.get(url).get_data(as_text=True)
            self.assertNotIn(f'for="role-{resident_id}"', html)
            start = html.index("no-role-control@example.test")
            resident_row = html[html.rfind("<tr>", 0, start):html.index("</tr>", start)]
            self.assertNotIn("Change role", resident_row)
            self.assertNotIn("Delete permanently", resident_row)
        residents = self.client.get("/admin/settings/users?role=resident&status=all").get_data(as_text=True)
        self.assertNotIn('id="um-role-confirm"', residents)
        self.assertNotIn("Change role / office", residents)
        self.assertIn("View No Role Control Resident", residents)

        direct = self.client.post("/admin/settings/users", data={
            "account_id": resident_id, "operation": "role", "role": "staff", "office": "bpso",
        })
        self.assertEqual(direct.status_code, 403)
        delete_resident = self.client.post("/admin/settings/users", data={
            "account_id": resident_id, "operation": "delete",
        })
        self.assertEqual(delete_resident.status_code, 403)
        conn = dbh.get_db()
        row = conn.execute("SELECT role FROM users WHERE id=?", (resident_id,)).fetchone()
        log = conn.execute(
            "SELECT result FROM audit_logs WHERE action='Denied staff role change' "
            "AND target='no-role-control@example.test' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertEqual(row["role"], "resident")
        self.assertEqual(log["result"], "Denied")

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
