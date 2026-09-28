"""Fixture bersama: stack mock penuh di direktori sementara."""
import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

RAW_UJI = {
    "timezone": "Asia/Jakarta",
    "models": {"default": "mock", "cheap": "mock"},
    "budgets": {"daily_llm_usd": 5.0, "daily_email_sends": 100, "daily_wa_sends": 50},
    # jendela quiet hours kosong (start == end) -> tidak pernah quiet saat tes
    # cooldown 0 agar pipeline end-to-end bisa kirim email+WA ke lead yang sama
    "send_policy": {"quiet_hours_start": "03:00", "quiet_hours_end": "03:00",
                    "per_lead_cooldown_hours": 0},
    "warmup": {"enabled": True, "day1_cap": 10, "growth_per_day": 1.5},
    "sentinel": {"interval_minutes": 5, "stuck_minutes": 15,
                 "heartbeat_stale_minutes": 10, "llm_error_rate_threshold": 0.2,
                 "backup_hour": 3},
    "sequences": {"email_steps": [0, 2, 5, 9], "wa_steps": [1, 4]},
    "followup": {"interval_minutes": 15},
    "emailer": {"poll_interval_minutes": 5},
}


@pytest.fixture()
def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("FORCE_MOCK", "true")
    monkeypatch.setenv("MOS_DATA_DIR", str(tmp_path / "data"))
    for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "IMAP_HOST", "IMAP_USER",
              "IMAP_PASS", "WA_PHONE_NUMBER_ID", "WA_ACCESS_TOKEN", "OWNER_EMAIL"):
        monkeypatch.delenv(k, raising=False)

    from mos.config import Settings
    from mos.db import init_db, make_engine
    from mos.llm import LLM
    from mos.agents.base import AgentContext

    settings = Settings.load(root=tmp_path)
    # deepcopy: tiap tes dapat salinan sendiri — mutasi satu tes tidak bocor ke tes lain
    settings.raw = copy.deepcopy(RAW_UJI)
    engine = make_engine(settings.db_path)
    init_db(engine)
    llm = LLM(settings, engine)
    ctx = AgentContext(settings, engine, llm)
    return settings, engine, llm, ctx


@pytest.fixture()
def campaign(stack, tmp_path):
    """Kampanye contoh sudah terdaftar + task research ter-antre."""
    from mos.config import load_campaign_file
    from mos.db import Campaign, session_scope
    from mos import tasks as taskmod

    settings, engine, _, _ = stack
    yaml_path = Path(__file__).resolve().parents[1] / "campaigns" / "contoh-produk-digital.yaml"
    data = load_campaign_file(yaml_path)
    with session_scope(engine) as s:
        c = Campaign(slug=data["slug"], name=data["name"], product_yaml=yaml_path.read_text())
        s.add(c)
        s.flush()
        taskmod.enqueue(s, "research", campaign_id=c.id, priority=80)
        cid = c.id
    return cid
