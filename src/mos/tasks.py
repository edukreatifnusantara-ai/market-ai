"""Antrean tugas di DB — satu-satunya jalur komunikasi antar-agen."""
from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .db import Task, utcnow


def enqueue(session: Session, type: str, campaign_id: int | None = None,
            payload: dict | None = None, priority: int = 50,
            delay_minutes: float = 0, max_attempts: int = 4,
            dedup: bool = False) -> Task:
    """Tambah tugas. dedup=True hanya menambah bila belum ada tugas queued sejenis."""
    if dedup:
        exists = session.scalar(
            select(func.count(Task.id)).where(
                Task.type == type, Task.status.in_(("queued", "running")),
                Task.campaign_id == campaign_id))
        if exists:
            return session.scalar(select(Task).where(
                Task.type == type, Task.status.in_(("queued", "running")),
                Task.campaign_id == campaign_id).limit(1))
    task = Task(type=type, campaign_id=campaign_id,
                payload=json.dumps(payload or {}, ensure_ascii=False),
                priority=priority,
                run_at=utcnow() + timedelta(minutes=delay_minutes),
                max_attempts=max_attempts)
    session.add(task)
    session.flush()
    return task


def claim(session: Session, worker_id: str, task_types: list[str]) -> Task | None:
    """Ambil satu tugas secara atomik (aman multi-worker di SQLite)."""
    now = utcnow()
    task_id = session.scalar(
        select(Task.id)
        .where(Task.status == "queued", Task.run_at <= now, Task.type.in_(task_types))
        .order_by(Task.priority.desc(), Task.run_at)
        .limit(1))
    if task_id is None:
        return None
    res = session.execute(
        update(Task)
        .where(Task.id == task_id, Task.status == "queued")
        .values(status="running", locked_by=worker_id, locked_at=now,
                attempts=Task.attempts + 1))
    if res.rowcount != 1:
        return None  # kalah balapan dengan worker lain
    session.flush()
    return session.get(Task, task_id)


def complete(session: Session, task: Task) -> None:
    task.status = "done"
    task.locked_by = None


def fail(session: Session, task: Task, error: str) -> None:
    """Gagal -> backoff eksponensial; habis percobaan -> 'dead' untuk Sentinel."""
    task.last_error = (error or "")[:4000]
    task.locked_by = None
    if task.attempts >= task.max_attempts:
        task.status = "dead"
    else:
        task.status = "queued"
        task.run_at = utcnow() + timedelta(minutes=2 ** task.attempts)


def reset_stuck(session: Session, stuck_minutes: int) -> list[Task]:
    """Task 'running' lebih lama dari ambang -> kembalikan ke antrean."""
    batas = utcnow() - timedelta(minutes=stuck_minutes)
    stuck = session.scalars(
        select(Task).where(Task.status == "running", Task.locked_at < batas)).all()
    for t in stuck:
        t.status = "queued"
        t.locked_by = None
        t.last_error = f"reset oleh sentinel (stuck > {stuck_minutes} mnt)"
    return stuck


def queue_depth(session: Session) -> int:
    return session.scalar(select(func.count(Task.id)).where(Task.status == "queued"))
