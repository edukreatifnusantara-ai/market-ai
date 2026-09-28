"""Agen 10 — ORKESTRATOR: loop worker 24/7, dispatch antrean, penjadwal periodik."""
from __future__ import annotations

import asyncio
import time

from sqlalchemy import func, select

from . import audit, tasks
from .agents import ALL_AGENTS
from .db import Heartbeat, Task, session_scope, utcnow


class Orchestrator:
    def __init__(self, ctx, workers: int = 4):
        self.ctx = ctx
        self.workers = workers
        self.registry = {}
        for cls in ALL_AGENTS:
            agen = cls()
            for t in cls.handles:
                self.registry[t] = agen
        self.task_types = list(self.registry.keys())
        self.stop_event = asyncio.Event()

    # ------------------------------------------------ inti dispatch
    def process_one(self, worker_id: str) -> bool:
        """Klaim + eksekusi satu tugas (sinkron). Return True bila ada kerja."""
        with session_scope(self.ctx.engine) as session:
            task = tasks.claim(session, worker_id, self.task_types)
            if task is None:
                return False
            agen = self.registry[task.type]
            try:
                agen.dispatch(self.ctx, session, task)
                self.ctx.llm.drain(session)
                tasks.complete(session, task)
            except Exception as e:
                self.ctx.llm.drain(session)
                tasks.fail(session, task, f"{type(e).__name__}: {e}")
                audit.log(session, "orchestrator", "task_error", "task", task.id,
                          {"error": str(e)[:800]})
        return True

    def claimable_count(self) -> int:
        with session_scope(self.ctx.engine) as s:
            return s.scalar(select(func.count(Task.id)).where(
                Task.status == "queued", Task.run_at <= utcnow(),
                Task.type.in_(self.task_types)))

    def run_until_idle(self, max_seconds: int = 300) -> None:
        """Mode --once: proses sampai antrean kosong (untuk demo & tes)."""
        batas = time.time() + max_seconds
        while time.time() < batas:
            if not self.process_one("run-once"):
                if self.claimable_count() == 0:
                    return
                time.sleep(1)

    # ------------------------------------------------ mode 24/7
    def _enqueue_periodic(self, tipe: str) -> None:
        with session_scope(self.ctx.engine) as s:
            tasks.enqueue(s, tipe, dedup=True)

    def _heartbeat(self) -> None:
        with session_scope(self.ctx.engine) as s:
            hb = s.get(Heartbeat, "orchestrator")
            if hb is None:
                hb = Heartbeat(agent="orchestrator")
                s.add(hb)
            hb.last_seen_at = utcnow()
            hb.queue_depth = self.claimable_count()

    async def _worker(self, wid: str) -> None:
        loop = asyncio.get_running_loop()
        while not self.stop_event.is_set():
            ada_kerja = await loop.run_in_executor(None, self.process_one, wid)
            if not ada_kerja:
                await asyncio.sleep(2)

    async def run_forever(self) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        s = self.ctx.settings
        sched = AsyncIOScheduler(timezone=s.timezone)
        sched.add_job(self._enqueue_periodic,
                      IntervalTrigger(minutes=int(s.get("sentinel", "interval_minutes", default=5))),
                      args=["sentinel_tick"], id="sentinel")
        sched.add_job(self._heartbeat, IntervalTrigger(minutes=1), id="heartbeat")
        sched.add_job(self._enqueue_periodic,
                      IntervalTrigger(minutes=int(s.get("followup", "interval_minutes", default=15))),
                      args=["followup_tick"], id="followup")
        sched.add_job(self._enqueue_periodic,
                      IntervalTrigger(minutes=int(s.get("emailer", "poll_interval_minutes", default=5))),
                      args=["poll_replies"], id="poll")
        sched.add_job(self._enqueue_periodic,
                      CronTrigger(hour=int(s.get("sentinel", "backup_hour", default=3)), minute=7),
                      args=["backup"], id="backup")
        sched.start()

        workers = [asyncio.create_task(self._worker(f"w{i}")) for i in range(self.workers)]
        loop = asyncio.get_running_loop()
        import signal as _signal
        for sig in (_signal.SIGINT, _signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop_event.set)
            except (NotImplementedError, RuntimeError):
                pass
        await self.stop_event.wait()
        for w in workers:
            w.cancel()
        sched.shutdown(wait=False)
