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



if __name__ == "__main__":
    unittest.main()
