"""Chat actions that used to fall through actually run."""

from __future__ import annotations

import pytest
from tests.company_os_harness import company_os_test_client as TestClient, load_company_os_app
from tests.unit.gtm.test_journey import journey_db as journey_db


@pytest.mark.parametrize("audience", ["Warehouse coordinators", "Community gardeners"])
@pytest.mark.parametrize("action", ["pagebuilder.build", "gtm.send", "routines.create"])
@pytest.mark.parametrize("same_reason", [True, False])
def test_local_early_gate_returns_shared_setup_reply(monkeypatch, audience, action, same_reason):
    import asyncio
    load_company_os_app()
    from app.chat import actions, execute

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    message = "Open Settings > Keys and connections, save your required settings, then retry."
    setup = {"kind": "text", "body_md": message, "action_started": False,
        "actions": [{"label": "Open Settings", "href": "/settings/local", "style": "primary"}], "citations": []}
    blocked = {"kind": "text", "body_md": message if same_reason else
        "You cannot run that action here. Choose a supported action to continue.", "actions": [], "citations": []}
    monkeypatch.setattr(actions, "beta_blocked", lambda *_args: blocked)
    monkeypatch.setattr(execute, "local_capability_reply", lambda _name: setup)
    monkeypatch.setattr("app.analytics.chat_usage.record_chat_action", lambda *_args, **_kwargs: None)
    if action == "pagebuilder.build":
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as denied:
            asyncio.run(execute.execute_action("venture", action, payload={"request": audience}))
        assert denied.value.status_code == 404
        return
    result = asyncio.run(execute.execute_action("venture", action, payload={"request": audience}))
    assert result == (setup if same_reason else blocked)


@pytest.mark.parametrize("idea", ["Manage freight invoices", "Plan family hiking trips"])
@pytest.mark.parametrize("action,capability_name", [
    ("gtm.find", "find_customers"), ("research.start", "research"),
    ("openworker_search", "research"), ("pagebuilder.build", "landing_pages"),
])
@pytest.mark.parametrize("ready", [True, False])
def test_local_dispatch_uses_capabilities(journey_db, monkeypatch, idea, action, capability_name, ready):
    import asyncio
    import sys
    from types import SimpleNamespace
    from app.chat import customers, execute
    from app.db.models import Venture
    from app.pagebuilder import api, tool
    from app.research import local_providers

    data = journey_db
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = idea
        session.commit()
    checked, calls = [], []
    message = "Open Settings > Keys and connections, finish your setup, then retry."
    monkeypatch.setitem(sys.modules, "app.core.local_capabilities", SimpleNamespace(
        enabled=lambda name: checked.append(name) or ready, blocked_message=lambda name: message,
        capability=lambda name: checked.append(name) or {"state": "ready" if ready else "needs_setup", "next_step": message}))
    monkeypatch.setattr(local_providers, "search_ready", lambda: True, raising=False)
    def run(*args, **kwargs):
        calls.append(action)
        return {"kind": "text", "body_md": "Your requested work started.", "action_started": True,
                "actions": [], "citations": []}
    async def async_run(*args, **kwargs):
        return run(*args, **kwargs)
    monkeypatch.setattr(customers, "execute", async_run)
    monkeypatch.setattr(execute, "_research_start", run)
    monkeypatch.setattr(execute, "_openworker_search", async_run)
    monkeypatch.setattr(tool, "connected_provider", lambda _actor: "codex")
    monkeypatch.setattr(api, "estimate", lambda *_args: {"ready": True})
    monkeypatch.setattr(execute, "_pagebuilder_start", run)
    result = asyncio.run(execute.execute_action(data["vid"], action,
        payload={"query": idea}, actor_subject=data["actor"]))
    assert capability_name in checked
    assert calls == ([action] if ready else [])
    if not ready:
        assert result["body_md"] == message and result["action_started"] is False
    assert result["kind"] != "confirmation"


@pytest.mark.parametrize("idea", ["Manage freight invoices", "Plan family hiking trips"])
@pytest.mark.parametrize("ready", [True, False])
def test_local_customer_search_uses_shared_readiness(journey_db, monkeypatch, idea, ready):
    import asyncio
    import sys
    from types import SimpleNamespace
    from app.chat import customers
    from app.db.models import GtmTouch, Job
    from app.research import local_providers
    from sqlalchemy import select

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    message = "Choose your search provider in Settings > Keys and connections, then retry."
    monkeypatch.setitem(sys.modules, "app.core.local_capabilities", SimpleNamespace(
        enabled=lambda name: True, blocked_message=lambda name: message, capability=lambda name: {"state": "ready"}))
    checked = []
    monkeypatch.setattr(local_providers, "search_ready", lambda: checked.append(True) or ready, raising=False)
    monkeypatch.setattr(local_providers, "setup_reply", lambda: pytest.fail("Local chat must not use hosted preflight"))
    monkeypatch.setattr(customers, "_view", lambda *_args: {"can_prepare": False, "icp": {"who": idea}})
    with journey_db["factory"]() as session:
        before = {model: set(session.scalars(select(model.id))) for model in (GtmTouch, Job)}
    result = asyncio.run(customers.find_reply(journey_db["vid"], query="Find potential customers for my saved idea"))
    assert checked
    if not ready:
        assert result["body_md"] == message and result["action_started"] is False
        with journey_db["factory"]() as session:
            assert {model: set(session.scalars(select(model.id))) for model in (GtmTouch, Job)} == before


