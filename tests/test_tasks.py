"""Tes antrean tugas: klaim atomik, backoff, dead, reset stuck."""
from datetime import timedelta

from mos import tasks as taskmod
from mos.db import Task, session_scope, utcnow


def test_klaim_dan_selesai(stack):
    _, engine, _, _ = stack
    with session_scope(engine) as s:
        taskmod.enqueue(s, "research", campaign_id=1)
    with session_scope(engine) as s:
        t = taskmod.claim(s, "w0", ["research"])
        assert t is not None and t.status == "running" and t.attempts == 1
        assert taskmod.claim(s, "w1", ["research"]) is None  # sudah terkunci
        taskmod.complete(s, t)
        assert t.status == "done"


def test_backoff_lalu_dead(stack):
    _, engine, _, _ = stack
    with session_scope(engine) as s:
        t = taskmod.enqueue(s, "research", campaign_id=1, max_attempts=2)
        t.attempts = 1
        taskmod.fail(s, t, "error uji")
        assert t.status == "queued" and t.run_at > utcnow()
        t.attempts = 2
        taskmod.fail(s, t, "error uji")
        assert t.status == "dead"


def test_reset_stuck(stack):
    _, engine, _, _ = stack
    with session_scope(engine) as s:
        t = taskmod.enqueue(s, "research", campaign_id=1)
        t.status = "running"
        t.locked_at = utcnow() - timedelta(minutes=30)
    with session_scope(engine) as s:
        hasil = taskmod.reset_stuck(s, stuck_minutes=15)
        assert len(hasil) == 1 and hasil[0].status == "queued"


def test_dedup(stack):
    _, engine, _, _ = stack
    with session_scope(engine) as s:
        taskmod.enqueue(s, "followup_tick", campaign_id=1, dedup=True)
        taskmod.enqueue(s, "followup_tick", campaign_id=1, dedup=True)
        n = s.query(Task).filter(Task.type == "followup_tick").count()
        assert n == 1
