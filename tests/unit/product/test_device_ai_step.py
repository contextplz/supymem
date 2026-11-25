"""AI-loop model steps are metered once against the reviewed order, scoped to it, and proposal-only."""
import base64
import json
from io import BytesIO

import pytest
from PIL import Image

from tests.unit.product.test_device_orders import orders  # noqa: F401


@pytest.fixture(autouse=True)
def hosted_vision(monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)


def _png(color="white") -> str:
    buffer = BytesIO()
    Image.new("RGB", (120, 80), color).save(buffer, "PNG")
    return base64.b64encode(buffer.getvalue()).decode()


REPLY = json.dumps({"thought": "t", "memory": "m", "subgoal_done": False,
                    "actions": [{"op": "key", "element_id": "", "x": 0, "y": 0, "text": "", "chord": "cmd+s",
                                 "direction": "", "amount": 0, "app": "", "ms": 0, "summary": ""}]})


@pytest.fixture
def ai_order(orders, monkeypatch, request):  # noqa: F811
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    svc, caller, manifest = orders
    from app.build import device_ai_step
    from app.db.models import CreditAccount

    with svc.db()() as session:
        session.get(CreditAccount, "cra_device_test").granted_micro = 1_000_000
        session.commit()
    desktop = svc.Manifest.model_validate({**manifest.model_dump(exclude_none=True),
        "applications": ["com.apple.TextEdit"], "origins": [], "actions": ["desktop.read", "desktop.interact"],
        "credit_ceiling_micro": 500_000,
        "desktop_verification": {"operation": "desktop_goal_v1", "application_bundle": "com.apple.TextEdit",
            "process_id": 123, "process_start_identity": "123456", "window_id": 10, "window_title": "Doc",
            "max_steps": 4, "founder_goal": getattr(request, "param", "Write a note"),
            **({"visual_review": True} if hasattr(request, "param") else {}), "reviewed_text": []}})
    order = svc.prepare("v_one", caller, desktop)
    for action in ("approve", "claim"):
        svc.transition("v_one", caller, order["id"], order["review_digest"], action)
    sent = []
    original_openai = device_ai_step._openai

    def openai(body, *, config=None):
        if config is not None:
            return original_openai(body, config=config)
        sent.append(body)
        schema_name = body.get("text", {}).get("format", {}).get("name")
        text = {"mac_plan": '{"subgoals":["write","save"]}',
                "mac_verify": '{"passed":false,"reason":"not saved"}'}.get(schema_name, REPLY)
        return {"status": "completed", "model": device_ai_step.MODEL, "usage": {"input_tokens": 2000, "output_tokens": 120},
                "output": [{"content": [{"type": "output_text", "text": text}]}]}

    monkeypatch.setattr(device_ai_step, "_openai", openai)
    return svc, caller, order, device_ai_step, sent


CTX = {"subgoals": ["a"], "subgoal": 0, "memory": "", "history": [], "frontmost": "TextEdit",
       "window_title": "Doc", "elements": [{"id": "e1", "ax_role": "AXTextArea", "label": "x"}], "hint": ""}


