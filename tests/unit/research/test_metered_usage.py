"""Unknown billing receipts stop before output parsing or another provider call."""
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.company_os_harness import load_company_os_app
from tests.unit.credits.conftest import account, credits_env
from tests.unit.credits.test_core_model_receipts import flow as capture_flow, sdk


@pytest.fixture()
def call(monkeypatch, tmp_path):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.credits import accounts, estimate, gate
    from app.core.operation_receipts import OperationJournal
    from app.research import metered

    row = SimpleNamespace(venture_id="ven_usage", workspace_id="wsp_usage")
    doc = {"_runtime": {"operations": {}}}
    recorded, released, parsed, calls, receipts, reconciled = [], [], [], [], [], []
    meter = SimpleNamespace(passthrough=False, hold=SimpleNamespace(reservation_id="hold_usage"),
        task_invocation="inv_usage", protect_outcome=lambda: None, record=lambda **counts: None,
        record_micro=lambda n, **kwargs: recorded.append((n, kwargs)), confirm_unspent=lambda: released.append(True))

    @contextmanager
    def charge(**kwargs):
        try:
            yield meter
        finally:
            receipts.append(deepcopy(doc["_runtime"]["operations"]["review"]))

    journal = OperationJournal(read=lambda _: row, document=lambda _: doc,
        mutate=lambda did, payload, update: update(row, doc, None), before_call=lambda *args: None,
        prefix="research")
    monkeypatch.setattr(accounts, "get_account", lambda _: SimpleNamespace(id="acc_usage", workspace_id="wsp_usage", scope="person"))
    monkeypatch.setattr(gate, "charge_sync", charge)
    monkeypatch.setattr(estimate, "actual_llm_micro", lambda model, **counts: counts["prompt_tokens"] + counts["completion_tokens"])
    def reconcile(usage, binding):
        reconciled.append((deepcopy(usage), binding))
        return 0 if usage.get("definitive_unspent") else usage["actual_micro"]
    monkeypatch.setattr(gate, "reconcile_receipt", reconcile, raising=False)

    def invoke(usage, *, cap=False, byok=False, failure=None, attempt=1, null_output=False):
        def respond():
            calls.append(True)
            if failure:
                raise failure
            return SimpleNamespace(usage=usage, choices=[SimpleNamespace(message=SimpleNamespace(content="not JSON"))])

        return metered.perform("run", "review", {"_job_attempt": attempt, "_worker_id": "worker"},
            purpose="research.deep", provider="test", model="test", estimate=10, journal=journal,
            callback=respond, parse=lambda text: parsed.append(text) or (None if null_output else {}),
            cap_at_reservation=cap, byok=byok)

    return SimpleNamespace(invoke=invoke, doc=doc, calls=calls, recorded=recorded,
                           released=released, parsed=parsed, error=metered.FatalError,
                           receipts=receipts, reconciled=reconciled, journal=journal)


@pytest.mark.parametrize("release_fails", [False, True])
@pytest.mark.parametrize("audience", ["Industrial procurement teams", "Home cooks planning family meals"])
def test_retry_before_provider_call_releases_own_operation_claim(call, monkeypatch, release_fails, audience):
    from app.core.jobs import RetryableError
    from app.credits import gate
    from app.credits.errors import CreditsUnavailable

    call.doc["audience"] = audience
    attempts = []
    def before_call(*args):
        attempts.append(1)
        if len(attempts) == 1:
            raise RetryableError("research_provider_slot_unavailable")
    call.journal.before_call = before_call
    reconcile = gate.reconcile_receipt
    def settle(usage, binding):
        assert usage["reservation_id"] == "hold_usage" and usage["definitive_unspent"]
        if release_fails:
            raise CreditsUnavailable()
        return reconcile(usage, binding)
    monkeypatch.setattr(gate, "reconcile_receipt", settle)
    usage = {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}
    with pytest.raises(RetryableError):
        call.invoke(usage)
    assert call.doc["_runtime"]["operations"]["review"]["state"] == ("release_pending" if release_fails else "retryable")
    assert not call.calls
    monkeypatch.setattr(gate, "reconcile_receipt", reconcile)
    call.invoke(usage, attempt=2)
    assert len(call.calls) == 1 and call.doc["_runtime"]["operations"]["review"]["state"] == "completed"


