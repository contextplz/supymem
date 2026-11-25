"""Real blocked async transport cancellation without network or GUI."""
import asyncio
import threading
import time

import httpx
import pytest

from desktop_runtime.cancellable_http import CancellableHTTP, ModelRequestDeadline, ModelRequestStopped


def test_stop_cancels_blocked_transport_and_releases_bounded_capacity():
    async def scenario():
        entered, cleaned = asyncio.Event(), asyncio.Event()
        async def handler(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        controller = CancellableHTTP(transport=httpx.MockTransport(handler), max_pending=1)
        stop = threading.Event()
        def request():
            return controller.request("POST", "https://example.invalid/ai-step", body={},
                headers={}, stop=stop, deadline=time.monotonic() + 5)
        worker = asyncio.create_task(asyncio.to_thread(request))
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(RuntimeError, match="transport_busy"):
            await asyncio.to_thread(request)
        began = time.monotonic()
        stop.set()
        with pytest.raises(ModelRequestStopped):
            await asyncio.wait_for(worker, 1)
        await asyncio.wait_for(cleaned.wait(), 1)
        await asyncio.sleep(0)
        assert time.monotonic() - began < 1
        assert controller.pending == 0 and not controller.tasks
        await controller.close()
    asyncio.run(scenario())


def test_deadline_cancels_transport_without_replay():
    async def scenario():
        calls = 0
        cleaned = asyncio.Event()
        async def handler(request):
            nonlocal calls
            calls += 1
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        controller = CancellableHTTP(transport=httpx.MockTransport(handler))
        with pytest.raises(ModelRequestDeadline):
            await asyncio.to_thread(controller.request, "POST", "https://example.invalid/ai-step",
                body={}, headers={}, stop=threading.Event(), deadline=time.monotonic() + .1)
        await asyncio.wait_for(cleaned.wait(), 1)
        await asyncio.sleep(0)
        assert calls == 1 and controller.pending == 0
        await controller.close()
    asyncio.run(scenario())


def test_browser_timeout_is_longer_but_stop_and_deadline_still_bound_it():
    async def scenario():
        seen = []
        async def handler(request):
            seen.append(request.extensions["timeout"])
            return httpx.Response(200, json={"ok": True})
        controller = CancellableHTTP(transport=httpx.MockTransport(handler))
        for route in ("browser-ai-call", "ai-step"):
            await asyncio.to_thread(controller.request, "POST", "https://example.invalid/" + route,
                body={}, headers={}, stop=threading.Event(), deadline=time.monotonic() + 1)
        assert seen[0]["read"] == 360 and seen[1]["read"] == 40
        assert seen[0]["connect"] == seen[1]["connect"] == 5
        await controller.close()
    asyncio.run(scenario())


def test_response_racing_stop_is_not_delivered_and_repeated_requests_do_not_leak():
    async def scenario():
        stop = threading.Event()
        calls = 0
        async def handler(request):
            nonlocal calls
            calls += 1
            stop.set()
            return httpx.Response(200, json={"late": True})
        controller = CancellableHTTP(transport=httpx.MockTransport(handler), max_pending=1)
        for _ in range(20):
            stop.clear()
            with pytest.raises(ModelRequestStopped):
                await asyncio.to_thread(controller.request, "POST", "https://example.invalid/ai-step",
                    body={}, headers={}, stop=stop, deadline=time.monotonic() + 1)
            await asyncio.sleep(0)
            assert controller.pending == 0 and not controller.tasks
        assert calls == 20
        await controller.close()
        with pytest.raises(RuntimeError, match="transport_unavailable"):
            stop.clear()
            await asyncio.to_thread(controller.request, "GET", "https://example.invalid/authority",
                body=None, headers={}, stop=stop, deadline=time.monotonic() + 1)
    asyncio.run(scenario())


def test_close_cancels_active_native_waiter_without_blocking_host_loop():
    async def scenario():
        entered = asyncio.Event()
        async def handler(request):
            entered.set()
            await asyncio.Event().wait()
        controller = CancellableHTTP(transport=httpx.MockTransport(handler))
        worker = asyncio.create_task(asyncio.to_thread(controller.request,
            "POST", "https://example.invalid/ai-step", body={}, headers={},
            stop=threading.Event(), deadline=time.monotonic() + 5))
        await asyncio.wait_for(entered.wait(), 1)
        await controller.close()
        with pytest.raises(ModelRequestStopped):
            await asyncio.wait_for(worker, 1)
        assert controller.pending == 0 and not controller.tasks
    asyncio.run(scenario())


def test_gateway_cancellation_covers_authority_before_and_after_model():
    from desktop_runtime.device_gateway import WorkOrderGateway
    calls = []
    stop = threading.Event()
    deadline = time.monotonic() + 1
    def unsafe_sync(*_):
        pytest.fail("model call escaped cancellable transport")
    def request(method, path, body, **kwargs):
        assert kwargs["stop"] is stop and kwargs["deadline"] == deadline
        calls.append((method, path))
        if path.endswith("/authority"):
            return {"current": True}
        if method == "GET":
            return {"id": "order", "venture_id": "venture", "device_id": "device",
                    "state": "claimed", "execution_id": "exec", "review_digest": "a" * 64}
        return {"authority": "proposal_only", "decision": {}}
    gateway = WorkOrderGateway(venture_id="venture", order_id="order", device_id="device",
                               request=unsafe_sync, cancellable_request=request)
    gateway.claimed, gateway.execution_id = True, "exec"
    gateway.ai_step("inv", "step", {}, cancelled=stop, deadline=deadline)
    assert [method for method, _ in calls] == ["GET", "GET", "POST", "GET", "GET"]


def test_gateway_refuses_unsafe_sync_fallback_when_cancel_transport_missing():
    from desktop_runtime.device_gateway import WorkOrderGateway
    gateway = WorkOrderGateway(venture_id="venture", order_id="order", device_id="device",
                              request=lambda *_: pytest.fail("unsafe fallback"))
    with pytest.raises(RuntimeError, match="transport_unavailable"):
        gateway.ai_step("inv", "step", {}, cancelled=threading.Event())


@pytest.mark.parametrize("route", ["ai-step", "browser-ai-call"])
def test_actual_bridge_lifespan_owned_model_request_is_interruptible(monkeypatch, tmp_path, route):
    import concurrent.futures
    from fastapi.testclient import TestClient
    from desktop_runtime.bridge import create_bridge_app
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BRIDGE_TOKEN", "synthetic-local-test-token")
    monkeypatch.setenv("GATEWAY_URL", "https://example.invalid")
    monkeypatch.delenv("COMPANY_OS_PUBLIC_URL", raising=False)
    monkeypatch.delenv("BRIDGE_AUTH_OWNER", raising=False)
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.core.local_mode import is_local
    entered, cleaned = threading.Event(), threading.Event()
    async def handler(request):
        if request.url.path.endswith("/" + route):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        if request.url.path.endswith("/authority"):
            return httpx.Response(200, json={"current": True})
        return httpx.Response(200, json={"id": "order", "venture_id": "venture",
            "device_id": "device", "state": "claimed", "execution_id": "exec",
            "review_digest": "a" * 64})
    app = create_bridge_app(transport=httpx.MockTransport(handler))
    with TestClient(app):
        session = app.state.bridge_session
        session.device_id = "device"
        if not is_local():
            session.account_id = "usr_cancellation_test"
            session.access_token, session.expires_at, session.connected = "synthetic-test-token", time.time() + 900, True
        gateway = app.state.native_order_gateway("venture", "order")
        gateway.claimed, gateway.execution_id = True, "exec"
        stop = threading.Event()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            # Exercise the exact signed bridge route, with its real async controller.
            future = executor.submit(gateway.cancellable_request, "POST", gateway.path + "/" + route, {},
                                     stop=stop, deadline=time.monotonic() + 5)
            assert entered.wait(1)
            stop.set()
            with pytest.raises(ModelRequestStopped):
                future.result(timeout=1)
            assert cleaned.wait(1)
        with pytest.raises(PermissionError, match="scope_invalid"):
            gateway.cancellable_request("GET", "/api/other-account", None,
                                         stop=threading.Event(), deadline=time.monotonic() + 1)
        session.device_id = "different-device"
        with pytest.raises(PermissionError, match="account_changed"):
            gateway.cancellable_request("GET", gateway.path, None,
                                         stop=threading.Event(), deadline=time.monotonic() + 1)
    assert app.state.model_http.closed


@pytest.mark.parametrize("through_adapter", [False, True])
def test_actual_bridge_browser_cuai_uses_owned_event_and_exact_phase_route(
    monkeypatch, tmp_path, through_adapter
):
    """The installed bridge boundary, rather than a callable-only fake gateway."""
    import json
    from fastapi.testclient import TestClient
    from desktop_runtime.bridge import create_bridge_app
    from desktop_runtime.cu_owned_browser_adapter import CuOwnedBrowserAdapter, profile_digest
    from tests.unit.product.test_cu_owned_browser_adapter import Authority, Browser, GOAL, actor

    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BRIDGE_TOKEN", "synthetic-local-test-token")
    monkeypatch.setenv("GATEWAY_URL", "https://example.invalid")
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.core.local_mode import is_local
    monkeypatch.delenv("COMPANY_OS_PUBLIC_URL", raising=False)
    monkeypatch.delenv("BRIDGE_AUTH_OWNER", raising=False)
    authority = Authority([
        {"subgoals": ["Read the current offer"]},
        actor("done", summary="Current eligible price is $23."),
        {"passed": True, "reason": "Observed exact requested fact"},
    ])
    authority.state = "claimed"
    profile = authority.manifest["general_browser_verification"]["execution_profile"]
    for role in profile["roles"].values():
        role["model"] = "gpt-5.6-luna"
    posts = []

    def handler(request):
        body = json.loads(request.content) if request.content and request.method == "POST" else None
        path = request.url.path
        if path.startswith("/ventures/"):
            path = "/api" + path
        value = authority.request(request.method, path, body)
        if is_local() and request.method == "GET" and path.endswith("/dwo_cuai"):
            value["actor_id"] = app.state.bridge_session.account_id
        if request.method == "POST":
            posts.append(path)
        if request.url.path.endswith("/browser-ai-call"):
            value["model"] = "gpt-5.6-luna"
        return httpx.Response(200, json=value)

    app = create_bridge_app(transport=httpx.MockTransport(handler))
    with TestClient(app):
        session = app.state.bridge_session
        session.device_id = "dev_cuai"
        if not is_local():
            session.account_id = "usr_cancellation_test"
            session.access_token, session.expires_at, session.connected = "synthetic-test-token", time.time() + 900, True
        gateway = app.state.native_order_gateway("ven_cuai", "dwo_cuai")
        gateway.claimed, gateway.execution_id = True, "dex_cuai"
        gateway.browser_cuai_profile_digest = profile_digest(profile)
        stop = threading.Event()
        browser = Browser()
        observation = browser.observe()
        if through_adapter:
            adapter = CuOwnedBrowserAdapter(browser=browser, gateway=gateway, profile=profile,
                goal=GOAL, stop=stop, deadline=time.monotonic() + 5)
            proposal = adapter.decide(observation)
            assert proposal.candidate.operation == "done" and proposal.semantic_satisfied is True
            assert adapter.calls == 3
        else:
            result = gateway.browser_ai_call("bai_test", "plan", observation,
                profile_digest=profile_digest(profile), checkpoint_digest="c" * 64,
                current_load=browser.last_load_evidence, context={"terminal_only": False},
                cancelled=stop, deadline=time.monotonic() + 5)
            assert result["phase"] == "plan"
        assert len(posts) == (3 if through_adapter else 1)
        assert set(posts) == {gateway.path + "/browser-ai-call"}
        assert authority.effects == browser.deliveries == []
        for method, path in [("POST", gateway.path + "/effects/admit"),
                             ("POST", gateway.path.replace("dwo_cuai", "dwo_other") + "/browser-ai-call"),
                             ("GET", gateway.path + "/browser-ai-call")]:
            with pytest.raises(PermissionError, match="scope_invalid"):
                gateway.cancellable_request(method, path, {}, stop=stop, deadline=time.monotonic() + 1)
        before = len(authority.requests)
        stop.set()
        with pytest.raises(ModelRequestStopped):
            gateway.cancellable_request("POST", gateway.path + "/browser-ai-call", {},
                stop=stop, deadline=time.monotonic() + 1)
        stop.clear()
        with pytest.raises(ModelRequestDeadline):
            gateway.cancellable_request("POST", gateway.path + "/browser-ai-call", {},
                stop=stop, deadline=time.monotonic() - 1)
        session.device_id = "different-device"
        with pytest.raises(PermissionError, match="account_changed"):
            gateway.cancellable_request("POST", gateway.path + "/browser-ai-call", {},
                stop=stop, deadline=time.monotonic() + 1)
        assert len(authority.requests) == before
    assert app.state.model_http.closed
