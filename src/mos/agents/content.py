"""Agen 5 — CONTENT: copy email sequence, template WA, sosial, landing."""
from __future__ import annotations

import json
import re
from datetime import timedelta

from sqlalchemy import select

from ..db import Campaign, Lead, SequenceStep, utcnow
from .base import BaseAgent

SYSTEM = """Kamu copywriter senior Indonesia spesialis penjualan UMKM. Dari riset +
strategi, tulis copy pack sebagai JSON VALID dengan kunci:
- "emails": 4 objek {"step": 0..3, "subject", "body"} (body pakai placeholder
  {nama}; WAJIB akhiri tiap body dengan baris opt-out: '--\nBalas BERHENTI untuk berhenti menerima email.')
- "whatsapp": 2 objek {"step": 0..1, "name", "body"} (gaya template Meta: sopan,
  singkat <300 karakter, placeholder {{1}} untuk nama, tanpa klaim berlebihan)
- "social": 3 caption singkat
- "landing_hero": 1 kalimat hero
Jawab HANYA JSON."""

KATA_TERLARANG = ("dijamin kaya", "100% gratis tanpa syarat", "pasti untung", "klik di sini!!!")


def qc_copy(pack: dict) -> list[str]:
    """QC sederhana sebelum copy dipakai. Return daftar masalah."""
    masalah = []
    emails = pack.get("emails", [])
    if len(emails) < 2:
        masalah.append("email sequence kurang dari 2 langkah")
    for m in emails:
        body = (m.get("body") or "").lower()
        if "berhenti" not in body and "unsubscribe" not in body:
            masalah.append(f"email step {m.get('step')} tanpa kalimat opt-out")
        for kata in KATA_TERLARANG:
            if kata in body or kata in (m.get("subject") or "").lower():
                masalah.append(f"email step {m.get('step')} mengandung kata terlarang '{kata}'")
    for w in pack.get("whatsapp", []):
        if len(w.get("body", "")) > 320:
            masalah.append(f"template WA '{w.get('name')}' > 320 karakter")
    return masalah


class ContentAgent(BaseAgent):
    name = "content"
    handles = ("create_content",)

    def on_create_content(self, ctx, session, task):
        campaign = session.get(Campaign, task.campaign_id)
        settings = ctx.settings
        pack_str = ctx.llm.generate(
            agent=self.name, purpose="copy_pack", max_tokens=6000, system=SYSTEM,
            prompt=f"Produk: {campaign.name}\nTarget: "
                   f"{ctx.campaign_brief(campaign).get('target_hint', '')}\n"
                   f"Strategi:\n{ctx.read_artifact(session, campaign.id, 'strategy')[:5000]}",
            context=ctx.llm_context(campaign, self.name))
        try:
            pack = json.loads(re.sub(r"^```(json)?|```$", "", pack_str.strip(), flags=re.M))
        except ValueError as e:
            raise ValueError(f"copy pack bukan JSON valid: {e}")

        masalah = qc_copy(pack)
        if masalah:
            from .. import audit
            audit.log(session, self.name, "qc_warning", "campaign", campaign.id,
                      {"masalah": masalah})
            for m in pack.get("emails", []):  # tambal opt-out otomatis
                if "berhenti" not in m.get("body", "").lower():
                    m["body"] = m.get("body", "") + "\n\n--\nBalas BERHENTI untuk berhenti menerima email."

        ctx.save_artifact(session, campaign, "copy_pack", "copy_pack.json",
                          json.dumps(pack, ensure_ascii=False, indent=2), self.name)
        campaign.status = "active"
        self._buat_sekuens(session, ctx, campaign)
        self.emit(session, "followup_tick", campaign.id, payload={}, priority=40, dedup=True)

    def _buat_sekuens(self, session, ctx, campaign: Campaign) -> None:
        """Buat langkah follow-up untuk semua lead kampanye."""
        email_days = ctx.settings.get("sequences", "email_steps", default=[0, 2, 5, 9])
        wa_days = ctx.settings.get("sequences", "wa_steps", default=[1, 4])
        leads = session.scalars(select(Lead).where(Lead.campaign_id == campaign.id,
                                                   Lead.unsubscribed_at.is_(None))).all()
        for lead in leads:
            self._buat_sekuens_lead(session, campaign, lead, email_days, wa_days)

    @staticmethod
    def _buat_sekuens_lead(session, campaign, lead, email_days, wa_days) -> None:
        sudah = {s.step_no for s in session.scalars(select(SequenceStep).where(
            SequenceStep.lead_id == lead.id, SequenceStep.channel == "email"))}
        for i, hari in enumerate(email_days):
            if i not in sudah:
                session.add(SequenceStep(lead_id=lead.id, campaign_id=campaign.id,
                                         step_no=i, channel="email",
                                         due_at=lead.created_at + timedelta(days=hari)))
        if lead.opt_in_wa and lead.phone:
            sudah_wa = {s.step_no for s in session.scalars(select(SequenceStep).where(
                SequenceStep.lead_id == lead.id, SequenceStep.channel == "whatsapp"))}
            for i, hari in enumerate(wa_days):
                if i not in sudah_wa:
                    session.add(SequenceStep(lead_id=lead.id, campaign_id=campaign.id,
                                             step_no=i, channel="whatsapp",
                                             due_at=lead.created_at + timedelta(days=hari)))
