"""Agen 2 — STRATEGIST: riset -> positioning, harga, spesifikasi produk."""
from __future__ import annotations

import json

from ..db import Campaign
from .base import BaseAgent

SYSTEM = """Kamu konsultan strategi produk & pemasaran. Dari laporan riset, tulis:
positioning (1 kalimat kuat), 3 persona prioritas, rekomendasi harga + struktur
penawaran, angle kampanye. Lalu, bila produk digital, hasilkan JUGA spesifikasi
produk sebagai JSON {"judul", "format", "outline": [...8 bab...], "cta"}.
Pisahkan bagian strategi dan JSON spesifikasi dengan baris '===SPEC==='."""


class Strategist(BaseAgent):
    name = "strategist"
    handles = ("strategy",)

    def on_strategy(self, ctx, session, task):
        campaign = session.get(Campaign, task.campaign_id)
        brief = ctx.campaign_brief(campaign)
        research = ctx.read_artifact(session, campaign.id, "research")
        hasil = ctx.llm.generate(
            agent=self.name, purpose="strategy", max_tokens=6000, system=SYSTEM,
            prompt=f"Produk: {brief.get('name')} ({brief.get('type')})\n"
                   f"Deskripsi: {brief.get('description')}\nHarga rencana: Rp {brief.get('price_hint_idr')}\n\n"
                   f"Laporan riset:\n{research[:8000]}",
            context=ctx.llm_context(campaign, self.name))
        if "===SPEC===" in hasil:
            strategi, spec = hasil.split("===SPEC===", 1)
        elif brief.get("type") == "digital":
            strategi = hasil
            spec = ctx.llm._mock("product_spec", ctx.llm_context(campaign, self.name)) \
                if ctx.llm.mock else '{"judul": "draft", "format": "ebook", "outline": [], "cta": ""}'
        else:
            strategi, spec = hasil, ""
        ctx.save_artifact(session, campaign, "strategy", "strategy.md", strategi.strip(), self.name)
        campaign.status = "production"
        if brief.get("type") == "digital":
            try:
                json.loads(spec)
            except (ValueError, TypeError):
                spec = '{"judul": "revisi", "format": "ebook PDF", "outline": [], "cta": ""}'
            ctx.save_artifact(session, campaign, "spec", "spec.json", spec, self.name)
            self.emit(session, "produce", campaign.id, priority=60)
        else:
            self.emit(session, "define_icp", campaign.id, priority=60)