@pytest.mark.parametrize("idea", ["Manage freight invoices", "Plan family hiking trips"])
@pytest.mark.parametrize("quoted", [False, True])
def test_local_page_build_starts_without_confirmation(journey_db, monkeypatch, idea, quoted):
    import sys
    from types import SimpleNamespace
    from app.chat import execute
    from app.core import local_mode
    from app.pagebuilder import api, tool

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setitem(sys.modules, "app.core.local_capabilities", SimpleNamespace(
        enabled=lambda name: True, blocked_message=lambda name: "", capability=lambda name: {"state": "ready"}))
    monkeypatch.setattr(local_mode, "is_test_mode", lambda: False)
    monkeypatch.setattr(tool, "connected_provider", lambda _actor: "codex")
    quote = {"ready": True, "ceiling_micro": 12000, "quote_fingerprint": "checked-quote"}
    monkeypatch.setattr(api, "estimate", lambda *_args: quote)
    calls = []
    monkeypatch.setattr(api, "_start", lambda venture, actor, **kwargs:
        calls.append(kwargs) or {"job_id": "private-page", "state": "queued"})
    payload = {"prepare": True, "request": idea}
    if quoted:
        payload.update(quote_fingerprint=quote["quote_fingerprint"], ceiling_micro=quote["ceiling_micro"])
    result = execute._build_landing_page(journey_db["vid"], payload, journey_db["actor"])
    assert calls == [{"expected_quote": "checked-quote" if quoted else None, "require_quote": True, "retry_failed": True}]
    assert result["kind"] != "confirmation" and "private" in result["body_md"]
    from fastapi import HTTPException
    calls.clear()
    with pytest.raises(HTTPException) as changed:
        execute._build_landing_page(journey_db["vid"], payload | {"quote_fingerprint": "stale-quote", "ceiling_micro": 12000}, journey_db["actor"])
    assert changed.value.status_code == 409 and calls == []


@pytest.mark.parametrize("audience", ["Freight coordinators", "Weekend hikers"])
@pytest.mark.parametrize("ready", [True, False])
def test_local_reply_request_reads_inbox_only_when_ready(journey_db, monkeypatch, audience, ready):
    import asyncio
    import sys
    from types import SimpleNamespace
    from app.chat import customers
    from app.gtm import journey

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    message = "Set your IMAP host and port in Settings > Keys and connections, then retry."
    monkeypatch.setitem(sys.modules, "app.core.local_capabilities", SimpleNamespace(
        enabled=lambda name: ready, blocked_message=lambda name: message,
        capability=lambda name: {"state": "ready" if ready else "needs_setup"}))
    monkeypatch.setattr(customers, "_view", lambda *_args: {})
    monkeypatch.setattr(customers, "replies_reply", lambda *_args: {"kind": "text", "body_md": audience, "actions": [], "citations": []})
    polls = []
    async def poll(venture_id, **kwargs):
        polls.append((venture_id, kwargs))
        return 0
    monkeypatch.setattr(journey.outreach, "poll_replies", poll)
    data = journey_db
    if ready:
        from tests.unit.gtm.test_journey import _mail

        _mail(data)
    result = asyncio.run(customers.run_command(data["vid"], "replies", None, "Any replies?", data["actor"]))
    assert polls == ([(data["vid"], {"force": True})] if ready else [])
    assert result["body_md"] == (audience if ready else message)


@pytest.mark.parametrize("audience", ["Freight coordinators", "Weekend hikers"])
def test_local_content_setup_blocks_drafts_before_creating_work(journey_db, monkeypatch, audience):
    import asyncio
    import sys
    from types import SimpleNamespace
    from app.chat import customers, offers

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    message = "Set your model provider in Settings > Keys and connections, then retry."
    monkeypatch.setitem(sys.modules, "app.core.local_capabilities", SimpleNamespace(
        enabled=lambda name: False, blocked_message=lambda name: message, capability=lambda name: {"state": "needs_setup"}))
    monkeypatch.setattr("app.planning.store.enqueue_task", lambda *_args, **_kwargs: pytest.fail("Blocked drafts must not create work"))
    venture_id, actor = journey_db["vid"], journey_db["actor"]
    for result in (offers.draft_prepare(venture_id, request=audience),
                   asyncio.run(offers.draft_run(venture_id, request=audience)),
                   asyncio.run(customers.run_command(venture_id, "draft_named", None, audience, actor))):
        assert result["body_md"] == message and result["kind"] != "confirmation"


@pytest.mark.parametrize("audience", ["Freight coordinators", "Weekend hikers"])
def test_reading_research_does_not_start_an_unrequested_search(monkeypatch, audience):
    import asyncio
    from app.chat import execute

    async def read(*args, **kwargs):
        return {"kind": "text", "body_md": audience, "actions": [
            {"label": "Research your idea", "action": "research.start", "style": "primary"}], "citations": []}
    async def start(*args, **kwargs):
        pytest.fail("A reading request must not start offered work")
    monkeypatch.setattr(execute, "_execute_action", read)
    monkeypatch.setattr(execute, "start_requested_work", start)
    monkeypatch.setattr("app.analytics.chat_usage.record_chat_action", lambda *_args, **_kwargs: None)
    result = asyncio.run(execute.execute_action("venture", "research"))
    assert result["body_md"] == audience


