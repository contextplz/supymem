import json
from datetime import timedelta

import pytest

from tests.unit.product.test_device_browser_ai import configure
from tests.unit.product.test_device_order_api import api, headers
from tests.unit.product.test_device_orders import orders


@pytest.mark.parametrize("goal", [
    "Use my Mac to check https://example.test/pricing and report the prices.",
    "Use my browser to compare https://example.test/features with https://second.test/features.",
    "Computer use: visit https://example.test and report its heading.",
    "Go to https://example.test/signup and prepare the signup form without submitting.",
])
def test_browser_routing_uses_server_profile_with_native_off(orders, monkeypatch, goal):
    svc, caller, _ = orders
    from app.build import device_browser_ai

    profile = configure(monkeypatch)
    monkeypatch.setenv("DEVICE_DESKTOP_EXECUTION_ENABLED", "0")
    monkeypatch.setattr(svc, "_current_open_task", lambda *args, **kwargs: "task_one")
    assert svc.classify_device_chat_intent(goal) == "general_browser"
    saved = svc.prepare_from_chat("v_one", caller, goal)
    assert saved["state"] == "pending" and not saved.get("claimed_at")
    assert saved["manifest"]["credit_ceiling_micro"] == 500000
    descriptor = saved["manifest"]["general_browser_verification"]
    assert descriptor["execution_profile"] == profile.model_dump()
    assert device_browser_ai.profile_digest(descriptor["execution_profile"]) == device_browser_ai.available_profile("v_one", caller)["execution_profile_digest"]
    assert "desktop_verification" not in saved["manifest"]


def test_client_digest_cannot_substitute_profile_and_missing_digest_works(api, monkeypatch):
    client, (svc, _, _) = api
    profile = configure(monkeypatch)
    monkeypatch.setenv("DEVICE_DESKTOP_EXECUTION_ENABLED", "0")
    monkeypatch.setattr(svc, "_current_open_task", lambda *args, **kwargs: "task_one")
    path = "/ventures/v_one/device-work-orders/from-chat"
    goal = "Use my browser to visit https://example.test/pricing and read only: summarize prices."
    bad = client.post(path, json={"instruction": goal, "browser_execution_profile_digest": "f" * 64}, headers=headers(path))
    assert bad.status_code == 409
    result = client.post(path, json={"instruction": goal}, headers=headers(path))
    assert result.status_code == 200, result.text
    assert result.json()["manifest"]["general_browser_verification"]["execution_profile"] == profile.model_dump()
    assert result.json()["manifest"]["actions"] == ["browser.read"]


def test_pending_review_has_24_hours_and_hard_expiry(orders, monkeypatch):
    svc, caller, manifest = orders
    now = svc.utcnow()
    monkeypatch.setattr(svc, "utcnow", lambda: now)
    saved = svc.prepare("v_one", caller, manifest)
    assert saved["expires_at"] == (now + timedelta(hours=24)).isoformat()
    monkeypatch.setattr(svc, "utcnow", lambda: now + timedelta(hours=24))
    with pytest.raises(ValueError, match="device_order_expired"):
        svc.transition("v_one", caller, saved["id"], saved["review_digest"], "approve")


