"""Dry-run-first transfer of database-referenced local uploads to staging Storage."""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

import storage


REFERENCED_FILES = (
    ("requests", "request_files", "stored_name"),
    ("complaints", "complaint_files", "stored_name"),
    ("profiles", "users", "profile_picture"),
)
STAGING_ENV = {
    "SUPABASE_URL": "STAGING_SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY": "STAGING_SUPABASE_SERVICE_ROLE_KEY",
    "SUPABASE_STORAGE_BUCKET_REQUESTS": "STAGING_SUPABASE_STORAGE_BUCKET_REQUESTS",
    "SUPABASE_STORAGE_BUCKET_COMPLAINTS": "STAGING_SUPABASE_STORAGE_BUCKET_COMPLAINTS",
    "SUPABASE_STORAGE_BUCKET_PROFILES": "STAGING_SUPABASE_STORAGE_BUCKET_PROFILES",
}


def _read_references(source_path):
    source = Path(source_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"SQLite source does not exist: {source}")
    connection = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        references = set()
        for subdir, table, column in REFERENCED_FILES:
            references.update(
                (subdir, row[0])
                for row in connection.execute(
                    f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'
                )
            )
        return source, sorted(references)
    finally:
        connection.close()


def _file_path(upload_root, subdir, stored_name):
    if subdir not in {entry[0] for entry in REFERENCED_FILES}:
        raise ValueError("Invalid upload category in database.")
    if not stored_name or Path(stored_name).name != stored_name:
        raise ValueError("Invalid stored filename in database.")
    root = Path(upload_root).resolve()
    path = (root / subdir / stored_name).resolve()
    if root not in path.parents:
        raise ValueError("Upload file resolves outside the selected upload directory.")
    return path


def inspect_files(source_path, upload_root):
    source, references = _read_references(source_path)
    digest = hashlib.sha256()
    missing = 0
    total_bytes = 0
    for subdir, stored_name in references:
        path = _file_path(upload_root, subdir, stored_name)
        if not path.is_file():
            missing += 1
            continue
        file_digest = hashlib.sha256()
        size = 0
        with path.open("rb") as source_file:
            while chunk := source_file.read(1024 * 1024):
                file_digest.update(chunk)
                digest.update(chunk)
                size += len(chunk)
        total_bytes += size
        digest.update(b"\0")
    return {
        "source_database": str(source),
        "upload_root": str(Path(upload_root).resolve()),
        "referenced_file_count": len(references),
        "missing_file_count": missing,
        "total_bytes": total_bytes,
        "source_files_sha256": digest.hexdigest(),
        "status": "ready" if not missing else "missing local files; transfer refused",
    }, references


def _target_environment(target):
    prefix = "STAGING_" if target == "staging" else ""
    required = (
        f"{prefix}SUPABASE_URL",
        f"{prefix}SUPABASE_SERVICE_ROLE_KEY",
    )
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        raise RuntimeError(
            f"{target.title()} Storage credentials are required: "
            + ", ".join(missing)
        )
    keys = ("STORAGE_BACKEND", *STAGING_ENV)
    saved = {name: os.environ.get(name) for name in keys}
    os.environ["STORAGE_BACKEND"] = "supabase"
    for app_variable, staging_variable in STAGING_ENV.items():
        variable = staging_variable if prefix else app_variable
        value = os.environ.get(variable)
        if value:
            os.environ[app_variable] = value
        else:
            os.environ.pop(app_variable, None)
    return saved


def _restore_environment(saved):
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def transfer(upload_root, references, target, on_verified=None):
    saved = _target_environment(target)
    verified = 0
    try:
        for subdir, stored_name in references:
            path = _file_path(upload_root, subdir, stored_name)
            source_hash = hashlib.sha256(path.read_bytes()).digest()
            try:
                existing = storage.download(subdir, stored_name)
            except storage.StorageNotFound:
                existing = None
            if existing is not None:
                destination_hash = hashlib.sha256(existing).digest()
                if destination_hash != source_hash:
                    raise RuntimeError(
                        f"Destination object {subdir}/{stored_name} exists with different contents; "
                        "it was not overwritten."
                    )
            else:
                with path.open("rb") as source_file:
                    storage.upload(subdir, stored_name, source_file)
                destination_hash = hashlib.sha256(
                    storage.download(subdir, stored_name)
                ).digest()
            if source_hash != destination_hash:
                raise RuntimeError(
                    "Storage checksum verification failed; do not switch storage backends."
                )
            verified += 1
            if on_verified:
                on_verified(verified)
        return verified
    finally:
        _restore_environment(saved)


def _write_report(path, report, *, create=False):
    if not path:
        return
    with Path(path).open("x" if create else "w", encoding="utf-8") as report_file:
        json.dump(report, report_file, indent=2)
        report_file.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", required=True, help="SQLite DB containing upload references")
    parser.add_argument(
        "--upload-root",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads"),
    )
    confirmation = parser.add_mutually_exclusive_group()
    confirmation.add_argument(
        "--confirm-upload",
        action="store_true",
        help="Transfer to staging Supabase Storage",
    )
    confirmation.add_argument(
        "--confirm-production-upload",
        action="store_true",
        help="Explicitly transfer to production Supabase Storage",
    )
    parser.add_argument("--report", help="Create a new JSON report file; existing files are not overwritten")
    args = parser.parse_args(argv)
    report = {
        "mode": "production-transfer"
        if args.confirm_production_upload
        else "staging-transfer"
        if args.confirm_upload
        else "dry-run"
    }
    report_reserved = False
    try:
        if args.report:
            report.update(status="in_progress", verified_file_count=0)
            _write_report(args.report, report, create=True)
            report_reserved = True
        file_report, references = inspect_files(args.source_db, args.upload_root)
        report.update(file_report)
        if file_report["missing_file_count"]:
            raise ValueError(file_report["status"])
        if args.confirm_upload or args.confirm_production_upload:
            target = "production" if args.confirm_production_upload else "staging"
            report["target"] = (
                "SUPABASE_URL" if target == "production" else "STAGING_SUPABASE_URL"
            )

            def record_verified(count):
                report["verified_file_count"] = count
                report["status"] = "transfer in progress"
                if report_reserved:
                    _write_report(args.report, report)

            report["verified_file_count"] = transfer(
                args.upload_root,
                references,
                target,
                on_verified=record_verified if report_reserved else None,
            )
            report["status"] = "completed; verify the report before switching STORAGE_BACKEND"
        else:
            report["status"] = "dry-run complete; no files were uploaded"
        if report_reserved:
            _write_report(args.report, report)
        print(json.dumps(report, indent=2))
        return 0
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as error:
        report.update(status="failed", error=str(error))
        if report_reserved:
            _write_report(args.report, report)
        print(json.dumps(report, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
