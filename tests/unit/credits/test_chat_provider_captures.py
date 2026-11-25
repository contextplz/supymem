"""Central chat capture -> actual offline SDK -> original SQLite wallet custody."""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest
from openai import AsyncOpenAI, _base_client
from sqlalchemy import select

httpx = getattr(_base_client, "httpx", None) or _base_client.httpx2
MODEL = "supymem-gpt-6-1-sol"
QUESTION = "Private synthetic capture question"
CLOCK = "Now (server clock): 2026-10-09T12:34:56+00:00 (Friday, UTC); founder timezone: unconfirmed, using UTC."
COUNTS = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
    "input_tokens_details": {"cached_tokens": 80}, "output_tokens_details": {"reasoning_tokens": 7}}
PARENT = "frq_synthetic_original_chat_request"


@pytest.mark.parametrize("audience", ["Industrial procurement teams", "Family cycling groups"])
def test_conn_regression_exhaustion_is_not_an_unknown_provider_call(chat_capture, account, monkeypatch, audience):
    from contextlib import contextmanager
    from app.chat import recovery
    from app.credits.errors import InsufficientCredits, LIMIT_MESSAGE

    original = chat_capture.gate.charge_sync
    failure = InsufficientCredits(account.id, 0, 100)

    @contextmanager
    def exhausted(*args, **kwargs):
        raise failure
        yield

    monkeypatch.setattr(chat_capture.gate, "charge_sync", exhausted)
    with pytest.raises(InsufficientCredits) as caught:
        chat_capture.invoke()
    assert caught.value is failure and chat_capture.wire == []
    reply = recovery.founder_failure(failure, "venture", audience, {"action": "chat.reply"})
    assert reply["body_md"] == LIMIT_MESSAGE
    assert [action["label"] for action in reply["actions"]] == ["Buy more"]
    monkeypatch.setattr(chat_capture.gate, "charge_sync", original)
    assert chat_capture.invoke()
    assert len(chat_capture.wire) == 1


