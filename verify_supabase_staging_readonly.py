"""Verify staging PostgreSQL row counts and download referenced private objects."""

import hashlib
import json
import os
import sys
from contextlib import contextmanager
from urllib.parse import unquote, urlsplit

import db as dbh
import storage
from migrate_sqlite_to_postgres import TABLE_ORDER


EXPECTED_TABLE_COUNT = 21
EXPECTED_ROW_COUNT = 163
STAGING_STORAGE_ENV = {
    "SUPABASE_URL": "STAGING_SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY": "STAGING_SUPABASE_SERVICE_ROLE_KEY",
    "SUPABASE_STORAGE_BUCKET_REQUESTS": "STAGING_SUPABASE_STORAGE_BUCKET_REQUESTS",
    "SUPABASE_STORAGE_BUCKET_COMPLAINTS": "STAGING_SUPABASE_STORAGE_BUCKET_COMPLAINTS",
    "SUPABASE_STORAGE_BUCKET_PROFILES": "STAGING_SUPABASE_STORAGE_BUCKET_PROFILES",
}


def _same_database_target(first_url, second_url):
    if not first_url or not second_url:
        return False
    first = urlsplit(first_url)
    second = urlsplit(second_url)
    return (
        first.scheme.lower() in {"postgres", "postgresql"}
        and second.scheme.lower() in {"postgres", "postgresql"}
        and first.hostname == second.hostname
        and first.port == second.port
        and first.path.rstrip("/") == second.path.rstrip("/")
    )


def _required_staging_environment():
    required = ("STAGING_DATABASE_URL", "STAGING_SUPABASE_URL", "STAGING_SUPABASE_SERVICE_ROLE_KEY")
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        raise RuntimeError("Missing staging variables: " + ", ".join(missing))

    database_url = os.environ["STAGING_DATABASE_URL"].strip()
    parsed_database_url = urlsplit(database_url)
    if (
        parsed_database_url.scheme.lower() not in {"postgres", "postgresql"}
        or not parsed_database_url.hostname
        or not parsed_database_url.path.strip("/")
    ):
        raise RuntimeError("STAGING_DATABASE_URL is not a valid PostgreSQL URL.")
    if _same_database_target(database_url, os.environ.get("DATABASE_URL", "").strip()):
        raise RuntimeError("Staging database URL matches DATABASE_URL; refusing to connect.")

    supabase_url = os.environ["STAGING_SUPABASE_URL"].strip().rstrip("/")
    parsed_supabase_url = urlsplit(supabase_url)
    if parsed_supabase_url.scheme.lower() != "https" or not parsed_supabase_url.hostname:
        raise RuntimeError("STAGING_SUPABASE_URL must be an HTTPS URL.")
    production_supabase_url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    if (
        production_supabase_url
        and parsed_supabase_url.hostname
        == urlsplit(production_supabase_url).hostname
    ):
        raise RuntimeError("Staging Storage URL matches SUPABASE_URL; refusing to connect.")
    return database_url


@contextmanager
def _staging_storage_environment():
    names = ("STORAGE_BACKEND", *STAGING_STORAGE_ENV.keys())
    original = {name: os.environ.get(name) for name in names}
    os.environ["STORAGE_BACKEND"] = "supabase"
    for application_name, staging_name in STAGING_STORAGE_ENV.items():
        value = os.environ.get(staging_name)
        if value:
            os.environ[application_name] = value
        else:
            os.environ.pop(application_name, None)
    try:
        yield
    finally:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _referenced_objects(connection):
    query = (
        "SELECT DISTINCT subdir, stored_name FROM ("
        "SELECT 'requests' AS subdir, stored_name FROM public.request_files "
        "WHERE stored_name IS NOT NULL "
        "UNION ALL "
        "SELECT 'complaints' AS subdir, stored_name FROM public.complaint_files "
        "WHERE stored_name IS NOT NULL "
        "UNION ALL "
        "SELECT 'profiles' AS subdir, profile_picture AS stored_name FROM public.users "
        "WHERE profile_picture IS NOT NULL"
        ") AS file_refs ORDER BY subdir, stored_name"
    )
    return [(row[0], row[1]) for row in connection.execute(query).fetchall()]


def verify_read_only():
    database_url = _required_staging_environment()
    original_backend = os.environ.get("DB_BACKEND")
    original_database_url = os.environ.get("DATABASE_URL")
    os.environ["DB_BACKEND"] = "postgres"
    os.environ["DATABASE_URL"] = database_url
    connection = None
    try:
        connection = dbh.get_db()
        connection.execute("SET TRANSACTION READ ONLY")
        existing_tables = {
            row[0]
            for row in connection.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_type='BASE TABLE'"
            ).fetchall()
        }
        missing_tables = sorted(set(TABLE_ORDER) - existing_tables)
        if missing_tables:
            raise RuntimeError("Staging schema is missing expected application tables.")
        table_counts = {
            table: connection.execute(
                f'SELECT COUNT(*) FROM public."{table}"'
            ).fetchone()[0]
            for table in TABLE_ORDER
        }
        references = _referenced_objects(connection)
        if not references:
            raise RuntimeError("No database-referenced staging Storage objects were found.")
        return {
            "table_count": len(TABLE_ORDER),
            "row_count": sum(table_counts.values()),
            "expected_table_count": EXPECTED_TABLE_COUNT,
            "expected_row_count": EXPECTED_ROW_COUNT,
            "counts_match_expected": (
                len(TABLE_ORDER) == EXPECTED_TABLE_COUNT
                and sum(table_counts.values()) == EXPECTED_ROW_COUNT
            ),
            "referenced_object_count": len(references),
            "objects": references,
        }
    finally:
        if connection is not None:
            connection.close()
        if original_backend is None:
            os.environ.pop("DB_BACKEND", None)
        else:
            os.environ["DB_BACKEND"] = original_backend
        if original_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original_database_url


def verify_private_downloads(references):
    verified = []
    with _staging_storage_environment():
        for subdir, stored_name in references:
            content = storage.download(subdir, stored_name)
            expected_size = storage.file_size(subdir, stored_name)
            if not content or len(content) != expected_size:
                raise RuntimeError("A private Storage download did not match its reported size.")
            verified.append(
                {
                    "bucket_category": subdir,
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
    return verified


def main():
    try:
        database = verify_read_only()
        downloads = verify_private_downloads(database.pop("objects"))
        result = {
            "database_read_only_check": "passed",
            **database,
            "private_storage_downloads": downloads,
            "status": "passed" if database["counts_match_expected"] else "count mismatch",
        }
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "passed" else 1
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "message": str(error)
                    if isinstance(error, RuntimeError)
                    else "Staging verification failed; details suppressed.",
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