@pytest.mark.parametrize("usage", [None, {},
    {"prompt_tokens": 2, "completion_tokens": 1},
    {"prompt_tokens": True, "completion_tokens": 1, "total_tokens": 2},
    {"prompt_tokens": -1, "completion_tokens": 1, "total_tokens": 0},
    {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 2},
    {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": True},
    {"prompt_tokens": "2", "completion_tokens": 1, "total_tokens": 3},
])
def test_unknown_usage_is_unretryable_before_output_parse(call, usage):
    with pytest.raises(call.error, match="research_outcome_uncertain"):
        call.invoke(usage)
    assert call.doc["_runtime"]["operations"]["review"]["state"] == "uncertain"
    assert call.recorded == call.released == call.parsed == []
    with pytest.raises(call.error, match="research_outcome_uncertain"):
        call.invoke(usage)
    assert call.calls == [True]


@pytest.mark.parametrize("prompt,completion", [(0, 0), (2, 1)])
def test_complete_sdk_receipt_records_exact_cost_including_zero(call, prompt, completion):
    from openai.types import CompletionUsage
    result = call.invoke(CompletionUsage(prompt_tokens=prompt, completion_tokens=completion,
                                        total_tokens=prompt + completion))
    assert result == {}
    usage = call.doc["_runtime"]["operations"]["review"]["usage"]
    assert usage["actual_micro"] == prompt + completion
    assert usage["provenance"] == "provider_usage" and usage["settled"] is True
    assert call.recorded == [(prompt + completion, {"provider_actual_micro": prompt + completion})] and call.calls == [True]
    assert call.released == [] and call.parsed == ["not JSON"]


def test_journal_captures_complete_binding_cache_and_raw_cost_before_gate_exit(call):
    usage = {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12,
             "prompt_tokens_details": {"cached_tokens": 6}, "completion_tokens_details": {"reasoning_tokens": 0}}
    # A wallet cap rejects the output but cannot hide cost from the Task budget.
    with pytest.raises(call.error, match="research_provider_usage_above_ceiling"):
        call.invoke(usage, cap=True)
    saved = call.receipts[0]
    assert saved["state"] == "responded"
    expected = {"workspace_id": "wsp_usage", "venture_id": "ven_usage", "purpose": "research.deep",
        "provider": "test", "model": "test", "request_id": "run:review:1", "task_invocation": "inv_usage",
        "account_id": "acc_usage", "reservation_id": "hold_usage", "estimate_micro": 10,
        "prompt_tokens": 8, "completion_tokens": 4, "cached_tokens": 6,
        "actual_micro": 10, "provider_actual_micro": 12}
    assert {key: saved["usage"][key] for key in expected} == expected
    assert call.recorded == [(10, {"provider_actual_micro": 12})]
    assert call.reconciled[0][1] == {key: expected[key] for key in
        ("workspace_id", "venture_id", "request_id", "purpose", "provider", "model", "task_invocation")} | {"source": "platform"}


def test_task_reconciliation_failure_replays_saved_receipt_without_redial(call, monkeypatch):
    from app.credits import gate
    from app.core.jobs import RetryableError
    original = gate.reconcile_receipt
    monkeypatch.setattr(gate, "reconcile_receipt", lambda *args: (_ for _ in ()).throw(RuntimeError("counter commit failed")))
    usage = {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}
    with pytest.raises(RetryableError, match="research_settlement_pending"):
        call.invoke(usage)
    assert call.doc["_runtime"]["operations"]["review"]["state"] == "responded"
    monkeypatch.setattr(gate, "reconcile_receipt", original)
    assert call.invoke(usage) == {}
    assert call.calls == [True] and len(call.receipts) == 1
    assert call.reconciled[-1][0]["task_invocation"] == "inv_usage"


def test_definite_rejection_cannot_redial_until_release_and_abort_confirm(call, monkeypatch):
    from app.credits import gate
    from app.core.jobs import RetryableError
    from app.connectors.protocol import RateLimited
    original = gate.reconcile_receipt
    monkeypatch.setattr(gate, "reconcile_receipt", lambda *args: (_ for _ in ()).throw(RuntimeError("release commit failed")))
    for _ in range(2):
        with pytest.raises(RetryableError, match="research_settlement_pending"):
            call.invoke(None, failure=RateLimited(1))
    saved = call.doc["_runtime"]["operations"]["review"]
    assert saved["state"] == "release_pending" and saved["usage"]["definitive_unspent"] is True
    assert call.calls == [True] and call.receipts[0]["state"] == "release_pending"
    monkeypatch.setattr(gate, "reconcile_receipt", original)
    usage = {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}
    assert call.invoke(usage, attempt=2) == {}
    assert call.calls == [True, True] and call.reconciled[0][0]["request_id"] == "run:review:1"


@pytest.mark.parametrize("charge", [None, True, -1])
def test_unconfirmed_recovery_charge_never_completes_or_redials(call, monkeypatch, charge):
    from app.credits import gate
    from app.core.jobs import RetryableError
    monkeypatch.setattr(gate, "reconcile_receipt", lambda *args: charge)
    usage = {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}
    for _ in range(2):
        with pytest.raises(RetryableError, match="research_settlement_pending"):
            call.invoke(usage)
    assert call.calls == [True] and call.doc["_runtime"]["operations"]["review"]["state"] == "responded"


def test_null_parsed_model_output_is_charged_rejected_and_never_redialed(call):
    usage = {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}
    for _ in range(2):
        with pytest.raises(call.error, match="research_invalid_output"):
            call.invoke(usage, null_output=True)
    saved = call.doc["_runtime"]["operations"]["review"]
    assert saved["state"] == "completed" and saved["response"] == {} and saved["usage"]["settled"] is True
    assert call.calls == [True]


def test_null_fixed_provider_receipt_is_charged_rejected_and_never_redialed(call):
    from app.research import metered
    def callback():
        call.calls.append(True)
        return None
    for _ in range(2):
        with pytest.raises(call.error, match="research_invalid_output"):
            metered.perform("run", "review", {"_job_attempt": 1, "_worker_id": "worker"}, purpose="research.search",
                provider="test", estimate=10, journal=call.journal, callback=callback)
    saved = call.doc["_runtime"]["operations"]["review"]
    assert saved["state"] == "completed" and saved["response"] == {} and saved["usage"]["credits_micro"] == 10
    assert call.calls == [True]


@pytest.mark.parametrize("self_hosted,expected", [(True, 1), (False, 10)])
def test_firecrawl_durable_receipt_uses_served_server_price(call, self_hosted, expected):
    from app.research import metered
    result = {"items": [], **({"usage": {"firecrawl_self_hosted": True}} if self_hosted else {})}
    def callback():
        call.calls.append(True)
        return result
    for _ in range(2):
        assert metered.perform("run", "review", {"_job_attempt": 1, "_worker_id": "worker"}, purpose="research.search",
            provider="firecrawl", estimate=10, journal=call.journal, callback=callback) == result
    receipt = call.doc["_runtime"]["operations"]["review"]
    assert receipt["usage"]["actual_micro"] == receipt["usage"]["provider_actual_micro"] == expected
    assert receipt["usage"]["settled"] is True
    assert call.recorded == [(expected, {"provider_actual_micro": expected})]
    assert call.calls == [True]


def test_self_hosted_retry_after_is_preserved_in_durable_failure(call, monkeypatch):
    import httpx
    from app.research import metered
    from app.core.jobs import RetryableError
    monkeypatch.setenv("FIRECRAWL_SELF_HOSTED_URL", "https://crawl.example")
    monkeypatch.setenv("FIRECRAWL_SELF_HOSTED_TOKEN", "private-token")
    def callback():
        httpx.Response(503, headers={"Retry-After": "8"},
            request=httpx.Request("POST", "https://crawl.example/v2/search")).raise_for_status()
    with pytest.raises(RetryableError) as error:
        metered.perform("run", "review", {"_job_attempt": 1, "_worker_id": "worker"}, purpose="research.search",
            provider="firecrawl", estimate=10, journal=call.journal, callback=callback)
    assert error.value.retry_after_s == 8
    assert call.released == [True]
    assert call.doc["_runtime"]["operations"]["review"]["usage"]["credits_micro"] == 0


@pytest.mark.parametrize("purpose", ["research.deep", "research.prior_art.plan"])
@pytest.mark.parametrize("sol", [False, True])
def test_synthesis_quotes_captured_full_wire_before_single_sdk_dial(call, monkeypatch, purpose, sol):
    import json
    from pydantic import SecretStr
    from app.research import metered
    from app.llm import founder_provider
    from app.credits.provider_receipts import estimate_model_binding_micro
    from llm_gateway import sole
    from llm_gateway.boundary import Decision
    from llm_gateway.provider_foundry import ProviderConfig, normalize_base_url
    from openai import AsyncOpenAI, _base_client
    httpx = getattr(_base_client, "httpx", None) or _base_client.httpx2

    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_API_KEY", "offline")
    monkeypatch.setenv("LLM_API_VERSION", "2024-05-01-preview")
    deployment = "sol-deployment" if sol else "ds-deployment"
    catalog = "gpt-6.1-sol" if sol else "DeepSeek-V4-Pro"
    cap = 2048 if sol else 1200
    monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", json.dumps({purpose: {
        "model": deployment, "catalog_model": catalog, "reasoning_effort": "low"}}))
    resolved = founder_provider.ResolvedLLMConfig("wsp_usage", "azure_foundry",
        "https://offline.invalid/api/projects/synthetic", "supymem-pro", None, "platform_default")
    config = ProviderConfig(provider="azure_foundry", base_url=resolved.base_url,
        api_key=SecretStr("offline"), source="platform_default",
        deployment_pro="supymem-pro", deployment_flash="supymem-flash")
    monkeypatch.setattr(founder_provider, "register_workspace_llm_resolver", lambda: None)
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda _: resolved)
    monkeypatch.setattr(sole, "_primary_chat", None)
    monkeypatch.setattr(sole, "_usage_recorder", None)
    monkeypatch.setattr(sole, "boundary_decision", lambda messages, **kw: Decision(messages=messages))
    from llm_gateway import provider_foundry
    monkeypatch.setattr(provider_foundry, "load_provider_config_from_env", lambda: config.model_copy(deep=True))
    schema = {"type": "object", "properties": {"answer": {"type": "string", "maxLength": 30}}, "required": ["answer"]}
    messages = [{"role": "system", "content": "Select only cited evidence."}, {"role": "user", "content": "Synthetic sources."}]
    originals = deepcopy(messages)
    wire, admissions, clients = [], [], []

    def respond(request):
        wire.append(json.loads(request.content))
        assert request.headers["x-stainless-retry-count"] == "0"
        if not sol:
            return httpx.Response(200, json={"id": "chatcmpl_offline", "object": "chat.completion", "created": 1,
                "model": catalog, "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": '{"answer":"cited"}'}}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12}})
        return httpx.Response(200, json={"id": "resp_offline", "object": "response", "model": "gpt-6.1-sol",
            "status": "completed", "service_tier": "default", "output": [{"type": "message", "id": "msg_offline",
                "role": "assistant", "status": "completed", "content": [{"type": "output_text",
                    "text": '{"answer":"cited"}', "annotations": []}]}],
            "usage": {"input_tokens": 8, "output_tokens": 4, "total_tokens": 12,
                "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}})

    def factory(cfg):
        client = AsyncOpenAI(api_key="offline", base_url=normalize_base_url(cfg.provider, cfg.base_url, api_mode=cfg.api_mode),
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)), max_retries=2)
        clients.append(client)
        return client, cfg

    def perform(did, key, payload, **kwargs):
        admissions.append(kwargs)
        binding = kwargs.get("model_binding")
        assert binding is not None, "Research must capture schema/clock wire before the hold"
        assert binding["deployment"] == kwargs["model"] == deployment
        assert binding["catalog_model"] == catalog
        assert binding["api_mode"] == ("responses" if sol else "chat_completions")
        assert binding["max_output_tokens"] == cap
        assert kwargs["provider_identity"] == founder_provider.resolved_provider_identity(resolved)
        assert kwargs["estimate"] == estimate_model_binding_micro(binding)
        assert kwargs["token_ceiling"] == binding["input_token_limit"] + binding["max_output_tokens"]
        assert len(kwargs["request_digest"]) == 64
        # A map change after quoting cannot redirect the funded request.
        monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", "invalid after quote")
        result = kwargs["callback"]()
        assert binding["input_token_limit"] > len(json.dumps(wire[0], ensure_ascii=False).encode())
        return kwargs["parse"](result.choices[0].message.content)

    monkeypatch.setattr(sole, "create_async_client_from_config", factory)
    monkeypatch.setattr(metered, "perform", perform)
    assert metered.synthesize("run", {"_job_attempt": 1, "_worker_id": "worker"},
        purpose=purpose, messages=messages, schema=schema, parse=json.loads, journal=call.journal, max_tokens=1200) == {"answer": "cited"}
    assert len(wire) == len(admissions) == len(clients) == 1 and clients[0].is_closed()
    assert wire[0]["model"] == deployment
    assert wire[0]["max_output_tokens" if sol else "max_tokens"] == cap
    if sol:
        assert wire[0]["reasoning"] == {"effort": "low"} and wire[0]["store"] is False
        assert wire[0]["service_tier"] == "default"
    assert messages == originals


@pytest.mark.parametrize("purpose", ["research.prior_art.plan", "research.deep"])
@pytest.mark.parametrize("sol", [False, True])
def test_production_deepseek_env_research_confirms_receipt(request, monkeypatch, purpose, sol):
    import json

    flow = request.getfixturevalue("capture_flow")
    from app.research import metered
    from llm_gateway import provider_foundry, sole
    from openai import _base_client
    httpx = getattr(_base_client, "httpx", None) or _base_client.httpx2

    for name, value in {"LLM_PROFILE": "prod", "LLM_OFFLINE": "0", "LLM_PROVIDER": "azure_foundry",
        "LLM_API_KEY": "offline", "LLM_BASE_URL": "https://fixture-resource.services.ai.azure.com/api/projects/fixture-project",
        "LLM_DEPLOYMENT_PRO": "DeepSeek-V4-Pro", "LLM_DEPLOYMENT_FLASH": "DeepSeek-V4-Pro",
        "MODEL_ROUTING_OVERRIDES": json.dumps({purpose: {"model": "supymem-gpt-6-1-sol", "catalog_model": "gpt-6.1-sol"}} if sol else {})}.items():
        monkeypatch.setenv(name, value)
    env = provider_foundry.load_provider_config_from_env()
    assert env.catalog_model == "DeepSeek-V4-Pro" and env.api_mode == "chat_completions"
    requests, clients = [], []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"id": "chat_offline", "object": "chat.completion", "created": 1,
                "model": "DeepSeek-V4-Pro", "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": '{"ok":true}'}}],
                "service_tier": None, "usage": {"completion_tokens": 2, "prompt_tokens": 6, "total_tokens": 8,
                    "completion_tokens_details": None, "prompt_tokens_details": None, "audio_prompt_tokens": 0}})
        return httpx.Response(200, json={"id": "resp_offline", "object": "response", "status": "completed",
            "model": "supymem-gpt-6-1-sol", "service_tier": "default", "output": [{"type": "message", "id": "msg_offline",
                "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": '{"ok":true}', "annotations": []}]}],
            "usage": {"input_tokens": 8, "output_tokens": 4, "total_tokens": 12,
                "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}})

    real_http_client = provider_foundry.DefaultAsyncHttpxClient
    def transport(**kwargs):
        client = real_http_client(**{**kwargs, "transport": httpx.MockTransport(respond)})
        clients.append(client)
        return client
    monkeypatch.setattr(provider_foundry, "DefaultAsyncHttpxClient", transport)
    monkeypatch.setattr(sole, "_primary_chat", None)
    monkeypatch.setattr(sole, "_workspace_resolver", None)
    monkeypatch.setattr(sole, "_usage_recorder", None)
    kwargs = {"purpose": purpose, "messages": [{"role": "user", "content": "Return synthetic research JSON."}],
        "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False},
        "parse": json.loads, "journal": flow.journal, "max_tokens": 1200, "repair": False, "cap_at_reservation": True}
    try:
        result = metered.synthesize(flow.did, flow.payload, **kwargs)
    finally:
        path = "/openai/v1/responses" if sol else "/api/projects/fixture-project/openai/v1/chat/completions"
        assert len(requests) == 1 and str(requests[0].url) == "https://fixture-resource.services.ai.azure.com" + path
    assert result == {"ok": True}
    receipt = flow.saved()["usage"]
    assert receipt["model_binding"]["api_mode"] == receipt["provider_receipt"]["api_mode"] == ("responses" if sol else "chat_completions")
    assert receipt["provider_receipt"]["model"] == ("supymem-gpt-6-1-sol" if sol else "DeepSeek-V4-Pro") and receipt["settled"] is True
    assert receipt["provider_receipt"]["cached_tokens"] == receipt["provider_receipt"]["reasoning_tokens"] == 0
    if not sol:
        assert receipt["provider_receipt"]["service_tier"] is None and receipt["provider_actual_micro"] == 4
    assert receipt.get("model_receipt_error") is None and flow.saved()["state"] == "completed"
    assert flow.wallet().reserved_micro == 0 and flow.wallet().spent_micro == receipt["provider_actual_micro"] > 0
    debit = flow.entry()
    monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", "invalid after response")
    assert metered.synthesize(flow.did, flow.payload, **kwargs) == result
    assert flow.entry().id == debit.id and len(requests) == len(clients) == 1 and clients[0].is_closed


def test_nonbinding_research_deepseek_null_details_has_exact_cost_and_no_redial(request):
    import json

    flow = request.getfixturevalue("capture_flow")
    from app.research import metered
    response = sdk(6, 2, model="DeepSeek-V4-Pro")
    response.usage.prompt_tokens_details = response.usage.completion_tokens_details = None
    def respond():
        flow.calls.append(True)
        return response
    kwargs = {"purpose": "research.deep", "provider": "azure_foundry", "model": "DeepSeek-V4-Pro",
        "callback": respond, "estimate": 100, "journal": flow.journal, "parse": json.loads}
    assert metered.perform(flow.did, "review", flow.payload, **kwargs) == {"ok": True}
    assert flow.wallet().spent_micro == 4 and flow.wallet().reserved_micro == 0
    debit = flow.entry()
    assert metered.perform(flow.did, "review", flow.payload, **kwargs) == {"ok": True}
    assert flow.calls == [True] and flow.entry().id == debit.id


@pytest.mark.parametrize("change", ["config", "api_mode", "max_tokens", "purpose"])
def test_captured_research_binding_refuses_different_dial_before_admission(call, monkeypatch, change):
    import json
    from app.research import metered
    from llm_gateway.provider_foundry import load_provider_config_from_env
    from llm_gateway.routing import resolve_purpose_config
    monkeypatch.setenv("LLM_PROFILE", "prod")
    monkeypatch.setenv("LLM_PROVIDER", "azure_foundry")
    monkeypatch.setenv("LLM_API_KEY", "offline")
    monkeypatch.setenv("LLM_API_VERSION", "2024-05-01-preview")
    monkeypatch.setenv("LLM_BASE_URL", "https://offline.invalid/api/projects/synthetic")
    monkeypatch.setenv("LLM_DEPLOYMENT_PRO", "supymem-pro")
    monkeypatch.setenv("LLM_DEPLOYMENT_FLASH", "supymem-flash")
    monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", json.dumps({"research.prior_art.plan": {
        "model": "supymem-gpt-6-1-sol", "catalog_model": "gpt-6.1-sol"}}))
    env = load_provider_config_from_env()
    config = resolve_purpose_config("research.prior_art.plan", env)
    capture = metered.synthesis_capture(config, [], {"type": "json_object"},
        purpose="research.prior_art.plan", max_tokens=2048, request_id="synthetic",
        provider_identity="synthetic")
    dial = dict(messages=[], purpose="research.prior_art.plan", config=config,
        max_tokens=2048, single_attempt=True)
    if change == "api_mode":
        dial["config"] = config.model_copy(update={"api_mode": "chat_completions"})
    elif change == "config":
        dial["config"] = env
    elif change == "purpose":
        dial["purpose"] = "research.deep"
    else:
        dial["max_tokens"] = 4096
    monkeypatch.setattr(metered, "perform", lambda *args, **kwargs: pytest.fail("Mismatch admitted"))
    with pytest.raises(ValueError, match="captured|protocol"):
        metered.perform_llm("run", "review", {}, dial=dial,
            purpose="research.prior_art.plan", **capture)


