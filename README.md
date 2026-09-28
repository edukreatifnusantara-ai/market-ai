# Marketing OS — Perusahaan Marketing AI Multi-Agen (24/7)

Sistem otonom berisi **10 agen AI** yang bekerja bergiliran 24 jam: meriset pasar
secara nyata, menentukan target market, memproduksi produk digital dari data riset,
menulis materi pemasaran, mengirim & membalas email, mem-follow-up lewat WhatsApp,
dan **menjaga dirinya sendiri** lewat divisi perbaikan (Sentinel).

```
                 ┌──────────────────────────────┐
                 │   ORKESTRATOR (agen #10)      │
                 │  antrean tugas · pagu · 24/7  │
                 └───────────────┬──────────────┘
     ┌───────────────────────────┼───────────────────────────┐
     ▼                           ▼                           ▼
 DIVISI RISET & PRODUK     DIVISI PENJUALAN           DIVISI KONTROL
 #1 Researcher             #4 ICP / Market            #9 Sentinel
 #2 Strategist             #5 Content                 (QA & perbaikan)
 #3 Producer (digital)     #6 Email                   #10 Audit & log
                           #7 WhatsApp
                           #8 Follow-up
```

## Cara kerja singkat

Kampanye didefinisikan di satu file YAML (`campaigns/*.yaml`). Begitu diaktifkan,
pipeline berjalan sendiri:

```
research → strategy → produce → define_icp → create_content → outreach → follow-up
   #1         #2         #3          #4            #5          #6/#7        #8
```

Semua agen **tidak saling memanggil langsung** — mereka bertukar tugas lewat
antrean di database (`tasks`). Artinya agen boleh mati/restart kapan pun tanpa
kehilangan pekerjaan, dan bisa dijalankan paralel.

## Instalasi

```bash
cd ~/marketing-os
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

mos init          # membuat .env, data/, dan database
mos doctor        # cek konfigurasi & integrasi
```

### Menjalankan tanpa kredensial (mode mock)

```bash
mos demo          # kampanye contoh end-to-end, semua output ke data/outbox/
```

Semua agen bekerja penuh, tetapi LLM memakai konten mock dan pesan "terkirim"
ditulis ke `data/outbox/` — tidak ada email/WA nyata yang keluar.

### Menjalankan sungguhan

Isi `.env` (lihat `.env.example`):

| Kredensial | Untuk apa |
|---|---|
| `ANTHROPIC_API_KEY` **atau** `OPENAI_API_KEY` (salah satu) | riset web nyata + penulisan konten |
| `SMTP_*`, `IMAP_*`, `EMAIL_FROM` | kirim email + baca balasan (Gmail: App Password) |
| `WA_PHONE_NUMBER_ID`, `WA_ACCESS_TOKEN`, `WA_BUSINESS_ACCOUNT_ID`, `WA_VERIFY_TOKEN` | WhatsApp Business Cloud API resmi |
| `OWNER_EMAIL`, `OWNER_PHONE` | tujuan eskalasi Sentinel |

Lalu:

```bash
mos run                       # semua agen 24/7
mos web --host 0.0.0.0 --port 8010   # webhook WhatsApp (atau pakai systemd)
mos campaign add campaigns/produk-baru.yaml
mos leads import leads.csv --campaign slug-kampanye
mos campaign status slug-kampanye
mos approve                   # 20 pesan terakhir keluar/masuk
mos sentinel                  # satu siklus divisi perbaikan
```

### Menjalankan permanen (systemd user)

```bash
cp deploy/*.service deploy/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mos-orchestrator mos-web mos-sentinel.timer
systemctl --user status mos-orchestrator
```

`Restart=always` membuat semua proses hidup kembali sendiri setelah crash.

## Divisi perbaikan (Sentinel) — apa yang dijaga

| Bahaya | Tindakan otomatis |
|---|---|
| Task macet (`running` > 15 mnt) | dikembalikan ke antrean + dicatat |
| Task gagal ≥ 3x | status `dead` + email eskalasi ke pemilik |
| Agen/orchestrator diam | insiden dicatat, systemd me-restart, ping pemilik |
| Error rate LLM > 20%/jam | peringatan + task retry dengan backoff |
| Pagu biaya LLM harian terlampaui | **fail-safe**: pengiriman & LLM dijeda, rilis otomatis besok |
| Copy lolos QC tidak | ditolak/ditambal otomatis (link, kata terlarang, opt-out) |
| DB rusak | backup harian `data/backups/`, mode WAL |

Darurat: `touch data/KILL` → semua pengiriman berhenti seketika.

## Guardrails yang selalu aktif (tidak bisa dimatikan kampanye)

- Batas kirim per hari per channel, dengan **warm-up** bertahap
- Maksimal 1 pesan per lead per 48 jam per channel
- Quiet hours (default 21:00–08:00)
- WhatsApp **hanya** ke lead dengan `opt_in_wa=true`
- Email wajib ber-header `List-Unsubscribe` + kalimat opt-out
- Kata `STOP`/`BERHENTI` masuk blacklist permanen (semua channel)
- Dedup lintas kampanye, kill switch global

## Struktur proyek

```
src/mos/
  config.py db.py tasks.py llm.py guardrails.py audit.py notify.py
  orchestrator.py  web.py  cli.py
  agents/  base researcher strategist producer icp content
           emailer whatsapp followup sentinel
campaigns/            # brief kampanye (YAML)
deploy/               # unit systemd
samples/leads.csv     # contoh daftar lead
tests/                # pytest: guardrails, antrean, pipeline end-to-end
settings.yaml         # pagu, rate limit, quiet hours, interval
```

## Tes

```bash
pytest -q
```

Menguji kunci: klaim tugas atomik + backoff, kill switch, opt-in WA,
unsubscribe/blacklist, quiet hours, batas harian, dan pipeline penuh
research → produk PDF → konten → email/WA keluar (semua mock).
