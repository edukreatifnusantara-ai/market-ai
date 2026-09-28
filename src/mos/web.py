"""Server webhook: WhatsApp Cloud API + endpoint unsubscribe + healthz."""
from __future__ import annotations

import hashlib
import hmac

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from . import guardrails
from .agents.whatsapp import proses_webhook
from .db import session_scope


def create_app(settings, engine, llm) -> FastAPI:
    app = FastAPI(title="Marketing OS Webhook", docs_url=None)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "kill_switch": guardrails.kill_switch_active(settings)}

    # --- verifikasi webhook (Meta mengirim GET dgn param hub.mode/hub.verify_token/hub.challenge) ---
    @app.get("/webhook/whatsapp")
    def verify(request: Request):
        q = request.query_params
        token = q.get("hub.verify_token", q.get("verify_token", ""))
        if token and token == settings.env("WA_VERIFY_TOKEN"):
            return PlainTextResponse(q.get("hub.challenge", q.get("challenge", "")))
        raise HTTPException(403, "verify token salah")

    @app.post("/webhook/whatsapp")
    async def incoming(request: Request):
        body_bytes = await request.body()
        secret = settings.env("WA_APP_SECRET")
        if secret:  # verifikasi tanda tangan bila app secret diisi
            sig = request.headers.get("X-Hub-Signature-256", "")
            hitung = "sha256=" + hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, hitung):
                raise HTTPException(403, "signature tidak valid")
        aksi = proses_webhook(_CtxProxy(settings, llm), engine, await request.json())
        return {"aksi": aksi}

    @app.get("/unsubscribe", response_class=HTMLResponse)
    def unsubscribe(e: str = ""):
        with session_scope(engine) as s:
            guardrails.unsubscribe(s, "all", e, "klik link unsubscribe")
        return "<h2>Berhasil berhenti berlangganan.</h2><p>Anda tidak akan menerima pesan lagi.</p>"

    return app


class _CtxProxy:
    """Konteks minimal yang dibutuhkan proses_webhook (settings + llm)."""

    def __init__(self, settings, llm):
        self.settings = settings
        self.llm = llm
