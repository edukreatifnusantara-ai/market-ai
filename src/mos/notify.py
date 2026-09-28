"""Notifikasi eskalasi ke pemilik (email; fallback file log)."""
from __future__ import annotations

import smtplib
from email.message import EmailMessage

from .db import utcnow


def alert_owner(settings, subject: str, body: str) -> None:
    """Kirim eskalasi ke OWNER_EMAIL; tanpa SMTP -> data/owner_notifications.log."""
    owner = settings.env("OWNER_EMAIL")
    if settings.smtp_configured and owner:
        try:
            msg = EmailMessage()
            msg["From"] = settings.env("EMAIL_FROM") or settings.env("SMTP_USER")
            msg["To"] = owner
            msg["Subject"] = f"[MarketingOS] {subject}"
            msg.set_content(body)
            with smtplib.SMTP(settings.env("SMTP_HOST"), int(settings.env("SMTP_PORT", "587"))) as s:
                s.starttls()
                s.login(settings.env("SMTP_USER"), settings.env("SMTP_PASS"))
                s.send_message(msg)
            return
        except Exception:
            pass  # jatuh ke file log di bawah
    with (settings.data_dir / "owner_notifications.log").open("a") as f:
        f.write(f"{utcnow().isoformat()} | {subject}\n{body}\n{'-'*60}\n")
