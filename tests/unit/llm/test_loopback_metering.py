import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from openai import _base_client
from starlette.requests import Request

from llm_gateway import provider_foundry, sole
from tests.company_os_harness import load_company_os_app
from tests.loopback_model import serve_model

sdk_httpx = getattr(_base_client, "httpx", None) or _base_client.httpx2


@pytest.fixture(params=["factory equipment scheduling", "household meal planning"])
def local_owner(request, monkeypatch, tmp_path):
    load_company_os_app()
    from app.core import local_secrets, providers_local
    from llm_gateway.boundary import Decision

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    for name in ("SUPYMEM_DESKTOP_INSTANCE_ID", "SUPYMEM_LLM_PROVIDER", "OPENAI_COMPATIBLE_API_KEY",
                 "OPENAI_COMPATIBLE_BASE_URL", "SUPYMEM_MODEL_CHAT", "SUPYMEM_MODEL_PLANNING", "SUPYMEM_MODEL_EXTRACTION"):
        monkeypatch.delenv(name, raising=False)
    providers_local.save_settings(provider="openai_compatible", base_url="http://127.0.0.1:11434/v1", models={"chat": "small-model"})
    local_secrets.update_settings("spend", {"monthly_spend_cap": 0}, {})
    monkeypatch.setattr(provider_foundry, "sample_mode_enabled", lambda: False)
    monkeypatch.setattr(sole, "sample_mode_enabled", lambda: False)
    monkeypatch.setattr(sole, "boundary_decision", lambda messages, **kwargs: Decision(messages=messages))
    monkeypatch.setattr(sole, "_usage_recorder", None)
    monkeypatch.setattr(providers_local, "_sample_mode", lambda: False)
    return request.param, tmp_path


def fake_server(monkeypatch, installed=True):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/v1/models":
            return sdk_httpx.Response(200, json={"object": "list", "data": [
                {"id": "small-model" if installed else "other-model", "object": "model", "created": 0, "owned_by": "library"}]})
        assert request.url.path == "/v1/chat/completions"
        content = '{"connected":true}' if "response_format" in json.loads(request.content) else "ok"
        return sdk_httpx.Response(200, json={"id": "local_reply", "model": "small-model", "object": "chat.completion", "created": 0,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}})

    monkeypatch.setattr(provider_foundry, "DefaultAsyncHttpxClient", lambda **kwargs:
        sdk_httpx.AsyncClient(**{**kwargs, "transport": sdk_httpx.MockTransport(handler)}))
    return requests


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["venture.chat.assistant", "task.plan", "memory.extract"])
async def test_keyless_roles_record_zero_cost_under_zero_spend_cap(local_owner, monkeypatch, purpose):
    from app.credits import metered
    from app.llm import founder_provider

    audience, directory = local_owner
    fake_server(monkeypatch)
    founder_provider.register_workspace_llm_resolver()
    assert sole._usage_recorder is None
    result = await metered.llm([{"role": "user", "content": audience}], purpose=purpose, workspace_id="local-owner", max_tokens=64)
    assert result.choices[0].message.content == "ok"
    with sqlite3.connect(directory / "local_spend.sqlite3") as connection:
        assert connection.execute("SELECT status, prompt_tokens, completion_tokens, estimate_usd_micro, cost_usd_micro FROM calls").fetchall() == [("confirmed", 7, 3, 0, 0)]


@pytest.mark.asyncio
async def test_saved_loopback_configuration_uses_cpu_timeout(local_owner):
    from app.llm import founder_provider

    config = founder_provider.to_provider_config("local-owner")
    assert config.timeout_seconds == 600
    client, _ = provider_foundry.create_async_client_from_config(config)
    try:
        assert client.timeout.connect == 10 and client.timeout.read == 600
    finally:
        await client.close()


