"""Revoking automatic sending works without a model and preserves the venture's other limits."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app, local_chat as local_chat


@pytest.mark.parametrize("text", [
    "turn off sending", "switch sending off", "stop sending emails", "disable automatic sending",
    "pause sending outreach", "do not send emails anymore", "don't send messages again",
    "Could you stop sending emails?", "stop outreach",
])
def test_ongoing_stop_commands_are_deterministic(text):
    load_company_os_app()
    from app.chat.intent import classify_intent, split_speech_acts

    assert classify_intent(text) == "send_off"
    assert split_speech_acts(text) == [("send_off", text)]


@pytest.mark.parametrize("text", [
    "How do I turn off sending?", "Should I stop sending emails?", "Don't stop sending emails",
    "Draft an email. Do not send it.", "Do not send emails to Alex", 'The note says "stop sending emails"',
    "Turn off sending for another venture", "Stop sending emails and delete all drafts",
])
def test_questions_and_draft_constraints_do_not_revoke_permission(text):
    load_company_os_app()
    from app.chat.send_control import stop_sending_request

    assert not stop_sending_request(text)


@pytest.mark.parametrize("idea", [
    "help synthesizer repair shops track parts", "help families plan accessible weekend walks",
])
def test_stop_send_and_reviewed_reenable_preserve_other_saved_limits(tmp_path, monkeypatch, idea):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/sending.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "mem.sqlite3"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", "true")
    main = load_company_os_app()
    from app.core.local_mode import is_local
    if is_local():
        from app.core.local_secrets import update_settings
        from secrets import token_urlsafe
        update_settings("smtp", {"host": "smtp.example.test", "port": 587,
            "user": "founder@example.test", "sender_name": "Synthetic founder",
            "postal_address": "1 Example Street, Toronto, Canada"}, {"password": token_urlsafe(32)})
        update_settings("capabilities", {"send_email": True}, {})
    from app import state
    from app.autopilot import service
    from app.chat import llm_router
    from app.chat.actions import get_action
    from tests.unit.chat.test_assistant_routing import confirm_idea
    from tests.unit.needs_you.test_readiness import _drafts

    state.reset_db_factory()
    state.MEM.clear()
    with TestClient(main.create_app()) as client:
        vid = client.post("/ventures", json={"raw_idea": idea}).json()["id"]
        confirm_idea(vid)
        service.update_settings(vid, god_mode=True, daily_cap_credits=400, allow_mac=True,
                                allow_send=True, send_max_per_day=12)
        async def forbidden(*_args, **_kwargs):
            raise AssertionError("stopping sending must not depend on the model")
        monkeypatch.setattr(llm_router, "route", forbidden)
        r = client.post(f"/ventures/{vid}/chat", json={"message": "do not send emails anymore"})
        assert r.status_code == 200 and "Automatic sending is off" in r.json()["body_md"]
        allow = r.json()["actions"][0]
        assert allow["label"] == "Allow sending" and not allow.get("href")
        assert allow["confirmation"]["action"] == "autopilot.allow_send"
        saved = service.public_settings(vid)
        assert saved["allow_send"] is False and saved["send_max_per_day"] == 0
        assert saved["god_mode"] is True and saved["daily_cap_credits"] is None and saved["allow_mac"] is True
        # A later explicit tap on the existing reviewed permission card can re-enable
        # the default capped allowance; it does not alter the spending cap.
        assert get_action("autopilot.allow_send").consequential
        r = client.post(f"/ventures/{vid}/chat/confirm", json={"action": allow["confirmation"]["action"],
                       "payload": allow["confirmation"]["resulting_state"]})
        assert r.status_code == 200 and "No people to write to yet" in r.json()["body_md"]
        assert r.json()["actions"] == [{"label": "Find customers", "action": "gtm.find", "payload": {"prepare": True}, "style": "primary"}]
        assert service.public_settings(vid) == saved
        _drafts(vid, 1)
        r = client.post(f"/ventures/{vid}/chat/confirm", json={"action": allow["confirmation"]["action"],
                       "payload": allow["confirmation"]["resulting_state"]})
        assert r.status_code == 200 and "up to 15 emails a day" in r.json()["body_md"]
        stop = r.json()["actions"][0]
        assert stop == {"label": "Stop sending", "action": "autopilot.send_off", "style": "ghost"}
        saved = service.public_settings(vid)
        assert saved["allow_send"] is True and saved["send_max_per_day"] == 15
        assert saved["god_mode"] is True and saved["daily_cap_credits"] is None and saved["allow_mac"] is True
        r = client.post(f"/ventures/{vid}/chat/confirm", json={"action": stop["action"]})
        assert r.status_code == 200 and "Automatic sending is off" in r.json()["body_md"]
        assert service.public_settings(vid)["allow_send"] is False
        assert service.public_settings(vid)["send_max_per_day"] == 0
