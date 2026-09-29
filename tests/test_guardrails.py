"""Tes guardrails wajib: kill switch, opt-in WA, unsubscribe, quiet hours, cap harian."""
from mos import guardrails
from mos.db import Lead, SequenceStep, session_scope, utcnow


def _lead(s, **kw):
    kw.setdefault("name", "Uji")
    kw.setdefault("email", "uji@contoh.id")
    kw.setdefault("phone", "628111111111")
    kw.setdefault("opt_in_wa", True)
    lead = Lead(campaign_id=1, **kw)
    s.add(lead)
    s.flush()
    return lead


def test_domain_uji_diblokir(stack):
    """Alamat @example.com dll tidak pernah dikirimi — mencegah bounce reputasi."""
    settings, engine, _, _ = stack
    with session_scope(engine) as s:
        lead = _lead(s, email="siapa@example.com")
        ok, alasan = guardrails.check_send(settings, s, lead, "email")
        assert not ok and "domain uji" in alasan
        lead2 = _lead(s, email="asli@contoh.id")
        ok2, _ = guardrails.check_send(settings, s, lead2, "email")
        assert ok2  # domain normal tetap lolos


def test_kill_switch(stack):
    settings, engine, _, _ = stack
    settings.kill_switch_file.write_text("stop")
    with session_scope(engine) as s:
        lead = _lead(s)
        ok, alasan = guardrails.check_send(settings, s, lead, "email")
        assert not ok and "KILL" in alasan


def test_whatsapp_wajib_opt_in(stack):
    settings, engine, _, _ = stack
    with session_scope(engine) as s:
        lead = _lead(s, opt_in_wa=False)
        ok, alasan = guardrails.check_send(settings, s, lead, "whatsapp")
        assert not ok and "opt-in" in alasan
        ok_email, _ = guardrails.check_send(settings, s, lead, "email")
        assert ok_email  # email tetap boleh


def test_unsubscribe_blacklist_dan_sekuens_berhenti(stack):
    settings, engine, _, _ = stack
    with session_scope(engine) as s:
        lead = _lead(s)
        s.add(SequenceStep(lead_id=lead.id, campaign_id=1, step_no=0,
                           channel="email", due_at=utcnow(), status="pending"))
        guardrails.unsubscribe(s, "email", "UJI@contoh.id", "tes")
        assert guardrails.is_blacklisted(s, "email", "uji@contoh.id")
        assert lead.unsubscribed_at is not None
        step = s.query(SequenceStep).filter_by(lead_id=lead.id).one()
        assert step.status == "skipped"
        ok, alasan = guardrails.check_send(settings, s, lead, "email")
        assert not ok


def test_quiet_hours(stack):
    settings, engine, _, _ = stack
    settings.raw["send_policy"]["quiet_hours_start"] = "00:00"
    settings.raw["send_policy"]["quiet_hours_end"] = "23:59"
    assert guardrails.in_quiet_hours(settings)
    with session_scope(engine) as s:
        lead = _lead(s)
        ok, alasan = guardrails.check_send(settings, s, lead, "email")
        assert not ok and "quiet" in alasan


def test_batas_harian_warmup(stack):
    settings, engine, _, _ = stack
    with session_scope(engine) as s:
        lead = _lead(s)
        for _ in range(10):  # day1_cap = 10
            guardrails.record_send(s, "email")
        lead.last_contacted_at = None
        ok, alasan = guardrails.check_send(settings, s, lead, "email")
        assert not ok and "batas harian" in alasan


def test_cooldown_per_lead(stack):
    settings, engine, _, _ = stack
    settings.raw["send_policy"]["per_lead_cooldown_hours"] = 48
    with session_scope(engine) as s:
        lead = _lead(s)
        lead.last_contacted_at = utcnow()
        ok, alasan = guardrails.check_send(settings, s, lead, "email")
        assert not ok and "cooldown" in alasan
