"""Agen 1 — RESEARCHER: riset pasar nyata via web search (atau mock)."""
from __future__ import annotations

from ..db import Campaign
from .base import BaseAgent

SYSTEM = """Kamu analis riset pasar senior berbahasa Indonesia. Tulis laporan riset
yang TAJAM, berbasis data web nyata (tren, kompetitor, keluhan audiens, benchmark
harga). Format markdown: ringkasan eksekutif, tren, tabel kompetitor, 5 pain
points, rekomendasi positioning. Jujur bila data terbatas — jangan mengarang
angka; sebut sumbernya."""


class Researcher(BaseAgent):
    name = "researcher"
    handles = ("research",)

    def on_research(self, ctx, session, task):
        campaign = session.get(Campaign, task.campaign_id)
        brief = ctx.campaign_brief(campaign)
        pertanyaan = "\n".join(f"- {q}" for q in brief.get("research_questions", []))
        laporan = ctx.llm.generate(
            agent=self.name, purpose="research_report", use_web_search=True,
            max_tokens=6000, system=SYSTEM,
            prompt=f"Riset pasar untuk produk berikut.\n\nProduk: {brief.get('name')}\n"
                   f"Deskripsi: {brief.get('description')}\nTarget awal: {brief.get('target_hint')}\n"
                   f"Harga rencana: Rp {brief.get('price_hint_idr')}\n\n"
                   f"Wajib jawab pertanyaan brief ini dengan data web:\n{pertanyaan}",
            context=ctx.llm_context(campaign, self.name))
        ctx.save_artifact(session, campaign, "research", "research.md", laporan, self.name)
        campaign.status = "research_done"
        self.emit(session, "strategy", campaign.id, priority=60)
