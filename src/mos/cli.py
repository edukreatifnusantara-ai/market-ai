"""CLI `mos` — pusat komando Marketing OS."""
from __future__ import annotations

import argparse
import asyncio
import csv
import shutil
import sys
from pathlib import Path

from sqlalchemy import func, select

from .agents.base import AgentContext
from .agents.content import ContentAgent
from .config import Settings, load_campaign_file
from .db import (Artifact, Campaign, Lead, Message, Task, init_db, make_engine,
                 session_scope)
from .llm import LLM
from .orchestrator import Orchestrator
from . import tasks as taskmod


def _stack():
    settings = Settings.load()
    engine = make_engine(settings.db_path)
    init_db(engine)
    llm = LLM(settings, engine)
    return settings, engine, llm, AgentContext(settings, engine, llm)


# ---------------------------------------------------------------- init & doctor
def cmd_init(args) -> int:
    settings = Settings.load()
    env = settings.root / ".env"
    if not env.exists():
        shutil.copy(settings.root / ".env.example", env)
        print("✔ .env dibuat dari .env.example — isi kredensialmu di sana.")
    engine = make_engine(settings.db_path)
    init_db(engine)
    print(f"✔ Database siap di {settings.db_path}")
    print("✔ Direktori data/ siap (artifacts, outbox, inbox, backups, logs)")
    return 0


def cmd_doctor(args) -> int:
    settings = Settings.load()
    gagal = 0

    def cek(ok, label, saran=""):
        nonlocal gagal
        print(("✔" if ok else "❌"), label, (f"-> {saran}" if saran and not ok else ""))
        if not ok:
            gagal += 1

    cek(settings.raw != {}, "settings.yaml terbaca")
    try:
        engine = make_engine(settings.db_path)
        init_db(engine)
        with session_scope(engine) as s:
            s.execute(select(func.count(Task.id)))
        cek(True, f"database SQLite ({settings.db_path})")
    except Exception as e:
        cek(False, "database SQLite", str(e))
    cek(True, f"mode LLM: {'MOCK (tanpa API key)' if settings.force_mock else 'CLAUDE ASLI'}")
    if settings.force_mock:
        print("  ⚠  isi ANTHROPIC_API_KEY di .env untuk riset & konten nyata")
    cek(settings.smtp_configured or True, f"SMTP kirim: {'siap' if settings.smtp_configured else 'mock -> data/outbox/email/'}")
    cek(settings.imap_configured or True, f"IMAP baca balasan: {'siap' if settings.imap_configured else 'simulasi -> data/inbox/*.eml'}")
    cek(settings.wa_configured or True, f"WhatsApp Cloud API: {'siap' if settings.wa_configured else 'mock -> data/outbox/whatsapp/'}")
    for mod in ("anthropic", "fastapi", "fpdf", "apscheduler", "httpx"):
        try:
            __import__(mod)
            cek(True, f"dependensi {mod}")
        except ImportError:
            cek(False, f"dependensi {mod}", "pip install -e .")
    print(f"\n{'SEMUA SIAP' if gagal == 0 else f'{gagal} masalah — perbaiki lalu ulangi'}")
    return 0 if gagal == 0 else 1