def test_loopback_model_names_do_not_select_a_hosted_protocol(local_owner):
    from app.core.providers_local import load_local_config, save_settings
    from llm_gateway.routing import resolve_purpose_config

    audience, _ = local_owner
    model = "gpt-6.1-sol"
    save_settings(provider="openai_compatible", models={"chat": model})
    purpose = "venture.chat.assistant"
    config = resolve_purpose_config(purpose, load_local_config())
    wire = sole._completion_kwargs([{"role": "user", "content": audience}], config=config, model=model,
        purpose=purpose, max_tokens=80)
    assert config.api_mode == "chat_completions" and wire["model"] == model
    assert wire["max_tokens"] == 80 and "reasoning_effort" not in wire


@pytest.mark.asyncio
async def test_loopback_dial_is_not_cut_short_by_hosted_deadline(local_owner):
    from app.core.providers_local import load_local_config
    from app.research.metered import _transmit_with_deadline

    audience, _ = local_owner

    async def reply(**kwargs):
        await asyncio.sleep(0.02)
        return audience

    assert await _transmit_with_deadline(reply, {"config": load_local_config(), "purpose": "research.deep"}, 0.001) == audience


@pytest.mark.asyncio
@pytest.mark.parametrize("installed", [True, False])
async def test_keyless_connection_probe_lists_models_and_reports_missing_model(local_owner, monkeypatch, installed):
    from app.core import providers_local
    from fastapi import HTTPException

    requests = fake_server(monkeypatch, installed)

    async def receive():
        return {"type": "http.request", "body": json.dumps({"provider": "openai_compatible"}).encode()}

    request = Request({"type": "http", "headers": [(b"content-type", b"application/json")]}, receive)
    if installed:
        response = await providers_local.test_provider(request)
        assert json.loads(response.body)["models"] == ["small-model"]
        assert len(requests) == 2
    else:
        with pytest.raises(HTTPException) as error:
            await providers_local.test_provider(request)
        assert error.value.detail["code"] == "model_not_found"
        assert "Keys and connections" in error.value.detail["message"] and "Chat model" in error.value.detail["message"]
        assert error.value.detail["models"] == ["other-model"]
        assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stall", ["model list", "completion"])
async def test_loopback_connection_probe_bounds_discovery_and_inference(local_owner, monkeypatch, stall):
    from app.core import providers_local
    from app.credits import metered
    from app.llm import founder_provider
    from fastapi import HTTPException

    audience, _ = local_owner
    reached = []
    monkeypatch.setattr(providers_local, "PROVIDER_PROBE_TIMEOUT_SECONDS", 0.02, raising=False)
    provider_config = providers_local._provider_config
    monkeypatch.setattr(providers_local, "_provider_config", lambda settings:
        provider_config(settings).model_copy(update={"timeout_seconds": 0.02}))

    async def models(config):
        reached.append("model list")
        if stall == "model list":
            await asyncio.sleep(0.05)
        return [audience]

    async def completion(*args, **kwargs):
        reached.append("completion")
        await asyncio.sleep(0.05)
        pytest.fail("The probe did not cancel the completion")

    monkeypatch.setattr(founder_provider, "compatible_provider_models", models)
    monkeypatch.setattr(metered, "call", completion)

    async def receive():
        return {"type": "http.request", "body": b'{"provider":"openai_compatible"}'}

    request = Request({"type": "http", "headers": [(b"content-type", b"application/json")]}, receive)
    with pytest.raises(HTTPException) as error:
        await providers_local.test_provider(request)
    assert error.value.status_code == 504
    assert "start your model server" in error.value.detail.lower()
    assert "load your selected model" in error.value.detail.lower()
    assert reached == (["model list"] if stall == "model list" else ["model list", "completion"])


@pytest.fixture
def loopback_server():
    with serve_model() as server:
        yield server


