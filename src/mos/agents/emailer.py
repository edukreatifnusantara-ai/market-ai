"""Agen 6 — EMAIL: kirim via SMTP, baca balasan via IMAP, klasifikasi, balas."""
from __future__ import annotations

import imaplib
import shutil
import smtplib
import uuid
from email.message import EmailMessage
from email.parser import BytesParser
from email import policy

from sqlalchemy import select

from .. import audit, guardrails
from ..db import Campaign, Lead, Message, SequenceStep, utcnow
from .base import BaseAgent

TRANSIENT = ("quiet hours", "batas harian", "cooldown", "pagu harian")


class EmailAgent(BaseAgent):
    name = "emailer"
    handles = ("send_email", "poll_replies", "draft_reply")

    # ------------------------------------------------ kirim
    def on_send_email(self, ctx, session, task):
        p = task.payload_dict()
        lead = session.get(Lead, p["lead_id"])
        campaign = session.get(Campaign, task.campaign_id)
        ok, alasan = guardrails.check_send(ctx.settings, session, lead, "email")
        if not ok:
            self._tandai_step(session, p, "pending" if any(t in alasan for t in TRANSIENT) else "skipped")
            audit.log(session, self.name, "send_blocked", "lead", lead.id, {"alasan": alasan})
            return

        body = (p.get("body") or "").replace("{nama}", lead.name or "Bapak/Ibu")
        msg = EmailMessage()
        msg["From"] = ctx.settings.env("EMAIL_FROM") or ctx.settings.env("SMTP_USER") or "mos@localhost"
        msg["To"] = lead.email
        msg["Subject"] = p.get("subject", campaign.name)
        msg["Message-ID"] = f"<{uuid.uuid4().hex}@marketing-os>"
        reply_email = ctx.campaign_brief(campaign).get("reply_email") or msg["From"]
        msg["List-Unsubscribe"] = f"<mailto:{reply_email}?subject=BERHENTI>"
        msg.set_content(body)

        status = "mock"
        if ctx.settings.smtp_configured:
            with smtplib.SMTP(ctx.settings.env("SMTP_HOST"),
                              int(ctx.settings.env("SMTP_PORT", "587"))) as s:
                s.starttls()
                s.login(ctx.settings.env("SMTP_USER"), ctx.settings.env("SMTP_PASS"))
                s.send_message(msg)
            status = "sent"
        else:
            outbox = ctx.settings.data_dir / "outbox" / "email"
            fname = outbox / f"{utcnow().strftime('%Y%m%d-%H%M%S')}-{lead.id}.eml"
            fname.write_bytes(bytes(msg))

        session.add(Message(lead_id=lead.id, campaign_id=campaign.id, channel="email",
                            direction="out", subject=msg["Subject"], body=body,
                            status=status, sent_at=utcnow()))
        guardrails.record_send(session, "email")
        lead.last_contacted_at = utcnow()
        if lead.stage == "new":
            lead.stage = "contacted"
        self._tandai_step(session, p, "done")
        audit.log(session, self.name, f"email_{status}", "lead", lead.id,
                  {"subject": msg["Subject"]})

    def _tandai_step(self, session, payload: dict, status: str) -> None:
        if seq_id := payload.get("seq_id"):
            step = session.get(SequenceStep, seq_id)
            if step:
                step.status = status

    # ------------------------------------------------ baca balasan
    def on_poll_replies(self, ctx, session, task):
        if ctx.settings.imap_configured:
            self._poll_imap(ctx, session)
        else:
            self._poll_folder_simulasi(ctx, session)

    def _poll_imap(self, ctx, session):
        with imaplib.IMAP4_SSL(ctx.settings.env("IMAP_HOST"),
                               int(ctx.settings.env("IMAP_PORT", "993"))) as imap:
            imap.login(ctx.settings.env("IMAP_USER"), ctx.settings.env("IMAP_PASS"))
            imap.select("INBOX")
            _, data = imap.search(None, "UNSEEN")
            for num in (data[0] or b"").split():
                _, fetched = imap.fetch(num, "(RFC822)")
                msg = BytesParser(policy=policy.default).parsebytes(fetched[0][1])
                self._proses_balasan(ctx, session, msg.get("From", ""), msg.get("Subject", ""),
                                     _isi_teks(msg))
                imap.store(num, "+FLAGS", "\\Seen")

    def _poll_folder_simulasi(self, ctx, session):
        """Mode mock: .eml di data/inbox/ dianggap balasan masuk (simulasi lead)."""
        inbox = ctx.settings.data_dir / "inbox"
        for f in sorted(inbox.glob("*.eml")):
            msg = BytesParser(policy=policy.default).parsebytes(f.read_bytes())
            self._proses_balasan(ctx, session, msg.get("From", ""), msg.get("Subject", ""),
                                 _isi_teks(msg))
            (inbox / "processed").mkdir(exist_ok=True)
            shutil.move(str(f), inbox / "processed" / f.name)

    def _proses_balasan(self, ctx, session, dari: str, subjek: str, teks: str) -> None:
        alamat = _ekstrak_email(dari)
        lead = session.scalar(select(Lead).where(Lead.email == alamat).order_by(Lead.id.desc()))
        if not lead:
            return
        label = ctx.llm.classify(agent=self.name, text=f"{subjek}\n{teks}")
        session.add(Message(lead_id=lead.id, campaign_id=lead.campaign_id, channel="email",
                            direction="in", subject=subjek, body=teks[:4000], status=label))
        audit.log(session, self.name, f"reply_{label}", "lead", lead.id)
        if label == "unsubscribe":
            guardrails.unsubscribe(session, "email", alamat, "balas BERHENTI")
        elif label == "interested":
            lead.stage = "replied"
            from ..notify import alert_owner
            alert_owner(ctx.settings, f"Lead TERTARIK: {lead.name or alamat}",
                        f"{alamat} membalas dengan minat.\n\n{teks[:500]}")
            self.emit(session, "draft_reply", lead.campaign_id,
                      {"lead_id": lead.id, "original": teks[:2000]}, priority=80)
        elif label == "question":
            lead.stage = "replied"
            self.emit(session, "draft_reply", lead.campaign_id,
                      {"lead_id": lead.id, "original": teks[:2000]}, priority=70)
        else:
            lead.stage = "lost"

    def on_draft_reply(self, ctx, session, task):
        p = task.payload_dict()
        lead = session.get(Lead, p["lead_id"])
        campaign = session.get(Campaign, task.campaign_id)
        brief = ctx.campaign_brief(campaign)
        balasan = ctx.llm.generate(
            agent=self.name, purpose="reply_draft", max_tokens=800,
            system="Kamu CS ramah perusahaan. Balas pertanyaan prospek singkat, jujur, "
                   "sertakan tautan landing bila relevan. Bahasa Indonesia.",
            prompt=f"Produk: {brief.get('name')}\nLanding: {brief.get('landing_url')}\n"
                   f"Pesan prospek:\n{p.get('original', '')}",
            context=ctx.llm_context(campaign, self.name))
        self.emit(session, "send_email", campaign.id,
                  {"lead_id": lead.id, "subject": f"Re: {brief.get('name')}",
                   "body": balasan}, priority=80)


def _isi_teks(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                return part.get_content()
        return ""
    return msg.get_content() or ""


def _ekstrak_email(header: str) -> str:
    import re
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", header or "")
    return (m.group(0) if m else "").lower()