@pytest.fixture()
def synthesis_flow(capture_flow, monkeypatch):
    import json
    from app.research import metered
    from llm_gateway import sole
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_PROVIDER", "azure_foundry")
    monkeypatch.setenv("LLM_BASE_URL", "https://offline.invalid")
    monkeypatch.setenv("LLM_API_KEY", "offline")
    monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", json.dumps({"research.deep": {
        "model": "sol-deployment", "catalog_model": "gpt-6.1-sol", "reasoning_effort": "low"}}))
    calls, outcomes = [], []
    async def transmit(**kwargs):
        calls.append(kwargs)
        assert kwargs["single_attempt"] is True and kwargs["use_workspace_override"] is False
        assert kwargs["config"].catalog_model == "gpt-6.1-sol"
        outcome = outcomes[len(calls) - 1] if outcomes else sdk(model="gpt-6.1-sol", tier="default")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(sole, "transmit", transmit)
    def invoke(*, repair=False, parser=json.loads):
        return metered.synthesize(capture_flow.did, capture_flow.payload,
            messages=[{"role": "system", "content": "Select only cited evidence."},
                      {"role": "user", "content": "Synthetic evidence."}],
            schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
            parse=parser, journal=capture_flow.journal, max_tokens=1200, repair=repair)
    return SimpleNamespace(invoke=invoke, calls=calls, outcomes=outcomes, flow=capture_flow)


