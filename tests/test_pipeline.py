"""Tes end-to-end: kampanye dari research sampai pesan keluar (semua mock)."""
from datetime import timedelta

from sqlalchemy import select

from mos.db import (Artifact, Campaign, Lead, Message, SequenceStep,
                    session_scope, utcnow)
from mos.orchestrator import Orchestrator


def test_pipeline_lengkap(stack, campaign):
    settings, engine, llm, ctx = stack
    assert llm.mock  # wajib mode mock di tes

    # lead 1 dibuat "kemarin" supaya langkah WA hari-1 juga jatuh tempo saat tes
    kemarin = utcnow() - timedelta(days=2)
    with session_scope(engine) as s:
        s.add(Lead(campaign_id=campaign, name="Uji Satu", email="satu@example.com",
                   phone="628111111111", opt_in_wa=True, created_at=kemarin))
        s.add(Lead(campaign_id=campaign, name="Uji Dua", email="dua@example.com",
                   phone="", opt_in_wa=False))

    Orchestrator(ctx).run_until_idle(max_seconds=120)

    with session_scope(engine) as s:
        c = s.get(Campaign, campaign)
        assert c.status == "active", "pipeline harus sampai status active"

        kinds = {a.kind for a in s.scalars(select(Artifact).where(
            Artifact.campaign_id == campaign))}
        assert {"research", "strategy", "spec", "product", "icp", "copy_pack"} <= kinds

        # produk digital benar-benar diproduksi
        paths = [a.path for a in s.scalars(select(Artifact).where(
            Artifact.campaign_id == campaign, Artifact.kind == "product"))]
        assert any(p.endswith("ebook.pdf") for p in paths)
        assert any(p.endswith("landing.html") for p in paths)

        # pesan keluar tercatat; WA hanya ke lead opt-in
        msgs = s.scalars(select(Message)).all()
        assert any(m.channel == "email" and m.status == "mock" for m in msgs)
        wa_msgs = [m for m in msgs if m.channel == "whatsapp"]
        assert wa_msgs and all(
            s.get(Lead, m.lead_id).opt_in_wa for m in wa_msgs)

        # sekuens langkah 0 (hari 0) terkirim; langkah masa depan masih menunggu
        steps = s.scalars(select(SequenceStep)).all()
        assert any(st.status in ("done", "dispatched") for st in steps)
        assert any(st.status == "pending" for st in steps)

    # outbox email benar-benar berisi file .eml
    assert list((settings.data_dir / "outbox" / "email").glob("*.eml"))
    assert list((settings.data_dir / "outbox" / "whatsapp").glob("*.json"))


def test_sentinel_satu_siklus(stack, campaign):
    from mos import tasks as taskmod
    settings, engine, llm, ctx = stack
    with session_scope(engine) as s:
        taskmod.enqueue(s, "sentinel_tick")
        taskmod.enqueue(s, "backup")
    Orchestrator(ctx).run_until_idle(max_seconds=60)
    assert list((settings.data_dir / "backups").glob("mos-*.db"))
