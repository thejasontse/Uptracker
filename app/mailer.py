"""SMTP email alerts.

SMTP settings live in the ``settings`` table. If no SMTP host is configured,
emails are logged instead of sent (so the alert path is still observable in
development). Rendering uses the same ``{{placeholder}}`` substitution as
dataset templates (see templates.py) but is applied to subject/body strings.
"""
from __future__ import annotations

import smtplib
import sqlite3
from email.message import EmailMessage

from . import db
from .templates import render as _render_placeholders


def render(template: str, ctx: dict) -> str:
    """Render ``{{a.b}}`` placeholders from a nested *ctx* dict."""
    return _render_placeholders(template, ctx)


def send_email(conn: sqlite3.Connection, recipients: str, subject: str, body: str) -> bool:
    """Send an email using SMTP settings. Returns True if sent (or logged)."""
    host = db.get_setting(conn, "smtp_host", "")
    if not recipients or not recipients.strip():
        return False
    if not host:
        # Development fallback: log to stdout.
        print(f"[mailer] (no SMTP configured) to={recipients} subject={subject!r} body={body!r}")
        return True

    port = int(db.get_setting(conn, "smtp_port", "587") or 587)
    user = db.get_setting(conn, "smtp_user", "")
    password = db.get_setting(conn, "smtp_password", "")
    use_tls = db.get_setting(conn, "smtp_starttls", "1") == "1"
    mail_from = db.get_setting(conn, "mail_from", "uptracker@localhost")

    msg = EmailMessage()
    msg["From"] = mail_from
    msg["To"] = recipients
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            if use_tls:
                smtp.starttls()
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
        return True
    except Exception as exc:  # pragma: no cover - network dependent
        print(f"[mailer] send failed: {exc}")
        return False
