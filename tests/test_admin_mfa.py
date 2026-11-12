"""MFA tests use an isolated SQLite database and mocked outbound email."""
import os
import re
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from werkzeug.security import generate_password_hash
import app as A
import db as dbh


class AdminMFATests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for mocker in (
            patch.dict(os.environ, {"DB_BACKEND": "sqlite"}),
            patch.object(dbh, "DB_PATH", os.path.join(self.temp.name, "mfa.db")),
            patch.object(dbh, "auto_backup_if_due"),
            patch.object(A, "_db_ready", True),
            patch.dict(A.app.config, {"TESTING": True}),
        ):
            mocker.start()
            self.addCleanup(mocker.stop)
        dbh.init_db()
        self.conn_execute(
            "INSERT INTO users(full_name,email,password_hash,role,is_super_admin,is_founding_admin) VALUES (?,?,?,'admin',1,1)",
            ("Founding Admin", "admin@example.test", generate_password_hash("Admin@1234")),
        )
        self.client = A.app.test_client()
        email = patch.object(A.email_service, "send_email")
        self.send = email.start()
        self.addCleanup(email.stop)

    def conn_execute(self, sql, params=()):
        conn = dbh.get_db()
        try:
            result = conn.execute(sql, params)
            rows = result.fetchall() if result.description else []
            conn.commit()
            return rows
        finally:
            conn.close()

    def login(self, client=None):
        return (client or self.client).post("/login", data={"email": "admin@example.test", "password": "Admin@1234"})

    def code(self):
        return re.search(r"code is (\d{6})", self.send.call_args.args[2]).group(1)

    def post(self, path, code=None, client=None):
        client = client or self.client
        with client.session_transaction() as state:
            token = state.get("mfa_csrf", "")
        return client.post(path, data={"otp": code or "", "csrf_token": token})

    def challenge(self):
        return self.conn_execute("SELECT * FROM admin_otp_challenges WHERE user_id=1")[0]

    def age_send(self):
        self.conn_execute("UPDATE admin_otp_challenges SET sent_at=?", ((datetime.now(timezone.utc).replace(tzinfo=None)-timedelta(seconds=61)).isoformat(),))

    def assert_pending(self):
        with self.client.session_transaction() as state:
            self.assertNotIn("user_id", state)
            self.assertNotIn("role", state)

    def test_success_hashed_single_use_and_founding_privileges(self):
        self.assertEqual(self.login().location, "/verify-otp")
        code = self.code()
        self.assertEqual(self.send.call_args.args[0], "admin@example.test")
        row = self.challenge()
        self.assertNotIn(code, row["email_otp_hash"])
        self.assertEqual(len(row["email_otp_hash"]), 64)
        self.assertAlmostEqual((datetime.fromisoformat(row["expires_at"])-datetime.fromisoformat(row["sent_at"])).total_seconds(), 300)
        self.assert_pending()
        with self.client.session_transaction() as state:
            pending = dict(state)
        self.assertEqual(self.client.get("/verify-otp").status_code, 200)
        self.assertEqual(self.post("/verify-otp", code).location, "/admin/dashboard")
        self.assertIsNone(self.challenge()["email_otp_hash"])
        self.assertTrue(self.challenge()["used_at"])
        self.assertEqual(self.client.get("/admin/dashboard").status_code, 200)
        self.assertEqual(self.client.get("/admin/administrators").status_code, 200)
        # Replay even the original signed pending state cannot reuse a consumed OTP.
        with self.client.session_transaction() as state:
            state.clear()
            state.update(pending)
        self.assertEqual(self.post("/verify-otp", code).status_code, 400)
        self.assert_pending()

    def test_expired_otp(self):
        self.login()
        code = self.code()
        self.conn_execute("UPDATE admin_otp_challenges SET expires_at=?", ((datetime.now(timezone.utc).replace(tzinfo=None)-timedelta(seconds=1)).isoformat(),))
        self.assertEqual(self.post("/verify-otp", code).status_code, 400)
        self.assert_pending()

    def test_five_wrong_attempts_lock_verification_resend_and_new_login(self):
        self.login()
        code = self.code()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            self.assertEqual(self.post("/verify-otp", wrong).status_code, 400)
        self.assertEqual(self.challenge()["attempts"], 5)
        self.assertEqual(self.post("/verify-otp", code).status_code, 400)
        self.age_send()
        self.assertEqual(self.post("/resend-otp").status_code, 429)
        self.assertEqual(self.login(A.app.test_client()).status_code, 429)
        self.assertEqual(self.send.call_count, 1)
        self.assert_pending()

    def test_resend_cooldown_previous_code_and_attempt_budget(self):
        with patch("admin_mfa.secrets.randbelow", side_effect=[123456, 654321]):
            self.login()
            first = self.code()
            self.assertEqual(self.post("/resend-otp").status_code, 429)
            self.post("/verify-otp", "bad")
            self.age_send()
            self.assertEqual(self.post("/resend-otp").status_code, 302)
            second = self.code()
        self.assertNotEqual(first, second)
        self.assertEqual(self.challenge()["attempts"], 1)
        self.assertEqual(self.post("/verify-otp", first).status_code, 400)
        self.assertEqual(self.post("/verify-otp", second).status_code, 302)

    def test_hourly_send_cap_and_reset(self):
        self.login()
        for _ in range(4):
            self.age_send()
            self.assertEqual(self.post("/resend-otp").status_code, 302)
        self.age_send()
        self.assertEqual(self.post("/resend-otp").status_code, 429)
        self.assertEqual(self.login(A.app.test_client()).status_code, 429)
        self.assertEqual(self.send.call_count, 5)
        self.conn_execute("UPDATE admin_otp_challenges SET window_started_at=?", ((datetime.now(timezone.utc).replace(tzinfo=None)-timedelta(hours=2)).isoformat(),))
        self.assertEqual(self.post("/resend-otp").status_code, 302)
        self.assertEqual(self.challenge()["sends_in_window"], 1)

    def test_dashboard_and_reset_cannot_bypass_mfa(self):
        self.login()
        for path in ("/admin/dashboard", "/admin/administrators", "/first-login-reset"):
            self.assertEqual(self.client.get(path).location, "/login")
        self.assert_pending()

    def test_initial_admin_password_requires_mfa_before_reset_and_dashboard(self):
        self.conn_execute("UPDATE users SET must_reset_pw=1 WHERE id=1")
        self.login()
        self.assertEqual(self.post("/verify-otp", self.code()).location, "/first-login-reset")
        self.assert_pending()
        self.assertEqual(self.client.get("/first-login-reset").status_code, 200)
        response = self.client.post("/first-login-reset", data={"password": "Changed@123", "confirm_password": "Changed@123"})
        self.assertEqual(response.location, "/admin/dashboard")
        self.assertEqual(self.client.get("/admin/dashboard").status_code, 200)

    def test_legacy_pending_reset_cannot_bypass_mfa(self):
        self.conn_execute("UPDATE users SET must_reset_pw=1 WHERE id=1")
        with self.client.session_transaction() as state:
            state["pending_reset_user"] = 1
        response = self.client.post("/first-login-reset", data={"password": "Changed@123", "confirm_password": "Changed@123"})
        self.assertEqual(response.location, "/login")
        self.assert_pending()

    def test_email_failure_fails_closed_and_logs_no_secrets(self):
        self.send.side_effect = OSError("secret OTP 123456 SMTP_PASSWORD=hidden")
        with self.assertLogs(A.app.logger, level="ERROR") as logs:
            self.login()
        self.assertNotIn("123456", str(logs.output))
        self.assertNotIn("hidden", str(logs.output))
        self.assertIsNone(self.challenge()["email_otp_hash"])
        self.assertEqual(self.post("/verify-otp", "123456").status_code, 400)
        self.assert_pending()

    def test_csrf_required_for_verify_and_resend(self):
        self.login()
        for path in ("/verify-otp", "/resend-otp"):
            self.assertEqual(self.client.post(path, data={"otp": self.code()}).status_code, 400)
        self.assertEqual(self.challenge()["attempts"], 0)
        self.assertEqual(self.client.post("/verify-otp", data={"csrf_token": "\u2603"}).status_code, 400)

    def test_changed_credentials_or_inactive_account_reject_otp(self):
        self.login()
        code = self.code()
        self.conn_execute("UPDATE users SET password_hash=? WHERE id=1", (generate_password_hash("Changed@123"),))
        self.assertEqual(self.post("/verify-otp", code).location, "/login")
        self.assertEqual(self.post("/resend-otp").location, "/login")
        self.conn_execute("UPDATE users SET status='inactive' WHERE id=1")
        self.assertEqual(self.post("/verify-otp", code).location, "/login")
        self.assert_pending()

    def test_pending_session_expires_and_no_sensitive_cache(self):
        self.login()
        response = self.client.get("/verify-otp")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        with self.client.session_transaction() as state:
            pending = state["pending_mfa_user"]
            pending["started_at"] = (datetime.now(timezone.utc).replace(tzinfo=None)-timedelta(minutes=16)).isoformat()
            state["pending_mfa_user"] = pending
        self.assertEqual(self.post("/verify-otp", self.code()).location, "/login")

    def test_concurrent_verifications_only_one_consumes_code(self):
        self.login()
        code = self.code()
        with self.client.session_transaction() as state:
            pending = dict(state)
        clients = [A.app.test_client(), A.app.test_client()]
        for client in clients:
            with client.session_transaction() as state:
                state.update(pending)
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda client: self.post("/verify-otp", code, client), clients))
        self.assertEqual(sorted(response.status_code for response in responses), [302, 400])

    def test_resident_staff_and_normal_admin_roles(self):
        for role in ("resident", "staff"):
            email = f"{role}@example.test"
            self.conn_execute("INSERT INTO users(full_name,email,password_hash,role) VALUES (?,?,?,?)", (role, email, generate_password_hash("User@1234"), role))
            client = A.app.test_client()
            response = client.post("/login", data={"email": email, "password": "User@1234"})
            self.assertEqual(response.location, "/dashboard" if role == "resident" else "/staff/dashboard")
        self.send.assert_not_called()
        self.conn_execute("UPDATE users SET is_super_admin=0,is_founding_admin=0 WHERE id=1")
        self.login()
        self.post("/verify-otp", self.code())
        self.assertEqual(self.client.post("/admin/administrators", data={}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
