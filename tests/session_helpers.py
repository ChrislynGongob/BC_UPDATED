"""Build an authenticated test session matching the production session claims."""
import hashlib
import hmac
from datetime import datetime, timezone

import db as dbh
import app as A


def authenticate(client, user_id):
    conn = dbh.get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    with client.session_transaction() as state:
        state["user_id"] = user["id"]
        state["role"] = user["role"]
        state["auth_version"] = int(user["auth_version"] or 0)
        if user["role"] == "admin":
            state["admin_mfa_verified_at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
            state["admin_mfa_proof"] = hmac.new(
                A.app.secret_key.encode(),
                f"admin-credentials:{user['id']}:{user['password_hash']}:{user['email']}".encode(),
                hashlib.sha256,
            ).hexdigest()
            state["admin_last_activity_at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
            state.permanent = True
    return client
