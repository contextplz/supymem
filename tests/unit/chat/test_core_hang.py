"""Local chat reads and requested builds do not repeatedly probe coding CLIs."""
import asyncio
import json
import time
import threading
from contextlib import contextmanager
import signal

import pytest
from fastapi import BackgroundTasks

from tests.company_os_harness import (
    append_idea_revision, configure_local_test_model, fake_signed_in_codex, load_company_os_app, local_owner_headers,
)


@pytest.fixture(params=[
    ("Supplier invoice reconciliation", "Procurement teams"),
    ("Family cycling route planner", "Parents planning weekend rides"),
])
def confirmed_venture(tmp_path, monkeypatch, request):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/local.db")
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    main = load_company_os_app()
    configure_local_test_model()
    from app.core.local_owner import ensure_owner
    from app.coding_agents_consent import set_consent
    from app.db.models import ChatMessage, Job, Venture
    from app.state import db
    from core.clock import utcnow
    from tests.unit.pagebuilder.test_api import seed

    fake_signed_in_codex(tmp_path, monkeypatch)
    data = seed(db(), actor=ensure_owner().id)
    assert set_consent(data["actor"], "codex", True)["codex"]
    idea, audience = request.param
    with db()() as session:
        session.get(Venture, data["vid"]).raw_idea = idea
        data["rid"] = append_idea_revision(session, data["rid"], summary=idea,
            structured=json.dumps({"audience": audience})).id
        session.add(Job(id="failed-page", venture_id=data["vid"], workspace_id=data["wid"],
            kind="pagebuilder.build", status="failed_dead", payload_json="{}", created_at=utcnow()))
        for index in range(20):
            assistant = index % 2 == 1
            session.add(ChatMessage(id=f"turn-{index:02}", venture_id=data["vid"], workspace_id=data["wid"],
                role="assistant" if assistant else "user", kind="text", body_md=idea,
                citations_json="[]", created_at=utcnow(), actions_json=json.dumps([
                    {"label": "Build your landing page with Codex", "action": "pagebuilder.build",
                     "payload": {"prepare": True}, "style": "primary"}]) if assistant else "[]"))
        session.commit()
    return main, data


@contextmanager
def request_deadline():
    if not hasattr(signal, "setitimer"):
        pytest.skip("This request timeout uses the Mac/Linux signal timer")
    def expired(signum, frame):
        raise TimeoutError("Chat request exceeded one second")
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 1)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def test_chat_get_does_not_probe_tools_while_rendering_saved_moves(confirmed_venture, monkeypatch):
    from app.api.chat_api import get_chat
    from app.chat.conversations import bind_conversation
    from app.tenancy import bind
    from desktop_runtime import agent_status

    data = confirmed_venture[1]
    probes = []
    def slow_statuses():
        probes.append(True)
        threading.Event().wait(0.1)
        return [{"name": "codex", "installed": True, "signed_in": True,
                 "subscription_login": True, "available": True}]
    monkeypatch.setattr(agent_status, "statuses", slow_statuses)
    with bind(workspace_id=data["wid"], venture_id=data["vid"], actor=data["actor"]):
        with bind_conversation(data["vid"]), request_deadline():
            result = get_chat(data["vid"], BackgroundTasks())
    assert len(result["items"]) == 20
    assert not probes
    assert any(item["actions"] for item in result["items"] if item["role"] == "assistant")


def test_ready_tool_build_returns_without_repeated_cli_probes(confirmed_venture, monkeypatch):
    from fastapi.testclient import TestClient
    from app.db.models import Job
    from app.state import db
    from desktop_runtime import agent_status
    from sqlalchemy import select

    main, data = confirmed_venture
    probes = []
    def status(provider):
        probes.append(provider)
        threading.Event().wait(0.1)
        return {"name": provider, "installed": provider == "codex", "signed_in": provider == "codex",
                "subscription_login": provider == "codex", "available": provider == "codex"}
    monkeypatch.setattr(agent_status, "_status", status)
    monkeypatch.setenv("PATH", str(data["vid"]))
    assert next(item for item in agent_status.statuses() if item["name"] == "codex")["available"]
    probes.clear()
    client = TestClient(main.create_app(), headers=local_owner_headers(), base_url="http://127.0.0.1",
                        client=("127.0.0.1", 12345))
    started = time.perf_counter()
    reply = client.post(f'/ventures/{data["vid"]}/chat', json={
        "message": "Build my landing page with Codex", "idempotency_key": "latency-build"})
    elapsed = time.perf_counter() - started
    assert reply.status_code == 200, reply.json()
    assert elapsed < 3
    assert not probes
    with db()() as session:
        jobs = session.scalars(select(Job).where(Job.venture_id == data["vid"],
            Job.kind == "pagebuilder.build", Job.id != "failed-page")).all()
        assert len(jobs) == 1
        job_id = jobs[0].id
    from app.pagebuilder import tool
    from app.tenancy import bind
    from fastapi import HTTPException
    monkeypatch.setattr(tool, "_start_order", lambda *args: pytest.fail("Creating your order must not wait for the tool"))
    with bind(workspace_id=data["wid"], venture_id=data["vid"], actor=data["actor"]):
        order = tool.prepare_job(job_id, data["actor"])
        repeated = tool.prepare_job(job_id, data["actor"])
    assert order["state"] == "approved" and repeated["id"] == order["id"]
    with pytest.raises(HTTPException) as refused:
        tool.prepare_job(job_id)
    assert refused.value.status_code == 401
    with db()() as session:
        saved = json.loads(session.get(Job, job_id).payload_json)
        assert saved["tool_build"]["order_id"] == order["id"]


