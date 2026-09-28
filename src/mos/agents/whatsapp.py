"""Agen 7 — WHATSAPP: Meta Cloud API resmi + webhook masuk."""
from __future__ import annotations

import json

import httpx
from sqlalchemy import select

from .. import audit, guardrails
from ..db import Campaign, Lead, Message, utcnow
from .base import BaseAgent
from .emailer import TRANSIENT

GRAPH = "https://graph.facebook.com"


class WhatsAppAgent(BaseAgent):
    name = "whatsapp"
    handles = ("send_wa",)

    def on_send_wa(self, ctx, session, task):
        p = task.payload_dict()
        lead = session.get(Lead, p["lead_id"])
        ok, alasan = guardrails.check_send(ctx.settings, session, lead, "whatsapp")
        if not ok:
            if seq_id := p.get("seq_id"):
                from ..db import SequenceStep
                step = session.get(SequenceStep, seq_id)
                if step:
                    step.status = "pending" if any(t in alasan for t in TRANSIENT) else "skipped"
            audit.log(session, self.name, "wa_blocked", "lead", lead.id, {"alasan": alasan})
            return

        nama_template = p.get("template_name", "mos_sapaan")
        isi = (p.get("body") or "").replace("{{1}}", lead.name or "")
        status = kirim_template(ctx.settings, lead.phone, nama_template, lead.name or "")
        if status == "mock":
            out = ctx.settings.data_dir / "outbox" / "whatsapp" / \
                f"{utcnow().strftime('%Y%m%d-%H%M%S')}-{lead.id}.json"
            out.write_text(json.dumps({"to": lead.phone, "template": nama_template,
                                       "isi": isi}, ensure_ascii=False, indent=2))

        session.add(Message(lead_id=lead.id, campaign_id=lead.campaign_id, channel="whatsapp",
                            direction="out", template_name=nama_template, body=isi,
                            status=status, sent_at=utcnow()))
        guardrails.record_send(session, "wa")
        lead.last_contacted_at = utcnow()
        if lead.stage == "new":
            lead.stage = "contacted"
        if seq_id := p.get("seq_id"):
            from ..db import SequenceStep
            step = session.get(SequenceStep, seq_id)
            if step:
                step.status = "done"
        audit.log(session, self.name, f"wa_{status}", "lead", lead.id)


def kirim_template(settings, phone: str, template: str, nama: str) -> str:
    """Kirim template resmi. Return 'sent' | 'mock'."""
    if not settings.wa_configured:
        return "mock"
    url = f"{GRAPH}/{settings.env('WA_API_VERSION', 'v21.0')}/{settings.env('WA_PHONE_NUMBER_ID')}/messages"
    resp = httpx.post(url, timeout=30,
                      headers={"Authorization": f"Bearer {settings.env('WA_ACCESS_TOKEN')}"},
                      json={"messaging_product": "whatsapp", "to": phone, "type": "template",
                            "template": {"name": template, "language": {"code": "id"},
                                         "components": [{"type": "body", "parameters":
                                                         [{"type": "text", "text": nama}]}]}})
    resp.raise_for_status()
    return "sent"


def proses_webhook(ctx, engine, body: dict) -> list[str]:
    """Olah payload webhook Meta. Dipanggil dari web.py. Return daftar aksi."""
    aksi = []
    from ..db import session_scope
    entri = (body.get("entry") or [])
    with session_scope(engine) as session:
        for e in entri:
            for ch in e.get("changes", []):
                val = ch.get("value", {})
                for msg in val.get("messages", []):
                    aksi.append(_proses_pesan(ctx, session, msg))
                for st in val.get("statuses", []):
                    if st.get("status") == "failed":
                        audit.log(session, "whatsapp", "delivery_failed", "wa", "",
                                  {"detail": str(st)[:500]})
        ctx.llm.drain(session)
    return aksi


def _proses_pesan(ctx, session, msg: dict) -> str:
    phone = msg.get("from", "")
    teks = (msg.get("text") or {}).get("body", "")
    lead = session.scalar(select(Lead).where(Lead.phone == phone).order_by(Lead.id.desc()))
    if not lead:
        lead = session.scalar(select(Lead).where(Lead.phone.like(f"%{phone[-9:]}")))
    if not lead:
        return "nomor tidak dikenal"
    label = ctx.llm.classify(agent="whatsapp", text=teks)
    session.add(Message(lead_id=lead.id, campaign_id=lead.campaign_id, channel="whatsapp",
                        direction="in", body=teks[:2000], status=label))
    audit.log(session, "whatsapp", f"wa_reply_{label}", "lead", lead.id)
    if label == "unsubscribe":
        guardrails.unsubscribe(session, "all", lead.phone, "balas STOP via WA")
        if lead.email:
            guardrails.unsubscribe(session, "all", lead.email, "STOP via WA")
    elif label == "interested":
        lead.stage = "replied"
        from ..notify import alert_owner
        alert_owner(ctx.settings, f"Lead TERTARIK (WA): {lead.name or lead.phone}", teks[:500])
    elif label == "question":
        lead.stage = "replied"
    else:
        lead.stage = "lost"
    return label
