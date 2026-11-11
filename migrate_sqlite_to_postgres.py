"""Dry-run-first, one-time import from an existing SQLite file to a staging PostgreSQL DB."""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

import psycopg


TABLE_ORDER = (
    "users",
    "community_issues",
    "requests",
    "complaints",
    "request_files",
    "community_issue_updates",
    "community_issue_follows",
    "complaint_files",
    "notifications",
    "audit_logs",
    "chat_messages",
    "settings",
    "admin_chart_preferences",
    "status_history",
    "password_reset_tokens",
    "password_reset_codes",
    "password_reset_limits",
    "admin_otp_challenges",
    "resident_concern_allowances",
    "concern_allowance_events",
    "announcements",
)
TABLE_KEYS = {
    "admin_chart_preferences": ("admin_id", "analytics_type", "category"),
    "password_reset_codes": ("user_id",),
    "password_reset_limits": ("user_id",),
    "admin_otp_challenges": ("user_id",),
    "resident_concern_allowances": ("user_id",),
}
FOREIGN_KEYS = {
    "requests": {
        "resident_id": "users",
        "assigned_staff_id": "users",
    },
    "complaints": {
        "resident_id": "users",
        "assigned_staff_id": "users",
        "community_issue_id": "community_issues",
        "verified_by": "users",
    },
    "request_files": {"request_id": "requests"},
    "community_issue_updates": {"community_issue_id": "community_issues"},
    "community_issue_follows": {
        "user_id": "users",
        "community_issue_id": "community_issues",
    },
    "complaint_files": {"complaint_id": "complaints"},
    "notifications": {"user_id": "users"},
    "audit_logs": {"actor_id": "users"},
    "chat_messages": {"user_id": "users"},
    "admin_chart_preferences": {"admin_id": "users"},
    "password_reset_tokens": {"user_id": "users"},
    "password_reset_codes": {"user_id": "users"},
    "password_reset_limits": {"user_id": "users"},
    "admin_otp_challenges": {"user_id": "users"},
    "resident_concern_allowances": {"user_id": "users"},
    "concern_allowance_events": {
        "user_id": "users",
        "complaint_id": "complaints",
    },
}


def _sqlite_identifier(name):
    return '"' + name.replace('"', '""') + '"'


def _open_source(path):
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"SQLite source does not exist: {resolved}")
    uri = resolved.as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _row_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest(), "length": len(value)}
    return value


def _table_columns(connection, table):
    return tuple(
        row["name"]
        for row in connection.execute(
            f"PRAGMA table_info({_sqlite_identifier(table)})"
        ).fetchall()
    )


def _primary_key(connection, table, columns):
    if table in TABLE_KEYS:
        return TABLE_KEYS[table]
    key = tuple(
        row["name"]
        for row in sorted(
            (
                row
                for row in connection.execute(
                    f"PRAGMA table_info({_sqlite_identifier(table)})"
                ).fetchall()
                if row["pk"]
            ),
            key=lambda row: row["pk"],
        )
    )
    return key or columns


def _table_report(connection, table):
    columns = _table_columns(connection, table)
    primary_key = _primary_key(connection, table, columns)
    quoted = ", ".join(_sqlite_identifier(column) for column in columns)
    ordering = ", ".join(_sqlite_identifier(column) for column in primary_key)
    digest = hashlib.sha256()
    count = 0
    query = (
        f"SELECT {quoted} FROM {_sqlite_identifier(table)} "
        f"ORDER BY {ordering}"
    )
    for row in connection.execute(query):
        canonical = json.dumps(
            [_row_value(row[column]) for column in columns],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(canonical + b"\n")
        count += 1
    return {
        "columns": list(columns),
        "primary_key": list(primary_key),
        "rows": count,
        "sha256": digest.hexdigest(),
    }


def inspect_source(path):
    connection = _open_source(path)
    try:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        missing = sorted(set(TABLE_ORDER) - tables)
        unexpected = sorted(tables - set(TABLE_ORDER))
        if missing or unexpected:
            raise ValueError(
                "The SQLite schema does not match the supported migration schema "
                f"(missing tables: {missing}; unexpected tables: {unexpected})."
            )
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise ValueError(
                f"The SQLite source has {len(violations)} foreign-key violations."
            )
        table_reports = {
            table: _table_report(connection, table) for table in TABLE_ORDER
        }
        return {
            "source": str(Path(path).resolve()),
            "source_read_only": True,
            "table_count": len(table_reports),
            "row_count": sum(item["rows"] for item in table_reports.values()),
            "tables": table_reports,
            "foreign_key_violations": 0,
            "password_hashes_included_in_checksums": True,
        }
    finally:
        connection.close()


def _connect_target(target):
    variable = "STAGING_DATABASE_URL" if target == "staging" else "DATABASE_URL"
    database_url = os.environ.get(variable, "").strip()
    if not database_url:
        raise RuntimeError(
            f"{variable} is required for a {target} import."
        )
    import psycopg
    from psycopg.conninfo import make_conninfo

    return psycopg.connect(make_conninfo(database_url, sslmode="require"))


def _postgres_columns(connection, table):
    rows = connection.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    return tuple(row[0] for row in rows)


def _verify_empty_destination(connection, source_report):
    for table, report in source_report["tables"].items():
        target_columns = _postgres_columns(connection, table)
        if tuple(report["columns"]) != target_columns:
            raise ValueError(
                f"PostgreSQL table {table!r} is missing or has a different column layout."
            )
        count = connection.execute(
            f'SELECT COUNT(*) FROM public."{table}"'
        ).fetchone()[0]
        if count:
            raise ValueError(
                f"PostgreSQL table {table!r} already contains records; import refused."
            )


def _insert_source(connection, source_path, source_report):
    import psycopg
    from psycopg import sql

    source = _open_source(source_path)
    try:
        for table in TABLE_ORDER:
            columns = source_report["tables"][table]["columns"]
            statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                sql.Identifier("public", table),
                sql.SQL(", ").join(map(sql.Identifier, columns)),
                sql.SQL(", ").join(sql.Placeholder() for _ in columns),
            )
            insert_cursor = connection.cursor()
            select_columns = ", ".join(_sqlite_identifier(column) for column in columns)
            ordering = ", ".join(
                _sqlite_identifier(column)
                for column in source_report["tables"][table]["primary_key"]
            )
            source_cursor = source.execute(
                f"SELECT {select_columns} FROM {_sqlite_identifier(table)} "
                f"ORDER BY {ordering}"
            )
            while rows := source_cursor.fetchmany(500):
                insert_cursor.executemany(
                    statement,
                    [tuple(row[column] for column in columns) for row in rows],
                )
        for table in TABLE_ORDER:
            columns = source_report["tables"][table]["columns"]
            if "id" not in columns:
                continue
            connection.execute(
                sql.SQL(
                    "SELECT setval(pg_get_serial_sequence(%s, 'id'), "
                    "COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM {}"
                ).format(sql.Identifier("public", table)),
                (f"public.{table}",),
            )
    finally:
        source.close()


