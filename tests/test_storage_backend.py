import io
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import storage
import app as application
from flask import get_flashed_messages


class LocalStorageTests(unittest.TestCase):
    def test_local_storage_round_trip_and_size(self):
        with tempfile.TemporaryDirectory() as upload_root, patch.dict(
            os.environ, {"STORAGE_BACKEND": "local"}
        ):
            payload = b"private upload contents"
            storage.upload("requests", "example.pdf", io.BytesIO(payload), upload_root)
            self.assertEqual(
                storage.download("requests", "example.pdf", upload_root), payload
            )
            self.assertEqual(
                storage.file_size("requests", "example.pdf", upload_root),
                len(payload),
            )

    def test_rejects_traversal_paths(self):
        with self.assertRaises(ValueError):
            storage.download("requests", "..\\secret.txt")
        with self.assertRaises(ValueError):
            storage.download("unknown", "safe.txt")


class SupabaseStorageTests(unittest.TestCase):
    def setUp(self):
        storage._PRIVATE_BUCKETS.clear()

    def test_supabase_upload_requires_and_uses_a_private_bucket(self):
        bucket_response = Mock(status_code=200)
        bucket_response.json.return_value = {"public": False}
        upload_response = Mock(status_code=200)
        with patch.dict(
            os.environ,
            {
                "STORAGE_BACKEND": "supabase",
                "SUPABASE_URL": "https://example.supabase.co",
                "SUPABASE_SERVICE_ROLE_KEY": "server-only-test-key",
                "SUPABASE_STORAGE_BUCKET_REQUESTS": "private-requests",
            },
        ), patch("storage.requests.get", return_value=bucket_response) as get, patch(
            "storage.requests.post", return_value=upload_response
        ) as post:
            storage.upload("requests", "document.pdf", io.BytesIO(b"contents"))
        self.assertIn("/storage/v1/bucket/private-requests", get.call_args.args[0])
        self.assertIn("/storage/v1/object/private-requests/document.pdf", post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs["headers"]["x-upsert"], "false")
        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"],
            "Bearer server-only-test-key",
        )

    def test_refuses_public_bucket(self):
        bucket_response = Mock(status_code=200)
        bucket_response.json.return_value = {"public": True}
        with patch.dict(
            os.environ,
            {
                "STORAGE_BACKEND": "supabase",
                "SUPABASE_URL": "https://example.supabase.co",
                "SUPABASE_SERVICE_ROLE_KEY": "server-only-test-key",
            },
        ), patch("storage.requests.get", return_value=bucket_response):
            with self.assertRaisesRegex(storage.StorageError, "must be private"):
                storage.download("profiles", "photo.jpg")

    def test_profile_route_reports_storage_failure_without_database_write(self):
        connection = Mock()
        user = {"id": 17, "role": "resident"}
        with application.app.test_request_context(
            "/profile",
            method="POST",
            data={"profile_picture": (io.BytesIO(b"image"), "photo.png")},
        ), patch.object(application.dbh, "get_db", return_value=connection), patch.object(
            application, "save_upload", side_effect=storage.StorageError("unavailable")
        ):
            response = application.profile.__wrapped__(user)
            messages = get_flashed_messages(with_categories=True)

        self.assertEqual(response.status_code, 302)
        self.assertIn(
            ("error", "File storage is temporarily unavailable. Please try again."),
            messages,
        )
        connection.execute.assert_not_called()
        connection.close.assert_called_once()

    def test_failed_orphan_cleanup_is_logged_without_masking_original_error(self):
        with patch.object(
            application.storage_service,
            "delete",
            side_effect=storage.StorageError("cleanup unavailable"),
        ), patch.object(application.app.logger, "error") as log_error:
            application._cleanup_uploaded_files([("requests", "stored-file.pdf")])
        log_error.assert_called_once()


if __name__ == "__main__":
    unittest.main()
