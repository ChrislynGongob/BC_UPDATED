import os
import secrets
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from werkzeug.security import check_password_hash, generate_password_hash

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db as dbh
from setup_founding_admin import FOUNDING_ADMIN_EMAIL, setup_founding_admin


class FoundingAdminSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = dbh.DB_PATH
        dbh.DB_PATH = os.path.join(self.temp_dir.name, "founding-admin.db")
        dbh.init_db()
        self.password = secrets.token_urlsafe(18) + "A1"
        self.env = patch.dict(
            os.environ,
            {
                "FOUNDING_ADMIN_NAME": "Ralph Patrick Ga",
                "FOUNDING_ADMIN_PASSWORD": self.password,
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        dbh.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    def test_existing_founder_and_password_are_never_overwritten(self):
        conn = dbh.get_db()
        founder_id = conn.execute(
            "INSERT INTO users (full_name,email,password_hash,role,is_super_admin) "
            "VALUES (?,?,?,?,1)",
            (
                "Old Founding Admin",
                "old-founder@example.test",
                generate_password_hash("old-password"),
                "admin",
            ),
        ).lastrowid
        conn.execute(
            "INSERT INTO notifications (user_id,message) VALUES (?,?)",
            (founder_id, "Existing notification"),
        )
        conn.execute(
            "INSERT INTO audit_logs (actor_id,actor_name,action) VALUES (?,?,?)",
            (founder_id, "Old Founding Admin", "Existing audit history"),
        )
        conn.commit()
        conn.close()

        with self.assertRaisesRegex(RuntimeError, "already exists"):
            setup_founding_admin()

        conn = dbh.get_db()
        account = conn.execute(
            "SELECT * FROM users WHERE id=?", (founder_id,)
        ).fetchone()
        self.assertEqual(account["email"], "old-founder@example.test")
        self.assertIsNone(account["recovery_email"])
        self.assertTrue(check_password_hash(account["password_hash"], "old-password"))
        self.assertFalse(check_password_hash(account["password_hash"], self.password))
        self.assertEqual(account["role"], "admin")
        self.assertEqual(account["is_super_admin"], 1)
        self.assertEqual(account["must_reset_pw"], 0)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM users WHERE role='admin' AND is_super_admin=1"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE user_id=?", (founder_id,)
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE actor_id=? AND action=?",
                (founder_id, "Existing audit history"),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE actor_id=? AND action LIKE 'Updated Founding Admin%'",
                (founder_id,),
            ).fetchone()[0],
            0,
        )
        conn.close()

    def test_refuses_to_take_email_from_another_account(self):
        conn = dbh.get_db()
        founder_id = conn.execute(
            "INSERT INTO users (full_name,email,password_hash,role,is_super_admin) "
            "VALUES (?,?,?,?,1)",
            (
                "Old Founding Admin",
                "old-founder@example.test",
                generate_password_hash("old-password"),
                "admin",
            ),
        ).lastrowid
        conn.execute(
            "INSERT INTO users (full_name,email,password_hash,role) VALUES (?,?,?,'resident')",
            (
                "Existing Email Owner",
                FOUNDING_ADMIN_EMAIL,
                generate_password_hash("other-password"),
            ),
        )
        conn.commit()
        conn.close()

        with self.assertRaisesRegex(ValueError, "belongs to another account"):
            setup_founding_admin()

        conn = dbh.get_db()
        founder = conn.execute(
            "SELECT email FROM users WHERE id=?", (founder_id,)
        ).fetchone()
        self.assertEqual(founder["email"], "old-founder@example.test")
        conn.close()

    def test_creates_founder_alongside_existing_admin_without_changing_it(self):
        conn = dbh.get_db()
        conn.execute(
            "INSERT INTO users (full_name,email,password_hash,role) VALUES (?,?,?,'admin')",
            (
                "Ordinary Admin",
                "ordinary@example.test",
                generate_password_hash("ordinary-admin-password"),
            ),
        )
        conn.execute(
            "INSERT INTO users (full_name,email,password_hash,role) "
            "VALUES (?,?,?,'resident')",
            (
                "Existing Resident",
                "resident@example.test",
                generate_password_hash("existing-resident-password"),
            ),
        )
        conn.commit()
        before_accounts = {
            row["email"]: dict(row)
            for row in conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        }
        conn.close()

        setup_founding_admin()

        conn = dbh.get_db()
        after_accounts = {
            row["email"]: dict(row)
            for row in conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        }
        founder = conn.execute(
            "SELECT * FROM users WHERE email=?", (FOUNDING_ADMIN_EMAIL,)
        ).fetchone()
        for email, account in before_accounts.items():
            self.assertEqual(after_accounts[email], account)
        self.assertEqual(founder["role"], "admin")
        self.assertEqual(founder["is_super_admin"], 1)
        self.assertEqual(len(after_accounts), 3)
        self.assertTrue(
            check_password_hash(
                after_accounts["ordinary@example.test"]["password_hash"],
                "ordinary-admin-password",
            )
        )
        self.assertTrue(
            check_password_hash(
                after_accounts["resident@example.test"]["password_hash"],
                "existing-resident-password",
            )
        )
        conn.close()

    def test_rerun_does_not_change_founder_or_reset_password(self):
        setup_founding_admin()

        conn = dbh.get_db()
        before = dict(conn.execute(
            "SELECT * FROM users WHERE email=?", (FOUNDING_ADMIN_EMAIL,)
        ).fetchone())
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE target=?", (FOUNDING_ADMIN_EMAIL,)
        ).fetchone()[0]
        conn.close()

        with patch.dict(
            os.environ,
            {
                "FOUNDING_ADMIN_NAME": "Changed Name",
                "FOUNDING_ADMIN_PASSWORD": "DifferentPassword456",
            },
        ):
            setup_founding_admin()

        conn = dbh.get_db()
        after = dict(conn.execute(
            "SELECT * FROM users WHERE email=?", (FOUNDING_ADMIN_EMAIL,)
        ).fetchone())
        self.assertEqual(after, before)
        self.assertTrue(check_password_hash(after["password_hash"], self.password))
        self.assertFalse(check_password_hash(after["password_hash"], "DifferentPassword456"))
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE target=?", (FOUNDING_ADMIN_EMAIL,)
            ).fetchone()[0],
            audit_count,
        )
        conn.close()

    def test_requires_name_and_a_minimum_length_password(self):
        invalid_environments = (
            {"FOUNDING_ADMIN_NAME": "", "FOUNDING_ADMIN_PASSWORD": self.password},
            {"FOUNDING_ADMIN_NAME": "Test Admin", "FOUNDING_ADMIN_PASSWORD": "Short1"},
            {"FOUNDING_ADMIN_NAME": "Test Admin", "FOUNDING_ADMIN_PASSWORD": ""},
        )
        for environment in invalid_environments:
            with self.subTest(environment=environment), patch.dict(
                os.environ, environment, clear=True
            ):
                with self.assertRaises(ValueError):
                    setup_founding_admin()

    def test_migrates_legacy_recovery_email_and_enforces_single_founder(self):
        legacy_db_path = os.path.join(self.temp_dir.name, "legacy.db")
        with open(dbh.SCHEMA_PATH, encoding="utf-8") as schema_file:
            schema = schema_file.read()
        schema = schema.replace(
            "profile_picture TEXT,                 -- stored filename of uploaded profile photo, NULL if none",
            "profile_picture TEXT                  -- stored filename of uploaded profile photo, NULL if none",
        )
        schema = schema.replace("    recovery_email  TEXT\n", "")
        schema = schema.replace(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_single_founding_admin\n"
            "    ON users(is_super_admin)\n"
            "    WHERE role='admin' AND is_super_admin=1;\n\n",
            "",
        )
        conn = sqlite3.connect(legacy_db_path)
        conn.executescript(schema)
        conn.execute(
            "INSERT INTO users (full_name,email,password_hash,role,is_super_admin,address) "
            "VALUES (?,?,?,?,1,?)",
            (
                "Legacy Founder",
                FOUNDING_ADMIN_EMAIL,
                generate_password_hash("legacy-password"),
                "admin",
                "recovery:legacy@example.test",
            ),
        )
        conn.commit()
        conn.close()

        with patch.object(dbh, "DB_PATH", legacy_db_path):
            dbh.migrate_db()
            conn = dbh.get_db()
            account = conn.execute(
                "SELECT recovery_email FROM users WHERE email=?",
                (FOUNDING_ADMIN_EMAIL,),
            ).fetchone()
            self.assertEqual(account["recovery_email"], "legacy@example.test")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO users (full_name,email,password_hash,role,is_super_admin) "
                    "VALUES (?,?,?,?,1)",
                    (
                        "Second Founder",
                        "second-founder@example.test",
                        generate_password_hash("second-password"),
                        "admin",
                    ),
                )
            conn.close()


if __name__ == "__main__":
    unittest.main()