def _postgres_table_report(connection, table, source_report):
    columns = source_report["tables"][table]["columns"]
    keys = source_report["tables"][table]["primary_key"]
    select_columns = ", ".join(f'"{column}"' for column in columns)
    ordering = ", ".join(f'"{column}"' for column in keys)
    digest = hashlib.sha256()
    count = 0
    cursor = connection.execute(
        f'SELECT {select_columns} FROM public."{table}" ORDER BY {ordering}'
    )
    for row in cursor:
        canonical = json.dumps(
            [_row_value(value) for value in row],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(canonical + b"\n")
        count += 1
    return {"rows": count, "sha256": digest.hexdigest()}


def _verify_foreign_keys(connection):
    for table, relations in FOREIGN_KEYS.items():
        for column, parent in relations.items():
            invalid_count = connection.execute(
                f'SELECT COUNT(*) FROM public."{table}" child '
                f'LEFT JOIN public."{parent}" parent ON parent.id=child."{column}" '
                f'WHERE child."{column}" IS NOT NULL AND parent.id IS NULL'
            ).fetchone()[0]
            if invalid_count:
                raise ValueError(
                    f"PostgreSQL foreign-key verification failed for {table}.{column}."
                )


def import_source(source_path, source_report, target, on_verified=None):
    connection = _connect_target(target)
    try:
        with connection.transaction():
            _verify_empty_destination(connection, source_report)
            _insert_source(connection, source_path, source_report)
            _verify_foreign_keys(connection)
            migrated = {
                table: _postgres_table_report(connection, table, source_report)
                for table in TABLE_ORDER
            }
            for table, expected in source_report["tables"].items():
                actual = migrated[table]
                if (
                    actual["rows"] != expected["rows"]
                    or actual["sha256"] != expected["sha256"]
                ):
                    raise ValueError(
                        f"PostgreSQL row or checksum verification failed for {table!r}."
                    )
            if on_verified:
                on_verified(migrated)
        return migrated
    finally:
        connection.close()


def _write_report(path, report, *, create=False):
    if not path:
        return
    output = Path(path)
    with output.open("x" if create else "w", encoding="utf-8") as report_file:
        json.dump(report, report_file, indent=2)
        report_file.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="SQLite database to read (never modified)")
    confirmation = parser.add_mutually_exclusive_group()
    confirmation.add_argument(
        "--confirm-import",
        action="store_true",
        help="Import into STAGING_DATABASE_URL (empty destination only)",
    )
    confirmation.add_argument(
        "--confirm-production-import",
        action="store_true",
        help="Explicitly import into DATABASE_URL (empty destination only)",
    )
    parser.add_argument("--report", help="Create a new JSON report file; existing files are not overwritten")
    args = parser.parse_args(argv)
    target = (
        "production"
        if args.confirm_production_import
        else "staging"
        if args.confirm_import
        else None
    )
    report = {"mode": f"{target}-import" if target else "dry-run"}
    report_reserved = False
    try:
        if args.report:
            report.update(status="in_progress")
            _write_report(args.report, report, create=True)
            report_reserved = True
        report.update(inspect_source(args.source))
        if target:
            report["destination"] = (
                "DATABASE_URL" if target == "production" else "STAGING_DATABASE_URL"
            )
            report["destination_rows_before"] = "verified empty"

            def record_verified(migrated):
                report["migrated_tables"] = migrated
                report["status"] = "verified; transaction commit pending"
                if report_reserved:
                    _write_report(args.report, report)

            report["migrated_tables"] = import_source(
                args.source,
                report,
                target,
                on_verified=record_verified if report_reserved else None,
            )
            report["status"] = "completed"
        else:
            report["status"] = "dry-run complete; no destination was modified"
        if report_reserved:
            _write_report(args.report, report)
        print(json.dumps(report, indent=2))
        return 0
    except (OSError, sqlite3.Error, psycopg.Error, ValueError, RuntimeError) as error:
        report.update(status="failed", error=str(error))
        if report_reserved:
            _write_report(args.report, report)
        print(json.dumps(report, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
