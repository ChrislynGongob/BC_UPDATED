import hashlib
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import storage
import migrate_sqlite_to_postgres as database_migration
import migrate_uploads_to_supabase as storage_migration


class StorageMigrationSafetyTests(unittest.TestCase):
    def test_admin_hierarchy_migration_does_not_guess_or_modify_founder_account(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        migration_path = os.path.join(
            root, "postgres_migrations", "001_admin_hierarchy.sql"
        )
        with open(migration_path, encoding="utf-8") as migration_file:
            migration = migration_file.read().lower()

        self.assertIn("add column if not exists is_founding_admin", migration)
        self.assertIn("create unique index idx_users_single_founding_admin", migration)
        self.assertNotIn("update users", migration)
        self.assertNotIn("password_hash", migration)
        self.assertNotRegex(migration, r"\bwhere\s+id\s*=\s*17\b")

    def test_production_confirmation_flag_runs_production_transfer(self):
        with patch.object(
            storage_migration,
            "inspect_files",
            return_value=(
                {"referenced_file_count": 1, "missing_file_count": 0},
                [("requests", "document.pdf")],
            ),
        ), patch.object(
            storage_migration, "transfer", return_value=1
        ) as transfer, patch("sys.stdout", new_callable=io.StringIO):
            result = storage_migration.main(
                [
                    "--source-db",
                    "unused.db",
                    "--confirm-production-upload",
                ]
            )
        self.assertEqual(result, 0)
        transfer.assert_called_once_with(
            storage_migration.os.path.join(
                os.path.dirname(os.path.abspath(storage_migration.__file__)), "uploads"
            ),
            [("requests", "document.pdf")],
            "production",
            on_verified=None,
        )

    def test_report_collision_prevents_upload_or_database_import(self):
        with tempfile.TemporaryDirectory() as directory:
            report_path = os.path.join(directory, "existing.json")
            with open(report_path, "w", encoding="utf-8") as report_file:
                report_file.write("keep")
            with patch.object(storage_migration, "inspect_files") as inspect_files, patch.object(
                storage_migration, "transfer"
            ) as transfer, patch.object(
                database_migration, "inspect_source"
            ) as inspect_source, patch.object(
                database_migration, "import_source"
            ) as import_source, patch("sys.stderr", new_callable=io.StringIO):
                upload_result = storage_migration.main(
                    [
                        "--source-db",
                        "unused.db",
                        "--confirm-upload",
                        "--report",
                        report_path,
                    ]
                )
                database_result = database_migration.main(
                    [
                        "--source",
                        "unused.db",
                        "--confirm-import",
                        "--report",
                        report_path,
                    ]
                )
            self.assertEqual(upload_result, 1)
            self.assertEqual(database_result, 1)
            inspect_files.assert_not_called()
            transfer.assert_not_called()
            inspect_source.assert_not_called()
            import_source.assert_not_called()
            with open(report_path, encoding="utf-8") as report_file:
                self.assertEqual(report_file.read(), "keep")

    def test_upload_transfer_resumes_only_after_checksum_match(self):
        payload = b"existing verified object"
        digest = hashlib.sha256(payload).digest()
        with tempfile.TemporaryDirectory() as directory:
            request_dir = os.path.join(directory, "requests")
            os.makedirs(request_dir)
            source_path = os.path.join(request_dir, "document.pdf")
            with open(source_path, "wb") as source_file:
                source_file.write(payload)

            existing_objects = {}

            def download(subdir, name):
                try:
                    return existing_objects[(subdir, name)]
                except KeyError as error:
                    raise storage.StorageNotFound("missing") from error

            def upload(subdir, name, source):
                existing_objects[(subdir, name)] = source.read()

            with patch.object(storage_migration, "_target_environment", return_value={}), patch.object(
                storage_migration, "_restore_environment"
            ), patch.object(storage_migration.storage, "download", side_effect=download), patch.object(
                storage_migration.storage, "upload", side_effect=upload
            ) as upload_mock:
                first = storage_migration.transfer(
                    directory, [("requests", "document.pdf")], "staging"
                )
                second = storage_migration.transfer(
                    directory, [("requests", "document.pdf")], "staging"
                )

            self.assertEqual(first, 1)
            self.assertEqual(second, 1)
            self.assertEqual(upload_mock.call_count, 1)
            self.assertEqual(
                hashlib.sha256(existing_objects[("requests", "document.pdf")]).digest(),
                digest,
            )

    def test_upload_transfer_refuses_mismatched_existing_object(self):
        payload = b"local object"
        with tempfile.TemporaryDirectory() as directory:
            request_dir = os.path.join(directory, "requests")
            os.makedirs(request_dir)
            with open(os.path.join(request_dir, "document.pdf"), "wb") as source_file:
                source_file.write(payload)
            with patch.object(storage_migration, "_target_environment", return_value={}), patch.object(
                storage_migration, "_restore_environment"
            ), patch.object(
                storage_migration.storage, "download", return_value=b"different object"
            ), patch.object(storage_migration.storage, "upload") as upload:
                with self.assertRaisesRegex(RuntimeError, "different contents"):
                    storage_migration.transfer(
                        directory, [("requests", "document.pdf")], "staging"
                    )
            upload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
