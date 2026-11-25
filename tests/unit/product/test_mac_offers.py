"""Charlie's Mac-task offer: prepare-only, secrets stay founder-only, voice cannot prepare."""
import pytest
from tests.company_os_harness import load_company_os_app

from tests.unit.product.test_device_order_api import api, headers  # noqa: F401
from tests.unit.product.test_device_orders import orders  # noqa: F401,PLC0414


@pytest.fixture(autouse=True)
def _native_on(monkeypatch):
    monkeypatch.setenv("DEVICE_DESKTOP_EXECUTION_ENABLED", "1")


def test_no_offer_unless_native_gate_is_exactly_one(monkeypatch):
    from app.chat.mac_offers import cu_offer

    for value in (None, "", "0", "true"):
        if value is None:
            monkeypatch.delenv("DEVICE_DESKTOP_EXECUTION_ENABLED", raising=False)
        else:
            monkeypatch.setenv("DEVICE_DESKTOP_EXECUTION_ENABLED", value)
        assert cu_offer("v1", "cursor_open") is None

@pytest.fixture(autouse=True)
def _company_os():
    load_company_os_app()


PATH = "/ventures/v_one/device-work-orders/from-chat"


def test_mac_doable_blocker_gets_offer_and_goals_are_reviewable():
    from app.build.device_orders import classify_device_chat_intent
    from app.chat.mac_offers import GOALS, cu_offer

    for code in GOALS:
        offer = cu_offer("v1", code, {"url": "https://example.com/signup"})
        assert offer["label"] == "Let Supymem do it on your Mac"
        assert offer["action"] == "mac.task.prepare"
        assert offer["payload"]["goal"] == offer["text"]
        assert classify_device_chat_intent(offer["text"]) is not None, code
    assert cu_offer("v1", "signup_page", {"url": "http://x"}) is None


def test_missing_permissions_become_grant_offer():
    from app.chat.mac_offers import cu_offer

    offer = cu_offer("v1", "cursor_open", {"permissions": {"screen-recording": "denied", "accessibility": "granted"}})
    assert offer == {"label": "Grant Mac permissions", "href": "/v/v1/settings#computer-use", "style": "primary"}
    granted = {"screen-recording": "granted", "accessibility": "granted"}
    assert cu_offer("v1", "cursor_open", {"permissions": granted})["action"] == "mac.task.prepare"


@pytest.mark.parametrize("text,code", [("I need to enter my password", "password_required"),
                                       ("add a payment card", "payment_required"),
                                       ("enter the 2FA code", "two_factor_code")])
def test_secret_blockers_have_no_offer_and_founder_only_note(text, code):
    from app.chat.mac_offers import FOUNDER_ONLY, FOUNDER_ONLY_NOTE, code_from_text, cu_offer

    assert code_from_text(text) == code and code in FOUNDER_ONLY
    assert cu_offer("v1", code, {"url": "https://example.com"}) is None
    assert "yours to do" in FOUNDER_ONLY_NOTE


def test_charlie_prompt_mentions_mac_tasks_and_secrets():
    from app.org.manager import persona_instructions

    text = persona_instructions("Charlie")
    assert "requested Mac tasks" in text and "never handles secrets" in text


def test_prepare_creates_pending_review_and_does_not_run(api, monkeypatch):
    client, (service, _, manifest) = api
    from app.chat.mac_offers import cu_offer

    # Plan-task lookup is out of scope here: reuse the fixture manifest through the real prepare().
    monkeypatch.setattr(service, "prepare_from_chat",
                        lambda vid, caller, *_a: service.prepare(vid, caller, manifest))
    goal = cu_offer("v_one", "cursor_open")["text"]
    result = client.post(PATH, json={"instruction": goal}, headers=headers(PATH))
    assert result.status_code == 200, result.text
    order = result.json()
    assert order["state"] == "pending"
    assert not order.get("claimed_at")
    assert order["review_path"].startswith("/v/v_one/device-work-orders/")


def test_voice_origin_cannot_prepare(api):
    client, _ = api
    result = client.post(PATH, json={"instruction": "Inspect the venture workspace with Cursor, read-only.",
                                     "source": "voice"}, headers=headers(PATH))
    assert result.status_code == 403
    assert result.json()["detail"]["error"]["code"] == "voice_cannot_confirm"
