import os
import unittest
from unittest.mock import Mock, patch

import db as dbh
import storage
from migrate_sqlite_to_postgres import TABLE_ORDER
import verify_supabase_staging_readonly as verifier


class SupabaseStagingReadOnlyTests(unittest.TestCase):
    def test_requires_explicit_staging_credentials_and_rejects_production_target(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(dbh, "get_db") as get_db:
            with self.assertRaisesRegex(RuntimeError, "STAGING_DATABASE_URL"):
                verifier.verify_read_only()
        get_db.assert_not_called()

        with patch.dict(
            os.environ,
            {
                "STAGING_DATABASE_URL": "postgresql://stage@pooler.example:5432/postgres",
                "DATABASE_URL": "postgresql://prod@pooler.example:5432/postgres?sslmode=require",
                "STAGING_SUPABASE_URL": "https://stage-project.supabase.co",
                "STAGING_SUPABASE_SERVICE_ROLE_KEY": "test-key",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "matches DATABASE_URL"):
                verifier.verify_read_only()

    def test_database_verification_sets_transaction_read_only(self):
        class FakeConnection:
            def __init__(self):
                self.statements = []
                self.closed = False

            def execute(self, query, params=()):
                self.statements.append(query)
                cursor = Mock()
                if query == "SET TRANSACTION READ ONLY":
                    pass
                elif "information_schema.tables" in query:
                    cursor.fetchall.return_value = [(table,) for table in TABLE_ORDER]
                elif query.startswith("SELECT COUNT(*)"):
                    cursor.fetchone.return_value = (
                        163 if 'public."users"' in query else 0,
                    )
                elif "SELECT DISTINCT subdir, stored_name" in query:
                    cursor.fetchall.return_value = [("requests", "stored.pdf")]
                else:
                    raise AssertionError(f"Unexpected query: {query}")
                return cursor

            def close(self):
                self.closed = True

        connection = FakeConnection()
        with patch.dict(
            os.environ,
            {
                "STAGING_DATABASE_URL": "postgresql://stage@pooler.example:5432/postgres",
                "STAGING_SUPABASE_URL": "https://stage-project.supabase.co",
                "STAGING_SUPABASE_SERVICE_ROLE_KEY": "test-key",
            },
            clear=True,
        ), patch.object(dbh, "get_db", return_value=connection):
            report = verifier.verify_read_only()

        self.assertEqual(connection.statements[0], "SET TRANSACTION READ ONLY")
        self.assertTrue(connection.closed)
        self.assertEqual(report["table_count"], 21)
        self.assertEqual(report["row_count"], 163)
        self.assertEqual(report["referenced_object_count"], 1)
        self.assertEqual(report["objects"], [("requests", "stored.pdf")])

    def test_storage_verifier_uses_staging_credentials_and_restores_environment(self):
        with patch.dict(
            os.environ,
            {
                "STAGING_SUPABASE_URL": "https://stage-project.supabase.co",
                "STAGING_SUPABASE_SERVICE_ROLE_KEY": "staging-key",
                "STAGING_SUPABASE_STORAGE_BUCKET_REQUESTS": "stage-requests",
                "SUPABASE_URL": "https://production-project.supabase.co",
                "SUPABASE_SERVICE_ROLE_KEY": "production-key",
                "SUPABASE_STORAGE_BUCKET_REQUESTS": "production-requests",
                "STORAGE_BACKEND": "local",
            },
            clear=True,
        ), patch.object(storage, "download", return_value=b"content") as download, patch.object(
            storage, "file_size", return_value=7
        ):
            result = verifier.verify_private_downloads(
                [("requests", "stored.pdf")]
            )
            self.assertEqual(os.environ["SUPABASE_URL"], "https://production-project.supabase.co")
            self.assertEqual(os.environ["SUPABASE_SERVICE_ROLE_KEY"], "production-key")
            self.assertEqual(os.environ["SUPABASE_STORAGE_BUCKET_REQUESTS"], "production-requests")
            self.assertEqual(os.environ["STORAGE_BACKEND"], "local")
        download.assert_called_once_with("requests", "stored.pdf")
        self.assertEqual(result[0]["bytes"], 7)
        self.assertEqual(result[0]["bucket_category"], "requests")


if __name__ == "__main__":
    unittest.main()
