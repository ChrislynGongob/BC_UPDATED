import os
import random
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from werkzeug.security import check_password_hash, generate_password_hash

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db as dbh  # noqa: E402

_boot_dir = tempfile.mkdtemp()
dbh.DB_PATH = os.path.join(_boot_dir, "boot.db")
import app as A  # noqa: E402
from tests.session_helpers import authenticate


class SecurityAndAllowanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._old_path = dbh.DB_PATH
        cls._random_state = random.getstate()
        cls._dir = tempfile.mkdtemp()
        dbh.DB_PATH = os.path.join(cls._dir, "test.db")
        dbh._last_auto_check = time.time()
        dbh.init_db()
        conn = dbh.get_db()
        conn.execute(
            "INSERT INTO users (full_name,email,password_hash,role,is_super_admin,contact_number) "
            "VALUES (?,?,?,?,1,?)",
            ("Ralph Patrick Ga", "ralphpatrickga@gmail.com", generate_password_hash("Admin@1234"), "admin", "+639171000001"),
        )
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        random.setstate(cls._random_state)
        dbh.DB_PATH = cls._old_path

    def create_resident(self, email):
        conn = dbh.get_db()
        cursor = conn.execute(
            "INSERT INTO users (full_name,email,password_hash,role) VALUES (?,?,?,'resident')",
            ("Test Resident", email, generate_password_hash("Resident@1234")),
        )
        conn.commit()
        resident_id = cursor.lastrowid
        conn.close()
        return resident_id

    def test_registration_allows_email_only_and_normalizes_optional_mobile(self):
        client = A.app.test_client()
        data = {
            "full_name": "New Resident",
            "email": "new-resident@example.test",
            "password": "Resident@1234",
            "confirm_password": "Resident@1234",
            "address": "Purok 1",
        }
        response = client.post("/register", data=data)
        self.assertEqual(response.status_code, 302)
        conn = dbh.get_db()
        self.assertIsNone(conn.execute(
            "SELECT contact_number FROM users WHERE email=?", (data["email"],)
        ).fetchone()["contact_number"])
        conn.close()

        data["email"] = "new-resident-with-mobile@example.test"
        data["contact_number"] = "0917 123 4567"
        response = client.post("/register", data=data)
        self.assertEqual(response.status_code, 302)
        conn = dbh.get_db()
        contact = conn.execute(
            "SELECT contact_number FROM users WHERE email=?", (data["email"],)
        ).fetchone()["contact_number"]
        conn.close()
        self.assertEqual(contact, "+639171234567")

    def resident_client(self, resident_id):
        client = A.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = resident_id
            session["role"] = "resident"
        return client

    def admin_client(self):
        return authenticate(A.app.test_client(), 1)

    def test_smtp_service_uses_configured_tls_auth_and_message(self):
        config = {
            "SMTP_HOST": "smtp.example.test",
            "SMTP_PORT": "587",
            "SMTP_USERNAME": "mailer@example.test",
            "SMTP_PASSWORD": "test-secret",
            "SMTP_FROM": "BarangayConnect <no-reply@example.test>",
            "SMTP_USE_TLS": "true",
            "SMTP_USE_SSL": "false",
        }
        smtp = MagicMock()
        smtp.__enter__.return_value = smtp
        with patch.dict(os.environ, config, clear=True), patch.object(
            A.email_service.ssl, "create_default_context", return_value=MagicMock()
        ), patch.object(
            A.email_service.smtplib, "SMTP", return_value=smtp
        ) as smtp_constructor:
            A.email_service.send_email("resident@example.test", "Test subject", "Test body")
        smtp_constructor.assert_called_once_with("smtp.example.test", 587, timeout=15)
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("mailer@example.test", "test-secret")
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["To"], "resident@example.test")
        self.assertEqual(message["Subject"], "Test subject")
        self.assertIn("Test body", message.get_content())

    def test_brevo_service_sends_text_and_html_and_needs_no_smtp_settings(self):
        config = {
            "EMAIL_PROVIDER": "brevo",
            "BREVO_API_KEY": "brevo-test-secret",
            "SMTP_FROM": "BarangayConnect <no-reply@example.test>",
        }
        response = MagicMock(status_code=201)
        with patch.dict(os.environ, config, clear=True), patch.object(
            A.email_service.requests, "post", return_value=response
        ) as post:
            A.email_service.send_email(
                "resident@example.test",
                "Test subject",
                "Plain text",
                html_body="<p>HTML content</p>",
            )

        post.assert_called_once_with(
            "https://api.brevo.com/v3/smtp/email",
            json={
                "sender": {"name": "BarangayConnect", "email": "no-reply@example.test"},
                "to": [{"email": "resident@example.test"}],
                "subject": "Test subject",
                "textContent": (
                    "Plain text\n\n"
                    "This is an automated message. Please do not reply to this email."
                ),
                "htmlContent": (
                    "<p>HTML content</p>"
                    "<p>This is an automated message. Please do not reply to this email.</p>"
                ),
            },
            headers={"api-key": "brevo-test-secret", "accept": "application/json"},
            timeout=(5, 15),
        )

    def test_brevo_reply_to_is_optional_and_validated(self):
        config = {
            "EMAIL_PROVIDER": "brevo",
            "BREVO_API_KEY": "brevo-test-secret",
            "SMTP_FROM": "verified-sender@example.test",
            "BREVO_REPLY_TO": "helpdesk@example.test",
        }
        response = MagicMock(status_code=201)
        with patch.dict(os.environ, config, clear=True), patch.object(
            A.email_service.requests, "post", return_value=response
        ) as post:
            A.email_service.send_email("resident@example.test", "Subject", "Body")

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["replyTo"], {"email": "helpdesk@example.test"})
        self.assertEqual(
            payload["sender"],
            {"name": "BarangayConnect", "email": "verified-sender@example.test"},
        )
        self.assertIn("This is an automated message.", payload["textContent"])

        config["BREVO_REPLY_TO"] = "invalid-address"
        with patch.dict(os.environ, config, clear=True), patch.object(
            A.email_service.requests, "post"
        ) as post:
            with self.assertRaisesRegex(
                A.email_service.EmailConfigurationError, "BREVO_REPLY_TO"
            ):
                A.email_service.send_email(
                    "resident@example.test", "Subject", "Body"
                )
        post.assert_not_called()

    def test_brevo_service_requires_api_key(self):
        config = {
            "EMAIL_PROVIDER": "brevo",
            "SMTP_FROM": "no-reply@example.test",
        }
        with patch.dict(os.environ, config, clear=True):
            with self.assertRaisesRegex(
                A.email_service.EmailConfigurationError, "BREVO_API_KEY"
            ):
                A.email_service.send_email("resident@example.test", "Subject", "Body")

    def test_brevo_service_requires_sender_from_environment_without_fallback(self):
        config = {
            "EMAIL_PROVIDER": "brevo",
            "BREVO_API_KEY": "brevo-test-secret",
        }
        with patch.dict(os.environ, config, clear=True), patch.object(
            A.email_service.requests, "post"
        ) as post:
            with self.assertRaisesRegex(
                A.email_service.EmailConfigurationError, "SMTP_FROM"
            ):
                A.email_service.send_email(
                    "resident@example.test", "Subject", "Body"
                )
        post.assert_not_called()

    def test_brevo_service_rejects_invalid_sender(self):
        config = {
            "EMAIL_PROVIDER": "brevo",
            "BREVO_API_KEY": "brevo-test-secret",
            "SMTP_FROM": "not-an-email",
        }
        with patch.dict(os.environ, config, clear=True), patch.object(
            A.email_service.requests, "post"
        ) as post:
            with self.assertRaisesRegex(
                A.email_service.EmailConfigurationError, "SMTP_FROM"
            ):
                A.email_service.send_email("resident@example.test", "Subject", "Body")
        post.assert_not_called()

    def test_brevo_authentication_error_is_safe_and_not_accepted(self):
        config = {
            "EMAIL_PROVIDER": "brevo",
            "BREVO_API_KEY": "brevo-test-secret",
            "SMTP_FROM": "no-reply@example.test",
        }
        response = MagicMock(status_code=401)
        with patch.dict(os.environ, config, clear=True), patch.object(
            A.email_service.requests, "post", return_value=response
        ):
            with self.assertLogs("email_service", level="ERROR") as logs:
                with self.assertRaises(A.email_service.EmailDeliveryError) as raised:
                    A.email_service.send_email(
                        "resident@example.test", "Subject", "sensitive-otp-123456"
                    )

        self.assertEqual(raised.exception.status_code, 401)
        self.assertNotIn("brevo-test-secret", "\n".join(logs.output))
        self.assertNotIn("123456", "\n".join(logs.output))
        self.assertIn("status=401", "\n".join(logs.output))

    def test_brevo_authentication_failure_keeps_reset_response_generic(self):
        email = "brevo-auth-failure@example.test"
        user_id = self.create_resident(email)
        config = {
            "EMAIL_PROVIDER": "brevo",
            "BREVO_API_KEY": "brevo-test-secret",
            "SMTP_FROM": "no-reply@example.test",
        }
        response = MagicMock(status_code=401)
        client = A.app.test_client()
        with patch.dict(os.environ, config, clear=True), patch.object(
            A.email_service.requests, "post", return_value=response
        ) as post, self.assertLogs(level="ERROR") as logs:
            failed = client.post(
                "/forgot-password", data={"email": email}, follow_redirects=True
            )
        unknown = client.post(
            "/forgot-password",
            data={"email": "no-such-brevo-user@example.test"},
            follow_redirects=True,
        )

        payload = post.call_args.kwargs["json"]
        code = re.search(r"code is (\d{6})", payload["textContent"]).group(1)
        output = "\n".join(logs.output)
        generic_notice = (
            "Password reset instructions will be sent to your inbox. "
            "Please check your email to continue."
        )
        self.assertIn(generic_notice, failed.get_data(as_text=True))
        self.assertIn(generic_notice, unknown.get_data(as_text=True))
        self.assertNotIn("brevo-test-secret", output)
        self.assertNotIn(code, output)
        self.assertIn("status=401", output)
        conn = dbh.get_db()
        reset_code = conn.execute(
            "SELECT code_hash, used_at FROM password_reset_codes WHERE user_id=?",
            (user_id,),
        ).fetchone()
        conn.close()
        self.assertIsNone(reset_code["code_hash"])
        self.assertIsNotNone(reset_code["used_at"])

    def test_brevo_timeout_and_network_errors_are_safe(self):
        config = {
            "EMAIL_PROVIDER": "brevo",
            "BREVO_API_KEY": "brevo-test-secret",
            "SMTP_FROM": "no-reply@example.test",
        }
        failures = (
            A.email_service.requests.exceptions.Timeout("sensitive-otp-123456"),
            A.email_service.requests.exceptions.ConnectionError("brevo-test-secret"),
        )
        with patch.dict(os.environ, config, clear=True):
            for failure in failures:
                with self.subTest(failure=type(failure).__name__), patch.object(
                    A.email_service.requests, "post", side_effect=failure
                ):
                    with self.assertLogs("email_service", level="ERROR") as logs:
                        with self.assertRaises(A.email_service.EmailDeliveryError):
                            A.email_service.send_email(
                                "resident@example.test", "Subject", "Body"
                            )
                    output = "\n".join(logs.output)
                    self.assertNotIn("brevo-test-secret", output)
                    self.assertNotIn("123456", output)

    def test_unset_provider_preserves_smtp_delivery(self):
        config = {
            "SMTP_HOST": "smtp.example.test",
            "SMTP_FROM": "no-reply@example.test",
        }
        smtp = MagicMock()
        smtp.__enter__.return_value = smtp
        with patch.dict(os.environ, config, clear=True), patch.object(
            A.email_service.ssl, "create_default_context", return_value=MagicMock()
        ), patch.object(
            A.email_service.smtplib, "SMTP", return_value=smtp
        ) as smtp_constructor:
            A.email_service.send_email("resident@example.test", "Subject", "Body")
        smtp_constructor.assert_called_once_with("smtp.example.test", 587, timeout=15)
        smtp.send_message.assert_called_once()

    def test_brevo_password_reset_logs_no_api_key_or_reset_code(self):
        config = {
            "EMAIL_PROVIDER": "brevo",
            "BREVO_API_KEY": "brevo-test-secret",
            "SMTP_FROM": "no-reply@example.test",
        }
        self.create_resident("brevo-reset-log@example.test")
        response = MagicMock(status_code=201)
        with patch.dict(os.environ, config, clear=True), patch.object(
            A.email_service.requests, "post", return_value=response
        ) as post:
            with self.assertLogs("email_service", level="INFO") as logs:
                result = A.app.test_client().post(
                    "/forgot-password",
                    data={"email": "brevo-reset-log@example.test"},
                )

        self.assertEqual(result.status_code, 302)
        payload = post.call_args.kwargs["json"]
        code = re.search(r"code is (\d{6})", payload["textContent"]).group(1)
        output = "\n".join(logs.output)
        self.assertNotIn("brevo-test-secret", output)
        self.assertNotIn(code, output)
        self.assertIn("inbox delivery is not confirmed", output)

    def test_smtp_service_rejects_partial_auth_configuration(self):
        config = {
            "SMTP_HOST": "smtp.example.test",
            "SMTP_PORT": "587",
            "SMTP_USERNAME": "mailer@example.test",
            "SMTP_FROM": "no-reply@example.test",
            "SMTP_USE_TLS": "true",
            "SMTP_USE_SSL": "false",
        }
        for password in ("", " \t "):
            with self.subTest(password_is_blank=not password.strip()), patch.dict(
                os.environ,
                {**config, "SMTP_PASSWORD": password, "EMAIL_PASSWORD": "decoy-password"},
                clear=True,
            ), patch.object(A.email_service.smtplib, "SMTP") as smtp_constructor:
                with self.assertRaises(A.email_service.EmailConfigurationError):
                    A.email_service.send_email(
                        "resident@example.test", "Test subject", "Test body"
                    )
                smtp_constructor.assert_not_called()

    def test_reset_email_code_is_hashed_single_use_and_resets_without_login(self):
        email = "reset-flow@example.test"
        resident_id = self.create_resident(email)
        client = A.app.test_client()
        with patch.dict(os.environ, {}, clear=True), patch.object(
            A.email_service, "send_email"
        ) as send_email:
            response = client.post("/forgot-password", data={"email": email})
            self.assertEqual(response.status_code, 302)
            confirmation = client.get(response.location)
        self.assertIn("six-digit code", confirmation.get_data(as_text=True))
        send_email.assert_called_once()
        body = send_email.call_args.args[2]
        self.assertIn("This code expires in 5 minutes", body)
        code = re.search(r"code is (\d{6})", body).group(1)
        self.assertEqual(send_email.call_args.args[0], email)
        conn = dbh.get_db()
        stored = conn.execute(
            "SELECT code_hash, expires_at, attempts, used_at, created_at "
            "FROM password_reset_codes WHERE user_id=?",
            (resident_id,),
        ).fetchone()
        self.assertNotEqual(stored["code_hash"], code)
        self.assertEqual(stored["code_hash"], A._otp_digest(code))
        self.assertEqual(stored["attempts"], 0)
        self.assertIsNone(stored["used_at"])
        self.assertEqual(
            datetime.fromisoformat(stored["expires_at"])
            - datetime.fromisoformat(stored["created_at"]),
            timedelta(minutes=5),
        )
        conn.close()

        self.assertEqual(client.get("/reset-password").status_code, 200)
        mismatch = client.post(
            "/reset-password",
            data={"email": email, "code": code, "password": "UpdatedPassword@123", "confirm_password": "DifferentPassword@123"},
        )
        self.assertEqual(mismatch.status_code, 200)
        self.assertIn("Passwords do not match", mismatch.get_data(as_text=True))
        response = client.post(
            "/reset-password",
            data={"email": email, "code": code, "password": "UpdatedPassword@123", "confirm_password": "UpdatedPassword@123"},
        )
        self.assertEqual(response.status_code, 302)
        conn = dbh.get_db()
        account = conn.execute("SELECT password_hash,must_reset_pw FROM users WHERE id=?", (resident_id,)).fetchone()
        password_hash = account["password_hash"]
        self.assertTrue(check_password_hash(password_hash, "UpdatedPassword@123"))
        self.assertFalse(check_password_hash(password_hash, "Resident@1234"))
        self.assertEqual(account["must_reset_pw"], 0)
        self.assertIsNotNone(conn.execute(
            "SELECT used_at FROM password_reset_codes WHERE user_id=?",
            (resident_id,),
        ).fetchone()["used_at"])
        conn.close()
        reused = client.post(
            "/reset-password",
            data={"email": email, "code": code, "password": "UpdatedPassword@456", "confirm_password": "UpdatedPassword@456"},
        )
        self.assertEqual(reused.status_code, 400)
        with patch.object(A.email_service, "send_email") as send_email:
            old_password = client.post(
                "/login", data={"email": email, "password": "Resident@1234"}
            )
            self.assertEqual(old_password.status_code, 200)
            self.assertEqual(send_email.call_count, 0)
            new_password = client.post(
                "/login", data={"email": email, "password": "UpdatedPassword@123"}
            )
            self.assertEqual(new_password.location, "/dashboard")
            self.assertEqual(send_email.call_count, 0)

    def test_staff_reset_code_uses_registered_email_and_invalidates_previous_code(self):
        email = "reset-staff@example.test"
        conn = dbh.get_db()
        staff_id = conn.execute(
            "INSERT INTO users (full_name,email,recovery_email,password_hash,role,office) "
            "VALUES (?,?,?,?, 'staff','bpso')",
            ("Reset Staff", email, "alternate@example.test", generate_password_hash("Staff@1234")),
        ).lastrowid
        conn.commit()
        conn.close()
        client = A.app.test_client()
        with patch.dict(os.environ, {
            "PASSWORD_RESET_RESEND_SECONDS": "1",
        }), patch.object(A.email_service, "send_email") as send_email:
            client.post("/forgot-password", data={"email": email})
            first_code = re.search(r"code is (\d{6})", send_email.call_args.args[2]).group(1)
            conn = dbh.get_db()
            conn.execute(
                "UPDATE password_reset_limits SET last_sent_at=? WHERE user_id=?",
                ((datetime.now() - timedelta(seconds=2)).isoformat(), staff_id),
            )
            conn.commit()
            conn.close()
            client.post("/forgot-password", data={"email": email})
        self.assertEqual(send_email.call_count, 2)
        self.assertEqual(send_email.call_args.args[0], email)
        second_code = re.search(r"code is (\d{6})", send_email.call_args.args[2]).group(1)
        self.assertNotEqual(first_code, second_code)
        conn = dbh.get_db()
        stored = conn.execute(
            "SELECT code_hash,used_at FROM password_reset_codes WHERE user_id=?",
            (staff_id,),
        ).fetchone()
        self.assertEqual(stored["code_hash"], A._otp_digest(second_code))
        self.assertNotEqual(stored["code_hash"], A._otp_digest(first_code))
        self.assertIsNone(stored["used_at"])
        conn.close()

    def test_password_reset_requests_are_rate_limited(self):
        email = "limited-reset@example.test"
        self.create_resident(email)
        client = A.app.test_client()
        with patch.dict(os.environ, {
            "PASSWORD_RESET_RESEND_SECONDS": "1",
            "PASSWORD_RESET_MAX_SENDS_PER_HOUR": "1",
        }), patch.object(A.email_service, "send_email") as send_email:
            client.post("/forgot-password", data={"email": email})
            limited = client.post(
                "/forgot-password", data={"email": email}, follow_redirects=True
            )
        self.assertEqual(send_email.call_count, 1)
        self.assertIn(
            "Password reset instructions will be sent to your inbox. "
            "Please check your email to continue.",
            limited.get_data(as_text=True),
        )

    def test_password_reset_diagnostic_events_are_safe_and_distinguish_outcomes(self):
        config = {
            "EMAIL_PROVIDER": "brevo",
            "PASSWORD_RESET_RESEND_SECONDS": "60",
            "PASSWORD_RESET_MAX_SENDS_PER_HOUR": "20",
        }
        sent_email = "diagnostic-sent@example.test"
        cooldown_email = "diagnostic-cooldown@example.test"
        capped_email = "diagnostic-capped@example.test"
        failed_email = "diagnostic-failed@example.test"
        self.create_resident(sent_email)
        cooldown_user_id = self.create_resident(cooldown_email)
        capped_user_id = self.create_resident(capped_email)
        failed_user_id = self.create_resident(failed_email)
        client = A.app.test_client()

        with patch.dict(os.environ, config), patch.object(
            A.email_service, "send_email"
        ) as send_email:
            with self.assertLogs(A.app.logger, level="INFO") as sent_logs:
                client.post("/forgot-password", data={"email": sent_email})
            sent_output = "\n".join(sent_logs.output)
            self.assertIn("event=sent provider=brevo result=provider_accepted", sent_output)
            otp = re.search(
                r"code is (\d{6})", send_email.call_args.args[2]
            ).group(1)
            self.assertNotIn(sent_email, sent_output)
            self.assertNotIn(otp, sent_output)

            old_timestamp = (datetime.now() - timedelta(seconds=61)).isoformat(
                timespec="microseconds"
            )
            conn = dbh.get_db()
            conn.execute(
                "INSERT INTO password_reset_limits "
                "(user_id,last_sent_at,window_started_at,sends_in_window) "
                "VALUES (?,?,?,?)",
                (capped_user_id, old_timestamp, datetime.now().isoformat(), 20),
            )
            conn.commit()
            conn.close()
            with self.assertLogs(A.app.logger, level="INFO") as cap_logs:
                client.post("/forgot-password", data={"email": capped_email})
            cap_output = "\n".join(cap_logs.output)
            self.assertIn(
                "event=hourly_cap_reached requests=20 limit=20", cap_output
            )
            self.assertNotIn(capped_email, cap_output)

            recent_timestamp = (datetime.now() - timedelta(seconds=30)).isoformat(
                timespec="microseconds"
            )
            conn = dbh.get_db()
            conn.execute(
                "INSERT INTO password_reset_limits "
                "(user_id,last_sent_at,window_started_at,sends_in_window) "
                "VALUES (?,?,?,?)",
                (cooldown_user_id, recent_timestamp, datetime.now().isoformat(), 1),
            )
            conn.commit()
            conn.close()
            with self.assertLogs(A.app.logger, level="INFO") as cooldown_logs:
                client.post("/forgot-password", data={"email": cooldown_email})
            cooldown_output = "\n".join(cooldown_logs.output)
            self.assertIn("event=cooldown_active remaining_seconds=", cooldown_output)
            self.assertNotIn(cooldown_email, cooldown_output)

            with self.assertLogs(A.app.logger, level="INFO") as missing_logs:
                client.post(
                    "/forgot-password",
                    data={"email": "diagnostic-missing@example.test"},
                )
            missing_output = "\n".join(missing_logs.output)
            self.assertIn("event=no_active_account", missing_output)
            self.assertNotIn("diagnostic-missing@example.test", missing_output)

        with patch.dict(os.environ, config), patch.object(
            A.email_service,
            "send_email",
            side_effect=A.email_service.EmailDeliveryError(
                "Brevo API rejected the email request.", status_code=401
            ),
        ), self.assertLogs(A.app.logger, level="ERROR") as failure_logs:
            client.post("/forgot-password", data={"email": failed_email})
        failure_output = "\n".join(failure_logs.output)
        self.assertIn(
            "event=send_failed provider=brevo type=EmailDeliveryError status_code=401",
            failure_output,
        )
        self.assertNotIn(failed_email, failure_output)
        self.assertNotIn(str(failed_user_id), failure_output)

    def test_password_reset_resend_countdown_persists_and_server_blocks_early_resend(self):
        email = "countdown-reset@example.test"
        self.create_resident(email)
        client = A.app.test_client()
        config = {
            "PASSWORD_RESET_RESEND_SECONDS": "60",
            "PASSWORD_RESET_MAX_SENDS_PER_HOUR": "30",
        }
        with patch.dict(os.environ, config), patch.object(
            A.email_service, "send_email"
        ) as send_email:
            response = client.post(
                "/forgot-password",
                data={"email": email},
                follow_redirects=True,
            )
            page = response.get_data(as_text=True)
            self.assertIn(
                "Password reset instructions will be sent to your inbox.",
                page,
            )
            reset_page = client.get("/reset-password").get_data(as_text=True)
            self.assertIn('data-idle-label="Resend Email"', reset_page)
            self.assertIn('formaction="/forgot-password"', reset_page)
            self.assertRegex(reset_page, r'<button[^>]*disabled[^>]*>')
            self.assertIn("Resend Email in 00:5", reset_page)

            refreshed = client.get("/forgot-password")
            refreshed_html = refreshed.get_data(as_text=True)
            self.assertIn("Resend Email in 00:5", refreshed_html)
            self.assertRegex(refreshed_html, r'data-remaining-ms="[1-9][0-9]*"')
            self.assertRegex(refreshed_html, r'<button[^>]*disabled[^>]*>')
            self.assertIn('data-idle-label="Send Reset Code"', refreshed_html)
            with client.session_transaction() as session:
                self.assertTrue(session.permanent)
                self.assertNotIn(email, str(dict(session)))

            client.post(
                "/forgot-password",
                data={
                    "email": email,
                    "code": "123456",
                    "password": "Ignored@123",
                    "confirm_password": "Ignored@123",
                },
            )
            self.assertEqual(send_email.call_count, 1)
            reblocked = client.get("/forgot-password").get_data(as_text=True)
            self.assertIn("Resend Email in 00:5", reblocked)

            with client.session_transaction() as session:
                session["password_reset_resend_until"] = (
                    datetime.now() - timedelta(seconds=1)
                ).isoformat()
            available = client.get("/forgot-password").get_data(as_text=True)
            self.assertIn('data-idle-label="Send Reset Code"', available)
            self.assertIn(">Send Reset Code</button>", available)

    def test_reset_code_page_has_resend_action_without_duplicate_email_field(self):
        response = A.app.test_client().post(
            "/forgot-password",
            data={"email": "unknown-reset@example.test"},
            follow_redirects=True,
        )
        html = response.get_data(as_text=True)
        self.assertIn(
            "Password reset instructions will be sent to your inbox. "
            "Please check your email to continue.",
            html,
        )
        self.assertIn('data-idle-label="Resend Email"', html)
        self.assertIn('formaction="/forgot-password"', html)
        self.assertIn("Resend Email in 00:5", html)
        self.assertEqual(html.count('name="email"'), 1)

    def test_password_reset_hourly_cap_allows_thirty_requests(self):
        email = "thirty-reset@example.test"
        user_id = self.create_resident(email)
        client = A.app.test_client()
        with patch.dict(os.environ, {
            "PASSWORD_RESET_RESEND_SECONDS": "1",
            "PASSWORD_RESET_MAX_SENDS_PER_HOUR": "30",
        }), patch.object(A.email_service, "send_email") as send_email:
            for attempt in range(30):
                client.post("/forgot-password", data={"email": email})
                if attempt < 29:
                    conn = dbh.get_db()
                    conn.execute(
                        "UPDATE password_reset_limits SET last_sent_at=? WHERE user_id=?",
                        ((datetime.now() - timedelta(seconds=2)).isoformat(), user_id),
                    )
                    conn.commit()
                    conn.close()
            client.post("/forgot-password", data={"email": email})

        self.assertEqual(send_email.call_count, 30)
        conn = dbh.get_db()
        rate = conn.execute(
            "SELECT sends_in_window FROM password_reset_limits WHERE user_id=?",
            (user_id,),
        ).fetchone()
        conn.close()
        self.assertEqual(rate["sends_in_window"], 30)

    def test_unknown_email_and_smtp_failure_have_generic_response(self):
        client = A.app.test_client()
        with patch.object(A.email_service, "send_email") as send_email:
            unknown = client.post(
                "/forgot-password",
                data={"email": "missing@example.test"},
                follow_redirects=True,
            )
        send_email.assert_not_called()
        email = "failed-reset@example.test"
        user_id = self.create_resident(email)
        with patch.object(
            A.email_service,
            "send_email",
            side_effect=A.email_service.EmailConfigurationError("not configured"),
        ):
            failed = client.post(
                "/forgot-password", data={"email": email}, follow_redirects=True
            )
        generic_notice = (
            "Password reset instructions will be sent to your inbox. "
            "Please check your email to continue."
        )
        self.assertIn(generic_notice, unknown.get_data(as_text=True))
        self.assertIn(generic_notice, failed.get_data(as_text=True))
        conn = dbh.get_db()
        reset_code = conn.execute(
            "SELECT code_hash,used_at FROM password_reset_codes WHERE user_id=?", (user_id,)
        ).fetchone()
        self.assertIsNone(reset_code["code_hash"])
        self.assertIsNotNone(reset_code["used_at"])
        conn.close()

    def test_invalid_and_expired_reset_codes_are_rejected(self):
        email = "expired-reset@example.test"
        user_id = self.create_resident(email)
        client = A.app.test_client()
        with patch.object(A.email_service, "send_email") as send_email:
            client.post("/forgot-password", data={"email": email})
        code = re.search(r"code is (\d{6})", send_email.call_args.args[2]).group(1)
        self.assertEqual(client.get("/reset-password/not-a-valid-token").status_code, 400)
        wrong_code = "000000" if code != "000000" else "000001"
        invalid = client.post(
            "/reset-password",
            data={
                "email": email,
                "code": wrong_code,
                "password": "UpdatedPassword@123",
                "confirm_password": "UpdatedPassword@123",
            },
        )
        self.assertEqual(invalid.status_code, 400)
        conn = dbh.get_db()
        conn.execute(
            "UPDATE password_reset_codes SET expires_at=? WHERE user_id=?",
            ((datetime.now() - timedelta(minutes=1)).isoformat(), user_id),
        )
        conn.commit()
        conn.close()
        expired = client.post(
            "/reset-password",
            data={
                "email": email,
                "code": code,
                "password": "UpdatedPassword@123",
                "confirm_password": "UpdatedPassword@123",
            },
        )
        self.assertEqual(expired.status_code, 400)

    def test_admin_login_requires_email_verification(self):
        email = "otp-admin@example.test"
        conn = dbh.get_db()
        conn.execute(
            "INSERT INTO users (full_name,email,password_hash,role,contact_number) VALUES (?,?,?,'admin',?)",
            ("OTP Admin", email, generate_password_hash("Admin@1234"), "+639171000002"),
        )
        conn.commit()
        conn.close()

        client = A.app.test_client()
        with patch.object(A.email_service, "send_email") as send_email:
            response = client.post("/login", data={"email": email, "password": "Admin@1234"})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.location, "/verify-otp")
            self.assertEqual(client.get("/verify-otp").status_code, 200)
            send_email.assert_called_once()
            with client.session_transaction() as state:
                self.assertNotIn("user_id", state)
            self.assertEqual(client.get("/admin/dashboard").location, "/login")

    def test_founding_admin_requires_otp_then_initial_password_change(self):
        email = "ralphpatrickga@gmail.com"
        conn = dbh.get_db()
        conn.execute(
            "UPDATE users SET must_reset_pw=1, recovery_email=? WHERE id=1",
            (email,),
        )
        conn.commit()
        conn.close()

        client = A.app.test_client()
        with patch.object(A.email_service, "send_email") as send_email:
            response = client.post(
                "/login",
                data={"email": email, "password": "Admin@1234"},
            )
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.location, "/verify-otp")
            send_email.assert_called_once()
            code = re.search(r"code is (\d{6})", send_email.call_args.args[2]).group(1)
            with client.session_transaction() as state:
                self.assertNotIn("user_id", state)
                csrf = state["mfa_csrf"]
            verified = client.post("/verify-otp", data={"otp": code, "csrf_token": csrf})
            self.assertEqual(verified.location, "/first-login-reset")
            self.assertEqual(client.get("/first-login-reset").status_code, 200)

        password = "NewFoundingPassword@123"
        response = client.post(
            "/first-login-reset",
            data={"password": password, "confirm_password": password},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin", response.location)
        conn = dbh.get_db()
        account = conn.execute(
            "SELECT password_hash, must_reset_pw FROM users WHERE id=1"
        ).fetchone()
        self.assertTrue(check_password_hash(account["password_hash"], password))
        self.assertEqual(account["must_reset_pw"], 0)
        conn.close()
        administrators = client.get("/admin/administrators")
        self.assertEqual(administrators.status_code, 200)
        self.assertIn(b"Create Administrator Account", administrators.data)

    def test_admin_password_recovery_uses_registered_email(self):
        login_email = "recover-admin@example.test"
        recovery_email = "admin-recovery@example.test"
        conn = dbh.get_db()
        conn.execute(
            "INSERT INTO users (full_name,email,recovery_email,password_hash,role) "
            "VALUES (?,?,?,?, 'admin')",
            (
                "Recovery Admin",
                login_email,
                recovery_email,
                generate_password_hash("Admin@1234"),
            ),
        )
        conn.commit()
        conn.close()

        client = A.app.test_client()
        with patch.object(A.email_service, "send_email") as send_email:
            response = client.post("/forgot-password", data={"email": login_email})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(send_email.call_args.args[0], login_email)
            code = re.search(r"code is (\d{6})", send_email.call_args.args[2]).group(1)

        password = "RecoveredAdminPassword@123"
        response = client.post(
            "/reset-password",
            data={
                "email": login_email,
                "code": code,
                "password": password,
                "confirm_password": password,
            },
        )
        self.assertEqual(response.status_code, 302)
        conn = dbh.get_db()
        account = conn.execute(
            "SELECT password_hash FROM users WHERE email=?", (login_email,)
        ).fetchone()
        self.assertTrue(check_password_hash(account["password_hash"], password))
        conn.close()

    def test_allowance_cooldown_restore_and_restore_once(self):
        resident_id = self.create_resident("allowance@example.test")
        client = self.resident_client(resident_id)
        complaint_ids = []
        for index in range(5):
            response = client.post(
                "/complaints/new",
                data={
                    "category": "Noise Complaint",
                    "description": f"Test concern {index}",
                    "location": f"Allowance test location {index}",
                },
                follow_redirects=True,
            )
            self.assertEqual(response.status_code, 200)
            conn = dbh.get_db()
            complaint = conn.execute(
                "SELECT id FROM complaints WHERE resident_id=? ORDER BY id DESC LIMIT 1",
                (resident_id,),
            ).fetchone()
            complaint_ids.append(complaint["id"])
            allowance = conn.execute(
                "SELECT * FROM resident_concern_allowances WHERE user_id=?", (resident_id,)
            ).fetchone()
            if index < 4:
                self.assertIsNone(allowance["cooldown_started_at"])
                self.assertIsNone(allowance["cooldown_expires_at"])
            else:
                started = datetime.fromisoformat(allowance["cooldown_started_at"])
                expires = datetime.fromisoformat(allowance["cooldown_expires_at"])
                self.assertEqual(expires - started, timedelta(hours=24))
            conn.close()

        blocked = client.post(
            "/complaints/new",
            data={
                "category": "Noise Complaint",
                "description": "Must remain blocked.",
                "location": "Blocked location",
            },
        )
        self.assertEqual(blocked.status_code, 200)
        conn = dbh.get_db()
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM complaints WHERE resident_id=?", (resident_id,)).fetchone()[0],
            5,
        )
        conn.close()

        admin = self.admin_client()
        admin.post(f"/complaints/{complaint_ids[0]}/update", data={"status": "Resolved"})
        conn = dbh.get_db()
        allowance = conn.execute(
            "SELECT * FROM resident_concern_allowances WHERE user_id=?", (resident_id,)
        ).fetchone()
        self.assertEqual(allowance["remaining"], 1)
        self.assertIsNone(allowance["cooldown_expires_at"])
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM concern_allowance_events "
            "WHERE complaint_id=? AND event_type='resolved_restoration'",
            (complaint_ids[0],),
        ).fetchone()[0], 1)
        conn.close()

        admin.post(f"/complaints/{complaint_ids[0]}/update", data={"status": "Resolved"})
        admin.post(f"/complaints/{complaint_ids[1]}/update", data={"status": "Rejected"})
        conn = dbh.get_db()
        self.assertEqual(
            conn.execute(
                "SELECT remaining FROM resident_concern_allowances WHERE user_id=?",
                (resident_id,),
            ).fetchone()["remaining"],
            1,
        )
        conn.close()

        client.get("/logout")
        with client.session_transaction() as session:
            session["user_id"] = resident_id
            session["role"] = "resident"
        self.assertIn("You have 1 of 5 concern submissions available.", client.get(
            "/complaints/new"
        ).get_data(as_text=True))

        conn = dbh.get_db()
        conn.execute(
            "UPDATE resident_concern_allowances SET remaining=0, cooldown_started_at=?, "
            "cooldown_expires_at=? WHERE user_id=?",
            (
                (datetime.now() - timedelta(hours=24, seconds=1)).isoformat(),
                (datetime.now() - timedelta(seconds=1)).isoformat(),
                resident_id,
            ),
        )
        conn.commit()
        conn.close()
        refreshed = client.get("/complaints/new").get_data(as_text=True)
        self.assertIn("You have 5 of 5 concern submissions available.", refreshed)

    def test_concurrent_submissions_cannot_exceed_five_slots(self):
        resident_id = self.create_resident("concurrent@example.test")

        def submit(index):
            client = self.resident_client(resident_id)
            response = client.post(
                "/complaints/new",
                data={
                    "category": "Noise Complaint",
                    "description": f"Concurrent submission {index}",
                    "location": f"Concurrent location {index}",
                },
            )
            return response.status_code

        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(submit, range(8)))
        self.assertEqual(statuses.count(302), 5)
        self.assertEqual(statuses.count(200), 3)
        conn = dbh.get_db()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM complaints WHERE resident_id=?", (resident_id,)
        ).fetchone()[0], 5)
        self.assertEqual(conn.execute(
            "SELECT remaining FROM resident_concern_allowances WHERE user_id=?", (resident_id,)
        ).fetchone()["remaining"], 0)
        conn.close()