def _lease_page(factory, kind):
    from app.core.jobs import lease

    with factory() as session:
        job, = lease(session, "landing-worker", kinds=[kind], n=1)
        session.commit()
    return {**job.payload, "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts}


def test_landing_admission_keeps_brief_when_worker_also_prepares(confirmed_venture, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from fastapi.testclient import TestClient
    from app.build import device_orders
    from app.pagebuilder import api, jobs, tool

    main, data = confirmed_venture
    worker_read = threading.Event()
    api_saved_brief = threading.Event()
    worker_building_brief = threading.Event()
    finish_worker = threading.Event()
    prepare = tool.prepare_job
    prepare_order = device_orders.prepare_landing_page_from_job
    build_brief = tool.build_brief

    def analytics(job_id, payload, inputs):
        if payload is not None:
            worker_read.set()
            assert api_saved_brief.wait(10)

    def brief(*args):
        if threading.current_thread().name.startswith("landing-test-worker"):
            worker_building_brief.set()
            assert finish_worker.wait(10)
        return build_brief(*args)

    def create_order(*args, **kwargs):
        if not threading.current_thread().name.startswith("landing-test-worker"):
            api_saved_brief.set()
            assert worker_building_brief.wait(10)
        return prepare_order(*args, **kwargs)

    def prepare_concurrently(job_id, caller=None, *, payload=None):
        if caller is not None:
            futures.append(worker.submit(jobs.run, _lease_page(data["factory"], api.KIND)))
            assert worker_read.wait(10)
        return prepare(job_id, caller, payload=payload)

    futures = []
    monkeypatch.setattr(jobs, "analytics_binding", analytics)
    monkeypatch.setattr(tool, "build_brief", brief)
    monkeypatch.setattr(tool, "prepare_job", prepare_concurrently)
    monkeypatch.setattr(device_orders, "prepare_landing_page_from_job", create_order)
    monkeypatch.setattr(tool, "_start_order", lambda *_: None)
    client = TestClient(main.create_app(), headers=local_owner_headers(), base_url="http://127.0.0.1",
                        client=("127.0.0.1", 12345))
    with ThreadPoolExecutor(1, thread_name_prefix="landing-test-worker") as worker:
        try:
            reply = client.post(f'/ventures/{data["vid"]}/chat', json={
                "message": "Build my landing page with Codex", "idempotency_key": "parallel-landing"})
        finally:
            finish_worker.set()
        futures[0].result(timeout=10)
    assert reply.status_code == 200, reply.json()
    job_id = reply.json()["actions"][0]["payload"]["job_id"]
    assert api.status(data["vid"], data["actor"], job_id)["order_id"]


@pytest.mark.parametrize("failure", ["refused_start", "refused_preflight", "unconfirmed_claim"])
def test_failed_landing_releases_only_unclaimed_orders(confirmed_venture, monkeypatch, failure):
    import httpx
    from fastapi import HTTPException
    from fastapi.testclient import TestClient
    from app.admin.authn import CallerContext
    from app.build import device_orders
    from app.core.jobs import FatalError
    from app.pagebuilder import api, jobs, tool

    main, data = confirmed_venture
    client = TestClient(main.create_app(), headers=local_owner_headers(), base_url="http://127.0.0.1",
                        client=("127.0.0.1", 12345))
    path = f'/ventures/{data["vid"]}'
    page = client.post(path + "/chat", json={"message": "Build my landing page with Codex",
        "idempotency_key": "refused-landing"})
    assert page.status_code == 200, page.json()
    job_id = page.json()["actions"][0]["payload"]["job_id"]
    order_id = api.status(data["vid"], data["actor"], job_id)["order_id"]
    order_path = path + f"/device-work-orders/{order_id}"
    order = client.get(order_path).json()

    def native_request(request):
        if request.method == "POST" and failure == "unconfirmed_claim":
            caller = CallerContext(data["actor"], "", "user", "", "POST", 0, job_id)
            device_orders.transition(data["vid"], caller, order_id, order["review_digest"], "claim")
            raise httpx.ReadTimeout("Reply lost", request=request)
        return httpx.Response(409 if request.method == "POST" else 404)

    def refuse_connection(*args, **kwargs):
        raise HTTPException(409, detail={"code": "pagebuilder_coding_tool_required", "message": tool.CONNECT_STEP})

    client_class = httpx.Client
    transport = httpx.MockTransport(native_request)
    with monkeypatch.context() as native:
        native.setattr(httpx, "Client", lambda **kwargs: client_class(transport=transport, **kwargs))
        if failure == "refused_preflight":
            native.setattr(tool, "require_connected", refuse_connection)
        code = "pagebuilder_coding_tool_required" if failure == "refused_preflight" else "pagebuilder_tool_start_unconfirmed"
        with pytest.raises(FatalError, match=code):
            jobs.run(_lease_page(data["factory"], api.KIND))

    page_status = client.get(path + f"/pagebuilder/jobs/{job_id}").json()
    assert page_status["state"] == "failed", page_status
    saved = client.get(order_path).json()
    claimed = failure == "unconfirmed_claim"
    assert saved["state"] == ("claimed" if claimed else "failed") and bool(saved.get("claimed_at")) == claimed, saved["state"]
    product = client.post(path + "/chat", json={"message": "Build my first app with Codex",
        "idempotency_key": "product-after-refusal"})
    assert product.status_code == 200 and product.json()["kind"] == ("navigation" if claimed else "build_tool_started"), product.json()


def test_landing_start_failure_cannot_overwrite_a_concurrent_claim(confirmed_venture, monkeypatch):
    from app.admin.authn import CallerContext
    from app.build import device_budget, device_orders
    from app.core.local_owner import _ensure_proxy_secret
    from app.pagebuilder import api

    _, data = confirmed_venture
    _ensure_proxy_secret()
    page = api.build(data["vid"], data["actor"])
    caller = CallerContext(data["actor"], "", "user", "", "POST", 0, page["job_id"])
    order = device_orders.retrieve(data["vid"], caller, page["order_id"])
    apply_budget = device_budget.apply
    claims = []

    def apply_with_claim(session, account, body, action, now):
        if action == "cancel":
            claims.append(device_orders.transition(data["vid"], caller, order["id"], order["review_digest"], "claim"))
        return apply_budget(session, account, body, action, now)

    monkeypatch.setattr(device_budget, "apply", apply_with_claim)
    with pytest.raises(ValueError, match="device_order_transition_invalid"):
        device_orders.fail_before_start(data["vid"], caller, order["id"], order["review_digest"], "native_execution_failed")
    saved = device_orders.retrieve(data["vid"], caller, order["id"])
    assert saved["state"] == "claimed" and saved["execution_id"] == claims[0]["execution_id"]


def test_health_and_ready_answer_during_chat_build_probe(confirmed_venture, monkeypatch):
    import httpx
    from sqlalchemy import select
    from app.db.models import Job
    from app.state import db
    from desktop_runtime import agent_status

    main, data = confirmed_venture
    monkeypatch.setenv("PATH", str(data["vid"]))
    application = main.create_app()
    headers = local_owner_headers()

    async def exercise():
        loop = asyncio.get_running_loop()
        probing = asyncio.Event()
        release_probe = threading.Event()
        probe_finished = threading.Event()

        def status(provider):
            available = provider == "codex"
            if available:
                loop.call_soon_threadsafe(probing.set)
                release_probe.wait(3)
                probe_finished.set()
            return {"name": provider, "installed": available, "signed_in": available,
                    "subscription_login": available, "available": available}

        monkeypatch.setattr(agent_status, "_status", status)
        transport = httpx.ASGITransport(app=application, client=("127.0.0.1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1", headers=headers) as client:
            build = asyncio.create_task(client.post(f'/ventures/{data["vid"]}/chat/confirm', json={
                "action": "pagebuilder.build", "payload": {"prepare": True},
            }))
            try:
                await asyncio.wait_for(probing.wait(), timeout=5)
                async with asyncio.timeout(1):
                    for path in ("/health", "/ready"):
                        assert (await client.get(path)).status_code == 200
                assert not probe_finished.is_set(), "Health checks waited for the coding tool probe"
            finally:
                release_probe.set()
                response = await build
            assert response.status_code == 200, response.json()

    asyncio.run(exercise())
    with db()() as session:
        jobs = session.scalars(select(Job.id).where(Job.venture_id == data["vid"],
            Job.kind == "pagebuilder.build", Job.id != "failed-page")).all()
        assert len(jobs) == 1