@pytest.mark.parametrize("ai_order", ["Record the supplier assessment", "Record the dinner recipe"], indirect=True)
@pytest.mark.parametrize("kind", ["step", "plan", "verify", "review"])
def test_local_desktop_uses_settings_without_platform_fallback(ai_order, monkeypatch, kind):
    from secrets import token_urlsafe
    from types import SimpleNamespace

    from app.llm import founder_provider
    from app.core.local_secrets import update_settings
    from llm_gateway.provider_foundry import ProviderConfig
    from pydantic import SecretStr

    encode_json = json.dumps

    _service, caller, order, ai, sent = ai_order
    goal = order["manifest"]["desktop_verification"]["founder_goal"]
    model = next(value for value in sorted(ai.ALLOWED_MODELS)
        if value != ai.MODEL and value not in ai.loop.NO_NONE_EFFORT)
    config = ProviderConfig(provider="openai", base_url="", api_key=SecretStr(token_urlsafe()),
        deployment_pro=model, deployment_flash=model, source="founder_override")
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    update_settings("capabilities", {"background_work": True}, {})
    saved_config = SimpleNamespace(workspace_id=order["workspace_id"], provider=config.provider,
        base_url=None, model=model, api_key_ref="configured-ref")
    monkeypatch.setattr(founder_provider, "get_founder_config", lambda workspace_id: saved_config)

    def local_resolution(workspace_id):
        saved = founder_provider.get_founder_config(workspace_id)
        if saved is None:
            raise founder_provider.FounderLLMConfigError("Choose your vision model in Settings.")
        return founder_provider.ResolvedLLMConfig(workspace_id=saved.workspace_id, provider=saved.provider,
            base_url=saved.base_url, model=saved.model, api_key_ref=saved.api_key_ref, source="founder_override")

    monkeypatch.setattr(founder_provider, "resolve_llm_config", local_resolution)

    def resolved(workspace_id, *, resolved_config):
        assert workspace_id == order["workspace_id"] == resolved_config.workspace_id
        assert resolved_config.source == "founder_override" and resolved_config.model == model
        return config

    monkeypatch.setattr(founder_provider, "to_provider_config", resolved)
    monkeypatch.setattr("llm_gateway.provider_foundry.load_provider_config_from_env",
        lambda: pytest.fail("Local tasks must not resolve platform settings"))

    def local_step(endpoint, *, headers, json):
        from llm_gateway.provider_foundry import normalize_base_url

        body = json
        assert endpoint == normalize_base_url(config.provider, config.base_url).rstrip("/") + "/responses"
        authorization_matches = headers.get("Authorization") == "Bearer " + config.api_key.get_secret_value()
        assert authorization_matches
        assert body["model"] == model
        sent.append(body)
        text = (encode_json({"verdict": "continue", "visible_facts": [goal], "next_step": goal,
            "click_control_id": "none", "input_edit_control_id": "none"}) if kind == "review" else REPLY)
        value = {"status": "completed", "model": model, "usage": {"input_tokens": 2000, "output_tokens": 120},
            "output": [{"content": [{"type": "output_text", "text": text}]}]}
        return SimpleNamespace(status_code=200, content=b"{}", json=lambda: value)

    def local_text(system, user, max_tokens, *, config, workspace_id, kind):
        assert config.deployment_pro == model and workspace_id == order["workspace_id"]
        sent.append(user)
        return json.dumps({"subgoals": [goal]} if kind == "plan" else {"passed": False, "reason": goal}), model, 2000, 120

    monkeypatch.setattr(ai._client, "post", local_step)
    monkeypatch.setattr(ai, "_deepseek", local_text)
    if kind == "review":
        from app.build import device_vision

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            post = staticmethod(local_step)

        monkeypatch.setattr(device_vision.httpx, "Client", lambda **kwargs: Client())

    def run(invocation_id):
        if kind == "review":
            return device_vision.review_desktop("v_one", caller, order["id"], invocation_id=invocation_id,
                snapshot_id="ps1_local", screenshot_png_base64=_png())
        return ai.run("v_one", caller, order["id"], invocation_id=invocation_id, kind=kind,
            context={**CTX, "notes": goal}, image_base64=_png() if kind == "step" else None)

    result = run("local_route")
    if kind == "review":
        assert result["authority"] == "observation_only"
    else:
        assert result["authority"] == "proposal_only" and result["model"] == model
    assert len(sent) == 1
    monkeypatch.setattr(founder_provider, "get_founder_config", lambda workspace_id: None)
    with pytest.raises(ValueError, match="Settings"):
        run("missing_settings")
    from app.api import device_orders_api
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as rejected:
        device_orders_api.call(run, "missing_settings_api")
    assert rejected.value.status_code == 409
    assert "Settings" in rejected.value.detail["message"]
    assert len(sent) == 1
    monkeypatch.setattr(founder_provider, "get_founder_config", lambda workspace_id: saved_config)
    monkeypatch.setattr(founder_provider, "to_provider_config", lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match="Settings"):
        run("unavailable_settings_key")
    assert len(sent) == 1
    saved_config.provider = "anthropic"
    with pytest.raises(ValueError, match="Settings"):
        run("unsupported_vision_provider")
    assert len(sent) == 1


