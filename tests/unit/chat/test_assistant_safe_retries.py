"""Provider-free attempts, error propagation and founder-visible paused copy."""
import asyncio
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.company_os_harness import load_company_os_app
from tests.unit.chat.test_assistant_routing import env as env  # noqa: F401

load_company_os_app()

from llm_gateway.errors import ProviderCallError  # noqa: E402


def response(content="A grounded reply.", usage=None):
    return SimpleNamespace(usage=usage if usage is not None else {
        "prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


@pytest.fixture
def transport_boundary(monkeypatch):
    from app.chat import assistant
    from app.core.local_mode import is_local
    from app.credits import accounts, gate, metered
    from app.llm import founder_provider
    from llm_gateway import sole

    original_charge = gate.charge_sync
    local = is_local()
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    monkeypatch.setenv("LLM_FALLBACK_PROVIDER", "")
    monkeypatch.setattr(metered, "absorb_hosted_chat_failure", lambda meter: setattr(meter, "definitive_failure", True))
    monkeypatch.setattr(founder_provider, "register_workspace_llm_resolver", lambda: None)
    config = SimpleNamespace(model="DeepSeek-V4-Pro", provider="azure_foundry", source="platform_default")
    monkeypatch.setenv("LLM_DEPLOYMENT_FLASH", config.model)
    if not local:
        monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda _: config)
    from llm_gateway.provider_foundry import ProviderConfig

    if not local:
        monkeypatch.setattr(founder_provider, "to_provider_config", lambda *args, **kwargs: ProviderConfig(
            provider=config.provider, source=config.source, deployment_pro=config.model,
            deployment_flash=config.model))
    monkeypatch.setattr(accounts, "account_for_workspace", lambda _: object())
    scopes, calls, sleeps = [], [], []
    ack = {"released": True}

    @contextmanager
    def charge(**kwargs):
        if local:
            with original_charge(**kwargs) as meter:
                scopes.append(meter)
                yield meter
                meter.exited = True
            return
        n = len(scopes)
        byok = kwargs.get("payer") == "founder"
        meter = gate.Meter(hold=None if byok else SimpleNamespace(
            reservation_id=f"reservation-{n}", account_id="account", amount_micro=10),
            passthrough=byok, purpose=kwargs["purpose"], workspace_id=kwargs["workspace_id"],
            account_id="account", payer=kwargs.get("payer"))
        meter.protect_outcome = lambda: setattr(meter, "outcome_protected", True)
        scopes.append(meter)
        yield meter
        meter.exited = True
        if meter.recorded and not byok:
            meter.settle_result = SimpleNamespace(reservation_id=meter.hold.reservation_id)

    monkeypatch.setattr(gate, "charge_sync", charge)

    def released(meter, **kwargs):
        assert meter.exited and meter.definitive_failure
        return ack["released"]

    monkeypatch.setattr(assistant, "_rejection_release_confirmed", released)

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(assistant.asyncio, "sleep", sleep)

    async def fallback(meter, messages, *, config, max_tokens):
        return await sole.transmit(messages, purpose=meter.purpose, config=config,
            max_tokens=max_tokens, single_attempt=True)

    monkeypatch.setattr(metered, "transmit_hosted_chat_fallback", fallback)

    def install(outcomes):
        async def transmit(messages, **kwargs):
            calls.append(kwargs)
            assert scopes[-1].outcome_protected
            assert kwargs["single_attempt"] is True
            outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        monkeypatch.setattr(sole, "transmit", transmit)

    return SimpleNamespace(scopes=scopes, calls=calls, sleeps=sleeps, ack=ack,
                           config=config, install=install, assistant=assistant)


def rate_limit():
    return ProviderCallError("rate_limited", "Synthetic rejection", status=429, retryable=True)


@pytest.mark.parametrize("audience", ["Freight coordinators", "Weekend hikers"])
@pytest.mark.parametrize("sample,test_mode", [(False, False), (True, False), (False, True)])
def test_local_completion_uses_mode_resolver(monkeypatch, audience, sample, test_mode):
    from app.chat import assistant
    from app.core import local_mode
    from app.llm import founder_provider
    from llm_gateway import sole

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("LLM_OFFLINE", "true")
    monkeypatch.setattr(local_mode, "is_sample_mode", lambda: sample)
    monkeypatch.setattr(local_mode, "is_test_mode", lambda: test_mode)
    monkeypatch.setattr(local_mode, "is_offline", lambda: sample or test_mode)
    monkeypatch.setattr(founder_provider, "register_workspace_llm_resolver", lambda: None)
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda *_args: SimpleNamespace(source="platform_default"))
    attempts, skipped = [], []
    monkeypatch.setattr(sole, "load_provider_config_from_env", lambda: attempts.append(audience))
    monkeypatch.setattr(assistant, "note_assistant_model_skip", skipped.append)
    assert asyncio.run(assistant._complete([], workspace_id="workspace")) == ""
    assert attempts == ([] if sample or test_mode else [audience])
    assert skipped == (["llm_offline"] if sample or test_mode else ["no_provider_config"])


@pytest.mark.hosted_only
def test_rephrase_uses_flash_cap_and_never_retries(transport_boundary):
    from app.chat.llm_router import PURPOSE

    boundary = transport_boundary
    boundary.install([rate_limit()])
    with pytest.raises(ProviderCallError):
        asyncio.run(boundary.assistant._complete([], workspace_id="workspace", purpose=PURPOSE))
    assert len(boundary.calls) == 1
    assert boundary.calls[0]["purpose"] == PURPOSE
    assert boundary.calls[0]["max_tokens"] == 400
    assert boundary.sleeps == []
    assert boundary.scopes[0].purpose == PURPOSE