@pytest.fixture
def chat_capture(account, monkeypatch):
    from app.chat import assistant, llm_router
    from app.credits import accounts, gate, metered, provider_receipts
    from app.db.models import CreditReservation, EventLog
    from app.llm import pricing
    from app.state import db
    from app.tenancy import bind
    from core import clock
    from llm_gateway import sole
    from llm_gateway.boundary import Decision
    from llm_gateway.provider_foundry import normalize_base_url

    checkout = Path(__file__).resolve().parents[3]
    for module in (assistant, llm_router, gate, metered, provider_receipts, sole):
        assert Path(module.__file__).resolve().is_relative_to(checkout)
    vid = account.attribution_venture_id
    wid, actor = "ws_wallet:" + vid, "per_wallet:" + vid
    for name, value in {"LLM_OFFLINE": "0", "LLM_PROFILE": "prod", "LLM_PROVIDER": "azure_foundry",
            "LLM_CASSETTE_MODE": "off",
            "LLM_API_KEY": "synthetic-chat-capture-key", "LLM_BASE_URL": "https://offline.invalid/models",
            "LLM_FALLBACK_PROVIDER": "",
            "LLM_DEPLOYMENT_FLASH": "gpt-6.1-sol",
            "MODEL_ROUTING_OVERRIDES": json.dumps({purpose: {"model": MODEL,
                "catalog_model": "gpt-6.1-sol", "reasoning_effort": "low"}
                for purpose in (assistant.ASSISTANT_PURPOSE, llm_router.PURPOSE)})}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("COMPANY_OS_PACKAGED", raising=False)
    monkeypatch.setattr(clock, "_clock", lambda: datetime(2026, 10, 9, 12, 34, 56, tzinfo=timezone.utc))
    monkeypatch.setattr(sole, "time_context", lambda: CLOCK)
    monkeypatch.setattr(sole, "_primary_chat", None)
    monkeypatch.setattr(sole, "_fallback_chat", None)
    monkeypatch.setattr(sole, "_usage_recorder", None)
    monkeypatch.setattr(sole, "boundary_decision", lambda messages, **kwargs: Decision(messages=messages))
    monkeypatch.setattr(llm_router, "_state_hint", lambda *args: {"venture": "Synthetic saved state"})

    wire, clients, configs, quotes, allowances, owner_calls, sleeps = [], [], [], [], [], [], []
    outcome = {"counts": deepcopy(COUNTS), "output": None, "statuses": [200], "flip": False}
    active = {"mode": None, "messages": None, "tools": None, "request_id": None}
    changed_prices = deepcopy(pricing.load_pricing())
    for row in changed_prices["models"].values():
        row["input_per_mtok"], row["output_per_mtok"] = 90, 100

    def rows():
        with db()() as session:
            holds = session.scalars(select(CreditReservation).order_by(CreditReservation.request_id)).all()
            events = session.scalars(select(EventLog).where(
                EventLog.topic.like("credits.model.receipt.%"))).all()
            session.expunge_all()
            return holds, events

    def journal(request_id):
        return {row.topic.rsplit(".", 1)[-1]: json.loads(row.payload_json)
            for row in rows()[1] if json.loads(row.payload_json)["usage"]["request_id"] == request_id}

    original_charge = gate.charge_sync

    @contextmanager
    def charge(*args, **kwargs):
        quotes.append(deepcopy(kwargs))
        if outcome["flip"]:
            monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", "invalid replacement after capture")
            monkeypatch.setenv("LLM_API_KEY", "changed-synthetic-key-after-capture")
            monkeypatch.setenv("LLM_BASE_URL", "https://changed.invalid/models")
            monkeypatch.setattr(pricing, "load_pricing", lambda: changed_prices)
            active["messages"][0]["content"] = "Changed caller message after capture"
            if active["tools"]:
                active["tools"][0]["function"]["name"] = "changed_tool_after_capture"
        with original_charge(*args, **kwargs) as meter:
            yield meter

    monkeypatch.setattr(gate, "charge_sync", charge)
    monkeypatch.setattr(gate, "actual_llm_micro", lambda *args, **kwargs: pytest.fail("Legacy financial repricing used"))
    original_allowance = sole.transport_prompt_tokens

    def allowance(messages, **kwargs):
        assert type(kwargs["clock_text"]) is str and len(kwargs["clock_text"]) == 256
        amount = original_allowance(messages, **kwargs)
        allowances.append({"input_tokens": amount, "tools": deepcopy(kwargs.get("tools")),
            "response_schema": deepcopy(kwargs.get("response_schema")),
            "max_output_tokens": kwargs["config"].max_output_tokens})
        return amount

    monkeypatch.setattr(sole, "transport_prompt_tokens", allowance)
    original_record = gate.record_model_receipt

    def record(meter, usage):
        saved = journal(usage["request_id"])
        assert saved.keys() == {"begin", "calling", "response"}
        assert saved["response"]["usage"] == usage
        assert "actual_micro" not in saved["response"]["usage"]
        owner_calls.append(deepcopy(usage))
        return original_record(meter, usage)

    monkeypatch.setattr(gate, "record_model_receipt", record)
    monkeypatch.setattr(metered, "record_model_receipt", record)

    async def respond(request):
        wire.append(request)
        current_id = quotes[-1]["request_id"]
        saved = journal(current_id)
        assert saved.keys() == {"begin", "calling"}
        holds, _ = rows()
        current = [hold for hold in holds if hold.request_id == current_id]
        assert len(current) == 1 and current[0].status == "reconciling"
        assert current[0].account_id == account.id
        assert saved["begin"]["usage"]["reservation_id"] == current[0].id
        assert saved["begin"]["usage"]["account_id"] == account.id
        assert all(hold.status == ("settled" if active["mode"] == "diagram" else "released")
                   for hold in holds if hold.request_id != current_id)
        if "provider_wait" in outcome:
            outcome["provider_started"].set()
            await outcome["provider_wait"].wait()
        status = outcome["statuses"][min(len(wire) - 1, len(outcome["statuses"]) - 1)]
        if status == 429:
            return httpx.Response(429, json={"error": {"type": "rate_limit_error", "message": "Synthetic rejection"}})
        text = '{"acts":[],"clarify":"Which saved evidence should I use?"}' if active["mode"] == "router" else "A saved answer."
        if active["mode"] == "diagram" and "text" in json.loads(request.content):
            text = ""
        output = outcome["output"] if outcome["output"] is not None else [{"id": "msg_chat_capture",
            "type": "message", "role": "assistant", "status": "completed", "content": [
                {"type": "output_text", "text": text, "annotations": []}]}]
        return httpx.Response(200, json={"id": "resp_chat_capture", "object": "response", "created_at": 0,
            "status": outcome.get("status", "completed"), "incomplete_details": outcome.get("incomplete_details"),
            "model": MODEL, "service_tier": "default",
            "usage": deepcopy(outcome["counts"]), "output": output})

    def factory(config):
        configs.append(config.model_copy(deep=True))
        client = AsyncOpenAI(api_key=config.api_key.get_secret_value(),
            base_url=normalize_base_url(config.provider, config.base_url, api_mode=config.api_mode), max_retries=2,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond),
                follow_redirects=False, trust_env=False))
        clients.append(client)
        return client, config

    monkeypatch.setattr(sole, "create_async_client_from_config", factory)

    async def sleep(seconds):
        holds, _ = rows()
        assert len(holds) == 1 and holds[0].status == "released"
        assert journal(holds[0].request_id)["completed"]["result"]["outcome"] == "rejected"
        sleeps.append(seconds)

    monkeypatch.setattr(assistant.asyncio, "sleep", sleep)

    def invoke(mode="assistant", *, missing=False, tools=False):
        request_id = None if missing else PARENT + (":router" if mode == "router" else ":act:0:answer")
        messages = [{"role": "user", "content": QUESTION}]
        tool_list = [{"type": "function", "function": {"name": "saved_evidence", "description": "Read saved evidence",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}}] if tools else None
        active.update(mode=mode, messages=messages, tools=tool_list, request_id=request_id)
        with bind(workspace_id=wid, venture_id=vid, actor=actor):
            if mode == "router":
                return asyncio.run(llm_router._ask_model(vid, QUESTION,
                    SimpleNamespace(venture={"workspace_id": wid}), request_id=request_id))
            if mode == "diagram":
                messages.insert(0, {"role": "system", "content": assistant.SYSTEM_PROMPT})
                return asyncio.run(assistant._answer_body(messages,
                    assistant.diagram_messages(messages, question="Draw the process as a diagram"),
                    workspace_id=wid, request_id=request_id))
            return asyncio.run(assistant._complete(messages, workspace_id=wid,
                tools=tool_list, request_id=request_id))

    return SimpleNamespace(invoke=invoke, outcome=outcome, workspace_id=wid,
        wallet=lambda: accounts.get_account_by_id(account.id),
        rows=rows, journal=journal, wire=wire, clients=clients, configs=configs, quotes=quotes,
        allowances=allowances, owner_calls=owner_calls, sleeps=sleeps, gate=gate)