@pytest.mark.parametrize("ai_order", ["Record a supplier inspection", "Save a knitting pattern"], indirect=True)
def test_local_step_obeys_saved_monthly_cap_before_provider_io(ai_order, monkeypatch):
    from secrets import token_urlsafe

    from app.core import providers_local
    from app.core.local_secrets import update_settings
    from app.credits.errors import CreditsUnavailable

    svc, caller, order, ai, sent = ai_order
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    providers_local.save_settings(provider="openai", api_key=token_urlsafe(),
                                  models={"chat": ai.MODEL, "vision": ai.MODEL})
    update_settings("capabilities", {"background_work": True}, {})
    update_settings("spend", {"monthly_spend_cap": 0}, {})

    def provider(body, *, config=None):
        sent.append(body)
        return {"status": "completed", "model": ai.MODEL, "usage": {"input_tokens": 2000, "output_tokens": 120},
                "output": [{"content": [{"type": "output_text", "text": REPLY}]}]}

    monkeypatch.setattr(ai, "_openai", provider)
    with pytest.raises(CreditsUnavailable, match="monthly.*Keys and connections"):
        ai.run("v_one", caller, order["id"], invocation_id="capped_step", kind="step", context=CTX)
    assert sent == []
    receipt = svc.retrieve("v_one", caller, order["id"])["invocations"]["capped_step"]
    assert receipt["state"] == "completed" and receipt["actual_micro"] == 0


def test_step_is_metered_once_goal_comes_from_the_order_and_reply_is_proposal_only(ai_order):
    svc, caller, order, ai, sent = ai_order
    args = ("v_one", caller, order["id"])
    result = ai.run(*args, invocation_id="ai_one", kind="step", context=CTX, image_base64=_png())
    assert result["authority"] == "proposal_only" and "cmd+s" in result["reply"]
    text = sent[0]["input"][1]["content"][0]["text"]
    assert "TASK (founder approved): Write a note" in text and sent[0]["store"] is False
    receipt = svc.retrieve(*args)["invocations"]["ai_one"]
    assert receipt["usage"] == {"model": ai.MODEL, "input_tokens": 2000, "output_tokens": 120}
    with pytest.raises(ValueError, match="already_attempted"):
        ai.run(*args, invocation_id="ai_one", kind="step", context=CTX, image_base64=_png())
    assert len(sent) == 1


@pytest.mark.parametrize("kind", ["step", "plan", "verify"])
def test_replay_binding_and_quote_ignore_only_the_generated_clock(ai_order, monkeypatch, kind):
    svc, caller, order, ai, sent = ai_order
    monkeypatch.setattr("llm_gateway.provider_foundry.load_provider_config_from_env",
                        lambda: (_ for _ in ()).throw(ValueError("synthetic configuration absent")))
    clocks = ["NOW: Tue 2026-10-06 23:59 (UTC+0000)", "NOW: " + "Wednesday 2026-10-07 00:00 (UTC+0530)".ljust(55, "0")]
    monkeypatch.setattr(ai.loop, "now_line", lambda: clocks[0])
    quoted = []
    estimate = ai.estimate_llm_micro
    def quote(*args, **kwargs):
        quoted.append(kwargs["prompt_tokens"])
        return estimate(*args, **kwargs)
    monkeypatch.setattr(ai, "estimate_llm_micro", quote)
    context = {**CTX, "notes": "NOW: caller evidence\n" + "x" * 2570}
    args = ("v_one", caller, order["id"])
    ai.run(*args, invocation_id="clock_bound", kind=kind, context=context)
    receipt = svc.retrieve(*args)["invocations"]["clock_bound"]
    text = sent[0]["input"][1]["content"][0]["text"]
    if kind != "plan":
        assert clocks[0] in text and ai.QUOTE_CLOCK_LINE not in text
    monkeypatch.setattr(ai.loop, "now_line", lambda: clocks[1])
    with pytest.raises(ValueError, match="^device_ai_step_already_attempted$"):
        ai.run(*args, invocation_id="clock_bound", kind=kind, context=context)
    assert quoted[0] == quoted[1]
    assert len(sent) == 1
    assert svc.retrieve(*args)["invocations"]["clock_bound"] == receipt


