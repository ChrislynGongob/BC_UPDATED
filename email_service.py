import logging
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import getaddresses

import requests


class EmailConfigurationError(RuntimeError):
    pass


class EmailDeliveryError(OSError):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


_BREVO_URL = "https://api.brevo.com/v3/smtp/email"
_BREVO_TIMEOUT = (5, 15)
_AUTOMATED_FOOTER = "This is an automated message. Please do not reply to this email."
_logger = logging.getLogger(__name__)


def _configured_email_address(value, setting_name):
    addresses = getaddresses([value])
    address = addresses[0][1] if len(addresses) == 1 else ""
    if (
        not address
        or "\r" in value
        or "\n" in value
        or not re.fullmatch(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+", address)
    ):
        raise EmailConfigurationError(f"{setting_name} must contain a valid email address.")
    return address


def _send_brevo_email(recipient, subject, body, html_body=None):
    api_key = os.environ.get("BREVO_API_KEY", "").strip()
    if not api_key:
        raise EmailConfigurationError("BREVO_API_KEY must be configured for Brevo.")

    sender_config = os.environ.get("SMTP_FROM", "").strip()
    sender_email = _configured_email_address(sender_config, "SMTP_FROM")

    payload = {
        "sender": {"name": "BarangayConnect", "email": sender_email},
        "to": [{"email": recipient}],
        "subject": subject,
        "textContent": f"{body.rstrip()}\n\n{_AUTOMATED_FOOTER}",
    }
    reply_to = os.environ.get("BREVO_REPLY_TO", "").strip()
    if reply_to:
        payload["replyTo"] = {
            "email": _configured_email_address(reply_to, "BREVO_REPLY_TO")
        }
    if html_body is not None:
        payload["htmlContent"] = (
            f"{html_body.rstrip()}"
            f'<p>{_AUTOMATED_FOOTER}</p>'
        )

    try:
        response = requests.post(
            _BREVO_URL,
            json=payload,
            headers={"api-key": api_key, "accept": "application/json"},
            timeout=_BREVO_TIMEOUT,
        )
    except requests.exceptions.RequestException as exc:
        _logger.error(
            "Brevo API request failed before an acceptance response (type=%s).",
            type(exc).__name__,
        )
        raise EmailDeliveryError("Brevo API request failed.") from exc

    if not 200 <= response.status_code < 300:
        _logger.error(
            "Brevo API rejected transactional email (status=%s); provider did not accept the request.",
            response.status_code,
        )
        raise EmailDeliveryError(
            "Brevo API rejected the email request.",
            status_code=response.status_code,
        )

    _logger.info(
        "Brevo accepted transactional email request (status=%s); inbox delivery is not confirmed.",
        response.status_code,
    )


def send_email(recipient, subject, body, html_body=None):
    provider = os.environ.get("EMAIL_PROVIDER", "").strip().lower() or "smtp"
    if provider == "brevo":
        return _send_brevo_email(recipient, subject, body, html_body)
    if provider != "smtp":
        raise EmailConfigurationError("EMAIL_PROVIDER must be either 'brevo' or 'smtp'.")

    host = os.environ.get("SMTP_HOST", "").strip()
    sender = os.environ.get("SMTP_FROM", "").strip()
    if not host or not sender:
        raise EmailConfigurationError("SMTP_HOST and SMTP_FROM must be configured.")

    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
        timeout = float(os.environ.get("SMTP_TIMEOUT_SECONDS", "15"))
    except ValueError as exc:
        raise EmailConfigurationError("SMTP_PORT and SMTP_TIMEOUT_SECONDS must be numeric.") from exc
    if not 1 <= port <= 65535 or timeout <= 0:
        raise EmailConfigurationError("SMTP_PORT must be valid and SMTP_TIMEOUT_SECONDS must be positive.")

    use_ssl = os.environ.get("SMTP_USE_SSL", "false").lower() in {"1", "true", "yes"}
    use_tls = os.environ.get("SMTP_USE_TLS", "true").lower() in {"1", "true", "yes"}
    if use_ssl and use_tls:
        raise EmailConfigurationError("Enable only one of SMTP_USE_SSL or SMTP_USE_TLS.")
    username = os.environ.get("SMTP_USERNAME", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    if bool(username) != bool(password.strip()):
        raise EmailConfigurationError("SMTP_USERNAME and SMTP_PASSWORD must both be configured.")

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(f"{body.rstrip()}\n\n{_AUTOMATED_FOOTER}")

    smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    context = ssl.create_default_context()
    with smtp_class(host, port, timeout=timeout, context=context) if use_ssl else smtp_class(
        host, port, timeout=timeout
    ) as smtp:
        if use_tls:
            smtp.starttls(context=context)
        if username:
            smtp.login(username, password)
        smtp.send_message(message)