@pytest.mark.hosted_only
@pytest.mark.parametrize("sol", [False, True])
@pytest.mark.hosted_only
def test_rephrase_reasoning_is_minimized_and_shared_cap_is_captured(transport_boundary, monkeypatch, sol):
    from app.chat import assistant, llm_router
    from app.credits import gate, metered
    from llm_gateway import sole
    from llm_gateway.provider_foundry import ProviderConfig

    boundary = transport_boundary
    config = ProviderConfig(source="platform_default", deployment_pro="DeepSeek-V4-Pro",
        deployment_flash="supymem-gpt-6-1-sol" if sol else "DeepSeek-V4-Pro", reasoning_effort="high")
    monkeypatch.delenv("MODEL_ROUTING_OVERRIDES", raising=False)
    monkeypatch.setattr(sole, "load_provider_config_from_env", lambda: config)
    captured = []

    def capture(config, messages, *, max_output_tokens, **kwargs):
        assert config.reasoning_effort == "low" and config.max_output_tokens == max_output_tokens == 2048
        captured.append(max_output_tokens)
        return {}, "captured-digest", 1000, max_output_tokens

    monkeypatch.setattr(assistant, "_captured_model_call", capture)
    monkeypatch.setattr(metered, "begin_model_call", lambda meter: meter.protect_outcome())
    monkeypatch.setattr(metered, "capture_model_receipt", lambda meter, response: response.usage)
    monkeypatch.setattr(metered, "checkpoint_model_call", lambda *args: None)
    monkeypatch.setattr(gate, "record_model_receipt", lambda meter, receipt: meter.record(
        prompt_tokens=receipt["prompt_tokens"], completion_tokens=receipt["completion_tokens"]))
    boundary.install([response("Explain the interviews.")])
    assert asyncio.run(assistant._complete([], workspace_id="workspace", request_id="rephrase-reasoning",
        purpose=llm_router.PURPOSE)) == "Explain the interviews."
    dial = boundary.calls[0]
    assert dial["max_tokens"] == (2048 if sol else 400)
    assert captured == ([2048] if sol else [])
    wire = sole._completion_kwargs([], config=dial["config"], model=dial["config"].deployment_flash,
        purpose=llm_router.PURPOSE, max_tokens=dial["max_tokens"])
    assert wire["reasoning_effort"] == "low" if sol else wire["extra_body"] == {"thinking": {"type": "disabled"}}


@pytest.mark.hosted_only
def test_diagram_reply_schema_cap_metering_and_telemetry(transport_boundary, monkeypatch, caplog):
    import json
    from app.chat import assistant
    from app.chat.diagram_reply import REPLY_SCHEMA
    from app.credits import estimate
    from tests.unit.chat.test_diagrams import SVG

    boundary = transport_boundary
    quoted = []
    monkeypatch.setattr(estimate, "estimate_llm_micro", lambda model, **kwargs: quoted.append(kwargs["max_tokens"]) or 1000)
    parts = [{"type": "text", "text": "Here is the flow."},
             {"type": "show_diagram", "title": "intake_flow", "svg": SVG},
             {"type": "text", "text": "Context keeps it grounded."}]
    boundary.install([response(json.dumps({"parts": parts}), {"prompt_tokens": 300, "completion_tokens": 1800, "total_tokens": 2100})])
    with caplog.at_level("INFO", logger="app.chat.assistant"):
        messages = assistant.diagram_messages([{"role": "system", "content": assistant.SYSTEM_PROMPT}], question="Diagram the process")
        reply = asyncio.run(assistant._complete(messages, workspace_id="workspace"))
    assert quoted == [2000] and boundary.calls[0]["max_tokens"] == 2000
    assert boundary.calls[0]["response_schema"] == REPLY_SCHEMA
    assert reply.tokens == 1800 and [part["type"] for part in reply.parts] == ["text", "diagram", "text"]
    assert boundary.scopes[0].completion_tokens == 1800
    assert '"diagram_count":1' in caplog.text and '"output_tokens":1800' in caplog.text and '"latency_ms":' in caplog.text
    assert SVG not in caplog.text


@pytest.mark.hosted_only
@pytest.mark.parametrize("router,cap", [(False, 700), (True, 400)])
@pytest.mark.parametrize("sol", [False, True])
@pytest.mark.hosted_only
def test_captured_sol_quote_and_dispatch_use_the_same_raised_cap(transport_boundary, monkeypatch, router, cap, sol):
    from app.chat import agent, assistant, llm_router
    from app.credits import gate, metered
    from app.credits import estimate
    from llm_gateway import sole
    from llm_gateway.provider_foundry import ProviderConfig
    boundary = transport_boundary
    quoted = []
    config = ProviderConfig(source="platform_default", deployment_pro="DeepSeek-V4-Pro",
        deployment_flash="supymem-gpt-6-1-sol" if sol else "DeepSeek-V4-Pro")
    monkeypatch.delenv("MODEL_ROUTING_OVERRIDES", raising=False)
    monkeypatch.setattr(sole, "load_provider_config_from_env", lambda: config)
    monkeypatch.setattr(sole, "resolve_override", lambda *args, **kwargs: None)
    monkeypatch.setattr(estimate, "estimate_llm_micro", lambda model, **kwargs: quoted.append(kwargs["max_tokens"]) or 1000)
    def capture(config, messages, *, purpose, max_output_tokens, request_id, **kwargs):
        assert request_id == "original-cap-check" and config.max_output_tokens == max_output_tokens
        return {}, "captured-digest", estimate.estimate_llm_micro(config.deployment_flash,
            max_tokens=max_output_tokens), max_output_tokens

    monkeypatch.setattr(assistant, "_captured_model_call", capture)
    monkeypatch.setattr(metered, "begin_model_call", lambda meter: meter.protect_outcome())
    monkeypatch.setattr(metered, "capture_model_receipt", lambda meter, response: response.usage)
    monkeypatch.setattr(metered, "checkpoint_model_call", lambda *args: None)
    monkeypatch.setattr(gate, "record_model_receipt",
        lambda meter, receipt: meter.record(prompt_tokens=receipt["prompt_tokens"],
            completion_tokens=receipt["completion_tokens"]))
    boundary.install([response("{}" if router else "A grounded reply.")])
    asyncio.run(llm_router._ask_model("venture", "Explain purpose", SimpleNamespace(
        venture={"workspace_id": "workspace"}), request_id="original-cap-check") if router else
        boundary.assistant._complete([], workspace_id="workspace", request_id="original-cap-check"))
    assert quoted == [(2048 if router else 3072) if sol else cap]
    assert boundary.calls[0]["max_tokens"] == quoted[0]
    assert boundary.calls[0]["config"].api_mode == ("responses" if sol else "chat_completions")
    assert boundary.calls[0]["use_workspace_override"] is False
    if sol and not router:
        assert boundary.calls[0]["config"].reasoning_effort == "low"
        assert agent._estimate([], "workspace", request_id="original-cap-check") == 1000
        assert quoted == [3072, 3072]


@pytest.mark.hosted_only
@pytest.mark.parametrize("router", [False, True])
@pytest.mark.hosted_only
def test_three_rejections_have_three_fresh_meters_and_no_fourth(transport_boundary, router):
    from app.chat import llm_router
    b = transport_boundary
    b.install([rate_limit(), rate_limit(), rate_limit()])
    with pytest.raises(ProviderCallError) as error:
        asyncio.run(llm_router._ask_model("venture", "Explain purpose", SimpleNamespace(
            venture={"workspace_id": "workspace"})) if router else
            b.assistant._complete([], workspace_id="workspace"))
    assert error.value.unspent_confirmed is True
    assert len(b.calls) == len(b.scopes) == 3 and b.sleeps == [1, 2]
    assert len({m.hold.reservation_id for m in b.scopes}) == 3
    assert all(m.definitive_failure and not m.recorded for m in b.scopes)


