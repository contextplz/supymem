import asyncio

import pytest

from tests.company_os_harness import load_company_os_app, local_chat as local_chat

load_company_os_app()

from app.chat.actions import BETA_BLOCKED_ACTIONS, REGISTRY, beta_blocked
from app.chat.execute import execute_action


def test_blocked_actions_are_registered_and_refuse_honestly(monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.delenv("SUPYMEM_BETA_SCOPE", raising=False)
    assert BETA_BLOCKED_ACTIONS <= set(REGISTRY)
    for name in BETA_BLOCKED_ACTIONS:
        reply = beta_blocked(name)
        assert reply and "coming soon in the beta" in reply["body_md"] and reply["actions"] == []
    for kept in ("connections.connect", "outreach.send", "autopilot.allow_send", "autopilot.send_cap"):
        assert beta_blocked(kept) is None  # founder decision: Gmail stays, sends still need approval cards
    assert beta_blocked("memory.remember") is None and beta_blocked("site.publish") is None


@pytest.mark.parametrize("name", ["autopilot.god_on", "build.start", "mac.task.prepare", "social_connect"])
def test_execute_refuses_stale_card(monkeypatch, name):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.delenv("SUPYMEM_BETA_SCOPE", raising=False)
    out = asyncio.run(execute_action("ven_x", name, payload={}))
    assert "coming soon in the beta" in out["body_md"]


def test_gate_can_be_lifted(monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("SUPYMEM_BETA_SCOPE", "0")
    assert beta_blocked("autopilot.god_on") is None


@pytest.mark.parametrize("venture_id", ["ven_procurement", "ven_weekend_cooking"])
@pytest.mark.parametrize("name", ["build.start", "mac.task.prepare", "gtm.library_claim"])
def test_local_execute_blocks_unshipped_or_switched_off_work_before_any_side_effect(monkeypatch, venture_id, name):
    from tests.company_os_harness import company_os_test_client

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("SUPYMEM_BETA_SCOPE", "0")
    if name == "mac.task.prepare":
        from app.core.local_secrets import update_settings

        update_settings("capabilities", {"background_work": False}, {})
    actor = None
    if name == "build.start":
        with company_os_test_client(load_company_os_app().app) as client:
            response = client.post("/ventures", json={"raw_idea": venture_id, "start": False})
            assert response.status_code == 201, response.text
            venture_id = response.json()["id"]
    from app.chat import execute
    from app.core.local_owner import ensure_owner
    if name == "build.start":
        actor = ensure_owner().id
    started = []
    def build(vid, payload, actor):
        started.append((vid, payload, actor))
        return {"kind": "text", "body_md": "Your private build is starting. Review its result next.", "actions": []}
    monkeypatch.setattr(execute, "_start_build", build)
    reply = asyncio.run(execute.execute_action(venture_id, name, payload={}, actor_subject=actor))
    if name == "build.start":
        assert beta_blocked(name, venture_id) is None and started == [(venture_id, {}, actor)]
        assert reply["body_md"] == "Your private build is starting. Review its result next." and not reply["actions"]
    else:
        assert reply == beta_blocked(name, venture_id) and not started
        if name == "mac.task.prepare":
            assert "not in this version yet" in reply["body_md"] and "coding tool" in reply["body_md"]
            assert not reply["actions"]
        else:
            assert "not in this version yet; " in reply["body_md"]