def test_browser_configuration_revalidated_at_approve_and_claim(orders, monkeypatch):
    svc, caller, _ = orders
    profile = configure(monkeypatch)
    monkeypatch.setenv("DEVICE_DESKTOP_EXECUTION_ENABLED", "0")
    monkeypatch.setattr(svc, "_current_open_task", lambda *args, **kwargs: "task_one")
    saved = svc.prepare_browser_task("v_one", caller, "Visit https://example.test and read only: report the title.")
    monkeypatch.setenv("DEVICE_BROWSER_CUAI_EXECUTION_ENABLED", "0")
    with pytest.raises(ValueError, match="configuration_unavailable"):
        svc.transition("v_one", caller, saved["id"], saved["review_digest"], "approve")
    monkeypatch.setenv("DEVICE_BROWSER_CUAI_EXECUTION_ENABLED", "1")
    from app.db.models import CreditAccount
    with svc.db()() as session:
        session.get(CreditAccount, "cra_device_test").granted_micro = 1000000
        session.commit()
    approved = svc.transition("v_one", caller, saved["id"], saved["review_digest"], "approve")
    monkeypatch.setenv("DEVICE_BROWSER_CUAI_PROFILE_JSON", json.dumps({**profile.model_dump(), "max_provider_calls": 7}))
    with pytest.raises(ValueError, match="configuration_unavailable"):
        svc.transition("v_one", caller, saved["id"], approved["review_digest"], "claim")


def test_pending_browser_review_renewal_keeps_profile_and_requires_fresh_review(api, monkeypatch):
    client, (svc, _, _) = api
    profile = configure(monkeypatch)
    monkeypatch.setenv("DEVICE_DESKTOP_EXECUTION_ENABLED", "0")
    monkeypatch.setattr(svc, "_current_open_task", lambda *args, **kwargs: "task_one")
    base = "/ventures/v_one/device-work-orders/from-chat"
    created = client.post(base, json={"instruction": "On my Mac, visit https://example.test/pricing and read only: summarize prices."}, headers=headers(base))
    assert created.status_code == 200, created.text
    order = created.json()
    from app.db.models import VentureDocument
    with svc.db()() as session:
        row = session.get(VentureDocument, order["id"])
        body = json.loads(row.provenance_json)
        body["expires_at"] = "2000-01-01T00:00:00"
        row.provenance_json = json.dumps(body)
        session.commit()
    path = f"/ventures/v_one/device-work-orders/{order['id']}/renew"
    result = client.post(path, headers=headers(path))
    assert result.status_code == 200, result.text
    assert result.json()["state"] == "pending" and result.json()["id"] != order["id"]
    assert result.json()["manifest"]["general_browser_verification"]["execution_profile"] == profile.model_dump()


def test_private_browser_review_renewal_keeps_conversation_and_reusable_request(api, monkeypatch):
    from contextlib import contextmanager
    client, (svc, _, _) = api
    from app.chat import conversations, store
    profile = configure(monkeypatch)
    old = {"state": "pending", "expires_at": "2000-01-01T00:00:00", "claimed_at": None,
        "manifest": {"general_browser_verification": {"execution_profile": profile.model_dump()}},
        "browser_chat_scope": {"conversation_id": "cnv_private"}}
    monkeypatch.setattr(svc, "retrieve", lambda *args: old)
    lookups, saves, binds = [], [], []
    def saved_request(*args, **kwargs):
        lookups.append(kwargs)
        return {"body_md": "Visit https://example.test and read only: report prices."}
    monkeypatch.setattr(store, "idempotent_message", saved_request)
    monkeypatch.setattr(store, "append_message", lambda *args, **kwargs: saves.append(kwargs))
    @contextmanager
    def private(*args):
        binds.append(args)
        yield
    monkeypatch.setattr(conversations, "bind_conversation", private)
    def prepare(vid, caller, goal, **kwargs):
        assert kwargs == {"conversation_id": "cnv_private"}
        return {"id": "dwo_renewed", "venture_id": "v_one", "state": "pending", "review_path": "/v/v_one/device-work-orders/dwo_renewed"}
    monkeypatch.setattr(svc, "prepare_browser_task", prepare)
    path = "/ventures/v_one/device-work-orders/dwo_private/renew"
    result = client.post(path, headers=headers(path))
    assert result.status_code == 200, result.text
    assert lookups == [{"conversation_id": "cnv_private"}]
    assert binds == [("v_one", "cnv_private")]
    assert saves[0]["conversation_id"] == "cnv_private"
    assert saves[0]["idempotency_key"] == "mac-review:dwo_renewed:user"
