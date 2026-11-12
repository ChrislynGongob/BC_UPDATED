"""Create the initial Founding Admin only when no administrator exists."""

import os
import re
import sqlite3
import sys

from werkzeug.security import generate_password_hash

import db as dbh
from setup_founding_admin import FOUNDING_ADMIN_EMAIL


def init_founding_admin():
    if dbh.get_backend() == "postgres":
        dbh.ensure_postgres_schema()
    elif not os.path.isfile(dbh.DB_PATH):
        if os.environ.get("RENDER", "").lower() == "true" or os.environ.get(
            "APP_ENV", ""
        ).lower() == "production":
            raise RuntimeError(
                "The production SQLite database is missing; refusing to create it."
            )
        os.makedirs(os.path.dirname(dbh.DB_PATH), exist_ok=True)
        dbh.init_db()

    conn = dbh.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            has_admin = conn.execute(
                "SELECT 1 FROM users WHERE role='admin' LIMIT 1"
            ).fetchone()
        except sqlite3.OperationalError as exc:
            raise RuntimeError(
                "The existing database is not initialized with the required users schema."
            ) from exc

        if has_admin:
            conn.rollback()
            return False

        name = os.environ.get("FOUNDING_ADMIN_NAME", "").strip()
        password = os.environ.get("FOUNDING_ADMIN_PASSWORD", "")
        if not name:
            raise ValueError("FOUNDING_ADMIN_NAME is missing.")
        if (
            len(password) < 12
            or not re.search(r"[A-Za-z]", password)
            or not re.search(r"\d", password)
        ):
            raise ValueError(
                "FOUNDING_ADMIN_PASSWORD must have at least 12 characters, "
                "one letter, and one number."
            )

        try:
            conn.execute(
                "INSERT INTO users (full_name, email, recovery_email, password_hash, "
                "role, is_super_admin, is_founding_admin, status, must_reset_pw) "
                "VALUES (?, ?, ?, ?, 'admin', 1, 1, 'active', 1)",
                (
                    name,
                    FOUNDING_ADMIN_EMAIL,
                    FOUNDING_ADMIN_EMAIL,
                    generate_password_hash(password),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise RuntimeError(
                "The Founding Admin could not be created without conflicting with existing data."
            ) from exc

        conn.commit()
        return True
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()


if __name__ == "__main__":
    try:
        created = init_founding_admin()
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as error:
        print(f"[INIT ERROR] {error}", file=sys.stderr)
        sys.exit(1)
    if created:
        print("[INIT] Founding Admin created. Change the initial password after sign-in.")
    else:
        print("[INIT] An administrator already exists; no account was changed.")
