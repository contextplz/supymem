"""A screenshot review remains metered, scoped, and observation-only."""
import base64
import json
from io import BytesIO

import pytest
from PIL import Image

from tests.unit.product.test_device_orders import orders  # noqa: F401


@pytest.fixture(autouse=True)
def local_vision_settings(orders, monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    from app.build import device_vision
    from app.core import providers_local
    from app.core.local_secrets import update_settings
    from app.core.local_mode import is_local

    if is_local():
        for name in ("SUPYMEM_LLM_PROVIDER", *providers_local.PROVIDER_ENV["openai"],
                     *(f"SUPYMEM_MODEL_{role.upper()}" for role in providers_local.ROLES)):
            monkeypatch.delenv(name, raising=False)
        providers_local.save_settings(provider="openai", api_key="synthetic-test-key",
                                      models={"chat": device_vision.MODEL, "vision": device_vision.MODEL})
        update_settings("capabilities", {"background_work": True}, {})


def _png() -> str:
    image = Image.new("RGB", (120, 80), "white")
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return base64.b64encode(buffer.getvalue()).decode()


@pytest.fixture
def vision_order(orders, monkeypatch):  # noqa: F811
    svc, caller, manifest = orders
    from app.build import device_vision
    from app.db.models import CreditAccount

    with svc.db()() as session:
        account = session.get(CreditAccount, "cra_device_test")
        account.granted_micro = 1_000_000
        session.commit()
    desktop = svc.Manifest.model_validate({**manifest.model_dump(exclude_none=True),
        "applications": ["com.apple.Calculator"], "origins": [],
        "actions": ["desktop.read", "desktop.interact"],
        "credit_ceiling_micro": 500_000,
        "desktop_verification": {"operation": "desktop_goal_v1",
            "application_bundle": "com.apple.Calculator", "process_id": 123,
            "process_start_identity": "123456", "window_id": 10,
            "window_title": "Calculator", "max_steps": 4,
            "founder_goal": "Report the calculator display",
            "reviewed_text": [], "visual_review": True}})
    order = svc.prepare("v_one", caller, desktop)
    for action in ("approve", "claim"):
        svc.transition("v_one", caller, order["id"], order["review_digest"], action)
    monkeypatch.setenv("OPENAI_DEVICE_VISION_API_KEY", "synthetic-test-key")
    sent = []

    def provider(body, *, config=None):
        sent.append(body)
        saved = svc.retrieve("v_one", caller, order["id"])
        assert saved["invocations"]["vision_one"]["state"] == "calling"
        return {"model": device_vision.MODEL,
                "usage": {"input_tokens": 1000, "output_tokens": 100},
                "output": [{"content": [{"type": "output_text", "text": json.dumps({
                    "verdict": "done", "visible_facts": ["The display reads 227"],
                    "next_step": "", "click_control_id": "none"})}]}]}

    monkeypatch.setattr(device_vision, "_provider_call", provider)
    return svc, caller, order, device_vision, sent


def test_vision_meters_once_and_never_grants_effect_authority(vision_order):
    svc, caller, order, vision, sent = vision_order
    args = ("v_one", caller, order["id"])
    result = vision.review_desktop(*args, invocation_id="vision_one",
                                   snapshot_id="ps1_abc", screenshot_png_base64=_png())
    assert result["authority"] == "observation_only"
    assert result["report"]["visible_facts"] == ["The display reads 227"]
    assert sent[0]["store"] is False
    assert "does not certify that any prior click" in sent[0]["input"][0]["content"][0]["text"]
    assert "For an action-then-report goal" in sent[0]["input"][0]["content"][0]["text"]
    assert sent[0]["input"][1]["content"][3]["type"] == "input_image"
    assert sent[0]["input"][1]["content"][0]["text"] == (
        "Approved goal: Report the calculator display")
    receipt = svc.retrieve(*args)["invocations"]["vision_one"]
    assert receipt["usage"] == {"model": vision.MODEL, "input_tokens": 1000, "output_tokens": 100}
    with pytest.raises(ValueError, match="already_attempted"):
        vision.review_desktop(*args, invocation_id="vision_one",
                              snapshot_id="ps1_abc", screenshot_png_base64=_png())
    assert len(sent) == 1


def test_vision_control_choice_must_match_offered_control(vision_order):
    _svc, _caller, _order, vision, _sent = vision_order
    from coding_agents.jev_decision import Control

    controls = [Control(id="c1", label="OK", operations=["click"], visible=True, enabled=True)]
    response = {"output": [{"content": [{"type": "output_text", "text": json.dumps({
        "verdict": "continue", "visible_facts": ["Dialog visible"],
        "next_step": "Click OK", "click_control_id": "c1"})}]}]}
    assert vision._review_text(response, controls)["click_control_id"] == "c1"
    response["output"][0]["content"][0]["text"] = response["output"][0]["content"][0]["text"].replace(
        '"c1"', '"c2"')
    with pytest.raises(ValueError, match="response_invalid"):
        vision._review_text(response, controls)


def test_visual_choice_creates_one_use_proposal_for_gate_invoke(vision_order, monkeypatch):
    svc, caller, order, vision, sent = vision_order
    from app.build import device_effects
    from coding_agents.computer_use_gate import ComputerUseGate
    from coding_agents.jev_decision import Control, Observation

    observation = Observation(revision="ps1_abc", application="com.apple.Calculator",
        location="mac-window:10", goal="Report the calculator display", context="",
        controls=[Control(id="c1", label="OK", operations=["click"], visible=True, enabled=True),
                  Control(id="c2", label="Delete", operations=["click"], visible=True, enabled=True)],
        permitted=True, confirmation_pending=False, prior_effect_uncertain=False)
    def provider(body, *, config=None):
        sent.append(body)
        return {"model": vision.MODEL, "usage": {"input_tokens": 1000, "output_tokens": 100},
            "output": [{"content": [{"type": "output_text", "text": json.dumps({
                "verdict": "continue", "visible_facts": ["OK is visible"],
                "next_step": "Click OK", "click_control_id": "c1"})}]}]}
    monkeypatch.setattr(vision, "_provider_call", provider)
    result = vision.review_desktop("v_one", caller, order["id"], invocation_id="vision_one",
        snapshot_id="ps1_abc", screenshot_png_base64=_png(), observation=observation)
    assert result["proposal_invocation_id"] == "vision_one"
    assert result["observation_digest"] == observation.digest()
    assert sent[0]["text"]["format"]["schema"]["properties"]["click_control_id"]["enum"] == ["none", "c1"]
    dispatched = []
    gate = ComputerUseGate(order_id=order["id"], session_id="session_1",
        driver=lambda args, *, session_id: dispatched.append((args, session_id)) or {"effect": "returned"},
        authorize=lambda *_args: True,
        admit_effect=lambda _order, _session, invocation, digest: device_effects.admit(
            "v_one", caller, order["id"], invocation_id=invocation,
            observation_digest=observation.digest(), effect_digest=digest))
    args = {"action": "click", "app": "com.apple.Calculator", "snapshot_id": "ps1_abc",
            "element_id": "elem_1"}
    assert gate.invoke("vision_one", args) == {"effect": "returned"}
    assert len(dispatched) == 1
    with pytest.raises(PermissionError, match="effect_not_admitted"):
        gate.invoke("vision_one", args)
    assert len(dispatched) == 1


def test_visual_choice_cannot_propose_consequential_label(vision_order, monkeypatch):
    _svc, caller, order, vision, sent = vision_order
    from coding_agents.jev_decision import Control, Observation

    observation = Observation(revision="ps1_abc", application="com.apple.Calculator",
        location="mac-window:10", goal="Report the calculator display", context="",
        controls=[Control(id="c1", label="Delete", operations=["click"], visible=True, enabled=True)],
        permitted=True, confirmation_pending=False, prior_effect_uncertain=False)
    def provider(body, *, config=None):
        sent.append(body)
        return {"model": vision.MODEL, "usage": {"input_tokens": 1000, "output_tokens": 100},
            "output": [{"content": [{"type": "output_text", "text": json.dumps({
                "verdict": "continue", "visible_facts": ["Delete is visible"],
                "next_step": "Click Delete", "click_control_id": "c1"})}]}]}
    monkeypatch.setattr(vision, "_provider_call", provider)
    with pytest.raises(ValueError, match="response_invalid"):
        vision.review_desktop("v_one", caller, order["id"], invocation_id="vision_one",
            snapshot_id="ps1_abc", screenshot_png_base64=_png(), observation=observation)
    assert sent[0]["text"]["format"]["schema"]["properties"]["click_control_id"]["enum"] == ["none"]
    assert "proposal" not in vision.orders.retrieve("v_one", caller, order["id"])["invocations"]["vision_one"]


def test_delete_is_eligible_only_for_reviewed_transient_input_edit(orders, monkeypatch):  # noqa: F811
    svc, caller, base = orders
    from app.build import device_vision as vision
    from app.db.models import CreditAccount
    from coding_agents.jev_decision import Control, Observation

    with svc.db()() as session:
        account = session.get(CreditAccount, "cra_device_test")
        account.granted_micro = 1_000_000
        session.commit()
    manifest = svc.Manifest.model_validate({**base.model_dump(exclude_none=True),
        "applications": ["com.apple.Calculator"], "origins": [],
        "actions": ["desktop.read", "desktop.interact"], "credit_ceiling_micro": 500_000,
        "desktop_verification": {"operation": "desktop_goal_v1",
            "application_bundle": "com.apple.Calculator", "process_id": 123,
            "process_start_identity": "123456", "window_id": 10,
            "window_title": "Calculator", "max_steps": 4,
            "founder_goal": "Clear the current input", "reviewed_text": [],
            "visual_review": True}})
    order = svc.prepare("v_one", caller, manifest)
    for action in ("approve", "claim"):
        svc.transition("v_one", caller, order["id"], order["review_digest"], action)
    monkeypatch.setenv("OPENAI_DEVICE_VISION_API_KEY", "synthetic-test-key")
    sent = []
    observation = Observation(revision="ps1_abc", application="com.apple.Calculator",
        location="mac-window:10", goal="Clear the current input", context="",
        controls=[Control(id="c1", label="Delete", operations=["click"],
                          visible=True, enabled=True)],
        permitted=True, confirmation_pending=False, prior_effect_uncertain=False)
    def provider(body, *, config=None):
        sent.append(body)
        return {"model": vision.MODEL, "usage": {"input_tokens": 1000, "output_tokens": 100},
            "output": [{"content": [{"type": "output_text", "text": json.dumps({
                "verdict": "continue", "visible_facts": ["Calculator keypad is visible"],
                "next_step": "Clear the input", "click_control_id": "c1",
                "input_edit_control_id": "c1"})}]}]}
    monkeypatch.setattr(vision, "_provider_call", provider)
    result = vision.review_desktop("v_one", caller, order["id"], invocation_id="edit_one",
        snapshot_id="ps1_abc", screenshot_png_base64=_png(), observation=observation)
    assert result["report"]["input_edit_control_id"] == "c1"
    assert result["proposal_invocation_id"] == "edit_one"
    assert sent[0]["text"]["format"]["schema"]["properties"]["input_edit_control_id"]["enum"] == ["none", "c1"]
    bad = {"output": [{"content": [{"type": "output_text", "text": json.dumps({
        "verdict": "continue", "visible_facts": [], "next_step": "Clear",
        "click_control_id": "c1", "input_edit_control_id": "none"})}]}]}
    with pytest.raises(ValueError, match="response_invalid"):
        vision._review_text(bad, observation.controls, observation.goal)


@pytest.mark.parametrize("bad", ["not-base64", base64.b64encode(b"not-a-png").decode()])
def test_invalid_image_does_not_spend(vision_order, bad):
    svc, caller, order, vision, sent = vision_order
    with pytest.raises(ValueError, match="image_invalid"):
        vision.review_desktop("v_one", caller, order["id"], invocation_id="vision_one",
                              snapshot_id="ps1_abc", screenshot_png_base64=bad)
    assert sent == []
    assert svc.retrieve("v_one", caller, order["id"]).get("invocations", {}) == {}


def test_unreviewed_order_cannot_send_image(vision_order):
    svc, caller, order, vision, sent = vision_order
    saved = svc.retrieve("v_one", caller, order["id"])
    saved["manifest"]["desktop_verification"]["visual_review"] = False
    original = vision.orders.retrieve
    vision.orders.retrieve = lambda *_: saved
    try:
        with pytest.raises(ValueError, match="scope_invalid"):
            vision.review_desktop("v_one", caller, order["id"], invocation_id="vision_one",
                                  snapshot_id="ps1_abc", screenshot_png_base64=_png())
    finally:
        vision.orders.retrieve = original
    assert sent == []


def test_invalid_provider_report_keeps_known_token_usage(vision_order, monkeypatch):
    svc, caller, order, vision, sent = vision_order
    monkeypatch.setattr(vision, "_provider_call", lambda _body, *, config=None: {
        "model": vision.MODEL, "usage": {"input_tokens": 1000, "output_tokens": 50},
        "output": [{"content": [{"type": "output_text", "text": "not json"}]}]})
    with pytest.raises(ValueError, match="response_invalid"):
        vision.review_desktop("v_one", caller, order["id"], invocation_id="vision_one",
                              snapshot_id="ps1_abc", screenshot_png_base64=_png())
    receipt = svc.retrieve("v_one", caller, order["id"])["invocations"]["vision_one"]
    assert receipt["state"] == "completed"
    assert receipt["usage"]["input_tokens"] == 1000
