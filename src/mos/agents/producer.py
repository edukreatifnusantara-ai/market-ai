"""Agen 3 — PRODUCER: memproduksi produk digital nyata dari spesifikasi riset."""
from __future__ import annotations

import json

from ..db import Campaign
from .base import BaseAgent

SYSTEM_CH = """Kamu penulis buku praktis untuk UMKM Indonesia. Tulis satu bab LENGKAP
(minimal 400 kata): pembuka, penjelasan, minimal 1 contoh nyata, 1 template siap
salin, dan latihan 10 menit. Bahasa santai tapi profesional, tanpa jargon teknis."""

LANDING_HTML = """<!doctype html>
<html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{judul}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:720px;margin:2rem auto;padding:0 1rem;line-height:1.6}}
.cta{{display:inline-block;background:#16a34a;color:#fff;padding:.8rem 1.6rem;border-radius:8px;text-decoration:none;font-weight:700}}</style>
</head><body>
<h1>{judul}</h1>
<p><strong>{hero}</strong></p>
<p>{deskripsi}</p>
<h2>Yang Anda dapatkan</h2>
<ul>{poin}</ul>
<p><a class="cta" href="#pesan">Pesan Sekarang — Rp {harga}</a></p>
<p><small>Untuk berhenti menerima info, balas pesan kami dengan kata BERHENTI.</small></p>
</body></html>"""


class Producer(BaseAgent):
    name = "producer"
    handles = ("produce",)

    def on_produce(self, ctx, session, task):
        campaign = session.get(Campaign, task.campaign_id)
        brief = ctx.campaign_brief(campaign)
        spec = json.loads(ctx.read_artifact(session, campaign.id, "spec") or "{}")
        judul = spec.get("judul", brief.get("name", "Produk"))
        outline = spec.get("outline") or ["Pendahuluan", "Isi Utama", "Penutup"]

        bab_list = []
        for bab in outline:
            isi = ctx.llm.generate(
                agent=self.name, purpose="ebook_chapter", system=SYSTEM_CH,
                prompt=f"Tulis bab '{bab}' untuk ebook '{judul}'. "
                       f"Pembaca: {brief.get('target_hint', '')}. "
                       f"Konteks riset ada di strategi: {ctx.read_artifact(session, campaign.id, 'strategy')[:2000]}",
                context={**ctx.llm_context(campaign, self.name), "chapter": bab})
            bab_list.append(isi if isi.startswith("##") else f"## {bab}\n\n{isi}")

        ebook_md = f"# {judul}\n\n*Produk digital — diproduksi otomatis dari riset pasar.*\n\n" \
                   + "\n\n".join(bab_list) + f"\n\n---\n{spec.get('cta', '')}\n"
        ctx.save_artifact(session, campaign, "product", "product/ebook.md", ebook_md, self.name)

        # Render PDF (latin-1 aman untuk font inti fpdf2)
        from fpdf import FPDF
        from fpdf.enums import XPos, YPos
        pdf = FPDF()
        pdf.set_auto_page_break(True, 15)
        pdf.add_page()
        pdf.set_font("helvetica", "B", 22)
        pdf.multi_cell(0, 12, judul.encode("latin-1", "replace").decode("latin-1"),
                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("helvetica", "", 11)
        for baris in ebook_md.splitlines():
            t = baris.strip().lstrip("#").strip()
            if not t:
                pdf.ln(3)
                continue
            if baris.startswith("#"):
                pdf.set_font("helvetica", "B", 14)
                pdf.ln(4)
                pdf.multi_cell(0, 7, t.encode("latin-1", "replace").decode("latin-1"),
                               new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.set_font("helvetica", "", 11)
            else:
                pdf.multi_cell(0, 6, t.encode("latin-1", "replace").decode("latin-1"),
                               new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf_path = ctx.settings.data_dir / "artifacts" / campaign.slug / "product" / "ebook.pdf"
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf.output(str(pdf_path))
        from ..db import Artifact
        session.add(Artifact(campaign_id=campaign.id, kind="product", path=str(pdf_path),
                             version=2, created_by=self.name))

        # Landing page statis
        poin = "".join(f"<li>{b.lstrip('0123456789. ')}</li>" for b in outline[:6])
        html = LANDING_HTML.format(
            judul=judul, hero=f"Panduan praktis untuk {brief.get('target_hint', 'Anda')}.",
            deskripsi=brief.get("description", "").strip(), poin=poin,
            harga=brief.get("price_hint_idr", ""))
        ctx.save_artifact(session, campaign, "product", "product/landing.html", html, self.name)
        self.emit(session, "define_icp", campaign.id, priority=60)
