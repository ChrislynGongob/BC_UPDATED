import hashlib
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import psycopg

import db as dbh
from migrate_sqlite_to_postgres import inspect_source
import seed


class PostgreSQLTranslationTests(unittest.TestCase):
    def test_row_factory_handles_commands_without_result_columns(self):
        row_factory = dbh._postgres_row_factory(Mock(description=None))
        self.assertEqual(row_factory((1, "affected")), (1, "affected"))

    def test_row_factory_preserves_dictionary_rows_for_result_columns(self):
        id_column = Mock()
        id_column.name = "id"
        name_column = Mock()
        name_column.name = "name"
        cursor = Mock(description=(id_column, name_column))
        row_factory = dbh._postgres_row_factory(cursor)
        row = row_factory((7, "Resident"))
        self.assertEqual(row["id"], 7)
        self.assertEqual(row[0], 7)

    def test_placeholders_in_literals_are_preserved(self):
        translated = dbh._postgres_sql(
            "SELECT '?' AS literal, value FROM sample WHERE id=? AND note='it''s ?'"
        )
        self.assertEqual(
            translated,
            "SELECT '?' AS literal, value FROM sample WHERE id=%s AND note='it''s ?'",
        )

    def test_sqlite_write_ignore_and_lock_are_translated(self):
        self.assertEqual(
            dbh._postgres_sql(
                "INSERT OR IGNORE INTO follows (user_id, issue_id) VALUES (?,?)"
            ),
            "INSERT INTO follows (user_id, issue_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
        )
        self.assertEqual(
            dbh._postgres_sql("BEGIN IMMEDIATE"),
            "SELECT pg_advisory_xact_lock(195215649, 1)",
        )

    def test_sqlite_date_and_string_functions_are_translated(self):
        self.assertEqual(
            dbh._postgres_sql(
                "SELECT datetime(created_at)>=? AND datetime(created_at)<?"
            ),
            "SELECT created_at::timestamp>=%s::timestamp "
            "AND created_at::timestamp<%s::timestamp",
        )
        self.assertIn(
            "(created_at::timestamp + INTERVAL '+8 hours')::date",
            dbh._postgres_sql(
                "SELECT date(created_at, '+8 hours')=date('now', '+8 hours')"
            ),
        )
        self.assertIn(
            "to_char(created_at::timestamp, 'YYYY-MM')",
            dbh._postgres_sql("SELECT strftime('%Y-%m', created_at)"),
        )
        self.assertIn(
            "LEAST(created_at, %s)",
            dbh._postgres_sql("SELECT MIN(created_at, ?)"),
        )
        self.assertIn(
            "GREATEST(created_at, %s)",
            dbh._postgres_sql("SELECT MAX(created_at, ?)"),
        )

    def test_case_insensitive_search_and_sort_are_translated(self):
        translated = dbh._postgres_sql(
            "SELECT label FROM labels WHERE label LIKE ? ORDER BY label COLLATE NOCASE"
        )
        self.assertEqual(
            translated,
            "SELECT label FROM labels WHERE label ILIKE %s ORDER BY LOWER(label)",
        )

    def test_compatibility_row_supports_named_and_positional_access(self):
        row = dbh._CompatRow(("id", "name"), (7, "Resident"))
        self.assertEqual(row["id"], 7)
        self.assertEqual(row[0], 7)
        self.assertEqual(row[1], "Resident")
        self.assertEqual(dict(row), {"id": 7, "name": "Resident"})

    def test_postgres_connection_forces_ssl_and_rolls_back_on_close(self):
        connection = Mock()
        connection.info.transaction_status = psycopg.pq.TransactionStatus.IDLE
        with patch.dict(
            os.environ,
            {
                "DB_BACKEND": "postgres",
                "DATABASE_URL": "postgresql://test-user:test-password@localhost:5432/test_db",
            },
        ), patch("psycopg.connect", return_value=connection) as connect:
            handle = dbh.get_db()
            self.assertIn("sslmode=require", connect.call_args.args[0])
            self.assertIs(connect.call_args.kwargs["row_factory"], dbh._postgres_row_factory)
            handle.close()
        connection.rollback.assert_not_called()
        connection.close.assert_called_once()

    def test_postgres_schema_is_never_initialized_automatically(self):
        with patch.dict(os.environ, {"DB_BACKEND": "postgres"}):
            with self.assertRaisesRegex(RuntimeError, "schema creation is explicit"):
                dbh.init_db()
            with self.assertRaisesRegex(RuntimeError, "Automatic PostgreSQL"):
                dbh.migrate_db()


class SQLiteMigrationInspectionTests(unittest.TestCase):
    def test_dry_run_inspects_read_only_and_preserves_source_bytes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = os.path.join(temp_dir, "source.db")
            with patch.object(dbh, "DB_PATH", source), patch.dict(
                os.environ, {"DB_BACKEND": "sqlite"}
            ):
                dbh.init_db()
                dbh.migrate_db()
                connection = dbh.get_db()
                connection.execute(
                    "INSERT INTO users (full_name,email,password_hash,role) "
                    "VALUES (?,?,?,'admin')",
                    ("Test Admin", "test@example.test", "password-hash"),
                )
                connection.commit()
                connection.close()

            with open(source, "rb") as source_file:
                before = hashlib.sha256(source_file.read()).digest()
            report = inspect_source(source)
            with open(source, "rb") as source_file:
                after = hashlib.sha256(source_file.read()).digest()
            self.assertEqual(before, after)
            self.assertTrue(report["source_read_only"])
            self.assertEqual(report["table_count"], 21)
            self.assertEqual(report["tables"]["users"]["rows"], 1)
            self.assertTrue(report["password_hashes_included_in_checksums"])

    def test_seed_refuses_to_change_an_existing_sqlite_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "existing.db")
            original = b"existing database must remain untouched"
            with open(path, "wb") as database_file:
                database_file.write(original)
            with patch.object(dbh, "DB_PATH", path), patch.dict(
                os.environ, {"DB_BACKEND": "sqlite"}
            ):
                with self.assertRaisesRegex(FileExistsError, "will not delete"):
                    seed.run()
            with open(path, "rb") as database_file:
                self.assertEqual(database_file.read(), original)


if __name__ == "__main__":
    unittest.main()
