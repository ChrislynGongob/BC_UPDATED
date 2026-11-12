import sqlite3
import os
import re
import random
import string
import shutil
import subprocess
from datetime import datetime, timedelta
from urllib.parse import unquote, urlsplit

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "instance", "barangayconnect.db")
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")
BACKUP_KEEP = 14                 # newest N backups are kept, older ones are deleted
AUTO_BACKUP_EVERY_HOURS = 24
_BACKUP_RE = re.compile(
    r"^barangayconnect-\d{4}-\d{2}-\d{2}_\d{6}(-\d+)?\.(?:db|dump)$"
)


def get_backend():
    backend = os.environ.get("DB_BACKEND", "sqlite").strip().lower()
    if backend not in {"sqlite", "postgres"}:
        raise ValueError("DB_BACKEND must be either 'sqlite' or 'postgres'.")
    return backend


class _CompatRow(dict):
    def __init__(self, names, values):
        super().__init__(zip(names, values))
        self._values = values

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


def _postgres_row_factory(cursor):
    if cursor.description is None:
        return lambda values: values
    names = [column.name for column in cursor.description]
    return lambda values: _CompatRow(names, values)


def _postgres_sql(sql):
    sql = sql.strip()
    if re.match(r"^BEGIN\s+IMMEDIATE$", sql, flags=re.IGNORECASE):
        # Preserve SQLite's writer serialization for the small critical sections using this statement.
        return "SELECT pg_advisory_xact_lock(195215649, 1)"

    ignore_insert = re.match(r"^(INSERT)\s+OR\s+IGNORE\s+INTO\b", sql, flags=re.IGNORECASE)
    if ignore_insert:
        sql = re.sub(
            r"^INSERT\s+OR\s+IGNORE\s+INTO\b",
            "INSERT INTO",
            sql,
            count=1,
            flags=re.IGNORECASE,
        )
        sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"

    sql = re.sub(
        r"\bdatetime\(\s*'now'\s*,\s*'([+-]\d+)\s+(hours?|days?)'\s*\)",
        lambda match: "(clock_timestamp() + INTERVAL "
        + repr(f"{match.group(1)} {match.group(2).rstrip('s')}s")
        + ")",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"\bdate\(\s*'now'\s*,\s*'([+-]\d+)\s+(hours?|days?)'\s*\)",
        lambda match: "(clock_timestamp() + INTERVAL "
        + repr(f"{match.group(1)} {match.group(2).rstrip('s')}s")
        + ")::date",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"\bdate\(\s*([A-Za-z_][A-Za-z_0-9.]*)\s*,\s*'([+-]\d+)\s+(hours?|days?)'\s*\)",
        lambda match: "(" + match.group(1) + "::timestamp + INTERVAL "
        + repr(f"{match.group(2)} {match.group(3).rstrip('s')}s")
        + ")::date",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"\bdatetime\(\s*'now'\s*\)",
        "to_char(clock_timestamp(), 'YYYY-MM-DD HH24:MI:SS')",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"\bdatetime\(\s*([A-Za-z_][A-Za-z_0-9.]*)\s*\)",
        r"\1::timestamp",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"\bdate\(\s*'now'\s*\)",
        "(clock_timestamp())::date",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"\bstrftime\(\s*'%Y-%m'\s*,\s*([A-Za-z_][A-Za-z_0-9.]*)\s*\)",
        r"to_char(\1::timestamp, 'YYYY-MM')",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"([A-Za-z_][A-Za-z_0-9.]*::timestamp\s*(?:>=|<=|<>|!=|=|>|<)\s*)\?",
        r"\1?::timestamp",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"\bMIN\(\s*([A-Za-z_][A-Za-z_0-9.]*)\s*,",
        r"LEAST(\1,",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"\bMAX\(\s*([A-Za-z_][A-Za-z_0-9.]*)\s*,",
        r"GREATEST(\1,",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(r"\b([A-Za-z_][A-Za-z_0-9.]*)\s+COLLATE\s+NOCASE\b", r"LOWER(\1)", sql)

    output = []
    quote = None
    index = 0
    while index < len(sql):
        char = sql[index]
        if quote:
            output.append(char)
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    output.append(sql[index + 1])
                    index += 1
                else:
                    quote = None
        elif sql.startswith("--", index):
            newline = sql.find("\n", index)
            end = len(sql) if newline == -1 else newline
            output.append(sql[index:end])
            index = end - 1
        elif sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end == -1:
                output.append(sql[index:])
                break
            output.append(sql[index : end + 2])
            index = end + 1
        elif char in {"'", '"'}:
            quote = char
            output.append(char)
        elif char == "?":
            output.append("%s")
        elif char.isalpha() or char == "_":
            end = index + 1
            while end < len(sql) and (sql[end].isalnum() or sql[end] in {"_", "$"}):
                end += 1
            keyword = sql[index:end]
            output.append("ILIKE" if keyword.upper() == "LIKE" else keyword)
            index = end - 1
        else:
            output.append(char)
        index += 1
    return "".join(output)


class _PostgresCursor:
    def __init__(self, connection, cursor, table_name=None):
        self._connection = connection
        self._cursor = cursor
        self._table_name = table_name

    @property
    def lastrowid(self):
        if not self._table_name:
            raise RuntimeError("lastrowid is only available after an INSERT statement.")
        row = self._connection.execute(
            "SELECT currval(pg_get_serial_sequence(%s, 'id')) AS id",
            (f"public.{self._table_name}",),
        ).fetchone()
        return row["id"]

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def __iter__(self):
        return iter(self._cursor)


class _PostgresConnection:
    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, params=()):
        match = re.match(
            r"\s*INSERT\s+(?:OR\s+IGNORE\s+)?INTO\s+(?:\"?public\"?\.)?\"?([A-Za-z_][A-Za-z_0-9]*)",
            sql,
            flags=re.IGNORECASE,
        )
        cursor = self._connection.cursor()
        cursor.execute(_postgres_sql(sql), params)
        return _PostgresCursor(self, cursor, match.group(1) if match else None)

    def executemany(self, sql, params):
        cursor = self._connection.cursor()
        cursor.executemany(_postgres_sql(sql), params)
        return _PostgresCursor(self, cursor)

    @property
    def in_transaction(self):
        from psycopg.pq import TransactionStatus

        return self._connection.info.transaction_status != TransactionStatus.IDLE

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        try:
            if self.in_transaction:
                self.rollback()
        finally:
            self._connection.close()


