"""Agen 4 — ICP/MARKET: definisi segmen pasar + scoring leads."""
from __future__ import annotations

import json

from sqlalchemy import select

from ..db import Campaign, Lead
from .base import BaseAgent

SYSTEM = """Kamu ahli segmentasi pasar. Dari riset & strategi, tulis definisi ICP:
segmen prioritas, rubrik skoring lead (sederhana, bisa dihitung), dan tahapan
pipeline. Format markdown ringkas."""


def skor_lead(lead: Lead) -> int:
    skor = 0
    if lead.email and lead.phone:
        skor += 20
    if lead.opt_in_wa:
        skor += 15
    if "komunitas" in (lead.source or "").lower():
        skor += 10
    return skor


class ICPAgent(BaseAgent):
    name = "icp"
    handles = ("define_icp",)

    def on_define_icp(self, ctx, session, task):
        campaign = session.get(Campaign, task.campaign_id)
        icp = ctx.llm.generate(
            agent=self.name, purpose="icp", max_tokens=3000, system=SYSTEM,
            prompt=f"Produk: {campaign.name}\nRiset:\n{ctx.read_artifact(session, campaign.id, 'research')[:4000]}\n"
                   f"Strategi:\n{ctx.read_artifact(session, campaign.id, 'strategy')[:4000]}",
            context=ctx.llm_context(campaign, self.name))
        ctx.save_artifact(session, campaign, "icp", "icp.md", icp, self.name)

        for lead in session.scalars(select(Lead).where(Lead.campaign_id == campaign.id)):
            lead.score = skor_lead(lead)
            tags = json.loads(lead.tags or "[]")
            if lead.score >= 40 and "prioritas" not in tags:
                tags.append("prioritas")
            lead.tags = json.dumps(tags, ensure_ascii=False)
        self.emit(session, "create_content", campaign.id, priority=60)