# ---------------------------------------------------------------- campaign
def cmd_campaign(args) -> int:
    settings, engine, llm, ctx = _stack()
    if args.action == "add":
        data = load_campaign_file(Path(args.target))
        with session_scope(engine) as s:
            c = s.scalar(select(Campaign).where(Campaign.slug == data["slug"]))
            if c is None:
                c = Campaign(slug=data["slug"], name=data["name"],
                             product_yaml=Path(args.target).read_text())
                s.add(c)
                s.flush()
            else:
                c.product_yaml = Path(args.target).read_text()
                taskmod.enqueue(s, "research", campaign_id=c.id, priority=80, dedup=True)
                print(f"✔ Kampanye '{c.slug}' diperbarui; pipeline dijalankan ulang (idempoten).")
                return 0
            if data.get("status") == "active":
                taskmod.enqueue(s, "research", campaign_id=c.id, priority=80)
                print(f"✔ Kampanye '{c.slug}' masuk. Pipeline riset -> produksi -> konten dimulai.")
            else:
                print(f"✔ Kampanye '{c.slug}' tersimpan sebagai draft.")
        return 0
    with session_scope(engine) as s:
        if args.action == "list":
            for c in s.scalars(select(Campaign)):
                n_leads = s.scalar(select(func.count(Lead.id)).where(Lead.campaign_id == c.id))
                print(f"{c.slug:30} {c.status:15} leads={n_leads}")
        else:
            c = s.scalar(select(Campaign).where(Campaign.slug == args.target))
            if not c:
                print("Kampanye tidak ditemukan."); return 1
            print(f"== {c.name} [{c.status}] ==")
            for a in s.scalars(select(Artifact).where(Artifact.campaign_id == c.id)
                               .order_by(Artifact.created_at)):
                print(f"  [{a.kind:10}] v{a.version} {Path(a.path).name} (oleh {a.created_by})")
            for stage, n in s.execute(select(Lead.stage, func.count(Lead.id))
                                      .where(Lead.campaign_id == c.id).group_by(Lead.stage)):
                print(f"  leads {stage}: {n}")
    return 0


# ---------------------------------------------------------------- leads
def cmd_leads(args) -> int:
    settings, engine, llm, ctx = _stack()
    if args.action == "list":
        with session_scope(engine) as s:
            q = select(Lead)
            if args.campaign:
                c = s.scalar(select(Campaign).where(Campaign.slug == args.campaign))
                q = q.where(Lead.campaign_id == (c.id if c else -1))
            for l in s.scalars(q.limit(50)):
                print(f"{l.id:4} {l.stage:10} skor={l.score:3} wa={'Y' if l.opt_in_wa else '-'} "
                      f"{l.name} <{l.email}> {l.phone}")
        return 0
    # import
    with session_scope(engine) as s:
        c = s.scalar(select(Campaign).where(Campaign.slug == args.campaign))
        if not c:
            print("Kampanye tidak ditemukan."); return 1
        masuk = lewati = 0
        with open(args.file, newline="") as f:
            for row in csv.DictReader(f):
                email = (row.get("email") or "").strip().lower()
                phone = "".join(ch for ch in (row.get("phone") or "") if ch.isdigit() or ch == "+")
                if not email and not phone:
                    lewati += 1; continue
                ada = s.scalar(select(func.count(Lead.id)).where(
                    Lead.campaign_id == c.id,
                    (Lead.email == email) | (Lead.phone == phone))) if (email or phone) else 1
                if ada:
                    lewati += 1; continue
                lead = Lead(campaign_id=c.id, name=(row.get("name") or "").strip(),
                            email=email, phone=phone,
                            source=(row.get("source") or "import").strip(),
                            opt_in_wa=(row.get("opt_in_wa") or "").strip().lower()
                            in ("1", "true", "ya", "yes"))
                s.add(lead); s.flush()
                if c.status == "active":
                    ContentAgent._buat_sekuens_lead(
                        s, c, lead,
                        settings.get("sequences", "email_steps", default=[0, 2, 5, 9]),
                        settings.get("sequences", "wa_steps", default=[1, 4]))
                masuk += 1
        if c.status == "active":
            taskmod.enqueue(s, "followup_tick", campaign_id=c.id, dedup=True)
        print(f"✔ {masuk} lead masuk, {lewati} dilewati (duplikat/kosong).")
    return 0


# ---------------------------------------------------------------- approve & sentinel
def cmd_approve(args) -> int:
    _, engine, _, _ = _stack()
    with session_scope(engine) as s:
        for m in s.scalars(select(Message).order_by(Message.id.desc()).limit(20)):
            arah = "→" if m.direction == "out" else "←"
            print(f"#{m.id} [{m.channel:8}] {arah} status={m.status:14} "
                  f"{m.subject or m.template_name or m.body[:40]}")
    return 0