@pytest.mark.hosted_only
@pytest.mark.parametrize("router", [False, True])
@pytest.mark.hosted_only
def test_retry_then_success_records_only_actual_success(transport_boundary, router):
    from app.chat import llm_router
    b = transport_boundary
    b.install([rate_limit(), response("{}" if router else "A grounded reply.")])
    result = asyncio.run(llm_router._ask_model("venture", "Explain purpose", SimpleNamespace(
        venture={"workspace_id": "workspace"})) if router else
        b.assistant._complete([], workspace_id="workspace"))
    assert (result.acts if router else result) == ([("assistant", "Explain purpose")] if router else "A grounded reply.")
    assert len(b.calls) == 2 and b.sleeps == [1]
    assert b.scopes[0].definitive_failure and not b.scopes[0].recorded
    assert b.scopes[1].recorded and (b.scopes[1].prompt_tokens, b.scopes[1].completion_tokens) == (10, 2)


@pytest.mark.hosted_only
@pytest.mark.parametrize("failure", [
    ProviderCallError("provider_down", "Synthetic timeout", retryable=True),
    ProviderCallError("provider_down", "Synthetic server error", status=503, retryable=True),
    ProviderCallError("rate_limited", "Status absent", retryable=True),
    ProviderCallError("provider_error", "Wrong code", status=429, retryable=True),
])
@pytest.mark.hosted_only
def test_unknown_rejection_never_retries(transport_boundary, failure):
    b = transport_boundary
    b.install([failure])
    with pytest.raises(ProviderCallError):
        asyncio.run(b.assistant._complete([], workspace_id="workspace"))
    assert len(b.calls) == 1 and b.sleeps == []
    assert not b.scopes[0].definitive_failure and not b.scopes[0].recorded


@pytest.mark.hosted_only
@pytest.mark.parametrize("code,status", [("bad_api_key", 401), ("bad_api_key", 403), ("out_of_credits", 402)])
@pytest.mark.hosted_only
def test_auth_or_credit_rejection_stops_once(transport_boundary, code, status):
    b = transport_boundary
    b.install([ProviderCallError(code, "Synthetic rejection", status=status)])
    with pytest.raises(ProviderCallError) as error:
        asyncio.run(b.assistant._complete([], workspace_id="workspace"))
    assert error.value.unspent_confirmed is True
    assert len(b.calls) == 1 and b.sleeps == [] and b.scopes[0].definitive_failure


@pytest.mark.hosted_only
def test_release_failure_never_authorizes_second_call(transport_boundary):
    b = transport_boundary
    b.ack["released"] = False
    b.install([rate_limit()])
    with pytest.raises(ProviderCallError, match="release is unconfirmed") as error:
        asyncio.run(b.assistant._complete([], workspace_id="workspace"))
    assert error.value.code == "provider_outcome_unknown"
    assert len(b.calls) == 1 and b.sleeps == []