@pytest.mark.parametrize("kind", ["step", "plan", "verify"])
def test_replay_keeps_exact_payload_goal_model_and_openai_route_binding(ai_order, monkeypatch, kind):
    svc, caller, order, ai, sent = ai_order
    monkeypatch.setattr("llm_gateway.provider_foundry.load_provider_config_from_env",
                        lambda: (_ for _ in ()).throw(ValueError("synthetic configuration absent")))
    # Equal quotes isolate the request digest from the separate price-conflict guard.
    monkeypatch.setattr(ai, "estimate_llm_micro", lambda *args, **kwargs: 10_000)
    monkeypatch.setattr(ai, "priority_tier", lambda: False)
    context = {**CTX, "future_field": {"value": 1}, "notes": "NOW: caller evidence"}
    image = _png() if kind == "step" else None
    args = ("v_one", caller, order["id"])
    ai.run(*args, invocation_id="payload_bound", kind=kind, context=context, image_base64=image)
    receipt = svc.retrieve(*args)["invocations"]["payload_bound"]
    scope = ai._scope
    changes = ["context", "goal", "model", "provider", "contract", "caller_clock_line"]
    if kind == "step":
        changes += ["image", "priority"]
    for change in changes:
        with monkeypatch.context() as patch:
            changed_context, changed_image = context, image
            if change == "context":
                changed_context = {**context, "future_field": {"value": 2}}
            elif change == "caller_clock_line":
                changed_context = {**context, "notes": "NOW: changed caller evidence"}
            elif change == "contract":
                changed_context = {**context, "action_contract_version": 2}
            elif change == "goal":
                patch.setattr(ai, "_scope", lambda *args: (*scope(*args)[:2], "Write a different note"))
            elif change == "model":
                patch.setattr(ai, "step_model", lambda: "gpt-6-luna")
            elif change == "provider":
                patch.setattr(ai, "ENDPOINT", "https://other-provider.example.invalid/v1/responses")
            elif change == "image":
                changed_image = _png("black")
            else:
                patch.setattr(ai, "priority_tier", lambda: True)
            with pytest.raises(ValueError, match="^device_invocation_conflict$"):
                ai.run(*args, invocation_id="payload_bound", kind=kind,
                       context=changed_context, image_base64=changed_image)
            assert len(sent) == 1
            assert svc.retrieve(*args)["invocations"]["payload_bound"] == receipt


def test_step_without_an_image_is_text_only_and_bad_images_or_contexts_are_rejected(ai_order):
    svc, caller, order, ai, sent = ai_order
    args = ("v_one", caller, order["id"])
    ai.run(*args, invocation_id="ai_text", kind="step", context=CTX)
    assert all(part["type"] != "input_image" for part in sent[0]["input"][1]["content"])
    with pytest.raises(ValueError):
        ai.run(*args, invocation_id="ai_bad", kind="step", context=CTX,
               image_base64=base64.b64encode(b"not a png").decode())
    with pytest.raises(ValueError):
        ai.run(*args, invocation_id="ai_ctx", kind="step", context={**CTX, "elements": "nope"})
    with pytest.raises(ValueError):
        ai.run(*args, invocation_id="ai_kind", kind="click", context=CTX)


def test_plan_uses_platform_llm_and_missing_configuration_uses_openai_text(ai_order, monkeypatch):
    svc, caller, order, ai, sent = ai_order
    args = ("v_one", caller, order["id"])
    monkeypatch.setattr(ai, "_deepseek", lambda system, user, max_tokens, **kwargs: (
        '{"subgoals": ["write", "save"]}', "DeepSeek-V4-Pro", 300, 40))
    out = ai.run(*args, invocation_id="ai_plan", kind="plan", context={"reason": "start"})
    assert json.loads(out["text"])["subgoals"] == ["write", "save"]
    assert svc.retrieve(*args)["invocations"]["ai_plan"]["usage"]["model"] == "DeepSeek-V4-Pro"

    from llm_gateway import provider_foundry
    monkeypatch.setattr(provider_foundry, "load_provider_config_from_env",
                        lambda: (_ for _ in ()).throw(ValueError("missing configuration")))
    out = ai.run(*args, invocation_id="ai_verify", kind="verify", context={"claim": "saved", "elements": []})
    assert out["model"] == ai.MODEL      # same text-only question answered by the existing OpenAI key
    assert sent[-1]["text"]["format"]["schema"] == ai.loop.VERDICT_SCHEMA
    assert json.loads(out["text"])["passed"] is False


@pytest.mark.parametrize("kind", ["plan", "verify"])
def test_uncertain_platform_request_retains_admission_without_second_provider(ai_order, monkeypatch, kind):
    from llm_gateway import provider_foundry
    svc, caller, order, ai, sent = ai_order
    monkeypatch.setattr(provider_foundry, "load_provider_config_from_env",
                        lambda: provider_foundry.ProviderConfig(source="platform_default"))
    admitted_requests = []
    def timed_out_after_dispatch(*args, **kwargs):
        admitted_requests.append(args)
        raise TimeoutError("provider may have accepted request")
    monkeypatch.setattr(ai, "_deepseek", timed_out_after_dispatch)
    args = ("v_one", caller, order["id"])
    with pytest.raises(ValueError, match="device_ai_step_provider_or_usage_unconfirmed"):
        ai.run(*args, invocation_id="ambiguous_" + kind, kind=kind, context={"elements": []})
    receipt = svc.retrieve(*args)["invocations"]["ambiguous_" + kind]
    assert len(admitted_requests) == 1 and sent == []
    assert receipt["state"] == "uncertain" and "actual_micro" not in receipt
    with pytest.raises(ValueError, match="already_attempted"):
        ai.run(*args, invocation_id="ambiguous_" + kind, kind=kind, context={"elements": []})
    assert len(admitted_requests) == 1 and sent == []