@pytest.mark.parametrize("idea,audience", [
    ("A stock-counting app for independent grocers", "independent grocers"),
    ("A practice journal for beginning violinists", "beginning violinists"),
])
def test_saved_loopback_provider_full_workflow(loopback_server, tmp_path, monkeypatch, idea, audience):
    from tests.company_os_harness import company_os_client, fake_signed_in_codex
    from tests.security.test_durable_journey_boundary import run_queued

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/workflow.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    monkeypatch.setenv("LLM_FALLBACK_PROVIDER", "bedrock")
    monkeypatch.setattr(sole, "_bedrock_fallback", lambda *args, **kwargs: pytest.fail("local flow tried a hosted fallback"))
    app = load_company_os_app()
    from app.coding_agents_consent import set_consent
    from app.core import local_secrets
    from app.core.jobs import FatalError
    from app.core.providers_local import load_local_config
    from app.core.local_owner import ensure_owner
    from app.core.operation_receipts import OperationJournal
    from app.idea.agent import Clarification
    from app.research import deep, metered
    from app.state import db
    from app.db.models import Venture, VentureDocument

    real_client = provider_foundry.DefaultAsyncHttpxClient

    async def loopback_only(request):
        assert request.url.host == "127.0.0.1" and str(request.url).startswith(loopback_server.url + "/")

    def guarded_client(**kwargs):
        kwargs["event_hooks"].setdefault("request", []).append(loopback_only)
        return real_client(**kwargs)

    monkeypatch.setattr(provider_foundry, "DefaultAsyncHttpxClient", guarded_client)
    fake_signed_in_codex(tmp_path, monkeypatch)
    set_consent(ensure_owner().id, "codex", True)
    with company_os_client(app.create_app()) as client:
        provider = {"provider": "openai_compatible", "base_url": loopback_server.url, "model": loopback_server.model}
        assert client.put("/local/providers", json={**provider, "context_window": 19}).status_code == 422
        saved = client.put("/local/providers", json=provider)
        assert saved.status_code == 200 and saved.json()["configured"] and not saved.json()["secrets_set"]["api_key"]
        assert load_local_config().context_window == 8192
        local_secrets.update_settings("spend", {"monthly_spend_cap": 0}, {})
        original_answer = loopback_server.answer
        loopback_server.answer = {"connected": True}
        tested = client.post("/local/providers/test", json=provider)
        loopback_server.answer = original_answer
        assert tested.status_code == 200 and tested.json()["models"] == [loopback_server.model], tested.text
        venture = client.post("/ventures", json={"raw_idea": idea})
        assert venture.status_code == 201, venture.text
        venture_id = venture.json()["id"]
        with db()() as session:
            workspace_id = session.get(Venture, venture_id).workspace_id
        base = f"/ventures/{venture_id}/idea/sessions"
        opened = client.post(base)
        assert opened.status_code == 201, opened.text
        understanding = {"offer": idea, "customer": audience, "problem": "Keeping track is difficult.",
            "customer_extraction": {"text": audience, "kind": "unknown", "quote": idea}}
        question = f"How do {audience} keep records today?"
        loopback_server.answer = Clarification.model_validate({"case": "know", "action": "question", "message": question,
            "understanding": understanding, "question": {"field": "current_workaround", "text": question,
                "why": "Your design should fit their routine.", "options": ["A paper log", "A spreadsheet"]}}).model_dump()
        clarification = base + "/" + opened.json()["id"]
        run_queued("idea.clarify_turn")
        assert any(turn["role"] == "agent" and turn["kind"] == "question" for turn in client.get(clarification).json()["turns"])
        loopback_server.answer = Clarification.model_validate({"case": "know", "action": "proposal", "message": f"Review your idea for {audience}.",
            "understanding": understanding}).model_dump()
        proposed = client.post(clarification + "/propose")
        assert proposed.status_code == 200, proposed.text
        run_queued("idea.clarify_turn", through_job_id=proposed.json()["job_id"])
        idea_session = client.get(clarification).json()
        assert any(turn["role"] == "agent" and turn["text"] == loopback_server.answer["message"] for turn in idea_session["turns"]), idea_session
        confirmed = client.post(clarification + "/confirm", json={"revision_id": idea_session["current_revision"]["id"], "start_research": False})
        assert confirmed.status_code == 200, confirmed.text

        def send(message):
            response = client.post(f"/ventures/{venture_id}/chat", json={"message": message})
            assert response.status_code == 200, response.text
            return response.json()

        loopback_server.answer = f"Your first step is to ask {audience} what they do today."
        reply = send("What does a small pilot teach you?")
        assert loopback_server.answer in reply["body_md"], reply

        review = {"overview": f"You have not verified demand from {audience}.",
            "observations": [{"source_id": "source_2", "quote_index": 0}], "assessments": [],
            "service_checks": [], "competitor_checks": [], "customer_reviews": [], "coverage_gaps": [], "proposed_tests": []}
        loopback_server.answer = review
        loopback_server.reject_format = True
        document = {"_runtime": {"operations": {}}}
        row = SimpleNamespace(venture_id=venture_id, workspace_id=workspace_id, summary=json.dumps(document))
        journal = OperationJournal(read=lambda _: row, document=lambda _: document,
            mutate=lambda did, payload, update: update(row, document, None), before_call=lambda *args: None, prefix="research")
        monkeypatch.setattr(deep, "ex", journal)
        monkeypatch.setattr(deep, "synthesize", lambda *args, **kwargs: metered.synthesize(*args, journal=journal, **kwargs))
        sources = [{"id": f"saved_{index}", "url": f"https://example.org/offers/{index}",
                    "summary": (f"Public notes about {audience} from page {index}. " * 42)[:1900],
                    "evidence": {}} for index in range(24)]
        summary = deep._review_sources("offline-research", {"_job_attempt": 1, "_worker_id": "offline-worker"},
            {"customer": audience, "offer": idea}, sources)
        request = loopback_server.requests[-1][1]
        sent = json.loads(next(message["content"] for message in request["messages"] if message["role"] == "user"))
        assert sent["snapshot"]["customer"] == audience and 1 < len(sent["sources"]) < len(sources)
        assert summary["observations"] == [{"source_id": "saved_1", "quote": sent["sources"][1]["excerpts"][0]}]
        assert summary["observations"][0]["quote"] in sources[1]["summary"]
        assert summary["overview"] == review["overview"]
        assert client.put("/local/providers", json={**provider, "context_window": 2048}).status_code == 200
        calls = len(loopback_server.requests)
        with pytest.raises(FatalError, match="research_context_length_exceeded"):
            deep._review_sources("offline-research", {"_job_attempt": 1, "_worker_id": "offline-worker"},
                {"customer": audience, "offer": idea}, sources, key="small-context")
        assert len(loopback_server.requests) == calls
        assert client.put("/local/providers", json={**provider, "context_window": 8192}).status_code == 200

        loopback_server.reject_format = True
        loopback_server.answer = {"hypothesis": f"You can test demand from {audience}.",
            "action": f"Interview five consenting {audience} about their current workaround.",
            "success_measure": "Pass if three want a trial, fail if none do, otherwise inconclusive."}
        plan = send("Design an experiment to test interest in my idea.")
        assert loopback_server.answer["action"] in plan["body_md"], plan
        loopback_server.reject_format = False

        page = send("Build a landing page with Codex now.")
        with db()() as session:
            orders = session.query(VentureDocument).filter_by(venture_id=venture_id, kind="device_work_order_v1").all()
            assert any(audience in order.provenance_json and "landing page" in order.provenance_json for order in orders), page

        loopback_server.answer = f"You can ask {audience} about their last attempt."
        async def read_stream():
            return [chunk async for chunk in sole.transmit_stream([{"role": "user", "content": idea}],
                purpose="venture.chat.assistant", workspace_id=workspace_id, max_tokens=80)]
        chunks = asyncio.run(read_stream())
        assert chunks[0]["content"] == loopback_server.answer and chunks[-1]["usage"]["completion_tokens"] == 40
        from llm_gateway.errors import ProviderCallError
        loopback_server.unavailable = True
        prior = len(loopback_server.requests)
        with pytest.raises(ProviderCallError, match="Start your model server|Restart your model server"):
            asyncio.run(read_stream())
        assert len(loopback_server.requests) == prior + 1
    assert any(body and body.get("stream") for _, body in loopback_server.requests)
    with sqlite3.connect(tmp_path / "local_spend.sqlite3") as connection:
        rows = connection.execute("SELECT purpose, estimate_usd_micro, cost_usd_micro FROM calls").fetchall()
    assert {"idea.clarify_turn", "venture.chat.assistant", "research.deep"} <= {row[0] for row in rows}
    assert all((estimate, cost) == (0, 0) for _, estimate, cost in rows), rows
