"""Administrator hierarchy permissions and ownership transfer safety."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

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
            "Founder", "founder@example.test", super_admin=1, founding_admin=1
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

    def test_founder_and_super_can_create_only_regular_admins(self):
        for user_id in (self.founder_id, self.super_id):
            response = self.create_admin(user_id)
            self.assertEqual(response.status_code, 302)
            conn = dbh.get_db()
            created = conn.execute(
                "SELECT is_super_admin,is_founding_admin FROM users WHERE email=?",
                ("new-admin@example.test",),
            ).fetchone()
            self.assertEqual((created["is_super_admin"], created["is_founding_admin"]), (0, 0))
            conn.execute("DELETE FROM users WHERE email='new-admin@example.test'")
            conn.commit()
            conn.close()

    def test_regular_admin_cannot_create_accounts_directly(self):
        self.assertEqual(self.create_admin(self.regular_id).status_code, 403)

    def test_duplicate_email_is_not_created(self):
        self.set_session(self.super_id)
        response = self.client.post("/admin/administrators", data={
            "full_name": "Duplicate", "email": "founder@example.test",
            "recovery_email": "recovery@example.test", "password": "Password123",
        })
        conn = dbh.get_db()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM users WHERE lower(email)='founder@example.test'"
        ).fetchone()[0], 1)
        conn.close()
        self.assertEqual(response.status_code, 302)

    def prepare_target_mfa(self, target_id, age_minutes=0):
        conn = dbh.get_db()
        conn.execute(
            "INSERT INTO admin_otp_challenges(user_id,mode,used_at) VALUES (?,'email_only',?)",
            (target_id, (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=age_minutes)).isoformat()),
        )
        conn.commit()
        conn.close()

    def transfer(self, email="ralphpatrick@gmail.com"):
        return self.client.post("/admin/administrators/transfer-founding-admin", data={
            "csrf_token": "csrf-test-token", "email": email, "confirmation": email,
        })

    def test_transfer_moves_founder_and_preserves_accounts_and_passwords(self):
        target_id = self.insert_user("Replacement", "ralphpatrick@gmail.com")
        self.prepare_target_mfa(target_id)
        self.set_session(self.founder_id, mfa=True)
        conn = dbh.get_db()
        before = conn.execute(
            "SELECT id,password_hash,status,is_super_admin,is_founding_admin FROM users "
            "WHERE id IN (?,?) ORDER BY id", (self.founder_id, target_id),
        ).fetchall()
        before_hashes = [row["password_hash"] for row in before]
        conn.close()

        response = self.transfer()
        self.assertEqual(response.status_code, 302)
        conn = dbh.get_db()
        rows = conn.execute(
            "SELECT id,password_hash,status,is_super_admin,is_founding_admin FROM users "
            "WHERE id IN (?,?) ORDER BY id", (self.founder_id, target_id),
        ).fetchall()
        self.assertEqual([(r["is_super_admin"], r["is_founding_admin"], r["status"]) for r in rows],
                         [(1, 0, "active"), (1, 1, "active")])
        self.assertEqual([row["password_hash"] for row in rows], before_hashes)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM users WHERE role='admin' AND is_founding_admin=1"
        ).fetchone()[0], 1)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action='Transferred Founding Admin ownership'"
        ).fetchone()[0], 1)
        conn.close()

    def test_transfer_requires_founder_recent_mfa_and_target_mfa(self):
        target_id = self.insert_user("Replacement", "ralphpatrick@gmail.com")
        self.prepare_target_mfa(target_id)
        self.set_session(self.super_id, mfa=True)
        self.assertEqual(self.transfer().status_code, 403)

        self.set_session(self.founder_id, mfa=True)
        with self.client.session_transaction() as state:
            state["admin_mfa_verified_at"] = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=11)).isoformat()
        self.assertEqual(self.transfer().status_code, 403)

        self.set_session(self.founder_id, mfa=True)
        conn = dbh.get_db()
        conn.execute("DELETE FROM admin_otp_challenges WHERE user_id=?", (target_id,))
        conn.commit()
        conn.close()
        self.assertEqual(self.transfer().status_code, 302)
        conn = dbh.get_db()
        self.assertEqual(conn.execute(
            "SELECT is_founding_admin FROM users WHERE id=?", (self.founder_id,)
        ).fetchone()[0], 1)
        conn.close()

    def test_transfer_rolls_back_if_audit_write_fails(self):
        target_id = self.insert_user("Replacement", "ralphpatrick@gmail.com")
        self.prepare_target_mfa(target_id)
        self.set_session(self.founder_id, mfa=True)
        with patch.object(A.dbh, "log_action", side_effect=RuntimeError("audit failure")):
            with self.assertRaises(RuntimeError):
                self.transfer()
        conn = dbh.get_db()
        founder = conn.execute(
            "SELECT is_super_admin,is_founding_admin,status FROM users WHERE id=?",
            (self.founder_id,),
        ).fetchone()
        target = conn.execute(
            "SELECT is_super_admin,is_founding_admin,status FROM users WHERE id=?",
            (target_id,),
        ).fetchone()
        self.assertEqual(tuple(founder), (1, 1, "active"))
        self.assertEqual(tuple(target), (0, 0, "active"))
        conn.close()


if __name__ == "__main__":
    unittest.main()
