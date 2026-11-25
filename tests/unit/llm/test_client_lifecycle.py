"""Real SDK clients must release their transport before short-lived loops close."""
import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from openai import AsyncOpenAI, _base_client

from llm_gateway import sole
from llm_gateway.boundary import Decision
from llm_gateway.errors import ProviderCallError
from llm_gateway.provider_foundry import ProviderConfig

# Use the HTTP implementation used by the installed SDK (httpx or httpx2).
httpx = getattr(_base_client, "httpx", None) or _base_client.httpx2


class LoopBoundTransport(httpx.AsyncBaseTransport):
    def __init__(self, mode):
        self.mode = mode
        self.loop = None
        self.closed = False
        self.calls = 0
        self.cancellation = asyncio.CancelledError("original cancellation")

    async def handle_async_request(self, request):
        self.loop = asyncio.get_running_loop()
        self.calls += 1
        if self.mode == "cancel":
            raise self.cancellation
        if self.mode == "error":
            return httpx.Response(429, json={"error": {"message": "offline rate limit"}})
        if self.mode == "stream":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=(
                'data: {"id":"offline","choices":[{"index":0,"delta":{"content":"saved"}}]}\n\n'
                'data: [DONE]\n\n').encode())
        return httpx.Response(200, json={"id": "offline", "object": "chat.completion",
            "model": "DeepSeek-V4-Pro", "choices": [{"index": 0, "message": {"role": "assistant", "content": "saved"}, "finish_reason": "stop"}],
            "data": [{"index": 0, "object": "embedding", "embedding": [0.1]}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}})

    async def aclose(self):
        # A TCP transport schedules its connection cleanup on the owning loop.
        # This guard faithfully rejects the late/wrong-loop close without sockets.
        self.loop.call_soon(lambda: None)
        assert self.loop is asyncio.get_running_loop()
        await asyncio.sleep(0)
        self.closed = True


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    transports = []
    clients = []
    usage = []
    cfg = ProviderConfig(provider="azure_foundry", base_url="https://offline.invalid/openai/v1", source="platform_default")
    mode = ["success"]
    def factory(_):
        transport = LoopBoundTransport(mode[0])
        client = AsyncOpenAI(api_key="offline", base_url=cfg.base_url,
            http_client=httpx.AsyncClient(transport=transport), max_retries=0)
        transports.append(transport)
        clients.append(client)  # prevent GC from hiding missing explicit cleanup
        return client, cfg
    monkeypatch.setattr(sole, "open_env_client", factory)
    monkeypatch.setattr(sole, "create_async_client_from_config", factory)
    monkeypatch.setattr(sole, "_primary_chat", None)
    monkeypatch.setattr(sole, "_workspace_resolver", None)
    monkeypatch.setattr(sole, "_usage_recorder", lambda *args, **kwargs: usage.append(kwargs))
    monkeypatch.setattr(sole, "boundary_decision", lambda messages, **kwargs: Decision(messages=messages))
    monkeypatch.setattr(sole, "_resolved_tier", lambda purpose, tier: tier)
    return cfg, mode, clients, transports, usage


@pytest.mark.parametrize("entry", ["foundry", "byok", "env", "embed"])
@pytest.mark.parametrize("mode_name", ["success", "error", "cancel"])
@pytest.mark.parametrize("threaded", [False, True])
def test_client_closed_on_owning_loop_before_sync_wrapper_returns(gateway, entry, mode_name, threaded):
    cfg, mode, clients, transports, usage = gateway
    mode[0] = mode_name
    async def call():
        messages = [{"role": "user", "content": "offline"}]
        if entry == "foundry":
            return await sole.transmit(messages, purpose="content.writer", single_attempt=True)
        if entry == "byok":
            return await sole.byok_chat_completion(messages, config=cfg, single_attempt=True)
        if entry == "env":
            return await sole.env_chat_completion(messages)
        return await sole.embed(["offline"])
    def run():
        if mode_name == "success":
            asyncio.run(call())
        else:
            with pytest.raises(asyncio.CancelledError if mode_name == "cancel" else ProviderCallError):
                asyncio.run(call())
    # Same short-lived-loop pattern as worker run_sync / synchronous content calls.
    for _ in range(2):
        if threaded:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(run).result()
        else:
            run()
        assert all(t.closed and t.loop.is_closed() and t.calls == 1 for t in transports)
        assert all(c.is_closed() for c in clients)
    assert len(usage) == (2 if mode_name == "success" else 0)


@pytest.mark.parametrize("early_exit", [False, True])
def test_stream_client_closed_on_exhaustion_or_explicit_generator_close(gateway, early_exit):
    _, mode, clients, transports, usage = gateway
    mode[0] = "stream"
    async def run():
        stream = sole.transmit_stream([{"role": "user", "content": "offline"}], purpose="content.writer")
        if early_exit:
            assert (await anext(stream))["content"] == "saved"
            await stream.aclose()
        else:
            assert [chunk async for chunk in stream][-1]["type"] == "usage"
        assert transports[0].closed
    asyncio.run(run())
    assert clients[0].is_closed() and transports[0].loop.is_closed()
    assert len(usage) == (0 if early_exit else 1)


