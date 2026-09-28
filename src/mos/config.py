"""Konfigurasi: settings.yaml + .env + file kampanye."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Settings:
    root: Path = ROOT
    data_dir: Path = ROOT / "data"
    raw: dict = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path | None = None) -> "Settings":
        root = root or ROOT
        load_dotenv(root / ".env")
        settings_file = root / "settings.yaml"
        raw = yaml.safe_load(settings_file.read_text()) if settings_file.exists() else {}
        s = cls(root=root, data_dir=Path(os.environ.get("MOS_DATA_DIR", root / "data")), raw=raw or {})
        for sub in ("artifacts", "outbox/email", "outbox/whatsapp", "inbox", "backups", "logs"):
            (s.data_dir / sub).mkdir(parents=True, exist_ok=True)
        return s

    # ---- accessor settings.yaml ----
    def get(self, *keys, default=None):
        node = self.raw
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    @property
    def timezone(self) -> str:
        return self.get("timezone", default="Asia/Jakarta")

    @property
    def model_default(self) -> str:
        return self.get("models", "default", default="claude-sonnet-5")

    @property
    def model_cheap(self) -> str:
        return self.get("models", "cheap", default="claude-haiku-4-5-20251001")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mos.db"

    @property
    def kill_switch_file(self) -> Path:
        return self.data_dir / "KILL"

    @property
    def budget_halt_file(self) -> Path:
        return self.data_dir / "BUDGET_HALT"

    # ---- env ----
    @staticmethod
    def env(key: str, default: str = "") -> str:
        return os.environ.get(key, default).strip()

    @property
    def anthropic_key(self) -> str:
        return self.env("ANTHROPIC_API_KEY")

    @property
    def force_mock(self) -> bool:
        v = self.env("FORCE_MOCK", "auto").lower()
        if v == "true":
            return True
        if v == "false":
            return False
        return not self.anthropic_key  # auto: mock bila tanpa API key

    @property
    def smtp_configured(self) -> bool:
        return bool(self.env("SMTP_HOST") and self.env("SMTP_USER") and self.env("SMTP_PASS"))

    @property
    def imap_configured(self) -> bool:
        return bool(self.env("IMAP_HOST") and self.env("IMAP_USER") and self.env("IMAP_PASS"))

    @property
    def wa_configured(self) -> bool:
        return bool(self.env("WA_PHONE_NUMBER_ID") and self.env("WA_ACCESS_TOKEN"))


def load_campaign_file(path: Path) -> dict:
    """Baca & validasi file YAML kampanye."""
    data = yaml.safe_load(Path(path).read_text())
    required = ["slug", "name", "type", "description"]
    missing = [k for k in required if not data.get(k)]
    if missing:
        raise ValueError(f"Kampanye {path} kurang field wajib: {missing}")
    if data["type"] not in ("digital", "physical"):
        raise ValueError("type harus 'digital' atau 'physical'")
    return data