def get_db():
    if get_backend() == "postgres":
        database_url = os.environ.get("DATABASE_URL", "").strip()
        if not database_url:
            raise RuntimeError("DATABASE_URL is required when DB_BACKEND=postgres.")
        import psycopg
        from psycopg.conninfo import make_conninfo

        connection = psycopg.connect(
            make_conninfo(database_url, sslmode="require"),
            row_factory=_postgres_row_factory,
        )
        return _PostgresConnection(connection)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    if get_backend() != "sqlite":
        raise RuntimeError(
            "PostgreSQL schema creation is explicit; apply postgres_schema.sql manually."
        )
    os.makedirs(os.path.join(BASE_DIR, "instance"), exist_ok=True)
    conn = get_db()
    with open(SCHEMA_PATH, "r") as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()


def ensure_postgres_schema():
    if get_backend() != "postgres":
        return
    import psycopg

    conn = get_db()
    try:
        conn.execute("SELECT id FROM users LIMIT 0").fetchall()
    except psycopg.errors.UndefinedTable as error:
        raise RuntimeError(
            "The PostgreSQL schema is missing. Apply postgres_schema.sql explicitly; "
            "the application will not initialize it automatically."
        ) from error
    finally:
        conn.close()


def migrate_db():
    """Upgrade an existing database in place. Safe to run on every start."""
    if get_backend() != "sqlite":
        raise RuntimeError(
            "Automatic PostgreSQL schema migrations are disabled; review and apply "
            "postgres_schema.sql explicitly."
        )
    if not os.path.isfile(DB_PATH):
        raise FileNotFoundError(
            "SQLite database is missing; migrate_db will not create it."
        )
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS admin_chart_preferences (
            admin_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            analytics_type TEXT NOT NULL CHECK (
                analytics_type IN ('complaints','document-requests')
            ),
            category TEXT NOT NULL,
            color TEXT NOT NULL CHECK (
                color GLOB '#[0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]'
            ),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (admin_id, analytics_type, category)
        );
        CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_user
            ON password_reset_tokens(user_id);
        CREATE TABLE IF NOT EXISTS password_reset_codes (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            code_hash TEXT,
            expires_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            used_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS password_reset_limits (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            last_sent_at TEXT,
            window_started_at TEXT,
            sends_in_window INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS admin_otp_challenges (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            email_otp_hash TEXT,
            mode TEXT NOT NULL DEFAULT 'email_only',
            expires_at TEXT,
            sent_at TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            window_started_at TEXT,
            sends_in_window INTEGER NOT NULL DEFAULT 0,
            used_at TEXT
        );
        CREATE TABLE IF NOT EXISTS resident_concern_allowances (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            remaining INTEGER NOT NULL DEFAULT 5 CHECK (remaining BETWEEN 0 AND 5),
            total_submitted INTEGER NOT NULL DEFAULT 0,
            cooldown_started_at TEXT,
            cooldown_expires_at TEXT
        );
        CREATE TABLE IF NOT EXISTS concern_allowance_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            complaint_id INTEGER REFERENCES complaints(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL CHECK (event_type IN ('submitted','resolved_restoration','cooldown_refill')),
            slots INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_concern_allowance_event_once
            ON concern_allowance_events(complaint_id, event_type)
            WHERE complaint_id IS NOT NULL;
    """)
    chart_preference_columns = {
        row["name"]: row for row in conn.execute(
            "PRAGMA table_info(admin_chart_preferences)"
        ).fetchall()
    }
    chart_preference_pk = tuple(
        row["name"]
        for row in sorted(
            (row for row in chart_preference_columns.values() if row["pk"]),
            key=lambda row: row["pk"],
        )
    )
    if (
        "analytics_type" not in chart_preference_columns
        or chart_preference_pk != ("admin_id", "analytics_type", "category")
    ):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "CREATE TABLE admin_chart_preferences_migration ("
            "admin_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
            "analytics_type TEXT NOT NULL CHECK "
            "(analytics_type IN ('complaints','document-requests')), "
            "category TEXT NOT NULL, "
            "color TEXT NOT NULL CHECK "
            "(color GLOB '#[0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]'), "
            "updated_at TEXT NOT NULL DEFAULT (datetime('now')), "
            "PRIMARY KEY (admin_id, analytics_type, category))"
        )
        if "analytics_type" in chart_preference_columns:
            conn.execute(
                "INSERT INTO admin_chart_preferences_migration "
                "(admin_id, analytics_type, category, color, updated_at) "
                "SELECT admin_id, analytics_type, category, color, updated_at "
                "FROM admin_chart_preferences"
            )
        else:
            conn.execute(
                "INSERT INTO admin_chart_preferences_migration "
                "(admin_id, analytics_type, category, color, updated_at) "
                "SELECT admin_id, 'complaints', category, color, updated_at "
                "FROM admin_chart_preferences"
            )
        conn.execute("DROP TABLE admin_chart_preferences")
        conn.execute(
            "ALTER TABLE admin_chart_preferences_migration "
            "RENAME TO admin_chart_preferences"
        )
        conn.commit()
    otp_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(admin_otp_challenges)").fetchall()
    }
    for column, definition in (
        ("email_otp_hash", "TEXT"),
        ("mode", "TEXT NOT NULL DEFAULT 'email_only'"),
    ):
        if column not in otp_cols:
            conn.execute(f"ALTER TABLE admin_otp_challenges ADD COLUMN {column} {definition}")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS community_issues ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "category TEXT NOT NULL, location TEXT NOT NULL, normalized_location TEXT NOT NULL, "
        "status TEXT NOT NULL CHECK (status IN ('Pending','Under Review','Scheduled','Resolved','Closed','Rejected')), "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS community_issue_updates ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "community_issue_id INTEGER NOT NULL REFERENCES community_issues(id) ON DELETE CASCADE, "
        "status TEXT NOT NULL CHECK (status IN ('Pending','Under Review','Scheduled','Resolved','Closed','Rejected')), "
        "changed_by TEXT, message TEXT, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    update_cols = {r["name"] for r in conn.execute("PRAGMA table_info(community_issue_updates)").fetchall()}
    if "changed_by" not in update_cols:
        conn.execute("ALTER TABLE community_issue_updates ADD COLUMN changed_by TEXT")
    if "message" not in update_cols:
        conn.execute("ALTER TABLE community_issue_updates ADD COLUMN message TEXT")
    user_cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "is_super_admin" not in user_cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN is_super_admin INTEGER NOT NULL DEFAULT 0"
        )
    if "is_founding_admin" not in user_cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN is_founding_admin INTEGER NOT NULL DEFAULT 0"
        )
    if "must_reset_pw" not in user_cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN must_reset_pw INTEGER NOT NULL DEFAULT 0"
        )
    if "status" not in user_cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
        )
    if "failed_logins" not in user_cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN failed_logins INTEGER NOT NULL DEFAULT 0"
        )
    if "locked_until" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN locked_until TEXT")
    if "contact_number" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN contact_number TEXT")
    user_cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "recovery_email" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN recovery_email TEXT")
        if "address" in user_cols:
            conn.execute(
                "UPDATE users SET recovery_email=substr(address, 10) "
                "WHERE role='admin' AND address LIKE 'recovery:%' "
                "AND recovery_email IS NULL"
            )
    # Upgrade the legacy single-founder flag without changing Super Admin status.
    # Existing deployments had exactly one is_super_admin account for the founder.
    founder_count = conn.execute(
        "SELECT COUNT(*) FROM users WHERE role='admin' AND is_founding_admin=1"
    ).fetchone()[0]
    legacy_founders = conn.execute(
        "SELECT id FROM users WHERE role='admin' AND is_super_admin=1"
    ).fetchall()
    if founder_count == 0 and len(legacy_founders) == 1:
        conn.execute(
            "UPDATE users SET is_founding_admin=1 "
            "WHERE id=? AND role='admin' AND is_super_admin=1 AND status='active'",
            (legacy_founders[0]["id"],),
        )
        founder_count = conn.execute(
            "SELECT COUNT(*) FROM users WHERE role='admin' AND is_founding_admin=1"
        ).fetchone()[0]
    if founder_count > 1:
        conn.close()
        raise sqlite3.IntegrityError(
            "Multiple Founding Admin accounts exist; resolve duplicates before migration."
        )
    conn.execute("DROP INDEX IF EXISTS idx_users_single_founding_admin")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_single_founding_admin "
        "ON users(is_founding_admin) WHERE role='admin' AND is_founding_admin=1"
    )
    if "office" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN office TEXT")
        # Best-effort mapping of existing staff from their old category assignment / position.
        # Anyone we cannot map stays office-less (no access) until an admin assigns an office.
        has_scope = "category_scope" in user_cols
        scope = "COALESCE(category_scope,'')" if has_scope else "''"
        conn.execute(
            f"UPDATE users SET office='bpso' WHERE role='staff' AND ({scope} IN "
            "('Noise Complaint','Neighbor Dispute','Illegal Parking','Sanitation','Public Safety',"
            "'Domestic Concern','Others') OR LOWER(COALESCE(position,'')) LIKE '%complaint%' "
            "OR LOWER(COALESCE(position,'')) LIKE '%bpso%')")
        conn.execute(
            f"UPDATE users SET office='treasurer' WHERE role='staff' AND office IS NULL AND "
            f"({scope} IN ('Barangay Clearance','Business Clearance') OR LOWER(COALESCE(position,'')) LIKE '%treasur%')")
        conn.execute(
            f"UPDATE users SET office='secretary' WHERE role='staff' AND office IS NULL AND "
            f"({scope} IN ('Certificate of Indigency','Certificate of Residency') "
            "OR LOWER(COALESCE(position,'')) LIKE '%secretar%')")
    req_cols = [r["name"] for r in conn.execute("PRAGMA table_info(requests)").fetchall()]
    if req_cols and "registered_voter" not in req_cols:
        conn.execute("ALTER TABLE requests ADD COLUMN registered_voter INTEGER NOT NULL DEFAULT 0")
    complaint_cols = [r["name"] for r in conn.execute("PRAGMA table_info(complaints)").fetchall()]
    if complaint_cols and "community_issue_id" not in complaint_cols:
        conn.execute("ALTER TABLE complaints ADD COLUMN community_issue_id INTEGER REFERENCES community_issues(id)")
    if complaint_cols and "is_verified" not in complaint_cols:
        conn.execute("ALTER TABLE complaints ADD COLUMN is_verified INTEGER NOT NULL DEFAULT 0")
    if complaint_cols and "verified_by" not in complaint_cols:
        conn.execute("ALTER TABLE complaints ADD COLUMN verified_by INTEGER REFERENCES users(id)")
    if complaint_cols and "verified_at" not in complaint_cols:
        conn.execute("ALTER TABLE complaints ADD COLUMN verified_at TEXT")
    follow_cols = [r["name"] for r in conn.execute("PRAGMA table_info(community_issue_follows)").fetchall()]
    if follow_cols and "community_issue_id" not in follow_cols:
        conn.execute("DROP INDEX IF EXISTS idx_community_issue_follows_issue")
        conn.execute("ALTER TABLE community_issue_follows RENAME TO community_issue_follows_legacy")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS community_issue_follows ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
        "community_issue_id INTEGER NOT NULL REFERENCES community_issues(id) ON DELETE CASCADE, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "UNIQUE(user_id, community_issue_id))"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_community_issue_follows_issue "
        "ON community_issue_follows(community_issue_id)"
    )
    if complaint_cols:
        complaints = conn.execute(
            "SELECT * FROM complaints WHERE community_issue_id IS NULL ORDER BY created_at, id"
        ).fetchall()
        status_order = {
            "Pending": 0, "Under Review": 1, "Scheduled": 2,
            "Resolved": 3, "Closed": 3, "Rejected": 3,
        }
        terminal_statuses = ("Resolved", "Closed", "Rejected")
        for complaint in complaints:
            normalized = normalize_community_issue_location(complaint["location"])
            issue = conn.execute(
                "SELECT * FROM community_issues WHERE category=? AND normalized_location=? "
                "AND status NOT IN ('Resolved','Closed','Rejected') ORDER BY id DESC LIMIT 1",
                (complaint["category"], normalized),
            ).fetchone()
            if issue is None:
                cursor = conn.execute(
                    "INSERT INTO community_issues "
                    "(category, location, normalized_location, status, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (complaint["category"], complaint["location"] or "Unknown location", normalized,
                     complaint["status"], complaint["created_at"], complaint["updated_at"]),
                )
                issue_id = cursor.lastrowid
                conn.execute(
                    "INSERT INTO community_issue_updates (community_issue_id, status, created_at) VALUES (?,?,?)",
                    (issue_id, complaint["status"], complaint["created_at"]),
                )
            else:
                issue_id = issue["id"]
                if complaint["status"] not in terminal_statuses and status_order.get(
                    complaint["status"], -1
                ) > status_order.get(issue["status"], -1):
                    conn.execute(
                        "UPDATE community_issues SET status=?, updated_at=? WHERE id=?",
                        (complaint["status"], complaint["updated_at"], issue_id),
                    )
                    conn.execute(
                        "INSERT INTO community_issue_updates (community_issue_id, status, created_at) "
                        "VALUES (?,?,?)",
                        (issue_id, complaint["status"], complaint["updated_at"]),
                    )
                conn.execute(
                    "UPDATE community_issues SET created_at=MIN(created_at, ?), "
                    "updated_at=MAX(updated_at, ?) WHERE id=?",
                    (complaint["created_at"], complaint["updated_at"], issue_id),
                )
            conn.execute(
                "UPDATE complaints SET community_issue_id=? WHERE id=?",
                (issue_id, complaint["id"]),
            )
            conn.execute(
                "INSERT OR IGNORE INTO community_issue_follows (user_id, community_issue_id) VALUES (?,?)",
                (complaint["resident_id"], issue_id),
            )
    legacy_follow_cols = [
        r["name"] for r in conn.execute("PRAGMA table_info(community_issue_follows_legacy)").fetchall()
    ]
    if {"user_id", "category", "normalized_location"}.issubset(legacy_follow_cols):
        legacy_follows = conn.execute("SELECT * FROM community_issue_follows_legacy").fetchall()
        for follow in legacy_follows:
            issues = conn.execute(
                "SELECT id, location FROM community_issues WHERE category=?",
                (follow["category"],),
            ).fetchall()
            for issue in issues:
                if normalize_complaint_location(issue["location"]) == follow["normalized_location"]:
                    conn.execute(
                        "INSERT OR IGNORE INTO community_issue_follows "
                        "(user_id, community_issue_id, created_at) VALUES (?,?,?)",
                        (follow["user_id"], issue["id"], follow["created_at"]),
                    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_community_issues_active_key "
        "ON community_issues(category, normalized_location) "
        "WHERE status IN ('Pending','Under Review','Scheduled')"
    )
    if complaint_cols:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_complaints_community_issue ON complaints(community_issue_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_complaints_verification "
            "ON complaints(community_issue_id, is_verified)"
        )
        if {"created_at", "category", "status"}.issubset(complaint_cols):
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_complaints_created_category_status "
                "ON complaints(created_at, category, status)"
            )
    request_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(requests)").fetchall()
    }
    if {"created_at", "request_type", "status"}.issubset(request_cols):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_requests_created_type_status "
            "ON requests(created_at, request_type, status)"
        )
    residents = conn.execute(
        "SELECT id FROM users WHERE role='resident' "
        "AND id NOT IN (SELECT user_id FROM resident_concern_allowances)"
    ).fetchall()
    for resident in residents:
        submitted_today = total_submitted = 0
        if complaint_cols:
            submitted_today = conn.execute(
                "SELECT COUNT(*) AS count FROM complaints WHERE resident_id=? "
                "AND date(created_at, '+8 hours')=date('now', '+8 hours')",
                (resident["id"],),
            ).fetchone()["count"]
            total_submitted = conn.execute(
                "SELECT COUNT(*) AS count FROM complaints WHERE resident_id=?",
                (resident["id"],),
            ).fetchone()["count"]
        remaining = max(0, 5 - submitted_today)
        cooldown_started = cooldown_expires = None
        if remaining == 0:
            cooldown_started = datetime.now().isoformat(timespec="seconds")
            cooldown_expires = (datetime.now() + timedelta(hours=24)).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO resident_concern_allowances "
            "(user_id, remaining, total_submitted, cooldown_started_at, cooldown_expires_at) "
            "VALUES (?,?,?,?,?)",
            (resident["id"], remaining, total_submitted, cooldown_started, cooldown_expires),
        )
    conn.commit()
    conn.close()


def gen_reference(prefix):
    """e.g. REQ-2026-4F2A1 / CMP-2026-9B3D0"""
    year = datetime.now().year
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
    return f"{prefix}-{year}-{suffix}"


def gen_temp_password(length=10):
    chars = string.ascii_letters + string.digits
    return "".join(random.choices(chars, k=length))


def log_action(conn, actor, action, target=None, details=None):
    conn.execute(
        "INSERT INTO audit_logs (actor_id, actor_name, action, target, details) VALUES (?,?,?,?,?)",
        (actor["id"] if actor else None, actor["full_name"] if actor else "System", action, target, details),
    )


def notify(conn, user_id, message, link=None):
    conn.execute(
        "INSERT INTO notifications (user_id, message, link) VALUES (?,?,?)",
        (user_id, message, link),
    )


MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15


def is_locked(user_row):
    if not user_row["locked_until"]:
        return False
    try:
        return datetime.strptime(user_row["locked_until"], "%Y-%m-%d %H:%M:%S") > datetime.now()
    except ValueError:
        return False


def register_failed_login(conn, user_row):
    fails = user_row["failed_logins"] + 1
    locked_until = None
    if fails >= MAX_FAILED_LOGINS:
        locked_until = (datetime.now() + timedelta(minutes=LOCKOUT_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
        fails = 0
    conn.execute(
        "UPDATE users SET failed_logins=?, locked_until=? WHERE id=?",
        (fails, locked_until, user_row["id"]),
    )
    conn.commit()
    return locked_until is not None


def clear_failed_logins(conn, user_id):
    conn.execute("UPDATE users SET failed_logins=0, locked_until=NULL WHERE id=?", (user_id,))
    conn.commit()


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row and row["value"] else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
        (key, value),
    )


def log_status(conn, entity_type, entity_id, status, changed_by_name, remarks=None):
    conn.execute(
        "INSERT INTO status_history (entity_type, entity_id, status, changed_by_name, remarks) "
        "VALUES (?,?,?,?,?)",
        (entity_type, entity_id, status, changed_by_name, remarks),
    )


# ------------------------------------------------------------------ backups --

def backup_dir():
    """Backups live next to the database file: instance/backups/ in production."""
    return os.path.join(os.path.dirname(DB_PATH), "backups")


def backup_database(keep=None):
    """Create a SQLite online backup or a PostgreSQL pg_dump file."""
    if get_backend() == "postgres":
        return _backup_postgres(keep)
    if not os.path.isfile(DB_PATH):
        raise FileNotFoundError(
            "SQLite database is missing; refusing to create an empty backup."
        )
    folder = backup_dir()
    keep = BACKUP_KEEP if keep is None else keep
    os.makedirs(folder, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dest = os.path.join(folder, f"barangayconnect-{stamp}.db")
    n = 1
    while os.path.exists(dest):          # two backups within the same second
        dest = os.path.join(folder, f"barangayconnect-{stamp}-{n}.db")
        n += 1
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    for old in list_backups()[keep:]:     # newest first -> drop the oldest
        try:
            os.remove(os.path.join(folder, old))
        except OSError:
            pass
    return dest


def _backup_postgres(keep=None):
    database_url = os.environ.get("DATABASE_URL", "").strip()
    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise RuntimeError("DATABASE_URL is not a valid PostgreSQL connection URL.")
    executable = os.environ.get("PG_DUMP_PATH") or shutil.which("pg_dump")
    if not executable:
        raise RuntimeError(
            "PostgreSQL backup requires pg_dump; configure PG_DUMP_PATH or use "
            "the managed Supabase backup workflow."
        )

    folder = backup_dir()
    keep = BACKUP_KEEP if keep is None else keep
    os.makedirs(folder, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dest = os.path.join(folder, f"barangayconnect-{stamp}.dump")
    suffix = 1
    while os.path.exists(dest):
        dest = os.path.join(folder, f"barangayconnect-{stamp}-{suffix}.dump")
        suffix += 1

    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PGSSLROOTCERT"}
    }
    env.update(
        {
            "PGHOST": parsed.hostname,
            "PGPORT": str(parsed.port or 5432),
            "PGUSER": unquote(parsed.username or ""),
            "PGDATABASE": unquote(parsed.path.lstrip("/")),
            "PGPASSWORD": unquote(parsed.password or ""),
            "PGSSLMODE": "require",
        }
    )
    try:
        result = subprocess.run(
            [
                executable,
                "--format=custom",
                "--no-owner",
                "--no-acl",
                "--file",
                dest,
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        if os.path.exists(dest):
            os.remove(dest)
        raise RuntimeError("PostgreSQL backup could not be completed.") from error
    if result.returncode:
        if os.path.exists(dest):
            os.remove(dest)
        raise RuntimeError(
            f"pg_dump failed with exit code {result.returncode}; no backup was retained."
        )

    for old in list_backups()[keep:]:
        try:
            os.remove(os.path.join(folder, old))
        except OSError:
            pass
    return dest


def list_backups():
    """[filename, ...] newest first. Only files that match our own naming pattern."""
    folder = backup_dir()
    if not os.path.isdir(folder):
        return []
    return sorted((f for f in os.listdir(folder) if _BACKUP_RE.match(f)), reverse=True)


def backup_info(name):
    path = os.path.join(backup_dir(), name)
    st = os.stat(path)
    return {"name": name, "size_kb": max(1, round(st.st_size / 1024)),
            "created": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")}


_last_auto_check = 0.0

def auto_backup_if_due(now_ts=None):
    """Daily automatic backup without a scheduler/extra dependency: called from a
    before_request hook, checks at most once an hour, and only backs up when the
    newest backup is older than AUTO_BACKUP_EVERY_HOURS. Never raises."""
    global _last_auto_check
    if get_backend() == "postgres":
        return None
    import time
    now_ts = time.time() if now_ts is None else now_ts
    if now_ts - _last_auto_check < 3600:
        return None
    _last_auto_check = now_ts
    try:
        if not os.path.isfile(DB_PATH):
            return None
        names = list_backups()
        if names:
            newest = os.path.getmtime(os.path.join(backup_dir(), names[0]))
            if now_ts - newest < AUTO_BACKUP_EVERY_HOURS * 3600:
                return None
        return backup_database()
    except Exception:
        return None


# ---------------------------------------------------------- resident filters --
_PUROK_RE = re.compile(r"\bpurok\s*(\d+)\b", re.IGNORECASE)


def purok_of(address):
    """'Purok 3, Barangay X' -> '3'. None when the address has no 'Purok N'."""
    m = _PUROK_RE.search(address or "")
    return str(int(m.group(1))) if m else None


def normalize_complaint_location(location):
    """Normalize a concern location for matching reports in the same area."""
    text = (location or "").strip()
    if not text:
        return ""
    value = re.sub(r"\s+", " ", text).lower()
    purok = purok_of(text)
    if purok is not None:
        return f"purok:{purok}"
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def normalize_community_issue_location(location):
    """Keep the complete normalized address for matching shared issue clusters."""
    value = re.sub(r"\s+", " ", (location or "").strip().lower())
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def purok_options(conn):
    """Purok numbers that actually appear in resident addresses (for the dropdown)."""
    rows = conn.execute("SELECT address FROM users WHERE role='resident'").fetchall()
    return sorted({p for r in rows if (p := purok_of(r["address"]))}, key=int)
