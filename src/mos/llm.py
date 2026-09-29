"""Wrapper LLM: Claude asli bila ada API key, mock deterministik bila tidak.
Semua panggilan tercatat di llm_calls (token, biaya) + dijaga pagu harian."""
from __future__ import annotations

import json
import time
from datetime import timedelta

from sqlalchemy import func, select

from .db import LLMCall, Metric, utcnow

# Harga per 1 juta token (input, output) dalam USD
HARGA_MODEL = {
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.4, 1.6),
    "gpt-4.1-nano": (0.1, 0.4),
    "gpt-5.6-luna": (2.5, 10.0),  # perkiraan — sesuaikan bila harga resmi diketahui
    "mock": (0.0, 0.0),
}

STOP_HINTS = ("stop", "berhenti", "unsubscribe", "jangan kirim", "hapus saya")
INTEREST_HINTS = ("tertarik", "minat", "mau beli", "interested", "bagaimana cara", "harga berapa")


class BudgetHalt(RuntimeError):
    """Pagu harian terlampaui — sistem jeda fail-safe sampai esok."""


class LLM:
    def __init__(self, settings, engine):
        self.settings = settings
        self.engine = engine
        self.mock = settings.force_mock
        self.provider = "mock"
        self._client = None
        self._buffer: list[dict] = []  # log menunggu drain ke sesi aktif (hindari nested writer SQLite)
        if not self.mock:
            if settings.anthropic_key:
                import anthropic
                self.provider = "anthropic"
                self._client = anthropic.Anthropic(api_key=settings.anthropic_key)
            elif settings.openai_key:
                import openai
                self.provider = "openai"
                self._client = openai.OpenAI(api_key=settings.openai_key)
            else:
                self.mock = True

    def _model_default(self, cheap: bool = False) -> str:
        if self.provider == "openai":
            key = "openai_cheap" if cheap else "openai_default"
            return self.settings.get("models", key) or "gpt-4o-mini"
        return self.settings.model_cheap if cheap else self.settings.model_default

    # ------------------------------------------------ internal
    def _check_budget(self) -> None:
        if self.settings.budget_halt_file.exists():
            raise BudgetHalt("BUDGET_HALT aktif — pagu LLM harian terlampaui")

    def _log(self, model: str, tin: int, tout: int, purpose: str, agent: str, ok: bool) -> None:
        harga = HARGA_MODEL.get(model, HARGA_MODEL["claude-sonnet-5"])
        self._buffer.append({
            "agent": agent, "model": model, "input_tokens": tin, "output_tokens": tout,
            "cost_usd": (tin * harga[0] + tout * harga[1]) / 1_000_000,
            "purpose": purpose, "ok": ok})

    def drain(self, session) -> None:
        """Tulis buffer log ke sesi yang sedang aktif. Dipanggil orkestratar/webhook
        setelah dispatch agar tidak membuka koneksi kedua (SQLite single-writer)."""
        if not self._buffer:
            return
        today = utcnow().strftime("%Y-%m-%d")
        for item in self._buffer:
            session.add(LLMCall(**item))
            m = session.scalar(select(Metric).where(Metric.date == today))
            if m is None:
                m = Metric(date=today)
                session.add(m)
                session.flush()
            m.llm_cost = (m.llm_cost or 0) + item["cost_usd"]
        self._buffer.clear()

    def cost_today(self) -> float:
        from .db import session_scope
        today = utcnow().strftime("%Y-%m-%d")
        with session_scope(self.engine) as s:
            return s.scalar(select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0))
                            .where(func.strftime("%Y-%m-%d", LLMCall.created_at) == today))

    # ------------------------------------------------ publik
    def generate(self, *, agent: str, purpose: str, system: str, prompt: str,
                 model: str | None = None, max_tokens: int = 4096,
                 use_web_search: bool = False, context: dict | None = None) -> str:
        self._check_budget()
        if self.mock:
            return self._mock(purpose, context or {})
        model = model or self._model_default()
        err = None
        for attempt in range(3):
            try:
                if self.provider == "openai":
                    text, tin, tout = self._call_openai(model, system, prompt, max_tokens, use_web_search)
                else:
                    text, tin, tout = self._call_anthropic(model, system, prompt, max_tokens, use_web_search)
                self._log(model, tin, tout, purpose, agent, True)
                return text
            except Exception as e:  # backoff: 2s, 4s, lalu lempar
                err = e
                time.sleep(2 * (attempt + 1))
        self._log(model, 0, 0, purpose, agent, False)
        raise RuntimeError(f"LLM {self.provider} gagal setelah 3x: {err}")

    def _call_anthropic(self, model, system, prompt, max_tokens, use_web_search):
        kwargs = dict(model=model, max_tokens=max_tokens, system=system,
                      messages=[{"role": "user", "content": prompt}])
        if use_web_search:
            kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}]
        resp = self._client.messages.create(**kwargs)
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        return text, resp.usage.input_tokens, resp.usage.output_tokens

    def _call_openai(self, model, system, prompt, max_tokens, use_web_search):
        if use_web_search:
            try:
                resp = self._client.responses.create(
                    model=model, instructions=system, input=prompt,
                    tools=[{"type": "web_search_preview"}],
                    max_output_tokens=max_tokens)
                return (resp.output_text or "").strip(), \
                    resp.usage.input_tokens, resp.usage.output_tokens
            except Exception:
                pass  # model/provider tak mendukung tool web_search -> lanjut tanpa browsing
        resp = self._client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": prompt}],
            max_tokens=max_tokens)
        return (resp.choices[0].message.content or "").strip(), \
            resp.usage.prompt_tokens, resp.usage.completion_tokens

    def classify(self, *, agent: str, text: str) -> str:
        """Klasifikasi balasan lead -> interested | question | not-interested | unsubscribe."""
        low = (text or "").lower()
        if any(k in low for k in STOP_HINTS):
            return "unsubscribe"
        if self.mock:
            if any(k in low for k in INTEREST_HINTS):
                return "interested"
            return "question" if "?" in low else "not-interested"
        jawaban = self.generate(
            agent=agent, purpose="classify", model=self._model_default(cheap=True), max_tokens=20,
            system="Klasifikasikan balasan prospek ke SATU label: interested, question, "
                   "not-interested, atau unsubscribe. Jawab HANYA labelnya.",
            prompt=text[:2000])
        label = jawaban.strip().lower().strip(".")
        return label if label in ("interested", "question", "not-interested", "unsubscribe") else "question"

    # ------------------------------------------------ konten mock
    def _mock(self, purpose: str, ctx: dict) -> str:
        nama = ctx.get("name", "Produk")
        target = ctx.get("target_hint", "segmen pasar")
        harga = ctx.get("price_hint_idr", "100000")
        self._log("mock", 100, 800, purpose, ctx.get("agent", "mock"), True)
        fn = getattr(self, f"_mock_{purpose}", None)
        return fn(nama, target, harga, ctx) if fn else f"[MOCK:{purpose}] konten untuk {nama}"

    def _mock_research_report(self, nama, target, harga, ctx):
        return f"""# Laporan Riset Pasar — {nama}
*(Mode MOCK — isi ANTHROPIC_API_KEY untuk riset web nyata)*

## 1. Ringkasan Eksekutif
Segmen {target} menunjukkan masalah konsisten: waktu terbatas untuk membuat konten
promosi dan gap keterampilan digital. Produk dengan harga Rp {harga} berada di bawah
median kelas online sejenis (Rp 250-500rb) — peluang positioning "praktis & terjangkau".

## 2. Tren
- Pencarian "AI untuk UMKM" naik signifikan 24 bulan terakhir.
- Adopsi AI di UKM Indonesia masih <20% -> pasar dini, edukasi = nilai jual.

## 3. Kompetitor
| Pemain | Penawaran | Harga |
|---|---|---|
| Kelas online umum | video course AI generik | Rp 250-500rb |
| Freelance jasa konten | jasa bulanan | Rp 500rb/bln |
| Ebook marketplace | panduan generik | Rp 50-100rb |

## 4. Pain Points (5 teratas)
1. Tidak punya waktu menulis caption/promo setiap hari.
2. Bingung mulai pakai AI dari mana (terlalu teknis).
3. Takut hasil AI terdengar kaku / tidak sesuai brand.
4. Budget terbatas untuk kursus mahal.
5. Tidak tahu mengukur hasil promosi.

## 5. Rekomendasi
Positioning: "AI untuk jualan harian — tanpa ribet". CTA utama lewat WhatsApp.
Bukti sosial & contoh nyata sebelum/sesudah wajib ada di materi promosi.
"""

    def _mock_strategy(self, nama, target, harga, ctx):
        return f"""# Strategi Produk — {nama}
## Positioning
Satu-satunya panduan AI berbahasa Indonesia yang dibuat KHUSUS untuk {target}:
praktis, bisa dipakai hari yang sama, tanpa istilah teknis.
## Persona Prioritas
1. **Bu Rina (38)** — pemilik usaha kuliner rumahan; kewalahan bikin konten harian.
2. **Mas Bima (29)** — admin olshop fashion; ingin respons pelanggan lebih cepat.
3. **Pak Hendra (45)** — penyedia jasa; skeptis AI tapi penasaran ROI-nya.
## Harga & Penawaran
Harga inti Rp {harga}; bonus template siap pakai; garansi 7 hari.
## Angle Kampanye
"Hemat 10 jam/minggu untuk promosi" — jual hasil, bukan fitur.
"""

    def _mock_product_spec(self, nama, target, harga, ctx):
        return json.dumps({
            "judul": nama,
            "format": "ebook PDF + template + landing page",
            "outline": [
                "1. Kenapa AI = rekan kerja harian UMKM",
                "2. 5 menit pertama: akun & alat gratis",
                "3. Menulis caption jualan yang hidup (10 contoh)",
                "4. Balas chat pelanggan otomatis tapi tetap manusiawi",
                "5. Riset kompetitor dalam 15 menit",
                "6. Kalender konten 30 hari + template",
                "7. Mengukur hasil: metrik sederhana",
                "8. Rencana aksi 7 hari pertama",
            ],
            "cta": f"Dapatkan {nama} seharga Rp {harga} — klik tautan landing page (mock).",
        }, ensure_ascii=False, indent=2)

    def _mock_ebook_chapter(self, nama, target, harga, ctx):
        bab = ctx.get("chapter", "Bab")
        return (f"## {bab}\n\n[Materi mock] Pada bagian ini pembaca belajar praktik langsung "
                f"yang relevan dengan {target}. Sertakan 1 contoh nyata, 1 template siap salin, "
                f"dan 1 latihan 10 menit. (Mode nyata mengisi bab penuh via Claude.)\n")

    def _mock_icp(self, nama, target, harga, ctx):
        return f"""# Definisi Pasar & Scoring — {nama}
## Segmen
- S1: {target} — prioritas utama (skor >= 40 hubungi duluan)
- S2: reseller/agen yang menjual ulang produk digital
## Rubrik Skoring
+20 punya email & telepon · +15 opt_in_wa · +10 sumber komunitas UMKM
+10 ada interaksi/balasan · -30 pernah unsubscribe (blacklist permanen)
## Tahapan Lead
new -> contacted -> replied -> qualified -> customer | lost
"""

    def _mock_copy_pack(self, nama, target, harga, ctx):
        return json.dumps({
            "emails": [
                {"step": 0, "subject": f"Cara {target.split(',')[0]} hemat 10 jam/minggu",
                 "body": f"Halo {{nama}}, banyak {target} kewalahan bikin konten promosi. "
                         f"Kami merangkum praktik AI paling praktis di '{nama}' (Rp {harga}).\n\n"
                         "--\nTidak ingin menerima email lagi? Balas dengan kata BERHENTI."},
                {"step": 1, "subject": "Contoh nyata: caption jadi dalam 2 menit",
                 "body": "Halo {{nama}}, ini contoh caption sebelum vs sesudah pakai metode kami...\n\n"
                         "--\nBalas BERHENTI untuk berhenti menerima email."},
                {"step": 2, "subject": "Pertanyaan yang paling sering muncul",
                 "body": "Halo {{nama}}, 'Apakah cocok untuk pemula?' — ya. 'Perlu komputer canggih?' — tidak...\n\n"
                         "--\nBalas BERHENTI untuk berhenti."},
                {"step": 3, "subject": "Terakhir: bonus template minggu ini",
                 "body": "Halo {{nama}}, penawaran bonus template ditutup minggu ini...\n\n"
                         "--\nBalas BERHENTI untuk berhenti."},
            ],
            "whatsapp": [
                {"step": 0, "name": "mos_sapaan", "body": "Halo {{1}}, kami bantu UMKM membuat promosi harian dengan AI. Boleh kirim contoh gratisnya?"},
                {"step": 1, "name": "mos_followup", "body": "Halo {{1}}, masih tertarik contoh template konten AI untuk usahanya? Balas YA untuk kami kirim."},
            ],
            "social": ["Poster mock: 10 jam hemat tiap minggu dengan AI #UMKM"],
            "landing_hero": f"{nama} — panduan AI paling praktis untuk {target}.",
        }, ensure_ascii=False, indent=2)

    def _mock_reply_draft(self, nama, target, harga, ctx):
        return (f"Halo {{nama}}, terima kasih atas ketertarikannya! {nama} memang dibuat "
                f"untuk penggunaan harian. Detail & cara pesan: [tautan landing]. "
                f"Ada yang bisa kami bantu jawab?")