@pytest.mark.parametrize("finish", ["timeout", "cancelled"])
def test_rephrase_deadline_keeps_call_alive_and_settles_once(chat_capture, account, monkeypatch, caplog, finish):
    from app.chat import rephrase, guard, decisions, store
    from app.credits.metered import model_call_key, recover_model_call
    from app.db.models import CreditLedgerEntry
    from app.state import db
    from app.tenancy import bind

    flow = chat_capture
    monkeypatch.setenv("CHAT_REPHRASE", "on")
    monkeypatch.setattr(rephrase, "TIMEOUT_S", 60 if finish == "cancelled" else 0.01)
    monkeypatch.setattr(guard, "_pending_card_label", lambda *_: None)
    monkeypatch.setattr(decisions, "pending_for_venture", lambda *_: [])
    monkeypatch.setattr(store, "list_messages", lambda *_, **__: [])
    vid = account.attribution_venture_id
    original = "explain that for the second one"
    flow.outcome["output"] = [{"id": "msg_rephrase", "type": "message", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": "Explain the interviews.", "annotations": []}]}]

    async def run():
        flow.outcome["provider_wait"], flow.outcome["provider_started"] = asyncio.Event(), asyncio.Event()
        token = rephrase.INPUT.set({"venture_id": vid, "original": original})
        try:
            with bind(workspace_id=flow.workspace_id, venture_id=vid, actor="per_wallet:" + vid):
                caller = asyncio.create_task(rephrase.prepare(vid, original,
                    workspace_id=flow.workspace_id, request_id=PARENT))
                await asyncio.wait_for(flow.outcome["provider_started"].wait(), timeout=15)
                if finish == "cancelled":
                    caller.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await caller
                else:
                    assert await caller == (original, original)
                assert await rephrase.prepare(vid, original, workspace_id=flow.workspace_id,
                    request_id=PARENT) == (original, original)
                assert len(flow.wire) == 1 and rephrase._BACKGROUND
                flow.outcome["provider_wait"].set()
                await rephrase.drain()
                assert not rephrase._BACKGROUND
        finally:
            flow.outcome["provider_wait"].set()
            await rephrase.drain()
            rephrase.INPUT.reset(token)

    caplog.set_level("INFO", logger="app.chat.rephrase")
    asyncio.run(run())
    holds, _ = flow.rows()
    assert len(holds) == 1 and holds[0].status == "settled"
    assert flow.wallet().reserved_micro == 0 and flow.wallet().spent_micro == 248
    saved = flow.journal(holds[0].request_id)
    assert saved.keys() == {"begin", "calling", "response", "completed"}
    assert saved["completed"]["result"]["outcome"] == "confirmed"
    with db()() as session:
        debits = session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.account_id == account.id,
            CreditLedgerEntry.idempotency_key == "settle:" + holds[0].id)).all()
    assert len(debits) == 1 and debits[0].amount_micro == -248
    usage = saved["completed"]["usage"]
    recover_model_call(model_call_key(**{name: usage[name] for name in
        ("workspace_id", "venture_id", "purpose", "request_id")}, actor_id=usage.get("actor_id")))
    assert flow.wallet().spent_micro == 248 and len(flow.wire) == 1
    assert json.loads(flow.wire[0].content)["reasoning"] == {"effort": "low"}
    assert json.loads(flow.wire[0].content)["max_output_tokens"] == 2048
    records = [record for record in caplog.records if record.name == "app.chat.rephrase"]
    assert len(records) == 1 and original not in records[0].getMessage()