@pytest.mark.parametrize("audience", ["Freight coordinators", "Weekend hikers"])
@pytest.mark.parametrize("ready", [True, False])
def test_local_product_build_uses_saved_tool_readiness_without_confirmation(journey_db, monkeypatch, audience, ready):
    import asyncio
    from types import SimpleNamespace
    from app.chat import execute, router
    from app.core.local_capabilities import blocked_message
    from app.core.local_secrets import update_settings
    from app.db.models import Job, Venture
    from sqlalchemy import select
    from tests.company_os_harness import configure_local_test_model

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    configure_local_test_model()
    update_settings("capabilities", {"background_work": False, "build_with_my_tool": ready}, {})
    monkeypatch.setattr("app.coding_agents_consent.consents", lambda *_args: {"codex": True})
    monkeypatch.setattr("desktop_runtime.agent_status.statuses", lambda: [{"name": "codex",
        "installed": True, "signed_in": True, "subscription_login": True, "available": True}])
    calls = []
    def start(venture_id, payload, actor):
        calls.append((venture_id, payload["text"], actor))
        return {"kind": "build_tool_started", "body_md": "Your build is queued. Open Build activity to watch it.",
                "action_started": True, "actions": [], "citations": []}
    monkeypatch.setattr(execute, "_active_product_build_reply", lambda *_args: None)
    monkeypatch.setattr(execute, "_start_tool_build", start)
    monkeypatch.setattr("app.chat.guard.guard_reply", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.chat.other_venture.other_venture_reply", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.chat.idea_bridge.written_request", lambda *_args: False)
    monkeypatch.setattr("app.build.device_orders.coding_loop_enabled", lambda *_args: pytest.fail("Local chat must not read the rollout flag"))
    data = journey_db
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = audience
        session.commit()
        before = set(session.scalars(select(Job.id)))
    context = SimpleNamespace(venture={"name": audience, "workspace_id": data["wid"]})
    text = "Build my product"
    result = asyncio.run(router._prepare_intent(data["vid"], context, "mac_prepare", text, None, data["actor"]))
    if ready:
        assert result["kind"] == "build_tool_started" and result["action_started"] is True
        assert calls == [(data["vid"], text, data["actor"])]
    else:
        assert result["body_md"] == blocked_message("build_with_my_tool") and result["action_started"] is False and not calls
    assert not any(action.get("confirmation") for action in result["actions"])
    with data["factory"]() as session:
        assert set(session.scalars(select(Job.id))) == before


@pytest.mark.parametrize("audience", ["Freight coordinators", "Weekend hikers"])
def test_local_fallback_reports_missing_model_setup(monkeypatch, audience):
    import sys
    from app.chat import execute
    from app.core import providers_local

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setitem(sys.modules, "app.core.local_capabilities", None)
    def missing_config():
        raise providers_local.LocalProviderError("No saved model provider for " + audience)
    monkeypatch.setattr(providers_local, "load_local_config", missing_config)
    result = execute.local_capability_reply("landing_pages")
    assert result["action_started"] is False and "Model provider" in result["body_md"]
    assert "Settings > Keys and connections" in result["body_md"] and audience not in result["body_md"]