def test_synthesis_real_wallet_uses_original_snapshot_and_replays_without_resolution(synthesis_flow, monkeypatch):
    from app.llm import pricing, founder_provider
    from app.credits.ledger import verify_chain
    f = synthesis_flow
    assert f.invoke() == {"ok": True}
    entry = f.flow.entry()
    receipt = f.flow.saved()["usage"]
    assert receipt["model_binding"]["catalog_model"] == "gpt-6.1-sol"
    assert receipt["provider_actual_micro"] == 248 and f.flow.wallet().spent_micro == 248
    monkeypatch.setattr(pricing, "load_pricing", lambda: pytest.fail("Recovery cannot reprice"))
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda _: pytest.fail("Recovery cannot re-resolve keys"))
    assert f.invoke() == {"ok": True}
    assert f.flow.entry().id == entry.id and len(f.calls) == 1
    assert verify_chain(f.flow.account.id) == (True, None)


@pytest.mark.parametrize("repair", [False, True])
def test_synthesis_known_paid_invalid_is_terminal_once_without_approved_repairs(synthesis_flow, repair):
    from app.core.jobs import FatalError
    f = synthesis_flow
    def invalid(_):
        raise ValueError("synthetic invalid output")
    for _ in range(2):
        with pytest.raises(FatalError, match="research_invalid_output"):
            f.invoke(parser=invalid, repair=repair)
    assert len(f.calls) == 1 and f.flow.wallet().spent_micro == 248
    assert f.flow.saved()["state"] == "completed" and f.flow.saved()["response"] == {}