@pytest.mark.parametrize("mode", ["assistant", "router"])
@pytest.mark.parametrize("counts,cost", [
    ({"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
      "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}, 0),
    ({"input_tokens": 1, "output_tokens": 0, "total_tokens": 1,
      "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}, 2),
    (COUNTS, 248),
    ({"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
      "input_tokens_details": None, "output_tokens_details": None}, 400),
], ids=["zero", "tiny", "cached-and-reasoning", "unreported-breakdown"])
def test_actual_sdk_receipt_owns_exact_original_wallet_fee(chat_capture, mode, counts, cost):
    flow = chat_capture
    flow.outcome["counts"] = deepcopy(counts)
    assert flow.invoke(mode)
    assert flow.wallet().spent_micro == cost and flow.wallet().reserved_micro == 0
    holds, events = flow.rows()
    assert len(flow.wire) == len(flow.clients) == len(holds) == len(flow.owner_calls) == 1
    assert flow.clients[0].is_closed() and flow.wire[0].headers["x-stainless-retry-count"] == "0"
    assert holds[0].settled_micro == cost and holds[0].request_id == PARENT + (
        ":router" if mode == "router" else ":act:0:answer") + ":provider-attempt:0"
    saved = flow.journal(holds[0].request_id)
    receipt = saved["response"]["usage"]["provider_receipt"]
    assert receipt["input_tokens"] == counts["input_tokens"]
    assert receipt["cached_tokens"] == (counts["input_tokens_details"] or {"cached_tokens": 0})["cached_tokens"]
    assert receipt["reasoning_tokens"] == (counts["output_tokens_details"] or {"reasoning_tokens": 0})["reasoning_tokens"]
    assert saved["completed"]["result"] == {"credits_micro": cost, "output_usable": True, "outcome": "confirmed"}
    assert all(QUESTION not in event.payload_json and "synthetic-chat-capture-key" not in event.payload_json for event in events)


@pytest.mark.parametrize("mode", ["assistant", "router"])
def test_capture_before_charge_freezes_route_quote_output_schema_and_tools(chat_capture, mode):
    from app.credits.provider_receipts import estimate_model_binding_micro

    flow = chat_capture
    flow.outcome["flip"] = True
    assert flow.invoke(mode, tools=mode == "assistant")
    assert flow.wallet().spent_micro == 248 and flow.wallet().reserved_micro == 0
    quote, config = flow.quotes[0], flow.configs[0]
    binding = quote["model_binding"]
    body = json.loads(flow.wire[0].content)
    output_limit = 2048 if mode == "router" else 2304
    assert config.model_dump()["max_output_tokens"] == binding["max_output_tokens"] == body["max_output_tokens"] == output_limit
    assert config.routing_purpose == binding["purpose"] and config.captured_tier == "flash"
    assert config.catalog_model == binding["catalog_model"] == "gpt-6.1-sol"
    assert config.api_mode == binding["api_mode"] == "responses"
    assert config.api_key.get_secret_value() == "synthetic-chat-capture-key"
    assert str(flow.wire[0].url) == "https://offline.invalid/openai/v1/responses"
    assert body["model"] == binding["deployment"] == MODEL and body["reasoning"] == {"effort": "low"}
    assert binding["requested_service_tier"] == body["service_tier"] == "default"
    assert binding["pricing_snapshot"]["models"]["gpt-6.1-sol"]["input_per_mtok"] == "2"
    assert quote["estimate_micro"] == estimate_model_binding_micro(binding)
    assert quote["max_tokens"] == binding["input_token_limit"] + output_limit
    assert binding["input_token_limit"] == flow.allowances[0]["input_tokens"]
    assert flow.allowances[0]["max_output_tokens"] == output_limit
    assert re.fullmatch(r"[a-f0-9]{64}", quote["request_digest"])
    assert binding["input_token_limit"] >= len(flow.wire[0].content) + 128 + 16 * len(body["input"])
    assert sum(message.get("content") == CLOCK for message in body["input"]) == 1
    assert len(json.dumps(CLOCK, ensure_ascii=False)[1:-1].encode()) <= 256
    assert QUESTION in json.dumps(body) and "Changed caller message" not in json.dumps(body)
    if mode == "router":
        assert body["text"]["format"] == {"type": "json_object"}
        assert flow.allowances[0]["response_schema"] == {"type": "json_object"}
    else:
        assert body["tools"][0]["name"] == "saved_evidence" and body["tool_choice"] == "auto"
        assert flow.allowances[0]["tools"][0]["function"]["name"] == "saved_evidence"
    saved = flow.journal(flow.rows()[0][0].request_id)
    assert saved["begin"]["usage"]["model_binding"] == binding
    assert saved["response"]["usage"]["request_digest"] == quote["request_digest"]


@pytest.mark.parametrize("mode", ["assistant", "router"])
def test_unadmitted_caller_cannot_mint_authority_or_dispatch(chat_capture, mode):
    flow = chat_capture
    assert not flow.invoke(mode, missing=True)
    assert not flow.wire and flow.rows() == ([], []) and not flow.quotes
    assert flow.wallet().spent_micro == flow.wallet().reserved_micro == 0


def test_assistant_full_wire_quote_enforces_tool_turn_limit_before_hold(chat_capture):
    from app.chat import agent, assistant
    from app.tools import tool_specs
    from llm_gateway.errors import ProviderCallError

    flow = chat_capture
    messages = [{"role": "system", "content": "Use the saved evidence only."},
        {"role": "user", "content": QUESTION}]
    tools = tool_specs()
    request_id = PARENT + ":act:0:tools:model-round:0"
    workspace_id = flow.workspace_id
    estimate = agent._estimate(messages, workspace_id, tools=tools, request_id=request_id)
    with pytest.raises(ProviderCallError) as failure:
        asyncio.run(assistant._complete(messages, workspace_id=workspace_id,
            tools=tools, request_id=request_id, max_estimate_micro=estimate - 1))
    assert failure.value.code == "turn_budget_exceeded"
    assert len(flow.allowances) == 2
    assert flow.allowances[0] == flow.allowances[1]
    assert flow.allowances[0]["tools"] == tools
    assert not flow.quotes and not flow.wire and flow.rows() == ([], [])


@pytest.mark.parametrize("mode", ["assistant", "router"])
def test_raw_numeric_string_retries_only_hosted_chat_without_charging_unconfirmed_usage(chat_capture, mode):
    from llm_gateway.errors import ProviderCallError

    flow = chat_capture
    flow.outcome["counts"]["input_tokens"] = "100"
    for _ in range(2):
        with pytest.raises(ProviderCallError) as failure:
            flow.invoke(mode)
        assert failure.value.code == "provider_outcome_unknown" and not failure.value.retryable
    holds, _ = flow.rows()
    hosted_reply = mode != "router"
    assert len(flow.wire) == len(flow.clients) == len(holds) == (2 if hosted_reply else 1)
    assert all(client.is_closed() for client in flow.clients)
    assert holds[0].status == ("released" if hosted_reply else "reconciling")
    assert holds[0].request_id.endswith(":provider-attempt:0")
    assert flow.wallet().spent_micro == 0
    assert flow.wallet().reserved_micro == (0 if hosted_reply else holds[0].amount_micro)
    saved = flow.journal(holds[0].request_id)
    if hosted_reply:
        assert all(hold.status == "released" for hold in holds)
        assert flow.configs[1].deployment_flash == "gpt-6.1-sol"
        assert flow.configs[1].timeout_seconds < flow.configs[0].timeout_seconds
        assert flow.journal(holds[1].request_id).keys() == {"begin", "calling", "failure", "absorbed"}
        assert saved.keys() == {"begin", "calling", "failure", "absorbed"}
        assert saved["absorbed"]["outcome"] == "platform_absorbed"
        assert "actual_micro" not in saved["absorbed"]["usage"]
    else:
        assert saved.keys() == {"begin", "calling", "response"}
        assert saved["response"]["usage"]["provider_receipt"] is None
        assert saved["response"]["usage"]["model_receipt_error"] == "counts_unknown"
        assert "actual_micro" not in saved["response"]["usage"]
    assert not flow.sleeps


@pytest.mark.parametrize("mode", ["assistant", "router"])
def test_known_paid_structurally_bad_output_is_charged_then_demoted_or_withheld(chat_capture, mode):
    from llm_gateway.errors import ProviderCallError

    flow = chat_capture
    flow.outcome["output"] = [None]
    for attempt in range(2):
        if mode == "router" and attempt == 0:
            result = flow.invoke(mode)
            assert result.acts == [("assistant", QUESTION)]
            continue
        with pytest.raises(ProviderCallError) as failure:
            flow.invoke(mode)
        assert failure.value.code == ("provider_output_invalid" if attempt == 0 else "provider_outcome_unknown")
        assert not failure.value.retryable and not getattr(failure.value, "unspent_confirmed", False)
    holds, _ = flow.rows()
    assert len(flow.wire) == len(flow.clients) == len(holds) == 1 and flow.clients[0].is_closed()
    assert holds[0].status == "settled" and holds[0].settled_micro == 248
    assert flow.wallet().spent_micro == 248 and flow.wallet().reserved_micro == 0
    saved = flow.journal(holds[0].request_id)
    assert saved["response"]["usage"]["provider_output_error"] == "invalid_output"
    assert saved["completed"]["result"] == {"credits_micro": 248, "output_usable": False, "outcome": "confirmed"}
    assert not flow.sleeps


@pytest.mark.parametrize("audience", ["Warehouse loading teams", "Weekend hiking groups"])
def test_absorbed_receipt_survives_release_failure_without_duplicate_event(chat_capture, monkeypatch, audience):
    from app.credits import metered, reserve
    from llm_gateway.errors import ProviderCallError

    monkeypatch.setitem(globals(), "QUESTION", f"Explain the workflow for {audience}.")
    original_release = reserve.release
    original_absorb = metered.absorb_hosted_chat_failure
    meters = []

    def capture(meter):
        meters.append(meter)
        original_absorb(meter)

    def failed_release(*args, **kwargs):
        raise RuntimeError("Synthetic interruption after the receipt was saved")

    monkeypatch.setattr(metered, "absorb_hosted_chat_failure", capture)
    monkeypatch.setattr(reserve, "release", failed_release)
    chat_capture.outcome["counts"]["input_tokens"] = "100"
    with pytest.raises(ProviderCallError):
        chat_capture.invoke()
    assert len(meters) == 1 and chat_capture.rows()[0][0].status == "reconciling"
    monkeypatch.setattr(reserve, "release", original_release)
    original_absorb(meters[0])
    assert chat_capture.rows()[0][0].status == "released"
    assert chat_capture.wallet().reserved_micro == chat_capture.wallet().spent_micro == 0
    assert chat_capture.journal(chat_capture.rows()[0][0].request_id)["absorbed"]["outcome"] == "platform_absorbed"


def test_empty_diagram_plain_retry_has_distinct_captured_admissions_and_exact_settlement(chat_capture, caplog):
    from llm_gateway.errors import ProviderCallError

    flow = chat_capture
    reply = flow.invoke("diagram")
    assert reply == "A saved answer." and reply.parts == [{"type": "text", "text": str(reply)}]
    holds, _ = flow.rows()
    assert len(flow.wire) == len(holds) == len(flow.owner_calls) == 2
    prefix = PARENT + ":act:0:answer"
    assert [hold.request_id for hold in holds] == [prefix + ":plain:provider-attempt:0", prefix + ":provider-attempt:0"]
    assert all(hold.status == "settled" and hold.settled_micro == 248 for hold in holds)
    assert flow.wallet().spent_micro == 496 and flow.wallet().reserved_micro == 0
    assert flow.allowances[0]["response_schema"]["required"] == ["parts"]
    assert flow.allowances[1]["response_schema"] is None
    first, plain = [json.loads(request.content) for request in flow.wire]
    assert first["text"]["format"] == {"type": "json_object"} and "text" not in plain
    assert "show_diagram" not in json.dumps(plain)
    assert flow.quotes[0]["request_digest"] != flow.quotes[1]["request_digest"]
    assert all(flow.journal(hold.request_id)["completed"]["result"] == {
        "credits_micro": 248, "output_usable": True, "outcome": "confirmed"} for hold in holds)
    assert "chat_diagram_mode_failed reason=empty_parts" in caplog.text
    with pytest.raises(ProviderCallError):
        flow.invoke("diagram")
    assert len(flow.wire) == 2 and flow.wallet().spent_micro == 496


def test_empty_length_exhausted_assistant_is_charged_once_and_never_replayed(chat_capture, caplog):
    from llm_gateway.errors import ProviderCallError

    flow = chat_capture
    flow.outcome.update(output=[], status="incomplete", incomplete_details={"reason": "max_output_tokens"},
        counts={"input_tokens": 100, "output_tokens": 3072, "total_tokens": 3172,
            "input_tokens_details": {"cached_tokens": 80}, "output_tokens_details": {"reasoning_tokens": 3072}})
    assert flow.invoke() == ""  # the caller answers with its grounded fallback
    holds, _ = flow.rows()
    assert len(flow.wire) == len(holds) == 1 and holds[0].status == "settled"
    assert holds[0].settled_micro == flow.wallet().spent_micro > 0 and flow.wallet().reserved_micro == 0
    assert not flow.sleeps
    records = [record for record in caplog.records if record.name == "app.chat.provider"]
    assert [record.provider_failure_reason for record in records] == ["empty_content", "finish_reason:length"]
    assert all(record.provider_content_length == 0 for record in records)
    with pytest.raises(ProviderCallError) as replay:
        flow.invoke()
    assert replay.value.code == "provider_outcome_unknown" and len(flow.wire) == 1


@pytest.mark.parametrize("mode", ["assistant", "router"])
@pytest.mark.parametrize("released", [True, False], ids=["durable-release", "release-unconfirmed"])
def test_429_advances_suffix_only_after_durable_original_release(chat_capture, monkeypatch, mode, released):
    from llm_gateway.errors import ProviderCallError

    flow = chat_capture
    flow.outcome["statuses"] = [429, 200]
    if not released:
        def failed_release(*args, **kwargs):
            raise RuntimeError("Synthetic durable release failure")

        monkeypatch.setattr(flow.gate, "release", failed_release)
        with pytest.raises(ProviderCallError):
            flow.invoke(mode)
        holds, _ = flow.rows()
        assert len(flow.wire) == len(flow.clients) == len(holds) == 1
        assert holds[0].status == "reconciling" and not flow.sleeps
        assert flow.wallet().spent_micro == 0 and flow.wallet().reserved_micro == holds[0].amount_micro
        assert "completed" not in flow.journal(holds[0].request_id)
    else:
        assert flow.invoke(mode)
        holds, _ = flow.rows()
        prefix = PARENT + (":router" if mode == "router" else ":act:0:answer") + ":provider-attempt:"
        assert [hold.request_id for hold in holds] == [prefix + "0", prefix + "1"]
        assert [hold.status for hold in holds] == ["released", "settled"]
        assert flow.journal(prefix + "0")["completed"]["result"] == {
            "credits_micro": 0, "output_usable": False, "outcome": "rejected"}
        assert flow.wallet().spent_micro == 248 and flow.wallet().reserved_micro == 0
        assert len(flow.wire) == len(flow.clients) == 2 and flow.sleeps == [1]
    assert all(client.is_closed() for client in flow.clients)
    assert all(request.headers["x-stainless-retry-count"] == "0" for request in flow.wire)