@pytest.mark.parametrize("audience", ["Freight coordinators", "Weekend hikers"])
def test_local_goal_gate_preserves_confirmation_validation(monkeypatch, audience):
    import asyncio
    import sys
    from types import SimpleNamespace
    from app.chat import execute

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    message = "Turn on background work in Settings, then retry."
    monkeypatch.setitem(sys.modules, "app.core.local_capabilities", SimpleNamespace(
        enabled=lambda name: False, blocked_message=lambda name: message, capability=lambda name: {"state": "needs_setup"}))
    payload = {"_goal_step": {"task_id": "task", "run_id": "run", "step_id": "step"}, "request": audience}
    result = asyncio.run(execute._execute_confirmed_action("venture", "research.start", payload, "owner"))
    assert result["body_md"] == message
    with pytest.raises(ValueError, match="Your task confirmation changed"):
        asyncio.run(execute._execute_confirmed_action("venture", "research.start", payload | {"_goal_step": {"task_id": 7}}, "owner"))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/chat_wired.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "mem.sqlite3"))
    monkeypatch.setenv("COMPANY_OS_BRAIN_GRAPH_PATH", str(tmp_path / "graph.sqlite3"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("CODING_AGENT", "inhouse_stub")
    main = load_company_os_app()
    from app import state as state_mod
    from app.memory import reset_memory_for_tests

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    reset_memory_for_tests(tmp_path / "mem.sqlite3")
    with TestClient(main.create_app()) as client:
        created = client.post(
            "/ventures",
            json={"raw_idea": "a marketplace for restoring vintage synthesizers"},
        )
        assert created.status_code == 201, created.text
        yield client, created.json()["id"], tmp_path


def test_classify_office_hours_and_ceo_review():
    load_company_os_app()
    from app.chat.router import _EXECUTABLE, classify_intent

    assert classify_intent("office hours — is this worth building?") == "office_hours"
    assert classify_intent("ceo review this plan") == "ceo_review"
    assert classify_intent("what do I know about Alice?") == "brain_brief"
    assert "office_hours" in _EXECUTABLE
    assert "ceo_review" in _EXECUTABLE
    assert "brain_brief" in _EXECUTABLE
    assert "social_guide" in _EXECUTABLE
    assert "social_status" in _EXECUTABLE


@pytest.mark.parametrize("idea", ["Manage wholesale freight invoices", "Plan family hiking trips"])
@pytest.mark.parametrize("preparation", ["pagebuilder.build", "build.prepare"])
def test_build_preparation_runs_with_its_checked_quote(journey_db, monkeypatch, idea, preparation):
    if preparation == "build.prepare":
        monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    from tests.company_os_harness import configure_local_gtm

    configure_local_gtm(monkeypatch)
    import asyncio
    from app.chat import execute
    from app.db.models import Venture
    from app.pagebuilder import api
    from app.core.local_mode import is_local
    from tests.unit.pagebuilder.test_api import connect_local_tool

    if is_local():
        connect_local_tool(monkeypatch)

    data = journey_db
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = idea
        session.commit()
    quote = {"ready": True, "ceiling_micro": 12000, "quote_fingerprint": "reviewed-quote"}
    monkeypatch.setattr(api, "estimate", lambda *_args: quote)
    monkeypatch.setattr("app.chat.router._product_build_reply", lambda *_args, **_kwargs: execute._pagebuilder_quote_card(data["vid"], quote))
    calls = []
    def start(venture, actor, *, expected_quote, **kwargs):
        assert kwargs == {"require_quote": True, "retry_failed": True}
        calls.append((venture, actor, expected_quote))
        return {"job_id": "requested_build", "state": "queued"}
    monkeypatch.setattr(api, "_start", start)
    payload = {"prepare": True}
    if is_local():
        payload.update(quote_fingerprint=quote["quote_fingerprint"], ceiling_micro=quote["ceiling_micro"])
    result = asyncio.run(execute.execute_action(data["vid"], preparation,
        payload=payload, actor_subject=data["actor"]))
    assert result["kind"] != "confirmation"
    assert calls == [(data["vid"], data["actor"], quote["quote_fingerprint"])]


@pytest.mark.parametrize("idea", ["Manage wholesale freight invoices", "Plan family hiking trips"])
def test_chat_pause_preserves_resumable_saved_tasks(journey_db, monkeypatch, idea):
    from app.chat.execute import _pause_work
    from app.db.models import PlannedTask
    from app.planning import store
    from app.ventures.lifecycle import is_paused

    data = journey_db
    store.save_tasks(data["vid"], [{"title": idea, "why_now": "Already saved", "runnable": True,
        "tags": ["test"], "est_minutes": 10}], source="planner")
    with data["factory"]() as session:
        before = {task.id: task.title for task in session.query(PlannedTask).filter_by(venture_id=data["vid"])}
    monkeypatch.setattr(store, "cancel_all_work", lambda *_args: pytest.fail("Pause must not cancel saved tasks"))
    result = _pause_work(data["vid"])
    assert is_paused(data["vid"]) and "saved tasks are kept" in result["body_md"]
    with data["factory"]() as session:
        tasks = session.query(PlannedTask).filter_by(venture_id=data["vid"]).all()
        assert {task.id: task.title for task in tasks} == before
        assert all(task.status not in {"cancelled", "deleted"} for task in tasks)


@pytest.mark.parametrize("new_path", [False, True])
def test_confirmation_saves_the_idea_without_claiming_validation(journey_db, monkeypatch, new_path):
    import asyncio
    from app.chat.execute import _execute_action
    from app.tenancy import bind
    calls = []
    data = journey_db

    def confirm(session_id, **kwargs):
        calls.append((session_id, kwargs))
        return {"session": {"current_revision": {"structured": {}}},
                "next_href": f"/v/{data['vid']}/context" if new_path else f"/v/{data['vid']}/site"}

    monkeypatch.setattr("app.idea.service.confirm", confirm)
    with bind(workspace_id=data["wid"], venture_id=data["vid"], actor=data["actor"]):
        result = asyncio.run(_execute_action(data["vid"], "idea.confirm", payload={"session_id": "s1", "revision_id": "r1"}))
    assert result["body_md"].startswith("Your idea is saved.")
    assert "validated" not in result["body_md"]
    assert calls[0][1]["start_research"] is False


@pytest.mark.parametrize("idea,audience", [
    ("Track customs paperwork for freight shipments", "Freight dispatchers"),
    ("Share recipes and weekly meal plans", "Home cooks"),
], ids=["business", "consumer"])
def test_landing_page_actions_keep_event_loop_responsive(journey_db, monkeypatch, idea, audience):
    from tests.company_os_harness import configure_local_gtm
    from app.core.local_mode import is_local
    from tests.unit.pagebuilder.test_api import connect_local_tool

    configure_local_gtm(monkeypatch)
    if is_local():
        connect_local_tool(monkeypatch)
    import asyncio
    import threading
    import time
    from app.chat.execute import execute_action
    from app.chat.store import CLIENT_REQUEST_ID
    from app.db.models import Venture
    from app.pagebuilder import api
    from app.tenancy import current_or_none

    data = journey_db
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = f"{idea}\nAudience: {audience}"
        session.commit()

    async def exercise(action_name, slow_step):
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        started_at = 0.0
        loop_thread = threading.get_ident()
        calls = []

        def record(step):
            nonlocal started_at
            scope = current_or_none()
            assert (scope.workspace_id, scope.venture_id, scope.actor) == (data["wid"], data["vid"], data["actor"])
            assert CLIENT_REQUEST_ID.get() == "landing-thread-check"
            calls.append((step, threading.get_ident()))
            if step == slow_step:
                started_at = loop.time()
                loop.call_soon_threadsafe(started.set)
                time.sleep(0.5)

        def confirm(session_id, **kwargs):
            record("confirm")
            assert session_id == "session-thread-check"
            assert kwargs["venture_id"] == data["vid"]
            assert kwargs["revision_id"] == "revision-thread-check"
            assert kwargs["start_research"] is False and kwargs["accept_validation"] is True
            return {}

        def estimate(venture_id, actor):
            record("estimate")
            assert (venture_id, actor) == (data["vid"], data["actor"])
            return {"ready": True, "ceiling_micro": 12000, "quote_fingerprint": "thread-check-quote"}

        def start(venture_id, actor, **kwargs):
            record("start")
            assert (venture_id, actor) == (data["vid"], data["actor"])
            expected_quote = None if is_local() and action_name == "idea.confirm" else "thread-check-quote"
            assert kwargs == {"expected_quote": expected_quote, "require_quote": True, "retry_failed": True}
            return {"job_id": "job-thread-check", "state": "queued"}

        monkeypatch.setattr("app.idea.service.confirm", confirm)
        monkeypatch.setattr(api, "estimate", estimate)
        monkeypatch.setattr(api, "_start", start)
        monkeypatch.setattr("app.autopilot.receipts.queued_job", lambda job_id: record("receipt"))
        payload = {"session_id": "session-thread-check", "revision_id": "revision-thread-check",
                   "build_landing_page": True, "ceiling_micro": 12000, "quote_fingerprint": "thread-check-quote"}

        async def probe():
            await started.wait()
            await asyncio.sleep(0)
            return loop.time() - started_at

        work = asyncio.create_task(execute_action(data["vid"], action_name, payload=payload,
                                  actor_subject=data["actor"], request_nonce="landing-thread-check"))
        try:
            latency = await asyncio.wait_for(probe(), timeout=1)
            assert latency < 0.2, f"Event loop blocked for {latency:.3f}s during {slow_step}"
            assert not work.done()
        finally:
            result = await work
        assert result["run_id"] == "job-thread-check"
        expected = (["confirm"] if action_name == "idea.confirm" else []) + ["estimate", "start", "receipt"]
        assert [step for step, thread_id in calls] == expected
        assert len({thread_id for step, thread_id in calls}) == 1
        assert all(thread_id != loop_thread for step, thread_id in calls)
        assert CLIENT_REQUEST_ID.get() is None

    for action_name, slow_step in [("idea.confirm", "confirm"), ("idea.confirm", "estimate"),
                                   ("idea.confirm", "start"), ("pagebuilder.build", "estimate"),
                                   ("pagebuilder.build", "start")]:
        asyncio.run(exercise(action_name, slow_step))


def test_office_hours_and_whats_next_via_chat(env):
    client, vid, _tmp = env
    hours = client.post(
        f"/ventures/{vid}/chat",
        json={"message": "office hours — is this worth building?"},
    )
    assert hours.status_code == 200, hours.text
    body = hours.json()["body_md"]
    assert "real demand" in body and "Office Hours" not in body and "Latest founder statement" not in body

    for idea in ["Inventory alerts for warehouse operators", "Walking groups for neighborhood families"]:
        created = client.post("/ventures", json={"raw_idea": idea, "start": False}).json()
        nxt = client.post(f"/ventures/{created['id']}/chat", json={"message": "what's next"})
        assert nxt.status_code == 200, nxt.text
        from app.core.local_mode import is_local

        assert nxt.json()["kind"] == "next_moves"
        if is_local():
            assert "Continue clarifying" in nxt.json()["body_md"]
            assert any(action.get("action") == "idea.review" for action in nxt.json()["actions"])
        else:
            assert "Your answers are saved. Your review is queued or running." in nxt.json()["body_md"]
            assert "within two minutes, choose Check review" in nxt.json()["body_md"]
            assert "skip the remaining questions and review now." in nxt.json()["body_md"]


@pytest.mark.parametrize("audience", ["Factory maintenance teams", "Families choosing bedtime stories"])
def test_build_me_my_startup_saves_chat_guidance_without_generic_task(env, audience):
    client, vid, _tmp = env
    from sqlalchemy import func, select
    from app.db.models import Job, PlannedTask
    from app.state import db
    from app.core.local_mode import is_local

    with db()() as session:
        before = [session.scalar(select(func.count()).select_from(model).where(model.venture_id == vid))
                  for model in (PlannedTask, Job)]
    request = "Build my app for " + audience if is_local() else "build me my startup"
    reply = client.post(f"/ventures/{vid}/chat", json={"message": request})
    assert reply.status_code == 200, reply.text
    if is_local():
        assert "Settings > Keys and connections > Coding tools" in reply.json()["body_md"]
        assert reply.json()["actions"][0]["href"] == "/settings/local#coding-tools"
        assert not any(action.get("confirmation") for action in reply.json()["actions"])
    else:
        assert reply.json()["actions"] == [{"label": "Review and confirm your idea", "action": "idea.review", "style": "primary"}]
        assert "Review and confirm your idea" in reply.json()["body_md"]
        assert "Research competitors and demand" in reply.json()["body_md"]
        assert "Make a build plan" in reply.json()["body_md"]
        assert "Nothing was started or published" in reply.json()["body_md"]
    assert 'credit' not in reply.json()['body_md'].lower()
    assert not reply.json().get("action_started")
    with db()() as session:
        after = [session.scalar(select(func.count()).select_from(model).where(model.venture_id == vid))
                 for model in (PlannedTask, Job)]
    assert after == before


@pytest.mark.parametrize(("subject", "note"), [
    ("Nora", "Nora schedules warehouse dispatches for carriers"),
    ("Leo", "Leo organizes neighborhood hikes for families"),
])
def test_brain_brief_and_remember_via_chat(env, subject, note):
    client, vid, _tmp = env
    from app.workspaces.resolve import LOCAL_PERSON_ID
    remember = client.post(
        f"/ventures/{vid}/chat",
        headers={"X-Subject": LOCAL_PERSON_ID},
        json={"message": f"remember that {note}"},
    )
    assert remember.status_code == 200, remember.text
    body_md = remember.json()["body_md"]
    assert remember.json()["kind"] == "memory_saved" and len(remember.json()["citations"]) == 1
    assert subject in body_md or "saved" in body_md.lower()

    brief = client.post(
        f"/ventures/{vid}/chat",
        json={"message": f"what do I know about {subject}?"},
    )
    assert brief.status_code == 200, brief.text
    body = brief.json()["body_md"]
    assert subject in body or any(abstention in body.lower() for abstention in
                                  ("don't know", "nothing stored", "don't have a record"))


@pytest.mark.parametrize("finding", ["Warehouse schedulers need reliable loading-bay bookings.",
                                      "Amateur runners need local routes with drinking-water stops."])
def test_research_prefers_existing_dossier_only_in_hosted_chat(env, monkeypatch, finding):
    client, vid, tmp_path = env
    from app.chat import actions, execute
    from app.core.local_mode import is_local

    research = tmp_path / "ventures" / vid / "research"
    research.mkdir(parents=True)
    (research / "validation_dossier.md").write_text(
        "# Research notes\n\n" + finding,
        encoding="utf-8",
    )
    from app.db.models import Job, Venture
    from app.state import MEM, db

    with db()() as session:
        session.get(Venture, vid).raw_idea = finding
        session.commit()

    MEM[f"research_dossier:{vid}"] = {
        "engine": "fixture",
        "download_path": f"/api/ventures/{vid}/research/dossier.md",
    }
    message = "research the market"
    if is_local():
        message = "Pressure-test my idea with market research"
        started = []

        def start(venture_id, actor_subject, *, expected_revision_id=None):
            started.append(venture_id)
            return {"kind": "text", "body_md": "Your research is starting.", "actions": [], "citations": []}

        monkeypatch.setattr(actions, "beta_blocked", lambda *_args: None)
        monkeypatch.setattr(execute, "local_capability_reply", lambda _name: None)
        monkeypatch.setattr(execute, "_research_start", start)
    reply = client.post(f"/ventures/{vid}/chat", json={"message": message})
    assert reply.status_code == 200, reply.text
    if is_local():
        assert started == [vid] and reply.json()["kind"] == "text"
        assert not any(action.get("confirmation") for action in reply.json()["actions"])
        return
    assert finding in reply.json()["body_md"]
    with db()() as session:
        assert not session.query(Job).filter_by(venture_id=vid, kind="research.deep").count()


def test_new_research_scope_does_not_replay_unrelated_dossier(env, monkeypatch):
    client, vid, tmp_path = env
    research = tmp_path / "ventures" / vid / "research"
    research.mkdir(parents=True)
    (research / "validation_dossier.md").write_text(
        "# Dossier\n\nVintage synthesizer restoration demand.", encoding="utf-8"
    )
    from app.state import MEM

    MEM[f"research_dossier:{vid}"] = {"engine": "fixture", "download_path": "dossier.md"}
    calls = []

    async def search(_venture_id, action, payload, actor_subject=None):
        calls.append((_venture_id, action, payload))
        return {"kind": "text", "body_md": "Official source result", "actions": [],
                "citations": [{"label": "Source", "href": "https://example.org/source"}]}

    monkeypatch.setattr("app.chat.router.execute_action", search)
    query = "Research official Greenhouse and Lever job posting API terms for LabHire today"
    reply = client.post(f"/ventures/{vid}/chat", json={"message": query})
    assert reply.status_code == 200, reply.text
    data = reply.json()
    assert data["kind"] == "text"
    assert "Official source result" in data["body_md"]
    assert "not a saved research dossier" in data["body_md"]
    assert "Vintage synthesizer" not in data["body_md"]
    assert data["citations"] == [{"label": "Source", "href": "https://example.org/source"}]
    assert calls == [(vid, "openworker_search", {"query": query})]


def test_research_with_evidence_constraints_still_uses_live_sources(env, monkeypatch):
    client, vid, tmp_path = env
    research = tmp_path / "ventures" / vid / "research"
    research.mkdir(parents=True)
    (research / "validation_dossier.md").write_text("# Old unrelated dossier", encoding="utf-8")
    from app.state import MEM

    MEM[f"research_dossier:{vid}"] = {"engine": "fixture", "download_path": "dossier.md"}
    calls = []

    async def search(_venture_id, action, payload, actor_subject=None):
        calls.append((_venture_id, action, payload))
        return {"kind": "text", "body_md": "Retrieved source links", "actions": [],
                "citations": [{"label": "Source", "href": "https://example.org/source"}]}

    monkeypatch.setattr("app.chat.router.execute_action", search)
    query = ("Research official Greenhouse and Lever job posting API terms for LabHire today. "
             "Give direct source links and say what remains unverified; do not scrape jobs or claim a completed dossier.")
    reply = client.post(f"/ventures/{vid}/chat", json={"message": query})
    assert reply.status_code == 200, reply.text
    data = reply.json()
    assert "Retrieved source links" in data["body_md"]
    assert "not a saved research dossier" in data["body_md"]
    assert "Old unrelated dossier" not in data["body_md"]
    assert data["citations"] == [{"label": "Source", "href": "https://example.org/source"}]
    assert calls == [(vid, "openworker_search", {"query": query})]


@pytest.mark.parametrize("idea", [
    "Inventory reconciliation for distribution teams",
    "Weekend walking plans for local families",
])
def test_requested_slate_override_saves_tasks_directly(env, idea):
    client, vid, _tmp = env
    from app.will.slate import generate_slate

    generate_slate(vid, because="test", idea=idea)
    before = client.get(f"/ventures/{vid}/tasks").json()["items"]
    preview = client.post(
        f"/ventures/{vid}/chat",
        json={"message": "do the first two only"},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["kind"] != "confirmation"
    assert "credit" not in preview.json()["body_md"].lower()
    assert client.get(f"/ventures/{vid}/tasks").json()["items"] != before

    done = client.post(
        f"/ventures/{vid}/chat/confirm",
        json={"action": "slate_override", "payload": {"query": "do the first two only"}},
    )
    assert done.status_code == 200, done.text
    assert "Keep items 1 and 2" in done.json()["body_md"]
    tasks = client.get(f"/ventures/{vid}/tasks")
    assert tasks.status_code == 200
    assert tasks.json()["items"]


def test_approve_says_none_pending(env):
    client, vid, _tmp = env
    preview = client.post(f"/ventures/{vid}/chat", json={"message": "approve this"})
    assert preview.status_code == 200, preview.text
    done = client.post(
        f"/ventures/{vid}/chat/confirm",
        json={"action": "approve", "payload": {"query": "approve"}},
    )
    assert done.status_code == 200, done.text
    assert "No pending" in done.json()["body_md"]


def test_old_negated_approval_card_cannot_execute(env):
    client, vid, _tmp = env
    done = client.post(
        f"/ventures/{vid}/chat/confirm",
        json={"action": "approve", "payload": {
            "query": "What should I do next for this personal project? Do not start a build or approve anything."
        }},
    )
    assert done.status_code == 200, done.text
    assert "No approval decision was recorded" in done.json()["body_md"]


def test_status_question_answers_instead_of_queueing(env):
    client, vid, _tmp = env
    from app.planning.store import save_tasks

    save_tasks(
        vid,
        [
            {
                "title": "Keep the real slate item",
                "why_now": "Must survive a status question.",
                "tags": ["test"],
                "est_minutes": 20,
                "runnable": True,
            }
        ],
        source="planner",
    )
    reply = client.post(
        f"/ventures/{vid}/chat",
        json={"message": "guys who is workign and what is the status update?"},
    )
    assert reply.status_code == 200, reply.text
    body = reply.json()["body_md"]
    # Status questions must not enqueue work (no "Change:" tasks) and must answer in prose.
    assert "Change:" not in body
    assert "**Now**" not in body
    assert "**Needs you**" not in body
    assert "Being done now" not in body  # old status-card template retired
    assert len(body.strip()) > 20
    tasks = client.get(f"/ventures/{vid}/tasks")
    assert tasks.status_code == 200
    titles = [row.get("title") for row in tasks.json().get("items") or []]
    assert "Keep the real slate item" in titles
    assert not any(str(title).startswith("Change:") for title in titles)


@pytest.mark.parametrize("audience", ["Factory maintenance teams", "Families choosing bedtime stories"])
def test_add_waitlist_form_enqueues_without_wiping_slate(env, monkeypatch, audience):
    client, vid, _tmp = env
    from app.planning.store import save_tasks
    from app.core.local_mode import is_local

    monkeypatch.setenv("CHAT_ROUTER_MODE", "off")
    save_tasks(
        vid,
        [
            {
                "title": "Existing slate work",
                "why_now": "Already planned.",
                "tags": ["test"],
                "est_minutes": 20,
                "runnable": True,
            }
        ],
        source="planner",
    )
    request = "add a waitlist form for " + audience if is_local() else "add a waitlist form"
    reply = client.post(f"/ventures/{vid}/chat", json={"message": request})
    assert reply.status_code == 200, reply.text
    if is_local():
        assert "Settings > Keys and connections > Coding tools" in reply.json()["body_md"]
        assert reply.json()["actions"][0]["href"] == "/settings/local#coding-tools"
        assert not reply.json().get("action_started")
    else:
        assert "is starting" in reply.json()["body_md"]
    tasks = client.get(f"/ventures/{vid}/tasks")
    titles = [row.get("title") for row in (tasks.json().get("items") or []) + (tasks.json().get("running") or [])]
    assert request in titles or request.capitalize() in titles
    assert "Existing slate work" in titles


def test_recorded_waitlist_enable_requires_consent_without_new_work(env, monkeypatch):
    from tests.company_os_harness import configure_local_gtm

    configure_local_gtm(monkeypatch)
    from sqlalchemy import func, select
    from app.chat import llm_router
    from app.core.local_mode import is_local
    from app.db.models import Job, PlannedTask
    from app.db.supymem_models import VentureWaitlist, VentureWaitlistEntry
    from app.planning.store import save_tasks
    from app.state import db
    from tests.fixtures.chat_eval.runner import DECISIONS
    from tests.unit.pagebuilder.test_api import connect_local_tool

    if is_local():
        connect_local_tool(monkeypatch)

    client, vid, _tmp = env
    monkeypatch.setenv("CHAT_ROUTER_MODE", "replay")
    monkeypatch.setenv("CHAT_ROUTER_DECISIONS", str(DECISIONS))
    monkeypatch.setattr(llm_router, "_REPLAY", None)
    save_tasks(vid, [{"title": "Existing reviewed slate", "why_now": "Kept.",
                     "tags": ["test"], "est_minutes": 20, "runnable": True}], source="planner")
    models = (Job, PlannedTask, VentureWaitlist, VentureWaitlistEntry)
    with db()() as session:
        before = [session.scalar(select(func.count()).select_from(model).where(model.venture_id == vid))
                  for model in models]
    request = "um so can you like turn the waitlist on for me thanks" if is_local() else "add a waitlist form"
    reply = client.post(f"/ventures/{vid}/chat", json={"message": request})
    assert reply.status_code == 200, reply.text
    payload = reply.json()
    assert payload["kind"] == "needs_you"
    assert "Next: build your landing page" in payload["body_md"]
    from app.conversion import waitlist
    with pytest.raises(waitlist.WaitlistError) as denied:
        waitlist.capture(vid, {"email": "reader@example.org", "consent": False, "consent_version": waitlist.VERSION})
    assert denied.value.code == "waitlist_consent_required"
    with db()() as session:
        after = [session.scalar(select(func.count()).select_from(model).where(model.venture_id == vid))
                 for model in models]
        assert after[:2] == before[:2] and after[3] >= before[3]
        assert session.get(VentureWaitlist, vid).enabled is True
        assert all(entry.is_test and entry.consent_version == waitlist.VERSION for entry in
                   session.scalars(select(VentureWaitlistEntry).where(VentureWaitlistEntry.venture_id == vid)))
    tasks = client.get(f"/ventures/{vid}/tasks")
    assert tasks.status_code == 200
    assert "Existing reviewed slate" in [row["title"] for row in tasks.json()["items"]]


def test_short_ambiguous_waitlist_command_clarifies_without_new_work(env):
    from sqlalchemy import func, select
    from app.db.models import Job, PlannedTask
    from app.planning.store import save_tasks
    from app.state import db
    client, vid, _tmp = env
    save_tasks(vid, [{"title": "Existing reviewed slate", "why_now": "Kept.",
                     "tags": ["test"], "est_minutes": 20, "runnable": True}], source="planner")
    with db()() as session:
        before_tasks = session.scalar(select(func.count(PlannedTask.id)).where(PlannedTask.venture_id == vid))
        before_jobs = session.scalar(select(func.count(Job.id)).where(Job.venture_id == vid))
    reply = client.post(f"/ventures/{vid}/chat", json={"message": "add a waitlist"})
    assert reply.status_code == 200, reply.text
    assert "which do you mean" in reply.json()["body_md"] and "Nothing has been started" in reply.json()["body_md"]
    with db()() as session:
        assert session.scalar(select(func.count(PlannedTask.id)).where(PlannedTask.venture_id == vid)) == before_tasks
        assert session.scalar(select(func.count(Job.id)).where(Job.venture_id == vid)) == before_jobs
    tasks = client.get(f"/ventures/{vid}/tasks")
    assert "Existing reviewed slate" in [row["title"] for row in tasks.json()["items"]]
