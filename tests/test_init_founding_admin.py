import os
import secrets
import sys
import tempfile
import unittest
from unittest.mock import patch

from werkzeug.security import check_password_hash, generate_password_hash

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db as dbh
from init_founding_admin import FOUNDING_ADMIN_EMAIL, init_founding_admin


class InitFoundingAdminTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = dbh.DB_PATH
        dbh.DB_PATH = os.path.join(self.temp_dir.name, "founding-admin.db")
        dbh.init_db()
        self.password = secrets.token_urlsafe(18) + "A1"
        self.env = patch.dict(
            os.environ,
            {
                "FOUNDING_ADMIN_NAME": "Test Founding Admin",
                "FOUNDING_ADMIN_PASSWORD": self.password,
            },
            clear=True,
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        dbh.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    def test_creates_founder_when_no_administrator_exists(self):
        self.assertTrue(init_founding_admin())

        conn = dbh.get_db()
        founder = conn.execute(
            "SELECT * FROM users WHERE email=?", (FOUNDING_ADMIN_EMAIL,)
        ).fetchone()
        self.assertIsNotNone(founder)
        self.assertEqual(founder["full_name"], "Test Founding Admin")
        self.assertEqual(founder["role"], "admin")
        self.assertEqual(founder["is_super_admin"], 1)
        self.assertEqual(founder["is_founding_admin"], 1)
        self.assertEqual(founder["status"], "active")
        self.assertEqual(founder["must_reset_pw"], 1)
        self.assertEqual(founder["recovery_email"], FOUNDING_ADMIN_EMAIL)
        self.assertTrue(check_password_hash(founder["password_hash"], self.password))
        conn.close()

    def test_initializes_a_missing_database_at_the_configured_path(self):
        dbh.DB_PATH = os.path.join(self.temp_dir.name, "new", "initial.db")
        self.assertFalse(os.path.exists(dbh.DB_PATH))

        self.assertTrue(init_founding_admin())

        self.assertTrue(os.path.isfile(dbh.DB_PATH))
        conn = dbh.get_db()
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM users WHERE role='admin'"
            ).fetchone()[0],
            1,
        )
        conn.close()

    def test_existing_founder_is_left_unchanged_without_credentials(self):
        conn = dbh.get_db()
        conn.execute(
            "INSERT INTO users "
            "(full_name,email,recovery_email,password_hash,role,is_super_admin,is_founding_admin,must_reset_pw) "
            "VALUES (?,?,?,?, 'admin',1,1,0)",
            (
                "Existing Founder",
                FOUNDING_ADMIN_EMAIL,
                "recovery@example.test",
                generate_password_hash("ExistingPassword123"),
            ),
        )
        conn.commit()
        before = dict(conn.execute(
            "SELECT * FROM users WHERE email=?", (FOUNDING_ADMIN_EMAIL,)
        ).fetchone())
        conn.close()

        with patch.dict(
            os.environ,
            {"FOUNDING_ADMIN_NAME": "", "FOUNDING_ADMIN_PASSWORD": ""},
            clear=True,
        ):
            self.assertFalse(init_founding_admin())

        conn = dbh.get_db()
        after = dict(conn.execute(
            "SELECT * FROM users WHERE email=?", (FOUNDING_ADMIN_EMAIL,)
        ).fetchone())
        conn.close()
        self.assertEqual(after, before)

    def test_any_existing_administrator_prevents_creation_and_is_unchanged(self):
        conn = dbh.get_db()
        admin_id = conn.execute(
            "INSERT INTO users (full_name,email,password_hash,role) "
            "VALUES (?,?,?,'admin')",
            (
                "Ordinary Administrator",
                "ordinary-admin@example.test",
                generate_password_hash("ExistingPassword123"),
            ),
        ).lastrowid
        conn.execute(
            "INSERT INTO users (full_name,email,password_hash,role) "
            "VALUES (?,?,?,'resident')",
            (
                "Resident",
                "resident@example.test",
                generate_password_hash("ResidentPassword123"),
            ),
        )
        conn.commit()
        before = dict(conn.execute(
            "SELECT * FROM users WHERE id=?", (admin_id,)
        ).fetchone())
        conn.close()

        self.assertFalse(init_founding_admin())

        conn = dbh.get_db()
        admins = conn.execute(
            "SELECT * FROM users WHERE role='admin'"
        ).fetchall()
        after = dict(conn.execute(
            "SELECT * FROM users WHERE id=?", (admin_id,)
        ).fetchone())
        conn.close()
        self.assertEqual(len(admins), 1)
        self.assertEqual(after, before)

    def test_invalid_credentials_do_not_create_or_modify_accounts(self):
        with patch.dict(
            os.environ,
            {
                "FOUNDING_ADMIN_NAME": "Test Founding Admin",
                "FOUNDING_ADMIN_PASSWORD": "short",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "at least 12 characters"):
                init_founding_admin()

        conn = dbh.get_db()
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            0,
        )
        conn.close()

    def test_existing_non_admin_owner_of_designated_email_is_not_modified(self):
        conn = dbh.get_db()
        resident_id = conn.execute(
            "INSERT INTO users (full_name,email,password_hash,role) "
            "VALUES (?,?,?,'resident')",
            (
                "Existing Resident",
                FOUNDING_ADMIN_EMAIL,
                generate_password_hash("ResidentPassword123"),
            ),
        ).lastrowid
        conn.commit()
        before = dict(conn.execute(
            "SELECT * FROM users WHERE id=?", (resident_id,)
        ).fetchone())
        conn.close()

        with self.assertRaisesRegex(RuntimeError, "conflicting with existing data"):
            init_founding_admin()

        conn = dbh.get_db()
        after = dict(conn.execute(
            "SELECT * FROM users WHERE id=?", (resident_id,)
        ).fetchone())
        conn.close()
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