@pytest.mark.parametrize("unknown", ["counts", "transport"])
def test_synthesis_unknown_retains_original_hold_and_never_repairs(synthesis_flow, monkeypatch, unknown):
    from app.core.jobs import FatalError, RetryableError
    from app.llm import founder_provider
    f = synthesis_flow
    response = sdk(model="gpt-6.1-sol", tier="default")
    response.usage = {"prompt_tokens": "private marker", "completion_tokens": 20, "total_tokens": 120}
    f.outcomes[:] = [TimeoutError("private marker") if unknown == "transport" else response]
    with pytest.raises((FatalError, RetryableError), match="outcome_uncertain|settlement_pending"):
        f.invoke(repair=True)
    held = f.flow.wallet().reserved_micro
    assert held == f.flow.saved()["usage"]["estimate_micro"] > 0 and f.flow.wallet().spent_micro == 0
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda _: pytest.fail("Unknown recovery cannot re-resolve keys"))
    with pytest.raises((FatalError, RetryableError), match="operation_in_progress|outcome_uncertain|settlement_pending"):
        f.invoke(repair=True)
    assert len(f.calls) == 1 and f.flow.wallet().reserved_micro == held
    assert "private marker" not in str(f.flow.saved())


def test_synthesis_settles_from_original_prices_when_live_resources_change(synthesis_flow, monkeypatch):
    from app.llm import pricing
    f = synthesis_flow
    async def transmit(**kwargs):
        f.calls.append(kwargs)
        monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", "invalid after quote")
        monkeypatch.setattr(pricing, "load_pricing", lambda: pytest.fail("Settlement cannot reprice"))
        return sdk(model="gpt-6.1-sol", tier="default")
    from llm_gateway import sole
    monkeypatch.setattr(sole, "transmit", transmit)
    assert f.invoke() == {"ok": True}
    assert len(f.calls) == 1 and f.flow.wallet().spent_micro == 248
    assert f.flow.saved()["usage"]["settled"] is True


