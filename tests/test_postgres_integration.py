"""Opt-in integration tests for an explicitly disposable PostgreSQL database."""

import os
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import db as dbh


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()
ENABLE_POSTGRES_TESTS = os.environ.get("ENABLE_POSTGRES_TESTS") == "YES"
TEST_DATABASE_IS_DISPOSABLE = (
    os.environ.get("TEST_DATABASE_IS_DISPOSABLE") == "YES"
)


def _validate_test_database_url(test_url, production_url, disposable_confirmed):
    parsed = urlsplit(test_url)
    production = urlsplit(production_url) if production_url else None
    same_target = bool(
        production
        and parsed.scheme.lower() == production.scheme.lower()
        and parsed.hostname == production.hostname
        and parsed.port == production.port
        and unquote(parsed.username or "") == unquote(production.username or "")
        and parsed.path.rstrip("/") == production.path.rstrip("/")
    )
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or not parsed.hostname
        or not parsed.path.strip("/")
        or not disposable_confirmed
        or same_target
    ):
        raise RuntimeError(
            "PostgreSQL integration tests require a separately configured disposable database."
        )


@unittest.skipUnless(
    TEST_DATABASE_URL and ENABLE_POSTGRES_TESTS,
    "Set TEST_DATABASE_URL, TEST_DATABASE_IS_DISPOSABLE=YES, and "
    "ENABLE_POSTGRES_TESTS=YES for disposable PostgreSQL tests.",
)
class PostgreSQLApplicationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        production_url = os.environ.get("DATABASE_URL", "").strip()
        _validate_test_database_url(
            TEST_DATABASE_URL,
            production_url,
            TEST_DATABASE_IS_DISPOSABLE,
        )
        cls._environment = patch.dict(
            os.environ,
            {
                "DB_BACKEND": "postgres",
                "DATABASE_URL": TEST_DATABASE_URL,
                "STORAGE_BACKEND": "local",
            },
        )
        cls._environment.start()
        dbh.ensure_postgres_schema()

        import app as application

        cls.application = application
        application._db_ready = False

    @classmethod
    def tearDownClass(cls):
        cls._environment.stop()
        cls.application._db_ready = False

    def test_connection_rows_and_rollback(self):
        key = f"postgres-test-{uuid.uuid4()}"
        connection = dbh.get_db()
        try:
            connection.execute(
                "INSERT INTO settings (key, value) VALUES (?,?)", (key, "temporary")
            )
            row = connection.execute(
                "SELECT key, value FROM settings WHERE key=?", (key,)
            ).fetchone()
            self.assertEqual(row["key"], key)
            self.assertEqual(row[0], key)
            connection.rollback()
        finally:
            connection.close()

        connection = dbh.get_db()
        self.assertIsNone(
            connection.execute(
                "SELECT key FROM settings WHERE key=?", (key,)
            ).fetchone()
        )
        connection.close()

    def test_auth_office_access_uploads_analytics_and_reports(self):
        application = self.application
        suffix = uuid.uuid4().hex
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        resident_email = f"resident-{suffix}@example.test"
        secretary_email = f"secretary-{suffix}@example.test"
        bpso_email = f"bpso-{suffix}@example.test"
        admin_email = f"admin-{suffix}@example.test"
        reference = f"REQ-2099-{suffix[:5].upper()}"
        complaint_reference = f"CMP-2099-{suffix[:5].upper()}"
        connection = dbh.get_db()
        user_ids = []
        request_id = None
        complaint_id = None
        try:
            for name, email, role, office in (
                ("Test Resident", resident_email, "resident", None),
                ("Test Secretary", secretary_email, "staff", "secretary"),
                ("Test BPSO", bpso_email, "staff", "bpso"),
                ("Test Admin", admin_email, "admin", None),
            ):
                cursor = connection.execute(
                    "INSERT INTO users "
                    "(full_name,email,password_hash,role,office,status,must_reset_pw) "
                    "VALUES (?,?,?,?,?,'active',0)",
                    (name, email, generate_password_hash("TestPassword123"), role, office),
                )
                user_ids.append(cursor.lastrowid)
            resident_id, secretary_id, bpso_id, admin_id = user_ids
            request_id = connection.execute(
                "INSERT INTO requests "
                "(reference_no,resident_id,request_type,created_at,updated_at) "
                "VALUES (?,?,'Certificate of Indigency',?,?)",
                (reference, resident_id, timestamp, timestamp),
            ).lastrowid
            complaint_id = connection.execute(
                "INSERT INTO complaints "
                "(reference_no,resident_id,category,description,location,created_at,updated_at) "
                "VALUES (?,?,'Noise Complaint','Test complaint','Purok 1',?,?)",
                (complaint_reference, resident_id, timestamp, timestamp),
            ).lastrowid
            connection.commit()

            secretary = connection.execute(
                "SELECT * FROM users WHERE id=?", (secretary_id,)
            ).fetchone()
            bpso = connection.execute(
                "SELECT * FROM users WHERE id=?", (bpso_id,)
            ).fetchone()
            secretary_handles_request = application.can_handle_request_type(
                secretary, "Certificate of Indigency"
            )
            secretary_handles_complaints = application.can_handle_complaints(secretary)
            bpso_handles_complaints = application.can_handle_complaints(bpso)
            connection.close()

            self.assertTrue(secretary_handles_request)
            self.assertFalse(secretary_handles_complaints)
            self.assertTrue(bpso_handles_complaints)

            login_client = application.app.test_client()
            response = login_client.post(
                "/login",
                data={"email": resident_email, "password": "TestPassword123"},
            )
            self.assertEqual(response.status_code, 302)

            with tempfile.TemporaryDirectory() as upload_root, patch.object(
                application, "UPLOAD_DIR", upload_root
            ):
                request_name = f"request-{suffix}.pdf"
                complaint_name = f"complaint-{suffix}.pdf"
                os.makedirs(os.path.join(upload_root, "requests"))
                os.makedirs(os.path.join(upload_root, "complaints"))
                with open(os.path.join(upload_root, "requests", request_name), "wb") as target:
                    target.write(b"%PDF-1.4 test")
                with open(os.path.join(upload_root, "complaints", complaint_name), "wb") as target:
                    target.write(b"%PDF-1.4 test")
                connection = dbh.get_db()
                connection.execute(
                    "INSERT INTO request_files "
                    "(request_id,file_type,filename,stored_name) VALUES (?,'valid_id',?,?)",
                    (request_id, "request.pdf", request_name),
                )
                connection.execute(
                    "INSERT INTO complaint_files "
                    "(complaint_id,filename,stored_name) VALUES (?,?,?)",
                    (complaint_id, "complaint.pdf", complaint_name),
                )
                connection.commit()
                connection.close()

                resident_client = application.app.test_client()
                with resident_client.session_transaction() as session:
                    session.update(user_id=resident_id, role="resident")
                self.assertEqual(
                    resident_client.get(f"/uploads/requests/{request_name}").status_code,
                    200,
                )
                bpso_client = application.app.test_client()
                with bpso_client.session_transaction() as session:
                    session.update(user_id=bpso_id, role="staff")
                self.assertEqual(
                    bpso_client.get(f"/uploads/requests/{request_name}").status_code,
                    403,
                )
                self.assertEqual(
                    bpso_client.get(f"/uploads/complaints/{complaint_name}").status_code,
                    200,
                )

            admin_client = application.app.test_client()
            with admin_client.session_transaction() as session:
                session.update(user_id=admin_id, role="admin")
            month = datetime.now().strftime("%Y-%m")
            self.assertEqual(
                admin_client.get(
                    f"/admin/reports/complaints/data?period=monthly&month={month}"
                ).status_code,
                200,
            )
            self.assertEqual(
                admin_client.get(
                    f"/admin/reports/document-requests/data?period=monthly&month={month}"
                ).status_code,
                200,
            )
            self.assertEqual(admin_client.get("/reports").status_code, 200)
        finally:
            cleanup_connection = None
            try:
                cleanup_connection = dbh.get_db()
                if request_id is not None:
                    cleanup_connection.execute(
                        "DELETE FROM request_files WHERE request_id=?", (request_id,)
                    )
                    cleanup_connection.execute(
                        "DELETE FROM requests WHERE id=?", (request_id,)
                    )
                if complaint_id is not None:
                    cleanup_connection.execute(
                        "DELETE FROM complaint_files WHERE complaint_id=?", (complaint_id,)
                    )
                    cleanup_connection.execute(
                        "DELETE FROM complaints WHERE id=?", (complaint_id,)
                    )
                if user_ids:
                    cleanup_connection.execute(
                        "DELETE FROM users WHERE id IN (?,?,?,?)", user_ids
                    )
                cleanup_connection.commit()
            except Exception:
                if cleanup_connection is not None:
                    cleanup_connection.rollback()
                raise
            finally:
                if cleanup_connection is not None:
                    cleanup_connection.close()


class PostgreSQLIntegrationGuardTests(unittest.TestCase):
    def test_accepts_standard_supabase_pooler_database_name(self):
            test_url = "postgresql://postgres.projectref@pooler.example:5432/postgres"
            _validate_test_database_url(test_url, "", True)

    def test_rejects_production_url_and_requires_disposable_confirmation(self):
            url = "postgresql://postgres.projectref@pooler.example:5432/postgres"
            with self.assertRaisesRegex(RuntimeError, "disposable"):
                _validate_test_database_url(url, url, True)
            with self.assertRaisesRegex(RuntimeError, "disposable"):
                _validate_test_database_url(
                    url + "?sslmode=require",
                    url,
                    True,
                )
            with self.assertRaisesRegex(RuntimeError, "disposable"):
                _validate_test_database_url(url, "", False)


if __name__ == "__main__":
    unittest.main()