def cmd_sentinel(args) -> int:
    settings, engine, llm, ctx = _stack()
    with session_scope(engine) as s:
        taskmod.enqueue(s, "sentinel_tick")
        taskmod.enqueue(s, "backup")
    Orchestrator(ctx).run_until_idle(60)
    print("✔ Sentinel selesai: stuck tasks, dead tasks, heartbeat, error-rate LLM, pagu, backup.")
    return 0


# ---------------------------------------------------------------- run & web & demo
def cmd_run(args) -> int:
    settings, engine, llm, ctx = _stack()
    orch = Orchestrator(ctx, workers=args.workers)
    if args.once:
        orch.run_until_idle(args.seconds)
        print("✔ Antrean kosong (mode --once selesai).")
    else:
        print(f"Marketing OS berjalan 24/7 ({args.workers} worker). Ctrl+C untuk berhenti.")
        print(f"Mode LLM: {'MOCK' if llm.mock else 'CLAUDE'} | Kill switch: {settings.kill_switch_file}")
        asyncio.run(orch.run_forever())
    return 0


def cmd_web(args) -> int:
    import uvicorn
    from .web import create_app
    settings, engine, llm, _ = _stack()
    uvicorn.run(create_app(settings, engine, llm), host=args.host, port=args.port)
    return 0


def cmd_demo(args) -> int:
    settings, engine, llm, ctx = _stack()
    cmd_init(args)
    contoh = settings.root / "campaigns" / "contoh-produk-digital.yaml"
    ns = argparse.Namespace(action="add", target=str(contoh))
    cmd_campaign(ns)
    sampel = settings.root / "samples" / "leads.csv"
    if sampel.exists():
        ns = argparse.Namespace(action="import", file=str(sampel),
                                campaign="panduan-ai-umkm")
        cmd_leads(ns)
    print("\n>>> Menjalankan pipeline (mode mock bila tanpa API key)...")
    Orchestrator(ctx).run_until_idle(300)
    with session_scope(engine) as s:
        c = s.scalar(select(Campaign).where(Campaign.slug == "panduan-ai-umkm"))
        n_msg = s.scalar(select(func.count(Message.id)))
        print(f"\n== HASIL DEMO ==\nKampanye: {c.name} [{c.status}]")
        print(f"Pesan keluar terkirim/tercatat: {n_msg}")
        print(f"Artefak: {settings.data_dir / 'artifacts' / 'panduan-ai-umkm'}")
        print(f"Outbox : {settings.data_dir / 'outbox'}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="mos", description="Marketing OS — perusahaan marketing AI multi-agen")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="siapkan .env, data/, database")
    sub.add_parser("doctor", help="cek konfigurasi & integrasi")
    r = sub.add_parser("run", help="jalankan semua agen 24/7")
    r.add_argument("--once", action="store_true", help="proses antrean sampai kosong lalu berhenti")
    r.add_argument("--seconds", type=int, default=300)
    r.add_argument("--workers", type=int, default=4)
    c = sub.add_parser("campaign", help="kelola kampanye")
    c.add_argument("action", choices=["add", "list", "status"])
    c.add_argument("target", nargs="?", help="file yaml (add) atau slug (status)")
    l = sub.add_parser("leads", help="kelola calon klien")
    l.add_argument("action", choices=["import", "list"])
    l.add_argument("file", nargs="?", help="csv (import)")
    l.add_argument("--campaign", default=None)
    sub.add_parser("approve", help="lihat 20 pesan terakhir keluar/masuk")
    sub.add_parser("sentinel", help="jalankan satu siklus divisi perbaikan")
    w = sub.add_parser("web", help="server webhook WhatsApp")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8000)
    sub.add_parser("demo", help="jalankan kampanye contoh end-to-end")
    args = p.parse_args(argv)
    return globals()[f"cmd_{args.cmd}"](args)


if __name__ == "__main__":
    sys.exit(main())