@pytest.mark.hosted_only
def test_cancellation_never_authorizes_second_call(transport_boundary):
    b = transport_boundary
    b.install([asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(b.assistant._complete([], workspace_id="workspace"))
    assert len(b.calls) == 1 and not b.scopes[0].definitive_failure


@pytest.mark.hosted_only
@pytest.mark.parametrize("audience", ["Warehouse loading teams", "Weekend hiking groups"])
@pytest.mark.hosted_only
def test_uncaptured_missing_usage_keeps_one_protected_hold(transport_boundary, audience):
    b = transport_boundary
    b.install([response(usage={})])
    with pytest.raises(ProviderCallError) as error:
        asyncio.run(b.assistant._complete([{"role": "user", "content": f"Explain the workflow for {audience}."}],
                                         workspace_id="workspace"))
    assert error.value.code == "provider_outcome_unknown"
    assert len(b.calls) == len(b.scopes) == 1
    assert b.scopes[0].outcome_protected and not b.scopes[0].definitive_failure
    assert not b.scopes[0].recorded


@pytest.mark.hosted_only
@pytest.mark.parametrize("router", [False, True])
@pytest.mark.hosted_only
def test_explicit_byok_retry_has_fresh_scopes_without_platform_holds(transport_boundary, router):
    from app.chat import llm_router
    b = transport_boundary
    b.config.source = "founder_override"
    b.install([rate_limit(), response("{}" if router else "A grounded reply.")])
    result = asyncio.run(llm_router._ask_model("venture", "Explain purpose", SimpleNamespace(
        venture={"workspace_id": "workspace"})) if router else
        b.assistant._complete([], workspace_id="workspace"))
    assert (result.acts if router else result) == ([("assistant", "Explain purpose")] if router else "A grounded reply.")
    assert len(b.calls) == 2 and all(m.hold is None and m.payer == "founder" for m in b.scopes)


@pytest.mark.hosted_only
@pytest.mark.parametrize("outcome", [ProviderCallError("rate_limited", "Nonretryable rejection", status=429),
                                     response(usage={}), TimeoutError("Synthetic timeout")])
@pytest.mark.hosted_only
def test_router_unknown_or_rejected_call_is_not_redialed(transport_boundary, monkeypatch, outcome):
    from app.chat import llm_router
    b = transport_boundary
    b.install([outcome])
    monkeypatch.setenv("CHAT_ROUTER_MODE", "live")
    with pytest.raises(ProviderCallError):
        asyncio.run(llm_router.route("venture", "research first then plan", [("research", "x")],
                                    SimpleNamespace(venture={"workspace_id": "workspace"})))
    assert len(b.calls) == 1 and b.sleeps == []
    assert b.calls[0]["purpose"] == llm_router.PURPOSE


@pytest.mark.hosted_only
def test_actual_handle_message_does_not_restart_terminal_assistant(env, transport_boundary, monkeypatch):
    from app.chat import llm_router, router
    client, vid = env
    b = transport_boundary
    b.install([rate_limit(), rate_limit(), rate_limit()])

    async def decision(*args, **kwargs):
        return llm_router.Decision(acts=[("assistant", "Explain the saved state")], clarify="")

    dispatches = []
    async def dispatch(*args, **kwargs):
        dispatches.append(args[2])
        return {"body_md": await b.assistant._complete([], workspace_id="workspace")}

    monkeypatch.setattr(llm_router, "route", decision)
    monkeypatch.setattr(router, "_dispatch_intent", dispatch)
    result = client.post(f"/ventures/{vid}/chat", json={"message": "Explain the saved state"})
    assert result.status_code == 200, result.text
    assert result.json()["kind"] == "error" and "Charlie is taking longer than usual." in result.json()["body_md"]
    assert len(b.calls) == 3 and dispatches == ["assistant"]


def test_unknown_ui_copy_has_no_private_error_or_retry_invitation(monkeypatch):
    from app.llm.provider_errors import provider_error_for_user
    error = ProviderCallError("provider_error", "private URL?credential=synthetic", retryable=True)
    for mode in ("local", "hosted"):
        monkeypatch.setenv("SUPYMEM_MODE", mode)
        result = provider_error_for_user(error)
        assert "private" not in str(result) and result["retryable"] is False
        assert "switch" not in result["hint"]
        assert "credential" not in str(provider_error_for_user(RuntimeError("private credential URL")))
        if mode == "local":
            assert "did not return a confirmed result" in result["message"]
            assert "Settings > Keys and connections > Your AI provider" in result["hint"]
            assert "test your connection" in result["hint"]
        else:
            assert "request paused" in result["message"] and "reconciliation" in result["hint"]


@pytest.mark.hosted_only
def test_router_actual_wait_for_timeout_stops_paid_phase(transport_boundary, monkeypatch, caplog):
    from app.chat import llm_router
    from llm_gateway import sole
    b = transport_boundary
    async def pending(*args, **kwargs):
        b.calls.append(kwargs)
        assert b.scopes[-1].outcome_protected
        await asyncio.Future()
    monkeypatch.setattr(sole, "transmit", pending)
    monkeypatch.setattr(llm_router, "TIMEOUT_S", 0.001)
    monkeypatch.setenv("CHAT_ROUTER_MODE", "live")
    with pytest.raises(ProviderCallError) as error:
        asyncio.run(llm_router.route("venture", "research first then plan", [("research", "x")],
                                    SimpleNamespace(venture={"workspace_id": "workspace"})))
    assert error.value.code == "provider_down"
    assert len(b.calls) == 1 and not b.scopes[0].definitive_failure and not b.scopes[0].recorded
    assert "phase=router reason=deadline_expired attempt=1" in caplog.text
    assert "exception_class=TimeoutError" in caplog.text


@pytest.mark.hosted_only
def test_actual_handle_message_unknown_router_cannot_enter_assistant(env, transport_boundary, monkeypatch):
    from app.chat import llm_router, router
    client, vid = env
    b = transport_boundary
    b.install([TimeoutError("Synthetic timeout")])
    monkeypatch.setenv("CHAT_ROUTER_MODE", "live")
    monkeypatch.setattr(llm_router, "needs_model", lambda *args: True)
    async def forbidden(*args, **kwargs):
        pytest.fail("Unknown router outcome cannot dispatch another phase")
    monkeypatch.setattr(router, "_dispatch_intent", forbidden)
    result = client.post(f"/ventures/{vid}/chat", json={"message": "Explain the saved state"})
    assert result.status_code == 409, result.text
    assert result.json()["detail"]["error"]["code"] == "chat_request_outcome_unknown"
    assert len(b.calls) == 1
    # TestClient may also schedule a short lifespan delay on this asyncio module.
    assert set(b.sleeps).isdisjoint({1, 2})


@pytest.mark.hosted_only
@pytest.mark.parametrize("content,reason", [("invalid JSON", "json_parse"), ("null", "not_object"),
    ("[]", "not_object"), ("{}", "acts_not_list"), ('{"acts":{}}', "acts_not_list"),
    ('{"acts":[false]}', "schema_invalid"), ('{"acts":[]}', "schema_invalid"),
    ('{"acts":[{"intent":"unknown"}]}', "unknown_intent:unknown"),
    ('{"acts":[{"intent":"agent_reach","text":"read reddit"}]}', "schema_invalid")])
@pytest.mark.hosted_only
def test_router_paid_invalid_output_has_one_settled_call_and_assistant_fallback(transport_boundary, monkeypatch, caplog, content, reason):
    from app.chat import llm_router
    b = transport_boundary
    b.install([response(content)])
    monkeypatch.setenv("CHAT_ROUTER_MODE", "live")
    result = asyncio.run(llm_router.route("venture", "explain first then plan", [("research", "x")],
                                         SimpleNamespace(venture={"workspace_id": "workspace"})))
    assert result.acts == [("assistant", "explain first then plan")]
    assert len(b.calls) == 1 and b.scopes[0].recorded
    record = [record for record in caplog.records if record.name == "app.chat.provider"][-1]
    assert record.provider_phase == "router" and record.provider_failure_reason == reason
    assert record.provider_content_length == len(content)


def test_paid_invalid_output_ui_copy_is_terminal_without_private_response(monkeypatch):
    from app.llm.provider_errors import provider_error_for_user

    for mode in ("local", "hosted"):
        monkeypatch.setenv("SUPYMEM_MODE", mode)
        result = provider_error_for_user(ProviderCallError("provider_output_invalid", "Synthetic private response"))
        assert result["retryable"] is False and "Synthetic private response" not in str(result)
        if mode == "local":
            assert "did not return an answer I could use" in result["message"]
            assert "Settings > Keys and connections > Your AI provider" in result["hint"]
        else:
            assert "completed this request" in result["message"]


@pytest.mark.hosted_only
@pytest.mark.parametrize("finish", [None, "stop", "length"])
@pytest.mark.hosted_only
def test_empty_assistant_output_is_settled_not_retried(transport_boundary, caplog, finish):
    boundary = transport_boundary
    reply = response("")
    reply.choices[0].finish_reason = finish
    boundary.install([reply])
    # Empty text is logged and returned so the caller answers with its grounded fallback.
    assert asyncio.run(boundary.assistant._complete([], workspace_id="workspace")) == ""
    assert len(boundary.calls) == 1 and not boundary.sleeps
    assert boundary.scopes[0].recorded and boundary.scopes[0].settle_result is not None
    records = [record for record in caplog.records if record.name == "app.chat.provider"]
    assert records[0].provider_failure_reason == "empty_content"
    assert all(record.provider_phase == "assistant" and record.provider_content_length == 0 for record in records)
    if finish:
        assert records[-1].provider_failure_reason == f"finish_reason:{finish}"


@pytest.mark.hosted_only
@pytest.mark.parametrize("content,reason", [("", "empty_parts"), ('{"parts":[]}', "empty_parts"),
                                          ('{"PRIVATE": "invalid contract"}', "parse_failed")])
@pytest.mark.hosted_only
def test_empty_diagram_retry_uses_two_settled_meters_and_safe_warning(transport_boundary, caplog, content, reason):
    boundary = transport_boundary
    assistant = boundary.assistant
    messages = [{"role": "system", "content": assistant.SYSTEM_PROMPT}]
    final = assistant.diagram_messages(messages, question="Diagram the process")
    boundary.install([response(content), response("Choose the audience. Send one personal message.")])
    reply = asyncio.run(assistant._answer_body(messages, final, workspace_id="workspace", request_id="turn:answer"))
    assert str(reply) == "Choose the audience. Send one personal message."
    assert reply.parts == [{"type": "text", "text": str(reply)}]
    assert len(boundary.calls) == len(boundary.scopes) == 2 and not boundary.sleeps
    assert all(meter.recorded and meter.settle_result for meter in boundary.scopes)
    assert boundary.calls[0]["response_schema"] == assistant.REPLY_SCHEMA
    assert "response_schema" not in boundary.calls[1]
    records = [record for record in caplog.records if record.msg == "chat_diagram_mode_failed reason=%s"]
    assert len(records) == 1 and records[0].levelname == "WARNING"
    assert records[0].getMessage() == f"chat_diagram_mode_failed reason={reason}"
    assert "PRIVATE" not in caplog.text
    if not content:
        assert any(record.levelname == "WARNING" and record.msg == "chat_assistant_model_skip reason=%s"
                   for record in caplog.records)


@pytest.mark.hosted_only
@pytest.mark.parametrize("failure", [ProviderCallError("out_of_credits", "PRIVATE credit refusal", status=402),
                                     ProviderCallError("turn_budget_exceeded", "PRIVATE spending limit")])
@pytest.mark.hosted_only
def test_diagram_credit_or_spending_refusal_never_retries(transport_boundary, caplog, failure):
    boundary = transport_boundary
    assistant = boundary.assistant
    messages = [{"role": "system", "content": assistant.SYSTEM_PROMPT}]
    final = assistant.diagram_messages(messages, question="Diagram the process")
    boundary.install([failure])
    with pytest.raises(ProviderCallError) as error:
        asyncio.run(assistant._answer_body(messages, final, workspace_id="workspace", request_id="turn:answer"))
    assert error.value is failure and len(boundary.calls) == len(boundary.scopes) == 1
    assert not boundary.scopes[0].recorded and not boundary.sleeps
    if failure.code == "out_of_credits":
        assert failure.unspent_confirmed and boundary.scopes[0].definitive_failure
    assert f"chat_diagram_mode_failed reason={failure.code}" in caplog.text
    assert "PRIVATE" not in caplog.text


@pytest.mark.hosted_only
@pytest.mark.parametrize("refusal", ["insufficient_credits", "account_suspended", "credits_unavailable"])
@pytest.mark.hosted_only
def test_local_credit_refusal_is_not_swallowed_into_plain_retry(transport_boundary, monkeypatch, refusal):
    from app.credits import gate
    from app.credits.errors import AccountSuspended, CreditsUnavailable, InsufficientCredits, LIMIT_MESSAGE
    from app.chat.recovery import founder_failure
    boundary = transport_boundary
    calls = []
    failure = {"insufficient_credits": InsufficientCredits("account", 0, 100),
        "account_suspended": AccountSuspended("account"),
        "credits_unavailable": CreditsUnavailable("PRIVATE gate policy or receipt failure")}[refusal]

    @contextmanager
    def charge(**kwargs):
        calls.append(kwargs)
        raise failure
        yield

    monkeypatch.setattr(gate, "charge_sync", charge)
    messages = [{"role": "system", "content": boundary.assistant.SYSTEM_PROMPT}]
    final = boundary.assistant.diagram_messages(messages, question="Diagram the process")
    with pytest.raises(type(failure)) as error:
        asyncio.run(boundary.assistant._answer_body(messages, final, workspace_id="workspace", request_id="turn:answer"))
    assert error.value.code == refusal and len(calls) == 1 and not boundary.calls
    assert error.value is failure
    reply = founder_failure(error.value, "venture", "your reply", {"action": "chat.reply"})
    assert "PRIVATE" not in str(reply)
    if refusal == "insufficient_credits":
        assert reply["body_md"] == LIMIT_MESSAGE and [action["label"] for action in reply["actions"]] == ["Buy more"]
    else:
        assert "usage limit" not in reply["body_md"] and reply["actions"]


@pytest.mark.parametrize("phase", ["router", "assistant"])
@pytest.mark.parametrize("saved,code", [
    ({"outcome": "confirmed", "output_usable": False}, "provider_output_invalid"),
    ({"outcome": "unknown", "output_usable": False}, "provider_outcome_unknown"),
    ({"outcome": "confirmed", "output_usable": True}, "provider_outcome_unknown"),
    (None, "provider_outcome_unknown")])
def test_captured_output_failure_requires_durable_confirmation(monkeypatch, caplog, phase, saved, code):
    from app.chat import assistant
    from app.credits import metered
    monkeypatch.setattr(metered, "recover_model_call", lambda key: saved)
    meter = SimpleNamespace(billing_output_error=True, model_journal_key="original")
    error = assistant._captured_model_error(meter, phase=phase, response=response("PRIVATE output"))
    assert error.code == code
    if code == "provider_output_invalid":
        record = caplog.records[-1]
        assert record.provider_phase == phase and record.provider_failure_reason == "schema_invalid"
        assert record.provider_content_length == len("PRIVATE output")
        assert "PRIVATE" not in record.getMessage()


@pytest.mark.hosted_only
@pytest.mark.parametrize("settled", [True, False])
@pytest.mark.hosted_only
def test_modern_router_unusable_output_only_falls_back_after_settlement(transport_boundary, monkeypatch, settled):
    from app.chat import llm_router
    from app.credits import gate, metered
    from app.credits.errors import CreditsUnavailable
    from llm_gateway import sole
    from llm_gateway.provider_foundry import ProviderConfig

    boundary = transport_boundary
    monkeypatch.setenv("CHAT_ROUTER_MODE", "live")
    monkeypatch.setattr(sole, "load_provider_config_from_env", lambda: ProviderConfig(
        source="platform_default", deployment_pro="supymem-gpt-6-1-sol", deployment_flash="supymem-gpt-6-1-sol"))
    monkeypatch.setattr(boundary.assistant, "_captured_model_call", lambda *args, **kwargs: ({}, "digest", 1000, 2048))
    monkeypatch.setattr(metered, "begin_model_call", lambda meter: meter.protect_outcome())
    monkeypatch.setattr(metered, "capture_model_receipt", lambda *args: {})
    monkeypatch.setattr(metered, "checkpoint_model_call", lambda *args: None)
    monkeypatch.setattr(metered, "recover_model_call", lambda key: {
        "outcome": "confirmed" if settled else "unknown", "output_usable": False})
    def reject_output(meter, receipt):
        meter.billing_output_error, meter.model_journal_key = True, "original"
        raise CreditsUnavailable("provider output is unusable")
    monkeypatch.setattr(gate, "record_model_receipt", reject_output)
    boundary.install([response("")])
    call = llm_router.route("venture", "Explain the saved direction", [("assistant", "x")],
        SimpleNamespace(venture={"workspace_id": "workspace"}), request_id="original")
    if settled:
        assert asyncio.run(call).acts == [("assistant", "Explain the saved direction")]
    else:
        with pytest.raises(ProviderCallError) as error:
            asyncio.run(call)
        assert error.value.code == "provider_outcome_unknown"
    assert len(boundary.calls) == 1 and not boundary.sleeps


@pytest.mark.parametrize("name", ["ghp_privatecredential", "PRIVATE prompt", "https://private.test/key", "sk-private"])
def test_output_warning_withholds_secret_or_unstructured_intent_names(caplog, name):
    from app.llm.provider_errors import log_chat_provider_failure
    log_chat_provider_failure(phase="router", reason=f"unknown_intent:{name}", content_length=20)
    record = caplog.records[-1]
    assert record.provider_failure_reason == "unknown_intent:unknown"
    assert name not in record.getMessage()


@pytest.mark.hosted_only
def test_legacy_router_paid_invalid_json_degrades_after_settlement(transport_boundary):
    from app.chat import llm_router

    boundary = transport_boundary
    boundary.install([response("invalid JSON")])
    result = asyncio.run(llm_router._ask_model("venture", "Explain the saved purpose",
        SimpleNamespace(venture={"workspace_id": "workspace"})))
    assert result.acts == [("assistant", "Explain the saved purpose")]
    assert len(boundary.calls) == 1 and boundary.scopes[0].recorded
    assert boundary.scopes[0].settle_result is not None
    assert boundary.calls[0]["config"].api_mode == "chat_completions"


@pytest.mark.hosted_only
def test_router_pre_dispatch_charge_failure_keeps_ordinary_fallback(transport_boundary, monkeypatch):
    from app.chat import llm_router
    from app.credits import gate

    b = transport_boundary

    def refused(**kwargs):
        raise ValueError("Synthetic pre-dispatch accounting failure")
    monkeypatch.setattr(gate, "charge_sync", refused)
    monkeypatch.setenv("CHAT_ROUTER_MODE", "live")
    assert asyncio.run(llm_router.route("venture", "research first then plan", [("research", "x")],
                                       SimpleNamespace(venture={"workspace_id": "workspace"}))) is None
    assert b.calls == b.scopes == []


@pytest.mark.hosted_only
@pytest.mark.parametrize("router", [False, True])
@pytest.mark.hosted_only
def test_usage_property_failure_is_unknown_not_prose_fallback(transport_boundary, router):
    from app.chat import llm_router
    class BrokenReceipt:
        @property
        def usage(self):
            raise ValueError("Synthetic unreadable receipt")
    b = transport_boundary
    b.install([BrokenReceipt()])
    with pytest.raises(ProviderCallError) as error:
        asyncio.run(llm_router._ask_model("venture", "Explain purpose", SimpleNamespace(
            venture={"workspace_id": "workspace"})) if router else
            b.assistant._complete([], workspace_id="workspace"))
    assert error.value.code == "provider_outcome_unknown"
    assert len(b.calls) == 1 and all(not scope.recorded for scope in b.scopes)
    assert b.scopes[0].outcome_protected and not b.scopes[0].definitive_failure


@pytest.mark.hosted_only
@pytest.mark.parametrize("field,value", [
    ("id", "other"), ("account_id", "foreign"), ("workspace_id", "foreign"),
    ("amount_micro", 11), ("purpose", "other"), ("status", "reconciling"), ("settled_micro", 0),
])
@pytest.mark.hosted_only
def test_release_readback_must_match_exact_durable_identity(monkeypatch, field, value):
    from app.chat import assistant
    from app.credits import _txn, gate
    row = SimpleNamespace(id="reservation", account_id="account", workspace_id="workspace",
                          amount_micro=10, purpose="purpose", status="released", settled_micro=None)
    meter = gate.Meter(hold=SimpleNamespace(reservation_id="reservation", account_id="account", amount_micro=10),
                       passthrough=False, purpose="purpose", workspace_id="workspace", account_id="account")
    @contextmanager
    def session():
        yield SimpleNamespace(get=lambda model, identity: row)
    monkeypatch.setattr(_txn, "session_factory", lambda: session)
    assert assistant._rejection_release_confirmed(meter, workspace_id="workspace", byok=False)
    setattr(row, field, value)
    assert not assistant._rejection_release_confirmed(meter, workspace_id="workspace", byok=False)


@pytest.mark.hosted_only
def test_byok_no_hold_requires_exact_founder_payer_and_scope():
    from app.chat import assistant
    from app.credits.gate import Meter
    meter = Meter(hold=None, passthrough=True, purpose="purpose", workspace_id="workspace", payer="founder")
    assert assistant._rejection_release_confirmed(meter, workspace_id="workspace", byok=True)
    meter.payer = None
    assert not assistant._rejection_release_confirmed(meter, workspace_id="workspace", byok=True)


@pytest.mark.hosted_only
@pytest.mark.parametrize("router", [False, True])
@pytest.mark.parametrize("fault", ["timeout", "missing", "invalid", "release", "cancel", "sdk_timeout"])
@pytest.mark.hosted_only
def test_closed_phase_logs_preserve_original_held_or_rejected_outcome(transport_boundary, caplog, router, fault):
    import httpx
    from openai import APITimeoutError
    from app.chat import llm_router
    from core.logging import JsonFormatter

    b = transport_boundary
    if fault == "timeout":
        outcome = TimeoutError("PRIVATE prompt/key https://private.invalid?credential=PRIVATE")
    elif fault in {"missing", "invalid"}:
        outcome = response("PRIVATE paid response")
        outcome.usage = None if fault == "missing" else {}
    elif fault == "release":
        b.ack["released"] = False
        outcome = rate_limit()
    elif fault == "cancel":
        outcome = asyncio.CancelledError("PRIVATE prompt")
    else:
        outcome = ProviderCallError("provider_down", "PRIVATE provider message")
        outcome.__cause__ = APITimeoutError(request=httpx.Request("POST", "https://private.invalid?key=PRIVATE"))
    b.install([outcome])
    with pytest.raises(asyncio.CancelledError if fault == "cancel" else ProviderCallError):
        asyncio.run(llm_router._ask_model("venture", "PRIVATE user question", SimpleNamespace(
            venture={"workspace_id": "workspace"})) if router else
            b.assistant._complete([{"role": "user", "content": "PRIVATE user question"}], workspace_id="workspace"))
    records = [r for r in caplog.records if r.name == "app.chat.provider"]
    expected = {"missing": "usage_missing", "invalid": "usage_invalid", "release": "rejection_release_unconfirmed",
                "cancel": "cancelled"}.get(fault, "deadline_expired" if router and fault == "timeout" else "transport_failed")
    matching = [r for r in records if r.provider_failure_reason == expected]
    attempts = 1
    assert len(matching) == attempts
    record = matching[0]
    assert record.provider_phase == ("router" if router else "assistant")
    assert record.provider_attempt == 1 and record.exc_info is None
    assert record.provider_http_status == (429 if fault == "release" else None)
    assert record.provider_usage_state == ({"missing": "missing", "invalid": "invalid"}.get(fault, "not_observed"))
    if fault == "sdk_timeout":
        assert record.provider_exception_class == "APITimeoutError"
    formatted = JsonFormatter().format(record)
    assert "PRIVATE" not in formatted and "https://" not in formatted
    assert "user question" not in formatted and "paid response" not in formatted
    assert len(b.calls) == attempts and b.sleeps == [] and all(not scope.recorded for scope in b.scopes)
    assert b.scopes[0].definitive_failure is (fault == "release")


@pytest.mark.hosted_only
def test_confirmed_rejection_logs_attempts_without_changing_retry_cap(transport_boundary, caplog):
    b = transport_boundary
    b.install([rate_limit(), rate_limit(), rate_limit()])
    with pytest.raises(ProviderCallError) as error:
        asyncio.run(b.assistant._complete([], workspace_id="workspace"))
    assert error.value.unspent_confirmed is True
    records = [r for r in caplog.records if r.name == "app.chat.provider"
               and r.provider_failure_reason == "rejection_confirmed"]
    assert [r.provider_attempt for r in records] == [1, 2, 3]
    assert all(r.provider_http_status == 429 for r in records)
    assert len(b.calls) == 3 and b.sleeps == [1, 2]


def test_closed_logger_ignores_unrecognized_fields_and_never_renders_error_text(caplog):
    from app.llm.provider_errors import log_chat_provider_failure
    error = ProviderCallError("PRIVATE credential", "PRIVATE body", status=True)
    log_chat_provider_failure(phase="PRIVATE actor", reason="PRIVATE prompt", attempt=True,
                              exc=error, usage_state="PRIVATE reply")
    record = [r for r in caplog.records if r.name == "app.chat.provider"][-1]
    assert record.provider_phase == record.provider_failure_reason == record.provider_usage_state == "unknown"
    assert record.provider_error_code == "unknown" and record.provider_http_status is None
    assert record.provider_attempt == 0 and "PRIVATE" not in record.getMessage() and record.exc_info is None


@pytest.mark.hosted_only
def test_logging_failure_cannot_replace_provider_outcome_or_authorize_retry(transport_boundary, monkeypatch):
    import logging
    b = transport_boundary
    failure = ProviderCallError("provider_down", "Synthetic held outcome")
    b.install([failure])
    def broken(*args, **kwargs):
        raise RuntimeError("Synthetic broken telemetry")
    monkeypatch.setattr(logging.getLogger("app.chat.provider"), "warning", broken)
    with pytest.raises(ProviderCallError) as error:
        asyncio.run(b.assistant._complete([], workspace_id="workspace"))
    assert error.value is failure
    assert len(b.calls) == 1 and b.sleeps == []
    assert not b.scopes[0].definitive_failure and not b.scopes[0].recorded


@pytest.mark.hosted_only
def test_pre_dispatch_local_failure_keeps_free_fallback_without_private_traceback(transport_boundary, monkeypatch, caplog):
    from app.llm import founder_provider
    b = transport_boundary
    failure = ValueError("PRIVATE key or provider URL https://private.invalid")
    def broken(*args, **kwargs):
        raise failure
    monkeypatch.setattr(founder_provider, "resolve_llm_config", broken)
    assert asyncio.run(b.assistant._complete([], workspace_id="workspace")) == ""
    assert b.calls == b.scopes == []
    record = [r for r in caplog.records if r.name == "app.chat.provider"][-1]
    assert record.provider_failure_reason == "local_failed"
    assert record.provider_usage_state == "not_observed"
    assert "PRIVATE" not in caplog.text and "https://" not in caplog.text
    assert record.exc_info is None
    reply = b.assistant._fallback_answer("Explain this venture's purpose", {"venture_id": "venture"})
    assert "credit" not in reply.lower() and "model" not in reply.lower() and "connection" not in reply.lower()


@pytest.mark.hosted_only
@pytest.mark.parametrize("fault", ["settle", "exit"])
@pytest.mark.parametrize("entry", ["completion", "agent", "answer"])
@pytest.mark.hosted_only
def test_paid_finalization_failure_retains_real_hold_without_caller_fallback(env, monkeypatch, fault, entry):
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    from sqlalchemy import select

    from app.chat import agent, assistant
    from app.credits import accounts, estimate, gate, policy
    from app.db.models import CreditReservation, Venture
    from app.llm import founder_provider
    from app.llm.provider_errors import provider_error_for_user
    from app.state import db
    from app import tools
    from llm_gateway import sole
    from tests.unit.credits.conftest import _seed_credit_infra

    _, vid = env
    _seed_credit_infra()
    policy.invalidate()
    with db()() as session:
        wid = session.get(Venture, vid).workspace_id
    before = accounts.ensure_account(vid, workspace_id=wid)
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setattr(founder_provider, "register_workspace_llm_resolver", lambda: None)
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda _: SimpleNamespace(
        model="DeepSeek-V4-Pro", provider="azure_foundry", source="platform_default"))
    monkeypatch.setattr(estimate, "estimate_llm_micro", lambda *args, **kwargs: 1_000)
    monkeypatch.setattr(agent, "TURN_BUDGET_MICRO", 1_000)
    calls, contexts = [], []
    context_type = tools.ToolContext

    def context(**kwargs):
        ctx = context_type(**kwargs)
        contexts.append(ctx)
        return ctx
    monkeypatch.setattr(tools, "ToolContext", context)

    async def transmit(messages, **kwargs):
        calls.append(kwargs)
        # Real reserve/protect_outcome must commit before provider dispatch.
        with db()() as session:
            rows = session.scalars(select(CreditReservation).where(
                CreditReservation.account_id == before.id)).all()
            assert len(rows) == 1 and rows[0].status == "reconciling"
        return response()
    monkeypatch.setattr(sole, "transmit", transmit)

    def broken(*args, **kwargs):
        raise RuntimeError("Synthetic accounting persistence failure")
    monkeypatch.setattr(gate, "settle" if fault == "settle" else "_exit", broken)
    messages = [{"role": "system", "content": "Be helpful."},
                {"role": "user", "content": "Look up competitor websites."}]
    with pytest.raises(ProviderCallError) as error:
        if entry == "completion":
            asyncio.run(assistant._complete(messages, workspace_id=wid, raw=True))
        elif entry == "agent":
            ctx = context(venture_id=vid, workspace_id=wid, budget_micro=1_000)
            asyncio.run(agent.run_turn(messages, ctx))
        else:
            # Real answer_question enters agent.answer/run_turn and must not
            # use its ordinary second _complete when the first call is unknown.
            asyncio.run(assistant.answer_question(vid, "Look up competitor websites.", workspace_id=wid))
    assert error.value.code == "provider_outcome_unknown"
    assert not getattr(error.value, "unspent_confirmed", False)
    visible = provider_error_for_user(error.value)
    assert visible["retryable"] is False and "Settings" not in visible["hint"]
    assert "reconciliation" in visible["hint"]
    assert len(calls) == 1
    assert all(ctx.spent_micro == ctx.budget_micro == 1_000 for ctx in contexts)
    after = accounts.get_account_by_id(before.id)
    assert after.spent_micro == before.spent_micro
    assert after.reserved_micro == before.reserved_micro + 1_000
    with db()() as session:
        rows = session.scalars(select(CreditReservation).where(
            CreditReservation.account_id == before.id)).all()
        assert len(rows) == 1 and rows[0].status == "reconciling"
        assert rows[0].settled_micro is None and rows[0].amount_micro == 1_000
    policy.invalidate()


@pytest.mark.hosted_only
@pytest.mark.parametrize("fault", ["settle", "exit"])
@pytest.mark.parametrize("content", [
    '{"acts":[{"intent":"assistant","text":"Explain the saved state"}],"clarify":""}',
    "invalid JSON",
], ids=["valid-json", "invalid-json"])
@pytest.mark.hosted_only
def test_router_finalization_failure_retains_real_hold_without_intent_or_fallback(env, monkeypatch, fault, content):
    from sqlalchemy import select

    from app.chat import assistant, llm_router, router
    from app.credits import accounts, estimate, gate, policy
    from app.db.models import CreditReservation, Venture
    from app.llm import founder_provider
    from app.state import db
    from llm_gateway import sole
    from tests.unit.credits.conftest import _seed_credit_infra

    client, vid = env
    _seed_credit_infra()
    policy.invalidate()
    with db()() as session:
        wid = session.get(Venture, vid).workspace_id
    before = accounts.ensure_account(vid, workspace_id=wid)
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    monkeypatch.setenv("CHAT_ROUTER_MODE", "live")
    monkeypatch.setattr(llm_router, "needs_model", lambda *args: True)
    monkeypatch.setattr(founder_provider, "register_workspace_llm_resolver", lambda: None)
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda _: SimpleNamespace(
        model="DeepSeek-V4-Pro", provider="azure_foundry", source="platform_default"))
    monkeypatch.setattr(estimate, "estimate_llm_micro", lambda *args, **kwargs: 1_000)
    calls = []

    async def transmit(messages, **kwargs):
        calls.append(kwargs)
        with db()() as session:
            rows = session.scalars(select(CreditReservation).where(
                CreditReservation.account_id == before.id)).all()
            assert len(rows) == 1 and rows[0].status == "reconciling"
        return response(content)
    monkeypatch.setattr(sole, "transmit", transmit)

    def broken(*args, **kwargs):
        raise RuntimeError("Synthetic accounting persistence failure")

    async def forbidden(*args, **kwargs):
        pytest.fail("Unconfirmed router settlement cannot enter any intent or assistant fallback")
    monkeypatch.setattr(gate, "settle" if fault == "settle" else "_exit", broken)
    monkeypatch.setattr(router, "_dispatch_intent", forbidden)
    monkeypatch.setattr(assistant, "_complete", forbidden)
    result = client.post(f"/ventures/{vid}/chat", json={"message": "Explain the saved state"})
    assert result.status_code == 409, result.text
    assert result.json()["detail"]["error"]["code"] == "chat_request_outcome_unknown"
    assert len(calls) == 1 and calls[0]["purpose"] == llm_router.PURPOSE
    after = accounts.get_account_by_id(before.id)
    assert after.spent_micro == before.spent_micro
    assert after.reserved_micro == before.reserved_micro + 1_000
    with db()() as session:
        rows = session.scalars(select(CreditReservation).where(
            CreditReservation.account_id == before.id)).all()
        assert len(rows) == 1 and rows[0].status == "reconciling"
        assert rows[0].settled_micro is None and rows[0].amount_micro == 1_000
    policy.invalidate()


