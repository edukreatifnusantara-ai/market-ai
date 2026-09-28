"""Agen 8 — FOLLOW-UP: menjadwalkan & mengirim tahap sekuens yang jatuh tempo."""
from __future__ import annotations

import json

from sqlalchemy import select

from .. import guardrails
from ..db import Campaign, Lead, SequenceStep, utcnow
from .base import BaseAgent


class FollowUpAgent(BaseAgent):
    name = "followup"
    handles = ("followup_tick",)

    def on_followup_tick(self, ctx, session, task):
        if guardrails.kill_switch_active(ctx.settings) or guardrails.in_quiet_hours(ctx.settings):
            return
        due = session.scalars(select(SequenceStep).where(
            SequenceStep.status == "pending", SequenceStep.due_at <= utcnow()
        ).order_by(SequenceStep.due_at).limit(50)).all()
        for step in due:
            lead = session.get(Lead, step.lead_id)
            campaign = session.get(Campaign, step.campaign_id)
            if not lead or not campaign or campaign.status != "active":
                step.status = "skipped"
                continue
            if lead.stage not in ("new", "contacted") or lead.unsubscribed_at:
                step.status = "skipped"
                continue
            pack = self._copy_pack(ctx, session, campaign.id)
            payload = self._payload(step, lead, pack)
            if not payload:
                step.status = "skipped"
                continue
            tipe = "send_email" if step.channel == "email" else "send_wa"
            self.emit(session, tipe, campaign.id, payload, priority=60)
            step.status = "dispatched"

    @staticmethod
    def _copy_pack(ctx, session, campaign_id: int) -> dict:
        mentah = ctx.read_artifact(session, campaign_id, "copy_pack")
        try:
            return json.loads(mentah) if mentah else {}
        except ValueError:
            return {}

    @staticmethod
    def _payload(step, lead, pack: dict) -> dict | None:
        base = {"lead_id": lead.id, "seq_id": step.id}
        if step.channel == "email":
            m = (pack.get("emails") or [])
            if step.step_no >= len(m):
                return None
            return {**base, "subject": m[step.step_no]["subject"],
                    "body": m[step.step_no]["body"]}
        w = (pack.get("whatsapp") or [])
        if step.step_no >= len(w):
            return None
        return {**base, "template_name": w[step.step_no]["name"],
                "body": w[step.step_no]["body"]}
