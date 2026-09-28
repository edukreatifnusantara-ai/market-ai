"""Kelas dasar agen + konteks bersama."""
from __future__ import annotations

import json
from pathlib import Path

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit, tasks
from ..db import Artifact, Campaign, Heartbeat, utcnow


class AgentContext:
    def __init__(self, settings, engine, llm):
        self.settings = settings
        self.engine = engine
        self.llm = llm

    def save_artifact(self, session: Session, campaign: Campaign, kind: str,
                      filename: str, content: str | bytes, created_by: str) -> Path:
        folder = self.settings.data_dir / "artifacts" / campaign.slug
        if filename.count("/"):
            folder = folder / filename.rsplit("/", 1)[0]
            filename = filename.rsplit("/", 1)[1]
        folder.mkdir(parents=True, exist_ok=True)
        version = session.scalar(select(func.count(Artifact.id)).where(
            Artifact.campaign_id == campaign.id, Artifact.kind == kind)) + 1
        path = folder / filename
        path.write_bytes(content.encode() if isinstance(content, str) else content)
        session.add(Artifact(campaign_id=campaign.id, kind=kind, path=str(path),
                             version=version, created_by=created_by))
        session.flush()
        return path

    def read_artifact(self, session: Session, campaign_id: int, kind: str) -> str:
        art = session.scalar(select(Artifact).where(
            Artifact.campaign_id == campaign_id, Artifact.kind == kind
        ).order_by(Artifact.version.desc()).limit(1))
        return Path(art.path).read_text() if art else ""

    def campaign_brief(self, campaign: Campaign) -> dict:
        return yaml.safe_load(campaign.product_yaml) or {}

    def llm_context(self, campaign: Campaign, agent: str) -> dict:
        b = self.campaign_brief(campaign)
        return {"agent": agent, "name": b.get("name", campaign.name),
                "target_hint": b.get("target_hint", ""),
                "price_hint_idr": b.get("price_hint_idr", "")}


class BaseAgent:
    name = "base"
    handles: tuple[str, ...] = ()

    def dispatch(self, ctx: AgentContext, session: Session, task) -> None:
        handler = getattr(self, f"on_{task.type}", None)
        if handler is None:
            raise ValueError(f"{self.name} tidak menangani task '{task.type}'")
        handler(ctx, session, task)
        audit.log(session, self.name, f"done:{task.type}", "task", task.id)

    def heartbeat(self, session: Session, depth: int = 0) -> None:
        hb = session.get(Heartbeat, self.name)
        if hb is None:
            hb = Heartbeat(agent=self.name)
            session.add(hb)
        hb.last_seen_at = utcnow()
        hb.queue_depth = depth

    def emit(self, session: Session, type: str, campaign_id=None,
             payload=None, priority=50, delay_minutes=0, dedup=False):
        return tasks.enqueue(session, type, campaign_id=campaign_id,
                             payload=payload, priority=priority,
                             delay_minutes=delay_minutes, dedup=dedup)
