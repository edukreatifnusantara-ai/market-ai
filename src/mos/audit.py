"""Audit log — setiap aksi agen terekam."""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from .db import AuditLog


def log(session: Session, actor: str, action: str, entity: str = "",
        entity_id: object = "", detail: dict | None = None) -> None:
    session.add(AuditLog(
        actor=actor, action=action, entity=entity, entity_id=str(entity_id or ""),
        detail=json.dumps(detail or {}, ensure_ascii=False)[:4000]))