def test_review_quote_uses_same_captured_schema_and_purpose(synthesis_flow, monkeypatch):
    import json
    from app.research import metered, selection, service
    from app.llm import founder_provider
    from app.credits import accounts
    from llm_gateway.provider_foundry import load_provider_config_from_env
    from llm_gateway.routing import resolve_purpose_config
    row = SimpleNamespace(workspace_id=synthesis_flow.flow.journal.read(synthesis_flow.flow.did).workspace_id, venture_id="synthetic",
        summary=json.dumps({"_runtime": {"snapshot": {"idea": "synthetic"}}}))
    monkeypatch.setattr(service, "review_inputs", lambda _: (row, [], "source-digest"))
    monkeypatch.setattr(accounts, "get_account", lambda _: SimpleNamespace(granted_micro=10_000_000,
        spent_micro=0, reserved_micro=0, status="active"))
    resolved = founder_provider.resolve_llm_config(row.workspace_id)
    config = resolve_purpose_config("research.deep", load_provider_config_from_env())
    expected = metered.synthesis_capture(config, selection.messages_for({"idea": "synthetic"}, []),
        selection.response_schema(), purpose="research.deep", max_tokens=8192,
        request_id="synthetic:review-quote:source-digest", provider_identity=founder_provider.resolved_provider_identity(resolved))
    quote = service.review_quote("synthetic")
    assert quote["model"] == "sol-deployment" and quote["ceiling_micro"] == expected["estimate"]
    assert quote["sufficient_now"] is True and quote["source_digest"] == "source-digest"
    assert "model_binding" not in quote and "request_digest" not in quote and synthesis_flow.calls == []