@pytest.mark.hosted_only
def test_router_paid_invalid_json_enters_assistant_and_replays_saved_reply(env, monkeypatch):
    """Settled malformed classification reaches assistant once; chat replay stays free."""
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    from sqlalchemy import select

    from app.chat import assistant, llm_router
    from app.credits import accounts, estimate, policy
    from app.db.models import CreditLedgerEntry, CreditReservation, Venture
    from app.db.supymem_models import FounderRequest
    from app.llm import founder_provider
    from app.state import db
    from llm_gateway import sole
    from tests.unit.credits.conftest import _seed_credit_infra

    assert Path(llm_router.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[3])
    client, vid = env
    _seed_credit_infra()
    policy.invalidate()
    with db()() as session:
        wid = session.get(Venture, vid).workspace_id
    before = accounts.ensure_account(vid, workspace_id=wid)
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("CHAT_ROUTER_MODE", "live")
    monkeypatch.setattr(llm_router, "needs_model", lambda *args: True)
    monkeypatch.setattr(founder_provider, "register_workspace_llm_resolver", lambda: None)
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda _: SimpleNamespace(
        model="DeepSeek-V4-Pro", provider="azure_foundry", source="platform_default"))
    monkeypatch.setattr(estimate, "estimate_llm_micro", lambda *args, **kwargs: 1_000)
    calls = []

    async def transmit(messages, **kwargs):
        calls.append(kwargs["purpose"])
        with db()() as session:
            current = session.scalars(select(CreditReservation).where(
                CreditReservation.account_id == before.id,
                CreditReservation.status == "reconciling")).all()
            assert len(current) == 1  # Real protected reservation precedes I/O.
        return response("invalid JSON" if kwargs["purpose"] == llm_router.PURPOSE
                        else "A grounded assistant reply.")

    monkeypatch.setattr(sole, "transmit", transmit)
    payload = {"message": "Explain the saved state", "idempotency_key": "paid-invalid-router-original"}
    result = client.post(f"/ventures/{vid}/chat", json=payload, headers={"X-Subject": "founder"})
    assert calls == [llm_router.PURPOSE, assistant.ASSISTANT_PURPOSE]
    assert result.status_code == 200, result.text
    assert "A grounded assistant reply." in result.json()["body_md"]
    after = accounts.get_account_by_id(before.id)
    assert after.spent_micro > before.spent_micro
    assert after.reserved_micro == before.reserved_micro
    with db()() as session:
        holds = session.scalars(select(CreditReservation).where(
            CreditReservation.account_id == before.id)).all()
        spends = session.scalars(select(CreditLedgerEntry).where(
            CreditLedgerEntry.account_id == before.id,
            CreditLedgerEntry.kind == "spend")).all()
        assert len(holds) == len(spends) == 2
        assert all(hold.status == "settled" for hold in holds)
        assert sum(hold.settled_micro for hold in holds) == after.spent_micro - before.spent_micro
        original = session.scalars(select(FounderRequest).where(FounderRequest.source == "chat_turn_v1")).all()
        assert len(original) == 1 and original[0].outcome == "completed"
    replay = client.post(f"/ventures/{vid}/chat", json=payload, headers={"X-Subject": "founder"})
    assert replay.status_code == 200 and replay.json() == result.json()
    assert calls == [llm_router.PURPOSE, assistant.ASSISTANT_PURPOSE]
    assert accounts.get_account_by_id(before.id).spent_micro == after.spent_micro
    policy.invalidate()
