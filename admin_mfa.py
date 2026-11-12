"""Database-backed email MFA for admins, including the founding admin.

Uses the existing admin_otp_challenges table and database transaction adapter.
All timestamps here are UTC. No OTP or provider exception text is logged.
"""
import hashlib
import hmac
import math
import re
import secrets
from datetime import datetime, timedelta, timezone

from flask import abort, flash, redirect, render_template, request, session, url_for


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def register_admin_mfa(app, dbh, email_service):
    def digest(value):
        return hmac.new(app.secret_key.encode(), value.encode(), hashlib.sha256).hexdigest()

    def otp_digest(user, nonce, code):
        # Binding to credentials also invalidates codes after password/email changes.
        return digest(f"admin-login:{user['id']}:{user['password_hash']}:{user['email']}:{nonce}:{code}")

    def timestamp(value):
        return datetime.fromisoformat(value) if value else None

    def pending_admin(conn):
        pending = session.get("pending_mfa_user")
        if not isinstance(pending, dict):
            return None
        started = timestamp(pending.get("started_at"))
        if not started or _utc_now() >= started + timedelta(minutes=15):
            session.pop("pending_mfa_user", None)
            return None
        user = conn.execute(
            "SELECT * FROM users WHERE id=? AND role='admin' AND status='active'",
            (pending["id"],),
        ).fetchone()
        if not user or not hmac.compare_digest(
            pending.get("credential_proof", ""),
            digest(f"admin-credentials:{user['id']}:{user['password_hash']}:{user['email']}"),
        ):
            session.pop("pending_mfa_user", None)
            return None
        return user

    def issue(conn, user, nonce):
        # Call under BEGIN IMMEDIATE, translated to a transaction advisory lock
        # by the PostgreSQL adapter. Limits are shared across workers/browsers.
        now = _utc_now()
        row = conn.execute("SELECT * FROM admin_otp_challenges WHERE user_id=?", (user["id"],)).fetchone()
        window = timestamp(row["window_started_at"]) if row else None
        fresh = not window or now >= window + timedelta(hours=1)
        sends = 0 if fresh else row["sends_in_window"]
        attempts = 0 if fresh else row["attempts"]
        sent = timestamp(row["sent_at"]) if row else None
        wait = max(0, math.ceil((sent + timedelta(seconds=60) - now).total_seconds())) if sent else 0
        if sends >= 5 or attempts >= 5:
            return None, "Too many verification requests or attempts. Try again after the one-hour limit resets."
        if wait:
            return None, f"Please wait {wait} seconds before requesting another code."
        code = f"{secrets.randbelow(1_000_000):06d}"
        conn.execute(
            "INSERT INTO admin_otp_challenges "
            "(user_id,email_otp_hash,mode,expires_at,sent_at,attempts,window_started_at,sends_in_window,used_at) "
            "VALUES (?,?,'email_only',?,?,?,?,?,NULL) ON CONFLICT(user_id) DO UPDATE SET "
            "email_otp_hash=excluded.email_otp_hash,mode=excluded.mode,expires_at=excluded.expires_at,"
            "sent_at=excluded.sent_at,attempts=excluded.attempts,window_started_at=excluded.window_started_at,"
            "sends_in_window=excluded.sends_in_window,used_at=NULL",
            (user["id"], otp_digest(user, nonce, code), (now + timedelta(minutes=5)).isoformat(),
             now.isoformat(), attempts, (now if fresh else window).isoformat(), sends + 1),
        )
        return code, None

    def deliver(user, nonce, code):
        try:
            email_service.send_email(
                user["email"], "Your BarangayConnect admin verification code",
                f"Your admin login code is {code}.\n\nThis code expires in 5 minutes and can only be used once."
                "\n\nIf you did not request this code, secure your account password.",
            )
        except (email_service.EmailConfigurationError, email_service.smtplib.SMTPException, OSError, ValueError):
            app.logger.error("admin_mfa_email event=send_failed")
            conn = dbh.get_db()
            try:
                conn.execute(
                    "UPDATE admin_otp_challenges SET email_otp_hash=NULL,used_at=? "
                    "WHERE user_id=? AND email_otp_hash=?",
                    (_utc_now().isoformat(), user["id"], otp_digest(user, nonce, code)),
                )
                conn.commit()
            finally:
                conn.close()
            flash("The verification email could not be sent. Try resending after 60 seconds.", "error")
            return
        flash("A verification code was sent to your registered email address.", "info")

    def start(user):
        session.clear()
        nonce = secrets.token_urlsafe(32)
        conn = dbh.get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            user = conn.execute(
                "SELECT * FROM users WHERE id=? AND role='admin' AND status='active' AND password_hash=?",
                (user["id"], user["password_hash"]),
            ).fetchone()
            if not user:
                return redirect(url_for("login"))
            code, error = issue(conn, user, nonce)
            conn.commit()
        finally:
            conn.close()
        if error:
            flash(error, "error")
            return render_template("login.html"), 429
        session["pending_mfa_user"] = {
            "id": user["id"], "nonce": nonce, "started_at": _utc_now().isoformat(),
            "credential_proof": digest(f"admin-credentials:{user['id']}:{user['password_hash']}:{user['email']}"),
        }
        session["mfa_csrf"] = secrets.token_urlsafe(32)
        deliver(user, nonce, code)
        return redirect(url_for("verify_otp"))

    def require_csrf():
        expected = session.get("mfa_csrf", "")
        if not expected or not hmac.compare_digest(expected.encode(), request.form.get("csrf_token", "").encode()):
            abort(400)

    @app.route("/verify-otp", methods=["GET", "POST"])
    def verify_otp():
        conn = dbh.get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            user = pending_admin(conn)
            if not user:
                return redirect(url_for("login"))
            row = conn.execute("SELECT * FROM admin_otp_challenges WHERE user_id=?", (user["id"],)).fetchone()
            status = 200
            if request.method == "POST":
                require_csrf()
                code = request.form.get("otp", "").strip()
                now = _utc_now()
                usable = bool(row and row["email_otp_hash"] and not row["used_at"]
                              and row["attempts"] < 5 and timestamp(row["expires_at"])
                              and now < timestamp(row["expires_at"]))
                valid = (usable and re.fullmatch(r"[0-9]{6}", code) and hmac.compare_digest(
                    otp_digest(user, session["pending_mfa_user"]["nonce"], code), row["email_otp_hash"]))
                if valid:
                    conn.execute(
                        "UPDATE admin_otp_challenges SET email_otp_hash=NULL,used_at=? WHERE user_id=?",
                        (now.isoformat(), user["id"]),
                    )
                    dbh.log_action(conn, user, "Admin email MFA verified")
                    conn.commit()
                    session.clear()
                    if user["must_reset_pw"]:
                        session["pending_reset_user"] = user["id"]
                        session["admin_reset_mfa"] = {
                            "proof": digest(f"admin-reset:{user['id']}:{user['password_hash']}"),
                            "verified_at": now.isoformat(),
                        }
                        return redirect(url_for("first_login_reset"))
                    session["user_id"] = user["id"]
                    session["role"] = user["role"]
                    session["admin_mfa_verified_at"] = now.isoformat()
                    session["admin_mfa_proof"] = digest(
                        f"admin-credentials:{user['id']}:{user['password_hash']}:{user['email']}"
                    )
                    return redirect(url_for("admin_dashboard"))
                if usable:
                    conn.execute("UPDATE admin_otp_challenges SET attempts=attempts+1 WHERE user_id=?", (user["id"],))
                conn.commit()
                flash("The verification code is invalid, expired, used, or has reached the five-attempt limit.", "error")
                status = 400
            sent = timestamp(row["sent_at"]) if row else None
            wait = max(0, math.ceil((sent + timedelta(seconds=60) - _utc_now()).total_seconds())) if sent else 0
            return render_template("verify_otp.html", resend_wait=wait, csrf_token=session["mfa_csrf"]), status
        finally:
            conn.close()

    @app.route("/resend-otp", methods=["POST"])
    def resend_otp():
        require_csrf()
        conn = dbh.get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            user = pending_admin(conn)
            if not user:
                return redirect(url_for("login"))
            nonce = session["pending_mfa_user"]["nonce"]
            code, error = issue(conn, user, nonce)
            conn.commit()
        finally:
            conn.close()
        if error:
            flash(error, "error")
            return render_template("verify_otp.html", resend_wait=60, csrf_token=session["mfa_csrf"]), 429
        deliver(user, nonce, code)
        return redirect(url_for("verify_otp"))

    def reset_mfa_valid(user):
        if user["role"] != "admin":
            return True
        proof = session.get("admin_reset_mfa", {})
        verified = timestamp(proof.get("verified_at"))
        return bool(verified and _utc_now() < verified + timedelta(minutes=15)
                    and hmac.compare_digest(proof.get("proof", ""),
                        digest(f"admin-reset:{user['id']}:{user['password_hash']}")))

    return start, reset_mfa_valid