def test_late_sdk_cleanup_reproduces_closed_loop_error(gateway):
    _, _, _, transports, _ = gateway
    async def unowned_call():
        client, _ = sole.open_env_client(None)
        await client.chat.completions.create(model="DeepSeek-V4-Pro", messages=[])
        return client
    client = asyncio.run(unowned_call())
    assert transports[0].loop.is_closed() and not transports[0].closed
    with pytest.raises(RuntimeError, match="Event loop is closed"):
        asyncio.run(client.close())


@pytest.fixture
def failing_close(monkeypatch):
    original = LoopBoundTransport.aclose
    async def close(self):
        await original(self)
        raise RuntimeError("SECRET cleanup URL https://private.invalid?token=hidden")
    monkeypatch.setattr(LoopBoundTransport, "aclose", close)


@pytest.mark.parametrize("entry", ["foundry", "byok", "env", "embed"])
@pytest.mark.parametrize("mode_name", ["success", "error", "cancel"])
def test_cleanup_failure_preserves_provider_outcome(gateway, failing_close, caplog, entry, mode_name):
    cfg, mode, _, transports, usage = gateway
    mode[0] = mode_name
    async def call():
        messages = [{"role": "user", "content": "offline"}]
        if entry == "foundry":
            return await sole.transmit(messages, purpose="content.writer", single_attempt=True)
        if entry == "byok":
            return await sole.byok_chat_completion(messages, config=cfg, single_attempt=True)
        if entry == "env":
            return await sole.env_chat_completion(messages)
        return await sole.embed(["offline"])
    if mode_name == "success":
        result = asyncio.run(call())
        assert result.usage.prompt_tokens == 3
        assert result.usage.total_tokens == 5
        if entry != "embed":
            assert result.choices[0].message.content == "saved"
    elif mode_name == "error":
        with pytest.raises(ProviderCallError) as caught:
            asyncio.run(call())
        assert caught.value.status == 429
    else:
        with pytest.raises(asyncio.CancelledError) as caught:
            asyncio.run(call())
        assert caught.value is transports[0].cancellation
    assert transports[0].closed and transports[0].calls == 1
    assert len(usage) == (1 if mode_name == "success" else 0)
    records = [r for r in caplog.records if r.message == "llm_client_cleanup_failed"]
    assert len(records) == 1 and records[0].error_type == "RuntimeError"
    assert records[0].exc_info is None
    assert "SECRET" not in caplog.text and "private.invalid" not in caplog.text


@pytest.mark.parametrize("early_exit", [False, True])
def test_stream_cleanup_failure_keeps_chunks_and_generator_close(gateway, failing_close, early_exit):
    _, mode, _, transports, usage = gateway
    mode[0] = "stream"
    async def run():
        stream = sole.transmit_stream([{"role": "user", "content": "offline"}], purpose="content.writer")
        assert (await anext(stream))["content"] == "saved"
        if early_exit:
            await stream.aclose()
        else:
            assert [chunk async for chunk in stream][-1]["type"] == "usage"
    asyncio.run(run())
    assert transports[0].closed
    assert len(usage) == (0 if early_exit else 1)


def test_cleanup_failure_does_not_turn_paid_response_into_uncertain_receipt(gateway, failing_close, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.research import metered
    from app.credits import accounts, gate, estimate

    receipts = []
    recorded = []
    journal = SimpleNamespace(
        recover=lambda *args: receipts[-1] if receipts else None,
        read=lambda *args: SimpleNamespace(venture_id="ven_offline", workspace_id="wsp_offline"),
        prepare=lambda *args: "lease",
        begin=lambda *args: None,
        save_operation=lambda did, key, receipt: receipts.append(receipt),
    )
    def record_micro(n, *, provider_actual_micro=None):
        assert provider_actual_micro == n == 5
        recorded.append(n)
    meter = SimpleNamespace(passthrough=False, hold=SimpleNamespace(reservation_id="hold_offline"),
        task_invocation=None, protect_outcome=lambda: None, record=lambda **counts: None,
        record_micro=record_micro)
    @contextmanager
    def charge(**kwargs):
        yield meter
    monkeypatch.setattr(accounts, "get_account", lambda _: SimpleNamespace(id="acc_offline", workspace_id="wsp_offline", scope="person"))
    monkeypatch.setattr(gate, "charge_sync", charge)
    monkeypatch.setattr(estimate, "actual_llm_micro", lambda model, **counts: counts["prompt_tokens"] + counts["completion_tokens"])
    result = metered.perform("run_offline", "writer", {"_job_attempt": 1}, purpose="content.writer", provider="azure_foundry",
        model="DeepSeek-V4-Pro", estimate=10, journal=journal, require_stop=True, parse=lambda content: {"body": content},
        callback=lambda: asyncio.run(sole.transmit([{"role": "user", "content": "offline"}], purpose="content.writer", single_attempt=True)))
    assert result["state"] == "responded" and result["response"] == {"body": "saved"}
    assert result["error_code"] is None
    assert result["usage"]["provenance"] == "provider_usage"
    assert result["usage"]["actual_micro"] == 5 and recorded == [5]


def test_new_task_cancellation_during_cleanup_is_not_swallowed(gateway, monkeypatch):
    async def run():
        entered = asyncio.Event()
        async def close(self):
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(LoopBoundTransport, "aclose", close)
        task = asyncio.create_task(sole.transmit([{"role": "user", "content": "offline"}], purpose="content.writer", single_attempt=True))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