def test_synthesis_missing_workspace_refuses_before_key_resolution(call, monkeypatch):
    from app.llm import founder_provider
    from app.research import metered
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setattr(call.journal, "read", lambda _: SimpleNamespace(workspace_id=None))
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda _: pytest.fail("Missing scope cannot resolve keys"))
    with pytest.raises(metered.FatalError, match="research_missing_workspace"):
        metered.synthesize("run", {"_job_attempt": 1}, messages=[], schema={}, parse=lambda _: {}, journal=call.journal)


def test_synthesis_missing_platform_key_refuses_before_hold(synthesis_flow, monkeypatch):
    from llm_gateway.errors import ProviderCallError

    f = synthesis_flow
    monkeypatch.delenv("LLM_API_KEY")
    with pytest.raises(ProviderCallError, match="Your model is not configured") as failure:
        f.invoke()
    assert failure.value.code == "model_not_configured"
    assert f.calls == [] and f.flow.saved() == {}
    assert f.flow.wallet().reserved_micro == f.flow.wallet().spent_micro == 0


def test_pending_founder_receipt_is_publicly_unsettled_without_wallet_hold(synthesis_flow):
    from app.research import service
    f = synthesis_flow.flow
    f.journal.save_operation(f.did, "review", {"state": "provider_responded",
        "usage": {"source": "byok", "reservation_id": None, "purpose": "research.deep"}})
    body = service.get_dossier(f.did)
    assert body["summary"]["usage"]["has_unsettled"] is True
    assert "provider_receipt" not in str(body)
