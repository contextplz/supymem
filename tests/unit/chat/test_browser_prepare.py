"""The deployment pin is the gate; tool/model arguments cannot qualify a profile."""
import asyncio

import pytest

from tests.company_os_harness import load_company_os_app
from tests.unit.chat.test_assistant_routing import env


def test_optional_tool_is_off_without_both_operator_flag_and_sha_pin(monkeypatch):
    load_company_os_app()
    from app.tools import tool_specs
    from app.chat.browser_prepare import FLAG, PIN
    for flag, pin, present in [(None, None, False), ("1", None, False), ("1", "model-approved", False),
                               ("0", "a" * 64, False), ("1", "a" * 64, True)]:
        for name, value in [(FLAG, flag), (PIN, pin)]:
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        specs = {spec["function"]["name"]: spec for spec in tool_specs()}
        assert ("prepare_browser_review" in specs) is present
        if present:
            spec = specs["prepare_browser_review"]["function"]
            assert "read and interact" in spec["description"]
            assert spec["parameters"]["additionalProperties"] is False
            assert set(spec["parameters"]["properties"]) == {"goal"}


def test_caller_context_retires_and_cannot_be_supplied_as_tool_arguments(monkeypatch):
    load_company_os_app()
    from app.chat.browser_prepare import _CALLER, bind_caller, FLAG, PIN
    from app.tools import ToolContext, execute_tool, validate_args
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(PIN, "a" * 64)
    with bind_caller("trusted-context-fixture"):
        assert _CALLER.get() == "trusted-context-fixture"
    assert _CALLER.get() is None
    with pytest.raises(ValueError):
        validate_args("prepare_browser_review", {"goal": "Open https://example.test/", "device_id": "dev_other"})
    ctx = ToolContext("v_one", "w_one")
    assert asyncio.run(execute_tool("prepare_browser_review", {"goal": "Open https://example.test/"}, ctx)) == "error: browser_preparation_unavailable"
    assert ctx.actions == []


def test_unconfirmed_write_never_emits_a_false_completed_review(monkeypatch):
    load_company_os_app()
    from app.chat import browser_prepare
    from app.tools import ToolContext
    def lost_ack(_, ctx):
        ctx.actions.append({"href": "/unconfirmed"})
        raise OSError("fixture acknowledgment lost after pending order save")
    monkeypatch.setattr(browser_prepare, "_prepare", lost_ack)
    ctx = ToolContext("v_one", "w_one")
    assert browser_prepare.prepare("Open https://example.test/", ctx) == "error: browser_preparation_unconfirmed"
    assert ctx.actions == []


def test_production_flags_route_browser_request_without_optional_tool_pin(monkeypatch):
    load_company_os_app()
    from app.chat import browser_prepare
    monkeypatch.delenv(browser_prepare.FLAG, raising=False)
    monkeypatch.delenv(browser_prepare.PIN, raising=False)
    monkeypatch.setenv("DEVICE_DESKTOP_EXECUTION_ENABLED", "0")
    monkeypatch.setenv("DEVICE_BROWSER_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("DEVICE_BROWSER_CUAI_EXECUTION_ENABLED", "1")
    for text in ("Use my Mac to check https://example.test/pricing", "Use my browser to compare prices", "Computer use", "On my Mac, go to https://example.test and report its heading"):
        assert browser_prepare.explicit_browser_request(text)
    for text in ('The note says "Use my browser to check prices"', "Don't use my Mac to check prices", "Open Notes and type hello"):
        assert not browser_prepare.explicit_browser_request(text)


def test_founder_browser_review_asks_for_site_and_keeps_mac_private_scope(monkeypatch):
    from types import SimpleNamespace
    load_company_os_app()
    from app.chat import browser_prepare
    from app.tools import ToolContext
    from app.build import device_orders
    caller = SimpleNamespace(legacy=False, role="user", device_id="dev_one", sub="founder")
    conversation = SimpleNamespace(id="cnv_one", visibility="private", venture_id="v_one", workspace_id="w_one", actor_id="founder")
    monkeypatch.setattr(browser_prepare, "subject_person_id", lambda sub: sub)
    monkeypatch.setattr(browser_prepare, "current_conversation", lambda: conversation)
    monkeypatch.setattr(browser_prepare, "resolve", lambda *args: conversation)
    monkeypatch.setattr("app.chat.link_context._safe_host", lambda host: host == "stripe.com")
    monkeypatch.setattr("app.chat.store.append_message", lambda *args, **kwargs: None)
    calls = []
    def prepare(vid, owner, goal, **kwargs):
        calls.append((vid, owner, goal, kwargs))
        return {"id": "dwo_test", "review_path": "/v/v_one/device-work-orders/dwo_test"}
    monkeypatch.setattr(device_orders, "prepare_browser_task", prepare)
    with browser_prepare.bind_caller(caller):
        ctx = ToolContext("v_one", "w_one")
        assert "Which website" in browser_prepare.prepare_founder_review("Use my browser to compare prices", ctx)
        assert calls == [] and ctx.actions == []
        assert "Mac app only" in browser_prepare.prepare_founder_review("Use my browser to check https://stripe.com/pricing", ctx)
        assert ctx.actions[0]["label"] == "Review browser task"
        assert calls[0][3] == {"conversation_id": "cnv_one"}
    with browser_prepare.bind_caller(None):
        assert "Mac app" in browser_prepare.prepare_founder_review("Computer use", ToolContext("v_one", "w_one"))


def test_http_chat_routes_browser_requests_without_native_or_model_dispatch(env, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    client, vid = env
    monkeypatch.setenv("DEVICE_DESKTOP_EXECUTION_ENABLED", "0")
    monkeypatch.setenv("DEVICE_BROWSER_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("DEVICE_BROWSER_CUAI_EXECUTION_ENABLED", "1")
    from app.chat import browser_prepare, llm_router
    calls = []
    def prepare(goal, ctx):
        calls.append(goal)
        ctx.actions.append({"label": "Review browser task", "href": f"/v/{vid}/device-work-orders/dwo_pending", "style": "primary"})
        return "Your browser task is ready for review. It runs in the Mac app only after approval and Start."
    async def forbidden(*args, **kwargs):
        pytest.fail("browser review must not invoke a routing model")
    monkeypatch.setattr(browser_prepare, "prepare_founder_review", prepare)
    monkeypatch.setattr(llm_router, "route", forbidden)
    for goal in ("Use my Mac to check https://stripe.com/pricing", "Use my browser to compare pricing", "Computer use", "On my Mac, go to https://stripe.com and report the heading"):
        result = client.post(f"/ventures/{vid}/chat", json={"message": goal})
        assert result.status_code == 200, result.text
        assert result.json()["kind"] == "confirmation"
        assert result.json()["actions"][0]["label"] == "Review browser task"
        assert "coming soon" not in result.json()["body_md"]
    assert len(calls) == 4
