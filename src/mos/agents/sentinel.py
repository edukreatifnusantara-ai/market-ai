"""Agen 9 — SENTINEL: divisi perbaikan & antisipasi kerusakan sistem."""
from __future__ import annotations

import json
import shutil
from datetime import timedelta

from sqlalchemy import func, select

from .. import audit, tasks
from ..db import AuditLog, Heartbeat, LLMCall, Task, utcnow
from ..notify import alert_owner
from .base import BaseAgent


class Sentinel(BaseAgent):
    name = "sentinel"
    handles = ("sentinel_tick", "backup")

    def on_sentinel_tick(self, ctx, session, task):
        self._reset_stuck(ctx, session)
        self._task_mati(ctx, session)
        self._cek_heartbeat(ctx, session)
        self._cek_error_llm(ctx, session)
        self._cek_budget(ctx, session)
        self.heartbeat(session, tasks.queue_depth(session))
        audit.log(session, self.name, "tick")

    # --- 1. task macet -> kembalikan ke antrean
    def _reset_stuck(self, ctx, session):
        ambang = ctx.settings.get("sentinel", "stuck_minutes", default=15)
        stuck = tasks.reset_stuck(session, ambang)
        for t in stuck:
            audit.log(session, self.name, "reset_stuck", "task", t.id, {"type": t.type})
        if stuck:
            alert_owner(ctx.settings, f"{len(stuck)} task macet di-reset",
                        "\n".join(f"#{t.id} {t.type}" for t in stuck))

    # --- 2. task 'dead' (gagal berulang) -> eskalasi sekali per task
    def _task_mati(self, ctx, session):
        mati = session.scalars(select(Task).where(Task.status == "dead")
                               .order_by(Task.id.desc()).limit(20)).all()
        for t in mati:
            sudah = session.scalar(select(func.count(AuditLog.id)).where(
                AuditLog.action == "escalate_dead", AuditLog.entity_id == str(t.id)))
            if not sudah:
                audit.log(session, self.name, "escalate_dead", "task", t.id,
                          {"error": (t.last_error or "")[:500]})
                alert_owner(ctx.settings, f"Task #{t.id} ({t.type}) GAGAL PERMANEN",
                            f"Error terakhir:\n{t.last_error}")

    # --- 3. agen diam -> catat insiden (restart ditangani systemd)
    def _cek_heartbeat(self, ctx, session):
        ambang = ctx.settings.get("sentinel", "heartbeat_stale_minutes", default=10)
        batas = utcnow() - timedelta(minutes=ambang)
        for hb in session.scalars(select(Heartbeat)):
            if hb.agent == self.name:
                continue
            if hb.last_seen_at < batas:
                audit.log(session, self.name, "heartbeat_stale", "agent", hb.agent,
                          {"terakhir": hb.last_seen_at.isoformat()})

    # --- 4. error rate LLM tinggi -> peringatan
    def _cek_error_llm(self, ctx, session):
        ambang = ctx.settings.get("sentinel", "llm_error_rate_threshold", default=0.2)
        sejam = utcnow() - timedelta(hours=1)
        total = session.scalar(select(func.count(LLMCall.id)).where(LLMCall.created_at > sejam))
        if not total or total < 5:
            return
        gagal = session.scalar(select(func.count(LLMCall.id)).where(
            LLMCall.created_at > sejam, LLMCall.ok.is_(False)))
        if gagal / total > ambang:
            alert_owner(ctx.settings, f"Error rate LLM {gagal}/{total} dalam 1 jam",
                        "Periksa API key Anthropic / konektivitas. Sistem tetap berjalan; "
                        "task akan retry otomatis dengan backoff.")

    # --- 5. pagu LLM harian -> halt fail-safe (rilis otomatis hari berikutnya)
    def _cek_budget(self, ctx, session):
        halt = ctx.settings.budget_halt_file
        pagu = float(ctx.settings.get("budgets", "daily_llm_usd", default=5.0))
        if halt.exists():
            # lepas halte bila ini file kemarin
            from datetime import date
            if date.fromtimestamp(halt.stat().st_mtime) < utcnow().date():
                halt.unlink()
                audit.log(session, self.name, "budget_halt_released")
            return
        biaya = ctx.llm.cost_today()
        if biaya >= pagu:
            halt.write_text(f"pagu ${pagu} terlampaui: ${biaya:.2f}")
            audit.log(session, self.name, "budget_halt", detail={"biaya": biaya})
            alert_owner(ctx.settings, "PAGU LLM HARIAN TERLAMPAUI — pengiriman dijeda",
                        f"Biaya hari ini ${biaya:.2f} >= pagu ${pagu}. "
                        f"Sistem lanjut otomatis besok, atau hapus file {halt}.")

    # --- 6. backup DB harian
    def on_backup(self, ctx, session, task):
        tujuan = ctx.settings.data_dir / "backups" / f"mos-{utcnow():%Y%m%d}.db"
        if tujuan.exists():
            return
        shutil.copy2(ctx.settings.db_path, tujuan)
        audit.log(session, self.name, "backup", "db", "", {"path": str(tujuan)})