def test_step_requires_a_claimed_desktop_order(ai_order):
    svc, caller, order, ai, sent = ai_order
    svc.transition("v_one", caller, order["id"], order["review_digest"], "cancel")
    with pytest.raises(ValueError):
        ai.run("v_one", caller, order["id"], invocation_id="ai_late", kind="step", context=CTX)
    assert sent == []


@pytest.fixture
def platform_sdk(ai_order, monkeypatch):
    from openai import AsyncOpenAI, _base_client as sdk_base

    httpx = getattr(sdk_base, "httpx2", None) or sdk_base.httpx
    from pydantic import SecretStr
    from llm_gateway import provider_foundry, sole
    from app.llm.pricing import record_usage

    config = provider_foundry.ProviderConfig(source="platform_default", provider="azure_foundry",
        base_url="https://owned-platform.example.invalid", api_key=SecretStr("synthetic-platform-key"),
        deployment_pro="DeepSeek-V4-Pro", deployment_flash="other-flash-model", reasoning_effort="high")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-platform-key")
    monkeypatch.setattr(provider_foundry, "load_provider_config_from_env", lambda: config)
    monkeypatch.setattr(sole, "_primary_chat", None)
    monkeypatch.setattr(sole, "_fallback_chat", lambda *args, **kwargs: pytest.fail("No provider fallback is permitted"))
    monkeypatch.setattr(sole, "_workspace_resolver", lambda *_: pytest.fail("Native platform route cannot use ambient BYOK"))
    wire = {"requests": [], "clients": [], "usage": {"prompt_tokens": 300, "completion_tokens": 40,
            "total_tokens": 340}, "status": 200, "timeout": False, "text": '{"subgoals":["Read title"]}', "notes": []}
    def usage_recorder(workspace_id, **kwargs):
        wire["notes"].append((workspace_id, kwargs))
        return record_usage(workspace_id, **kwargs)
    monkeypatch.setattr(sole, "_usage_recorder", usage_recorder)

    def handle(request):
        wire["requests"].append(request)
        if wire["timeout"]:
            raise httpx.ReadTimeout("synthetic response uncertainty", request=request)
        if wire["status"] != 200:
            return httpx.Response(wire["status"], json={"error": {"message": "synthetic failure", "type": "server_error"}})
        return httpx.Response(200, json={"id": "chatcmpl_owned", "object": "chat.completion", "created": 1,
            "model": json.loads(request.content)["model"], "usage": wire["usage"],
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": wire["text"]}}]})

    real_http_client = provider_foundry.DefaultAsyncHttpxClient
    monkeypatch.setattr(provider_foundry, "DefaultAsyncHttpxClient", lambda **kwargs:
        real_http_client(**{**kwargs, "transport": httpx.MockTransport(handle), "trust_env": False}))

    def client(**kwargs):
        # The real provider factory supplies its configured no-redirect client.
        # Keep that boundary and mock only the transport, without duplicate kwargs.
        value = AsyncOpenAI(**kwargs)
        wire["clients"].append(value)
        return value
    monkeypatch.setattr(provider_foundry, "AsyncOpenAI", client)
    return ai_order, config, wire


@pytest.mark.parametrize("kind", ["plan", "verify"])
def test_real_sdk_platform_route_is_pinned_and_usage_hook_does_not_bill_twice(platform_sdk, monkeypatch, kind):
    from app.build import device_stop
    from app.db.models import CreditAccount, CreditReservation
    (svc, caller, order, ai, sent), config, wire = platform_sdk
    args = ("v_one", caller, order["id"])
    original = ai.calls.admit
    def admit_then_change_environment(*args, **kwargs):
        result = original(*args, **kwargs)
        config.deployment_pro = "different-after-quote"
        config.base_url = "https://different-after-quote.example.invalid"
        return result
    monkeypatch.setattr(ai.calls, "admit", admit_then_change_environment)
    wire["text"] = "Known paid malformed answer"  # Text interpretation cannot erase actual usage.
    result = ai.run(*args, invocation_id="sdk_known", kind=kind, context={"elements": []})
    assert result["authority"] == "proposal_only" and result["text"] == wire["text"]
    assert result["model"] == "DeepSeek-V4-Pro" and (result["input_tokens"], result["output_tokens"]) == (300, 40)
    assert len(wire["requests"]) == 1 and sent == []
    request = wire["requests"][0]
    body = json.loads(request.content)
    assert request.url.host == "owned-platform.example.invalid" and request.url.path == "/openai/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer synthetic-platform-key"
    assert body["model"] == "DeepSeek-V4-Pro" and body["max_tokens"] == 700
    assert "extra_body" not in body and "reasoning_effort" not in body and "thinking" not in body and "tools" not in body
    user_messages = [message for message in body["messages"] if message["role"] == "user"]
    assert len(user_messages) == 1 and "Write a note" in user_messages[0]["content"]
    clock = [message for message in body["messages"] if message["role"] == "system" and message["content"].startswith("Now (server clock):")]
    assert len(clock) == 1 and "founder timezone:" in clock[0]["content"]
    assert all(value == 30 for value in request.extensions["timeout"].values())
    assert all(client.is_closed() for client in wire["clients"])
    assert len(wire["notes"]) == 1 and wire["notes"][0][0] == order["workspace_id"]
    assert wire["notes"][0][1]["purpose"] == "device.ai_step." + kind
    assert wire["notes"][0][1]["model"] == "DeepSeek-V4-Pro"
    assert wire["notes"][0][1]["prompt_tokens"] == 300 and wire["notes"][0][1]["completion_tokens"] == 40
    saved = svc.retrieve(*args)
    call = saved["invocations"]["sdk_known"]
    assert call["state"] == "completed" and call["provider_actual_micro"] == call["actual_micro"] > 0
    assert call["usage"] == {"model": "DeepSeek-V4-Pro", "input_tokens": 300, "output_tokens": 40}
    with svc.db()() as session:
        account = session.get(CreditAccount, "cra_device_test")
        assert account.spent_micro == 0 and account.reserved_micro == 500_000
        assert session.query(CreditReservation).filter_by(request_id=order["id"]).count() == 1
    with pytest.raises(ValueError):
        ai.run(*args, invocation_id="sdk_known", kind=kind, context={"elements": []})
    assert len(wire["requests"]) == len(wire["clients"]) == 1 and sent == []
    assert svc.retrieve(*args)["invocations"]["sdk_known"] == call
    device_stop.record(*args, execution_id=saved["execution_id"], evidence_digest="b" * 64,
                       processes_stopped=True, task_verified=False)
    with svc.db()() as session:
        account = session.get(CreditAccount, "cra_device_test")
        assert account.spent_micro == call["actual_micro"] and account.reserved_micro == 0


@pytest.mark.parametrize("kind", ["plan", "verify"])
def test_replay_binds_selected_nonsecret_platform_config(platform_sdk, monkeypatch, kind):
    (svc, caller, order, ai, sent), config, wire = platform_sdk
    monkeypatch.setattr(ai, "estimate_llm_micro", lambda *args, **kwargs: 10_000)
    args = ("v_one", caller, order["id"])
    ai.run(*args, invocation_id="route_bound", kind=kind, context={"elements": []})
    receipt = svc.retrieve(*args)["invocations"]["route_bound"]
    for field, value in [("provider", "openai_compatible"),
                         ("base_url", "https://different-provider.example.invalid"),
                         ("deployment_pro", "different-model"), ("source", "founder_override")]:
        original = getattr(config, field)
        setattr(config, field, value)
        try:
            with pytest.raises(ValueError, match="^device_invocation_conflict$"):
                ai.run(*args, invocation_id="route_bound", kind=kind, context={"elements": []})
        finally:
            setattr(config, field, original)
        assert len(wire["requests"]) == len(wire["clients"]) == 1 and sent == []
        assert svc.retrieve(*args)["invocations"]["route_bound"] == receipt


@pytest.mark.parametrize("kind", ["plan", "verify"])
@pytest.mark.parametrize("failure", ["timeout", "status", "missing", "bool", "total", "negative", "over_cap"])
def test_real_sdk_unknown_usage_or_transport_protects_hold_without_retry(platform_sdk, kind, failure):
    from app.db.models import CreditAccount
    (svc, caller, order, ai, sent), _config, wire = platform_sdk
    if failure == "timeout":
        wire["timeout"] = True
    elif failure == "status":
        wire["status"] = 503
    elif failure == "missing":
        wire["usage"] = None
    elif failure == "bool":
        wire["usage"]["prompt_tokens"] = True
    elif failure == "total":
        wire["usage"]["total_tokens"] = 339
    elif failure == "negative":
        wire["usage"].update(prompt_tokens=-1, total_tokens=39)
    else:
        wire["usage"].update(completion_tokens=701, total_tokens=1001)
    args = ("v_one", caller, order["id"])
    with pytest.raises(ValueError, match="^device_ai_step_provider_or_usage_unconfirmed$"):
        ai.run(*args, invocation_id="sdk_unknown", kind=kind, context={"elements": []})
    saved = svc.retrieve(*args)
    call = saved["invocations"]["sdk_unknown"]
    assert call["state"] == "uncertain" and "actual_micro" not in call and call["quote_micro"] > 0
    with svc.db()() as session:
        account = session.get(CreditAccount, "cra_device_test")
        assert account.spent_micro == 0 and account.reserved_micro == 500_000
    with pytest.raises(ValueError, match="already_attempted"):
        ai.run(*args, invocation_id="sdk_unknown", kind=kind, context={"elements": []})
    assert len(wire["requests"]) == len(wire["clients"]) == 1 and sent == []
    assert all(client.is_closed() for client in wire["clients"])


@pytest.mark.parametrize("kind", ["step", "verify"])
def test_refusal_records_known_usage_but_never_returns_provider_content(ai_order, monkeypatch, kind):
    svc, caller, order, ai, _ = ai_order
    from llm_gateway import provider_foundry
    monkeypatch.setattr(provider_foundry, "load_provider_config_from_env",
                        lambda: (_ for _ in ()).throw(ValueError("missing configuration")))
    monkeypatch.setattr(ai, "_openai", lambda body: {
        "status": "completed", "model": ai.MODEL, "usage": {"input_tokens": 2000, "output_tokens": 120},
        "output": [{"content": [{"type": "refusal", "refusal": "PRIVATE provider refusal"}]}]})
    args = ("v_one", caller, order["id"])
    with pytest.raises(ValueError, match="^openai_response_refused$"):
        ai.run(*args, invocation_id="refusal_" + kind, kind=kind, context=CTX)
    receipt = svc.retrieve(*args)["invocations"]["refusal_" + kind]
    assert receipt["usage"]["output_tokens"] == 120
    assert "PRIVATE" not in json.dumps(receipt)

@pytest.mark.parametrize('version', [None, 2, 3])
def test_step_negotiates_schema_without_changing_reviewed_authority(ai_order, version):
    svc, caller, order, ai, sent = ai_order
    context = dict(CTX)
    if version is not None:
        context['action_contract_version'] = version
    result = ai.run('v_one', caller, order['id'], invocation_id='schema_version', kind='step', context=context)
    props = sent[0]['text']['format']['schema']['properties']['actions']['items']['properties']
    assert ('path' in props) == (version in {2, 3})
    assert ('new_tab' in props['op']['enum']) == (version == 3)
    assert result['authority'] == 'proposal_only'


def test_unknown_action_contract_version_does_not_admit_or_call_provider(ai_order):
    svc, caller, order, ai, sent = ai_order
    with pytest.raises(ValueError, match='action_contract_version_invalid'):
        ai.run('v_one', caller, order['id'], invocation_id='unknown_schema', kind='step',
               context={**CTX, 'action_contract_version': 999})
    assert sent == []
    assert 'unknown_schema' not in svc.retrieve('v_one', caller, order['id']).get('invocations', {})


def test_same_invocation_changed_contract_cannot_reuse_binding_or_charge_twice(ai_order):
    svc, caller, order, ai, sent = ai_order
    args = ('v_one', caller, order['id'])
    ai.run(*args, invocation_id='bound_schema', kind='step', context=CTX)
    original = svc.retrieve(*args)['invocations']['bound_schema']
    with pytest.raises(ValueError):
        ai.run(*args, invocation_id='bound_schema', kind='step', context={**CTX, 'action_contract_version': 2})
    assert len(sent) == 1
    assert svc.retrieve(*args)['invocations']['bound_schema'] == original


@pytest.mark.parametrize('kind', ['plan', 'verify'])
def test_text_calls_reject_unknown_contract_before_admission(ai_order, kind):
    svc, caller, order, ai, sent = ai_order
    with pytest.raises(ValueError, match='action_contract_version_invalid'):
        ai.run('v_one', caller, order['id'], invocation_id='invalid_text_version', kind=kind,
               context={'action_contract_version': 999})
    assert sent == []
    assert 'invalid_text_version' not in svc.retrieve('v_one', caller, order['id']).get('invocations', {})


def test_core_verify_preserves_reserved_browser_envelope_and_marked_file_context(ai_order, monkeypatch):
    from coding_agents.cu_verification_context import compose
    svc, caller, order, ai, sent = ai_order
    monkeypatch.setattr('llm_gateway.provider_foundry.load_provider_config_from_env',
                        lambda: (_ for _ in ()).throw(ValueError('mock configuration absent')))
    monkeypatch.setattr(ai, '_deepseek', lambda *args: (_ for _ in ()).throw(AssertionError('must not dispatch')))
    row = {'seq': 1, 'page_scope': 'a' * 32, 'action': 'type', 'status': 'ok', 'element_index': 1,
           'before': {'field_type': 'text', 'checked': None}, 'after': {'field_type': 'text', 'checked': None}, 'action_input_matched': True}
    envelope = {'v': 1, 'source': 'owned_browser_backend', 'actions': [dict(row, seq=i + 1) for i in range(16)], 'responses': []}
    context = compose(['task'], 'file' * 1000, '\n' + json.dumps(envelope), 'notes')
    assert len(context.encode()) > 3200
    result = ai.run('v_one', caller, order['id'], invocation_id='bounded_verify_context', kind='verify', context={**CTX, 'memory': context, 'claim': 'done'})
    text = sent[-1]['input'][1]['content'][0]['text']
    assert context in text and 'file evidence truncated' in text
    assert 'owned_browser_backend' in text and len(text.encode()) <= 10240
    assert result['authority'] == 'proposal_only'  # response is never converted into automatic acceptance


def test_same_invocation_v2_to_v3_cannot_rebind_or_charge_twice(ai_order):
    svc, caller, order, ai, sent = ai_order
    args = ('v_one', caller, order['id'])
    ai.run(*args, invocation_id='bound_tabs_schema', kind='step', context={**CTX, 'action_contract_version': 2})
    original = svc.retrieve(*args)['invocations']['bound_tabs_schema']
    with pytest.raises(ValueError):
        ai.run(*args, invocation_id='bound_tabs_schema', kind='step', context={**CTX, 'action_contract_version': 3})
    assert len(sent) == 1
    assert svc.retrieve(*args)['invocations']['bound_tabs_schema'] == original


@pytest.mark.parametrize('version', [1, 2, 3])
@pytest.mark.parametrize('operation', ['new_tab', 'switch_tab'])
def test_step_admits_reply_only_in_negotiated_contract(ai_order, monkeypatch, version, operation):
    svc, caller, order, ai, sent = ai_order
    reply = json.dumps({'actions': [{'op': operation, 'text': 'https://synthetic.example/' if operation == 'new_tab' else '',
                                    'element_id': 't' + 'a' * 32 if operation == 'switch_tab' else ''}]})
    def provider(body):
        sent.append(body)
        return {'status': 'completed', 'model': ai.MODEL, 'usage': {'input_tokens': 2000, 'output_tokens': 120},
                'output': [{'content': [{'type': 'output_text', 'text': reply}]}]}
    monkeypatch.setattr(ai, '_openai', provider)
    args = ('v_one', caller, order['id'])
    context = {**CTX, 'action_contract_version': version}
    if version == 3:
        assert ai.run(*args, invocation_id='scoped_reply', kind='step', context=context)['reply'] == reply
    else:
        with pytest.raises(ValueError, match='action_op_invalid'):
            ai.run(*args, invocation_id='scoped_reply', kind='step', context=context)
    assert len(sent) == 1
    receipt = svc.retrieve(*args)['invocations']['scoped_reply']
    assert receipt['usage'] == {'model': ai.MODEL, 'input_tokens': 2000, 'output_tokens': 120}
    with pytest.raises(ValueError, match='already_attempted'):
        ai.run(*args, invocation_id='scoped_reply', kind='step', context=context)
    assert len(sent) == 1
    assert svc.retrieve(*args)['invocations']['scoped_reply'] == receipt
