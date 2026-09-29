"""Guardrails WAJIB — tidak bisa dimatikan oleh kampanye mana pun."""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import Blacklist, Lead, Metric, utcnow

COOLDOWN_HOURS_DEFAULT = 48

# Domain uji/dummy — kirim ke sini hanya menghasilkan bounce & merusak reputasi pengirim
DOMAIN_UJI = ("example.com", "example.org", "example.net", "test", "invalid", "localhost")


def _domain_uji(alamat: str) -> bool:
    dom = (alamat or "").rsplit("@", 1)[-1].lower()
    return dom in DOMAIN_UJI or dom.endswith(".invalid")


def kill_switch_active(settings) -> bool:
    return settings.kill_switch_file.exists()


def is_blacklisted(session: Session, channel: str, address: str) -> bool:
    if not address:
        return False
    return session.scalar(
        select(func.count(Blacklist.id)).where(
            Blacklist.address == address.lower(),
            Blacklist.channel.in_((channel, "all")))) > 0


def _daily_cap(settings, session: Session, channel: str) -> int:
    """Kapasitas hari ini; naik bertahap (warm-up) sampai batas penuh."""
    full = settings.get("budgets", "daily_email_sends", default=100) if channel == "email" \
        else settings.get("budgets", "daily_wa_sends", default=50)
    if not settings.get("warmup", "enabled", default=True):
        return full
    first = session.scalar(select(func.min(Metric.date)))
    if not first:
        return settings.get("warmup", "day1_cap", default=10)
    days = (datetime.strptime(utcnow().strftime("%Y-%m-%d"), "%Y-%m-%d")
            - datetime.strptime(first, "%Y-%m-%d")).days
    growth = settings.get("warmup", "growth_per_day", default=1.5)
    cap = int(settings.get("warmup", "day1_cap", default=10) * (growth ** days))
    return min(cap, full)


def in_quiet_hours(settings) -> bool:
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz).time()
    start = dtime.fromisoformat(settings.get("send_policy", "quiet_hours_start", default="21:00"))
    end = dtime.fromisoformat(settings.get("send_policy", "quiet_hours_end", default="08:00"))
    return now >= start or now < end if start > end else start <= now < end


def check_send(settings, session: Session, lead: Lead, channel: str) -> tuple[bool, str]:
    """Gerbang tunggal semua pesan keluar. Gagal -> (False, alasan)."""
    if kill_switch_active(settings):
        return False, "KILL switch aktif"
    if settings.budget_halt_file.exists():
        return False, "pagu harian terlampaui (BUDGET_HALT)"
    address = (lead.email if channel == "email" else lead.phone) or ""
    if not address:
        return False, f"lead tanpa {channel}"
    if channel == "email" and _domain_uji(address):
        return False, "domain uji (dummy) — diblokir agar tidak bounce"
    if is_blacklisted(session, channel, address):
        return False, "blacklist"
    if lead.unsubscribed_at:
        return False, "unsubscribed"
    if channel == "whatsapp" and not lead.opt_in_wa:
        return False, "tanpa opt-in WhatsApp"
    if in_quiet_hours(settings):
        return False, "quiet hours"
    today = utcnow().strftime("%Y-%m-%d")
    metric = session.scalar(select(Metric).where(Metric.date == today))
    sent = (metric.sends_email if channel == "email" else metric.sends_wa) if metric else 0
    if sent >= _daily_cap(settings, session, channel):
        return False, "batas harian tercapai"
    cooldown = settings.get("send_policy", "per_lead_cooldown_hours", default=COOLDOWN_HOURS_DEFAULT)
    if lead.last_contacted_at and utcnow() - lead.last_contacted_at < timedelta(hours=cooldown):
        return False, f"cooldown {cooldown} jam belum lewat"
    return True, "ok"


def record_send(session: Session, channel: str) -> None:
    today = utcnow().strftime("%Y-%m-%d")
    m = session.scalar(select(Metric).where(Metric.date == today))
    if m is None:
        m = Metric(date=today)
        session.add(m)
        session.flush()
    if channel == "email":
        m.sends_email += 1
    else:
        m.sends_wa += 1


def unsubscribe(session: Session, channel: str, address: str, reason: str = "") -> None:
    """Masuk blacklist + tandai semua lead terkait. Irreversible lewat agen."""
    address = (address or "").lower().strip()
    if not address:
        return
    if not is_blacklisted(session, channel, address):
        session.add(Blacklist(channel=channel, address=address, reason=reason[:200]))
    leads = session.scalars(select(Lead).where(
        (Lead.email == address) | (Lead.phone == address))).all()
    for lead in leads:
        lead.unsubscribed_at = utcnow()
    from .db import SequenceStep
    lead_ids = [l.id for l in leads]
    if lead_ids:
        session.query(SequenceStep).filter(
            SequenceStep.lead_id.in_(lead_ids), SequenceStep.status == "pending"
        ).update({"status": "skipped"}, synchronize_session=False)
