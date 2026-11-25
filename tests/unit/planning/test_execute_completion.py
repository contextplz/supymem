"""Searching the task title is not completion for non-research work."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tests.company_os_harness import company_os_client, configure_local_test_model, load_company_os_app


@pytest.fixture
def local_build_client(monkeypatch, request):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    return request.getfixturevalue("client")


@pytest.mark.parametrize("domain", ["Stock checks for wholesalers", "Meal planning for families"])
@pytest.mark.parametrize("entry", ["start_planned_task", "execute_planned_task"])
@pytest.mark.parametrize("connected", [True, False])
def test_local_requested_build_routes_preserve_saved_scope(local_build_client, monkeypatch, domain, entry, connected):
    import asyncio
    from app.build import device_orders
    from app.builder import ctl
    from app.planning import execute, store
    from fastapi import HTTPException

    client = local_build_client
    response = client.post("/ventures", json={"raw_idea": domain, "start": False})
    assert response.status_code == 201
    venture_id = response.json()["id"]
    detail = "Change only the requested screen. Keep the existing data and run its local checks."
    calls = []
    approvals = []

    def tool(*args, **kwargs):
        if not connected:
            raise HTTPException(409, detail={"code": "build_tool_needs_setup", "message": "Open Connections > Coding tools."})
        return "codex"

    def prepared(vid, caller, brief, **kwargs):
        calls.append((vid, brief, kwargs))
        return {"id": "order", "venture_id": vid, "state": "pending", "review_digest": "digest"}

    def approved(vid, caller, order_id, digest, action):
        approvals.append(action)
        return {"id": order_id, "venture_id": vid, "state": "approved"}

    def internal(*args, **kwargs):
        pytest.fail("Local requested work must not generate or dispatch internally")

    monkeypatch.setattr(ctl, "local_tool", tool)
    monkeypatch.setattr(device_orders, "prepare_coding_task_from_chat", prepared)
    monkeypatch.setattr(device_orders, "prepare_product_build_from_chat", internal)
    monkeypatch.setattr(device_orders, "transition", approved)
    monkeypatch.setattr(execute, "_ship_first_look", internal)
    monkeypatch.setattr(execute, "_run_prototype", internal)
    monkeypatch.setattr("app.firstlook.service.apply_founder_copy", internal)
    runner = getattr(execute, entry)
    for title in ("Build a landing page", "Rewrite the landing page headline", "Prototype the workflow",
                  "Fix the date sorting bug", "Create a scraper", "Create a small tool"):
        calls.clear()
        approvals.clear()
        task = store.enqueue_task(venture_id, {"title": title + " for " + domain, "detail": detail,
            "tags": ["plan_task:requested-task"], "runnable": True}, source="chat")
        if connected:
            result = runner(venture_id, task["id"])
            if entry == "execute_planned_task":
                result = asyncio.run(result)
            assert result["status"] == "queued" and result["device_work_order_id"] == "order"
            assert result["review_path"] == f"/v/{venture_id}/device-work-orders/order" and "completion" not in result and "artifact" not in result
            assert len(calls) == 1 and approvals == ["approve"]
            assert all(value in calls[0][1] for value in (task["id"], title, domain, detail, "chat", "requested-task"))
        else:
            with pytest.raises(HTTPException) as error:
                result = runner(venture_id, task["id"])
                if entry == "execute_planned_task":
                    asyncio.run(result)
            assert "Settings > Keys and connections > Coding tools" in error.value.detail["message"]
            assert calls == [] and approvals == []
        assert store.get_task(venture_id, task["id"])["status"] == "open"


@pytest.mark.parametrize("domain", ["Stock checks for wholesalers", "Meal planning for families"])
@pytest.mark.parametrize("control", ["code_denied", "cancelled_during_admission", "critical_scope"])
def test_local_build_keeps_owner_stop_controls(local_build_client, monkeypatch, domain, control):
    from app.build import device_orders
    from app.builder import ctl
    from app.db.models import PlannedTask
    from app.planning import execute, store
    from app.state import db

    response = local_build_client.post("/ventures", json={"raw_idea": domain, "start": False})
    assert response.status_code == 201
    venture_id = response.json()["id"]
    detail = "Drop the table holding local records." if control == "critical_scope" else "Change only the requested workflow."
    task = store.enqueue_task(venture_id, {"title": "Prototype " + domain, "detail": detail,
        "tags": ["plan_task:requested-task"], "runnable": True}, source="chat")
    calls, stopped = [], []

    def requested(*args, **kwargs):
        calls.append(args)
        with db()() as session:
            session.get(PlannedTask, task["id"]).status = "dismissed"
            session.commit()
        return {"id": "order", "review_path": "/watch"}

    monkeypatch.setattr(ctl, "request_local_build", requested)
    monkeypatch.setattr("app.munder.control.tool_decision", lambda *_args: {"deny": control == "code_denied"})
    monkeypatch.setattr(device_orders, "emergency_stop", lambda vid, caller, order_id: stopped.append(order_id))
    if control in {"code_denied", "critical_scope"}:
        with pytest.raises(execute.TaskRunError) as error:
            execute.start_planned_task(venture_id, task["id"])
        assert error.value.code == ("code_denied" if control == "code_denied" else "task_approval_required") and "Work" in error.value.message
        assert calls == [] and stopped == []
    else:
        result = execute.start_planned_task(venture_id, task["id"])
        assert result["status"] == "cancelled" and stopped == ["order"]
        assert store.get_task(venture_id, task["id"])["status"] == "dismissed"


@pytest.mark.parametrize("domain", ["Stock checks for wholesalers", "Meal planning for families"])
def test_local_research_notes_are_not_a_coding_request(local_build_client, monkeypatch, domain):
    from app.autopilot.authority import founder_scope
    from app.builder import ctl
    from app.planning import execute, store

    response = local_build_client.post("/ventures", json={"raw_idea": domain, "start": False})
    assert response.status_code == 201
    venture_id = response.json()["id"]
    monkeypatch.setattr(ctl, "request_local_build", lambda *args, **kwargs: pytest.fail("Research notes must stay notes"))
    for title in ("Write research notes on bug symptoms", "Draft a brief for the landing page headline",
                  "Prepare evidence notes about prototype options"):
        task = store.enqueue_task(venture_id, {"title": title + " for " + domain,
            "detail": "Record the observed evidence. Do not change the page or source code.",
            "tags": ["capability:firstlook.landing"], "runnable": True}, source="chat")
        assert execute.work_kind(task["title"], task["tags"], task["detail"]) == "research"
        assert execute._is_local_build_task(task) is False
        with founder_scope(venture_id, autonomous=True):
            result = execute.start_planned_task(venture_id, task["id"])
        assert result.get("job_id") and "device_work_order_id" not in result


@pytest.mark.parametrize("domain", ["Stock checks for wholesalers", "Meal planning for families"])
@pytest.mark.parametrize("connected", [True, False])
def test_local_task_worker_routes_and_reports_tool_setup(local_build_client, monkeypatch, domain, connected):
    import asyncio
    from app.builder import ctl
    from app.chat.store import list_messages
    from app.core.jobs import FatalError
    from app.db.models import PlannedTask
    from app.planning import execute, store
    from app.state import db
    from fastapi import HTTPException

    response = local_build_client.post("/ventures", json={"raw_idea": domain, "start": False})
    assert response.status_code == 201
    venture_id = response.json()["id"]
    task = store.enqueue_task(venture_id, {"title": "Prototype " + domain, "detail": "Change only the requested workflow.",
        "tags": ["plan_task:requested-task"], "runnable": True}, source="chat")
    with db()() as session:
        session.get(PlannedTask, task["id"]).status = "running"
        session.commit()
    calls = []

    def requested(vid, brief, **kwargs):
        if not connected:
            raise HTTPException(409, detail={"code": "build_tool_needs_setup", "message": "Open Connections > Coding tools."})
        calls.append(brief)
        return {"id": "order", "review_path": f"/v/{vid}/device-work-orders/order"}

    monkeypatch.setattr(ctl, "request_local_build", requested)
    monkeypatch.setattr(execute, "_run_prototype", lambda *_args: pytest.fail("A worker must not dispatch internally"))
    if connected:
        asyncio.run(execute.run_task_job({"venture_id": venture_id, "task_id": task["id"]}))
        assert len(calls) == 1 and task["id"] in calls[0] and domain in calls[0]
        assert any(action.get("href") == f"/v/{venture_id}/device-work-orders/order"
                   for message in list_messages(venture_id) for action in message.get("actions") or [])
        assert store.get_task(venture_id, task["id"])["status"] == "running"
    else:
        with pytest.raises(FatalError, match="build_tool_needs_setup"):
            asyncio.run(execute.run_task_job({"venture_id": venture_id, "task_id": task["id"]}))
        assert calls == []
        assert any("Settings > Keys and connections > Coding tools" in message["body_md"]
                   for message in list_messages(venture_id))
        assert store.get_task(venture_id, task["id"])["status"] == "open"


@pytest.mark.parametrize("idea", ["Maintenance scheduling for factories", "Birdwatching journals for families"])
def test_local_unrequested_legacy_build_cannot_report_fake_success(client, monkeypatch, idea):
    from app.builder import ctl
    from fastapi import HTTPException

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(ctl, "get_provider", lambda: pytest.fail("Fake provider is not a local build"))
    with pytest.raises(HTTPException) as error:
        ctl.run_build({"workspace_id": "workspace", "venture_id": "venture", "reason": idea})
    assert error.value.status_code == 409
    assert "chat" in error.value.detail["message"].lower()
    with pytest.raises(HTTPException) as error:
        ctl.run_qa({"build_run_id": "unverified", "app_id": "app", "workspace_id": "workspace", "venture_id": "venture"})
    assert error.value.status_code == 409 and "Build activity" in error.value.detail["message"]


@pytest.mark.parametrize("idea", ["Maintenance scheduling for factories", "Birdwatching journals for families"])
def test_local_generic_prototype_never_promotes_stub_files(client, monkeypatch, tmp_path, idea):
    import asyncio
    from app.planning import execute
    from coding_agents import dispatcher

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(dispatcher, "dispatch", lambda **kwargs: pytest.fail("Unrequested generic coding run"))
    complete, message = asyncio.run(execute._run_prototype("venture", "Build " + idea, "", tmp_path, "task"))
    assert complete is False and "chat" in message.lower()


@pytest.mark.parametrize("idea", ["Maintenance scheduling for factories", "Birdwatching journals for families"])
def test_local_adr_dispatch_and_preview_do_not_claim_a_completed_app(client, monkeypatch, tmp_path, idea):
    import asyncio
    from types import SimpleNamespace
    from app.build import dispatcher, preview
    from app.build.workorder import WorkOrder
    from app.builder import ctl

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(ctl, "request_local_build", lambda *args, **kwargs:
        {"id": "order", "state": "approved", "review_path": "/watch"}, raising=False)
    work = WorkOrder(id="work", venture_id="venture", title="Build " + idea, components=[], files_expected=[])
    monkeypatch.setattr(dispatcher, "locked_adr_for_venture", lambda *args: None)
    refused = asyncio.run(dispatcher.dispatch(work, workdir=tmp_path))
    assert not refused.ok and "save your architecture brief" in refused.reason
    monkeypatch.setattr(dispatcher, "locked_adr_for_venture", lambda *args:
        SimpleNamespace(title=idea, stack=[], protected_routes=[]))
    result = asyncio.run(dispatcher.dispatch(work, workdir=tmp_path))
    assert result.device_work_order_id == "order" and work.status != "done" and not result.ok
    assert preview.start_preview(5173).status == "unverified"


@pytest.mark.parametrize("idea", ["Maintenance scheduling for factories", "Birdwatching journals for families"])
def test_local_legacy_file_allowlist_is_not_silently_widened(client, monkeypatch, tmp_path, idea):
    import asyncio
    from app.build import dispatcher
    from app.build.workorder import WorkOrder
    from app.builder import ctl

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(dispatcher, "locked_adr_for_venture", lambda *args: object())
    monkeypatch.setattr(ctl, "request_local_build", lambda *args, **kwargs: pytest.fail("A file allowlist cannot become whole-project permission"), raising=False)
    work = WorkOrder(id="work", venture_id="venture", title="Build " + idea, components=[], files_expected=["only-this-file.py"])
    result = asyncio.run(dispatcher.dispatch(work, workdir=tmp_path))
    assert not result.ok and "venture chat" in result.reason and work.status == "planned"


@pytest.mark.parametrize("idea", ["Maintenance scheduling for factories", "Birdwatching journals for families"])
@pytest.mark.parametrize("provider", ["byo", "qm"])
def test_local_onboarding_does_not_materialize_a_fake_completed_app(client, monkeypatch, tmp_path, idea, provider):
    import asyncio
    from types import SimpleNamespace
    from app.builder import ctl
    from app.onboarding.state import Ctx
    from app.onboarding.steps import build

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(ctl, "local_tool", lambda *args, **kwargs: "codex")
    monkeypatch.setattr(ctl, "request_local_build", lambda *args, **kwargs:
        {"id": "coding_order", "state": "approved", "review_path": "/builds"})
    monkeypatch.setattr(build, "get_run_gate", lambda *args: None)
    monkeypatch.setattr(build, "set_run_gate", lambda *args: None)
    monkeypatch.setattr(build, "_stub_landing_files", lambda *args, **kwargs: pytest.fail("A stub is not your built app"))
    ctx = Ctx(venture=SimpleNamespace(id="venture", name=idea, raw_idea=idea, one_liner=idea, workspace_id=None),
              run=SimpleNamespace(id="onboarding_run", workspace_id=None))
    runner = getattr(build, "_landing_page_via_" + provider)
    repo = tmp_path / "repo"
    repo.mkdir()
    result = asyncio.run(runner(ctx, repo, idea, idea))
    assert result.pause is not None and result.partial is True
    assert result.output["device_work_order_id"] == "coding_order"
    assert result.output.get("files", []) == [] and list(repo.iterdir()) == []


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/exec.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("CODING_AGENT", "inhouse_stub")
    monkeypatch.delenv("ZERNIO_API_KEY", raising=False)
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    with company_os_client(main.create_app()) as c:
        yield c


def _venture(client: TestClient) -> str:
    created = client.post(
        "/ventures",
        json={"raw_idea": "lobbylock apartment buildings for section 8 waitlists"},
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


@pytest.mark.parametrize("idea", ["Dispatch checklists for repair shops", "Drawing practice for new artists"])
def test_local_owner_client_preserves_the_request_boundary(client, idea):
    from app.core.local_mode import is_local
    from app.db.models import Job, Venture
    from app.state import db
    from sqlalchemy import func, select

    if not is_local():
        pytest.skip("Local owner request boundary")
    with db()() as session:
        count = session.scalar(select(func.count()).select_from(Venture))
    credential = client.headers.pop("X-Local-Proxy-Secret")
    try:
        denied = client.post("/ventures", json={"raw_idea": idea, "start": False})
    finally:
        client.headers["X-Local-Proxy-Secret"] = credential
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "local_owner_required"
    with db()() as session:
        assert session.scalar(select(func.count()).select_from(Venture)) == count
    created = client.post("/ventures", json={"raw_idea": idea, "start": False})
    assert created.status_code == 201
    leaked = credential in created.text
    assert not leaked
    with db()() as session:
        assert session.scalars(select(Job.kind).where(Job.venture_id == created.json()["id"])).all() == []


def _researcher_assignee(vid):
    """Native registered catalog authority for tests that actually read sources."""
    from app.org.catalog import load_catalog
    from app.org.characters import encode_assignee
    from app.org.registry import registry
    from app.org.summary import persist_org, resolve_org
    entry = next(row for row in load_catalog()["roles"] if row["role"] == "researcher")
    instance = registry.create_agent_from_spec(venture_id=vid, role="researcher", name="Synthetic researcher",
        charge=entry["deliverable"], system_prompt="Read public sources within the registered research permissions.",
        allowed_tools=entry["allowed_tools"], model_tier=entry["model_tier"],
        fitness_metric=entry["fitness_metric"], retire_when=entry["retire_when"])
    agent = {"id": instance.id, "role": "researcher", "name": "Synthetic researcher", "status": "active"}
    org = resolve_org(vid)
    persist_org(vid, {**org, "agents": [*org.get("agents", []), agent]})
    return encode_assignee(agent)


def test_work_kind_classifies_playbook_titles():
    load_company_os_app()
    from app.planning.execute import work_kind

    assert work_kind("Publish a lobbylock apartment problem landing page") == "site"
    assert work_kind("Launch a lobbylock apartment waitlist test") == "site"
    assert work_kind("Write the lobbylock apartment value proposition") == "site"
    assert work_kind("Draft the lobbylock apartment founder outreach") == "outreach"
    assert work_kind("Run a lobbylock apartment buildings demand probe") == "research"
    assert work_kind("Prototype the lobbylock apartment core workflow") == "prototype"
    assert work_kind("Recruit lobbylock apartment design partners") == "recruit"
    assert work_kind("Compare lobbylock apartment alternatives") == "research"
    assert work_kind("Run a lobbylock apartment demand probe") == "research"


@pytest.mark.asyncio
async def test_approved_build_task_requires_metered_build_workflow(client: TestClient, monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    vid = _venture(client)
    from app.planning.execute import TaskRunError, execute_planned_task
    from app.planning.store import get_task, save_tasks

    saved = save_tasks(
        vid,
        [{
            "title": "Build the approved landing page",
            "why_now": "The founder approved this milestone.",
            "tags": ["plan:pln_1", "plan_task:tsk_plan_1", "capability:build.run"],
            "runnable": True,
        }],
        source="approved_plan:pln_1",
    )

    with pytest.raises(TaskRunError) as raised:
        await execute_planned_task(vid, saved[0]["id"])

    assert raised.value.status == 409
    assert raised.value.code == "dedicated_workflow_required"
    stored = get_task(vid, saved[0]["id"])
    assert stored is not None
    assert stored["status"] == "open"
    assert stored["runnable"] is True


@pytest.mark.parametrize("domain", ["Wholesaler stock tracking", "Family meal planning"])
@pytest.mark.parametrize("entry", ["start_planned_task", "execute_planned_task"])
async def test_local_build_without_detail_keeps_its_task_and_names_tool_setup(local_build_client, monkeypatch, domain, entry):
    from app.planning import execute
    from app.planning.store import enqueue_task, get_task
    from desktop_runtime import agent_status

    response = local_build_client.post("/ventures", json={"raw_idea": domain, "start": False})
    assert response.status_code == 201
    venture_id = response.json()["id"]
    task = enqueue_task(venture_id, {"title": "Build a landing page for " + domain, "detail": None,
        "tags": ["capability:build.run"], "runnable": True})
    monkeypatch.setattr(agent_status, "statuses", lambda: [])
    with pytest.raises(HTTPException) as failure:
        if entry == "execute_planned_task":
            await execute.execute_planned_task(venture_id, task["id"])
        else:
            execute.start_planned_task(venture_id, task["id"])
    assert failure.value.status_code == 409 and failure.value.detail["code"] == "build_tool_needs_setup"
    assert "Settings > Keys and connections > Coding tools" in failure.value.detail["message"]
    stored = get_task(venture_id, task["id"])
    assert stored["detail"] is None and stored["status"] == "open" and stored["runnable"] is True


@pytest.mark.asyncio
async def test_existing_out_of_scope_plan_task_cannot_run_from_old_work_projection(client: TestClient):
    vid = _venture(client)
    from app import state
    from app.db.models import Venture
    from app.db.supymem_models import ExecutionPlan
    from app.planning.execute import TaskRunError, execute_planned_task
    from app.planning.store import get_task, save_tasks
    from core.clock import utcnow
    import json

    with state.db()() as session:
        venture = session.get(Venture, vid)
        session.add(ExecutionPlan(id="pln_out", workspace_id=venture.workspace_id, venture_id=vid,
            version=1, state="approved", structured=json.dumps({"scope_details": {
                "tsk_out": {"scope": "out", "reason": "Executor unavailable."}}}), created_at=utcnow()))
        session.commit()
    saved = save_tasks(vid, [{"title": "Unavailable research executor", "why_now": "Old queue",
        "tags": ["plan_task:tsk_out", "capability:research.deep"], "runnable": True}],
        source="approved_plan:pln_out")

    with pytest.raises(TaskRunError) as raised:
        await execute_planned_task(vid, saved[0]["id"])

    assert raised.value.code == "plan_task_unavailable"
    assert get_task(vid, saved[0]["id"])["status"] == "open"


def test_draft_priya_note_is_private_email_not_title_search():
    load_company_os_app()
    from app.chat.steer import is_private_email_draft, parse_steer

    assert is_private_email_draft("draft Priya note")
    assert is_private_email_draft("draft a note to Priya")
    assert is_private_email_draft("draft an email to Priya, do not send")
    assert not is_private_email_draft("Draft the lobbylock apartment buildings founder outreach")
    ops = parse_steer("draft Priya note")
    assert [op.kind for op in ops] == ["draft_email"]
    assert ops[0].payload.get("to") == "Priya"


def test_no_delivery_gtm_preparation_is_research_not_live_outreach():
    load_company_os_app()
    from app.chat.steer import is_non_delivery_outreach
    from app.planning.execute import _founder_gate, work_kind

    request = (
        "Prepare the founder-reviewed GTM outreach channel options for LabHire, "
        "but do not contact anyone, send mail, or publish anything."
    )
    assert is_non_delivery_outreach(request)
    assert work_kind(request) == "research"
    assert _founder_gate(request, [], "ven_safe", request) is None
    assert not is_non_delivery_outreach("Publish this launch post to LinkedIn now.")
    gate = _founder_gate("Publish this launch post to LinkedIn now", [], "ven_live")
    assert gate is not None
    assert gate["provider"] == "social"


@pytest.mark.asyncio
async def test_no_delivery_gtm_preparation_saves_brief_without_social_call(
    client: TestClient, monkeypatch
):
    vid = _venture(client)
    social_calls = {"n": 0}

    async def fail_social(*_a, **_k):
        social_calls["n"] += 1
        raise AssertionError("preparation must not call social.post")

    async def fake_search(query, **_k):
        return {
            "mode": "live",
            "summary": f"Compared safe channel options for {query}",
            "pages": [
                {
                    "title": "Channel evidence",
                    "url": "https://example.com/channel-evidence",
                    "excerpt": "Founder-reviewable evidence.",
                    "chars": len("Founder-reviewable evidence."),
                }
            ],
            "hits": [{"url": "https://example.com/channel-evidence"}],
        }

    monkeypatch.setattr("app.planning.execute._run_social_post", fail_social)
    monkeypatch.setattr("app.research.explore.explore_web", fake_search)

    from app.planning.execute import execute_planned_task
    from app.planning.store import enqueue_task, get_task

    request = (
        "Prepare the founder-reviewed GTM outreach channel options for LabHire, "
        "but do not contact anyone, send mail, or publish anything."
    )
    task = enqueue_task(
        vid,
        {
            "title": request,
            "detail": request,
            "why_now": "The founder requested a reviewable plan only.",
            "tags": ["chat", "gtm", _researcher_assignee(vid)],
            "runnable": True,
        },
    )
    result = await execute_planned_task(vid, task["id"])

    assert result["status"] == "blocked"
    assert result["failure_code"] == "research_coverage_unverified"
    assert social_calls["n"] == 0
    stored = get_task(vid, task["id"])
    assert stored is not None
    assert stored["status"] == "open"
    assert stored.get("last_error") == "incomplete:research_coverage_unverified"
    artifact = result.get("artifact")
    assert artifact
    text = __import__("pathlib").Path(artifact).read_text(encoding="utf-8")
    assert "https://example.com/channel-evidence" in text
    assert "Email draft saved" not in text
    from app.config import data_root

    assert not (data_root() / "ventures" / vid / "repo" / "drafts").exists()


@pytest.mark.asyncio
async def test_draft_priya_note_writes_draft_and_closes(client: TestClient):
    vid = _venture(client)
    from app.planning.execute import execute_planned_task
    from app.planning.store import enqueue_task, get_task

    task = enqueue_task(
        vid,
        {
            "title": "draft Priya note",
            "detail": "Do this instead: draft Priya note.",
            "why_now": "You retargeted in-flight work to: “draft Priya note”",
            "tags": ["chat", "retarget"],
            "runnable": True,
        },
    )
    result = await execute_planned_task(vid, task["id"])
    assert result["status"] == "done"
    stored = get_task(vid, task["id"])
    assert stored is not None
    assert stored["status"] == "done"
    assert stored.get("last_error") in (None, "")
    from app.config import data_root

    draft = data_root() / "ventures" / vid / "repo" / "drafts" / "email-to-priya.md"
    assert draft.is_file(), draft
    text = draft.read_text(encoding="utf-8").lower()
    assert "not sent" in text
    assert "priya" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("idea", ["A fabric pattern guide for home sewists", "A maintenance handover tool for wastewater plants"])
async def test_unsent_interview_invitation_is_a_reviewable_draft_not_search(
    client: TestClient, monkeypatch, idea
):
    created = client.post("/ventures", json={"raw_idea": idea})
    assert created.status_code == 201, created.text
    vid = created.json()["id"]
    searched = {"n": 0}

    async def fail_search(*_args, **_kwargs):
        searched["n"] += 1
        raise AssertionError("a draft must not search for email examples")

    monkeypatch.setattr("app.research.explore.explore_web", fail_search)
    from app.planning.execute import execute_planned_task
    from app.planning.saved_work import task_note
    from app.planning.store import enqueue_task

    request = ("Draft an invitation to an optional 15-minute problem interview. "
               "The product beta is not live yet. Do not send or publish anything.")
    task = enqueue_task(vid, {"title": request, "detail": request,
                              "why_now": "Founder requested an unsent review draft.",
                              "tags": ["chat", "gtm"], "runnable": True})
    result = await execute_planned_task(vid, task["id"])
    note = task_note(vid, task["id"])
    assert result["status"] == "done"
    assert searched["n"] == 0
    assert "Subject: Optional 15-minute conversation" in note
    assert "The product beta is not live yet" in note
    assert "not sent or published" in note
    assert "you must review" in note.lower() and "[" not in note
    assert "Searched" not in note


@pytest.mark.asyncio
async def test_single_email_offer_with_invite_constraints_returns_composed_draft(client, monkeypatch):
    configure_local_test_model()
    vid = _venture(client)
    from app.chat import offers
    from app.planning import execute
    from app.planning.saved_work import task_note
    from app.planning.store import get_task

    request = (
        "Draft one short interview email for me to review for Kennel Scheduler. "
        "Sample Grooming Studio's own website https://sample-grooming.test/dog-grooming-bangalore "
        "says it offers dog grooming in Bangalore and publicly lists services@sample-grooming.test. "
        "Solo/independent ownership, paper-diary use and no-show pain are unknown; do not claim those. "
        "We have only an unlaunched concept, no product users, interviews, payments or validated outcomes. "
        "Invite its owner or appointment manager to a voluntary 10–15 minute non-sales call or email reply "
        "about their own general booking/reminder workflow and what happened the last time an appointment "
        "was canceled or missed. No client names, pet/client records, phone numbers, private messages or "
        "screenshots. Give exact subject and message; do not send anything, include a working-product "
        "claim or search for more contacts."
    )
    composed = "Draft 1\nSubject: Your appointment workflow\n\nNo product exists yet. A voluntary conversation about your own general workflow; no private records."
    seen = []

    async def compose(_vid, title, detail):
        seen.append((title, detail))
        return composed

    async def no_effect(*_args, **_kwargs):
        raise AssertionError("review-only email must not search, build or publish")

    monkeypatch.setattr(execute, "_review_drafts", compose)
    monkeypatch.setattr(execute, "_interview_invitation", lambda *_: pytest.fail("legacy invitation must not capture this task"))
    monkeypatch.setattr(execute, "_is_site_copy", lambda *_: True)
    monkeypatch.setattr(execute, "_ship_first_look", no_effect)
    monkeypatch.setattr(execute, "_run_prototype", no_effect)
    monkeypatch.setattr(execute, "_run_social_post", no_effect)
    monkeypatch.setattr("app.research.explore.explore_web", no_effect)
    result = await offers.draft_run(vid, request=request)
    assert composed in result["body_md"]
    assert len(seen) == 1 and request in seen[0][1]
    assert "Nothing was sent" in result["body_md"]
    from app.db.models import PlannedTask
    from app.state import db
    from sqlalchemy import select
    with db()() as session:
        rows = session.scalars(select(PlannedTask).where(PlannedTask.venture_id == vid,
            PlannedTask.title == "Draft one interview email for me to review")).all()
        assert len(rows) == 1
        task_id = rows[0].id
    assert get_task(vid, task_id)["status"] == "done"
    assert composed in task_note(vid, task_id)


@pytest.mark.asyncio
async def test_landing_title_search_does_not_close(client: TestClient, monkeypatch):
    from app.core.local_mode import is_local
    from desktop_runtime import agent_status

    vid = _venture(client)
    searched = {"n": 0}

    async def boom_search(*_a, **_k):
        searched["n"] += 1
        return {"summary": "should not run", "pages": [], "hits": []}

    async def no_html(venture_id, title, detail):
        return False, "\n\nFirst-look HTML did not change."

    monkeypatch.setattr("app.research.explore.explore_web", boom_search)
    monkeypatch.setattr("app.planning.execute._ship_first_look", no_html)

    from app.planning.execute import execute_planned_task
    from app.planning.store import enqueue_task, get_task

    task = enqueue_task(
        vid,
        {
            "title": "Publish a lobbylock apartment problem landing page",
            "detail": "A real product page, not a search dump.",
            "why_now": "Landing is still a stub.",
            "tags": ["phase", "venture.discovery"],
            "runnable": True,
        },
    )
    # The merged site workflow prepares a draft; it neither publishes nor completes missing HTML.
    if is_local():
        monkeypatch.setattr(agent_status, "statuses", lambda: [])
        with pytest.raises(HTTPException) as failure:
            await execute_planned_task(vid, task["id"])
        assert failure.value.detail["code"] == "build_tool_needs_setup"
        assert "Settings > Keys and connections > Coding tools" in failure.value.detail["message"]
    else:
        result = await execute_planned_task(vid, task["id"])
        assert result["status"] != "done"
    assert searched["n"] == 0
    stored = get_task(vid, task["id"])
    assert stored is not None
    assert stored["status"] == "open"
    assert stored.get("runnable") is is_local()


@pytest.mark.asyncio
async def test_waitlist_recruit_prototype_never_close_on_search(client: TestClient, monkeypatch):
    from app.core.local_mode import is_local
    from desktop_runtime import agent_status

    vid = _venture(client)
    searched = {"n": 0}

    async def boom_search(*_a, **_k):
        searched["n"] += 1
        return {"summary": "nope", "pages": [], "hits": []}

    async def no_files(*_a, **_k):
        return False, "\n\nDispatcher wrote no files; prototype is not done."

    async def no_html(*_a, **_k):
        return False, "\n\nFirst-look HTML did not change."

    monkeypatch.setattr("app.research.explore.explore_web", boom_search)
    monkeypatch.setattr("app.planning.execute._run_prototype", no_files)
    monkeypatch.setattr("app.planning.execute._ship_first_look", no_html)

    from app.planning.execute import execute_planned_task
    from app.planning.store import enqueue_task, get_task

    titles = [
        "Launch a lobbylock apartment waitlist test",
        "Prototype the lobbylock apartment core workflow",
        "Recruit lobbylock apartment design partners",
    ]
    monkeypatch.setattr(agent_status, "statuses", lambda: [])
    for position, title in enumerate(titles):
        task = enqueue_task(
            vid,
            {
                "title": title,
                "detail": "Must be real work.",
                "why_now": "Not a google query.",
                "tags": ["phase"],
                "runnable": True,
            },
        )
        if is_local() and position < 2:
            with pytest.raises(HTTPException) as failure:
                await execute_planned_task(vid, task["id"])
            assert failure.value.detail["code"] == "build_tool_needs_setup"
            assert "Settings > Keys and connections > Coding tools" in failure.value.detail["message"]
        else:
            result = await execute_planned_task(vid, task["id"])
            assert result["status"] != "done", title
        stored = get_task(vid, task["id"])
        assert stored["status"] == "open", title
    assert searched["n"] == 0


@pytest.mark.asyncio
async def test_live_research_note_cannot_close_without_verified_coverage(client: TestClient, monkeypatch):
    vid = _venture(client)

    async def fake_search(query, **_k):
        return {
            "mode": "live",
            "summary": f"Looked up {query}",
            "pages": [{"title": "A", "url": "https://example.com", "excerpt": "ok", "chars": 2}],
            "hits": [{"url": "https://example.com"}],
        }

    monkeypatch.setattr("app.research.explore.explore_web", fake_search)

    from app.planning.execute import execute_planned_task
    from app.planning.store import enqueue_task

    task = enqueue_task(
        vid,
        {
            "title": "Compare lobbylock apartment alternatives",
            "detail": "Who else is in this space?",
            "why_now": "Need a sourced read.",
            "tags": ["insight", "venture.discovery", _researcher_assignee(vid)],
            "runnable": True,
        },
    )
    result = await execute_planned_task(vid, task["id"])
    assert result["status"] == "blocked"
    assert result["failure_code"] == "research_coverage_unverified"


@pytest.mark.asyncio
async def test_site_closes_only_when_html_changes(client: TestClient, monkeypatch):
    from app.core.local_mode import is_local
    from app.planning.store import get_task
    from desktop_runtime import agent_status

    vid = _venture(client)

    async def ship_ok(*_a, **_k):
        return True, "\n\nServed first-look HTML is now version 2 at `/site/`."

    monkeypatch.setattr("app.planning.execute._ship_first_look", ship_ok)

    from app.planning.execute import execute_planned_task
    from app.planning.store import enqueue_task

    task = enqueue_task(
        vid,
        {
            "title": "Write the lobbylock apartment value proposition",
            "detail": "Put the offer on the served page.",
            "why_now": "Copy is still generic.",
            "tags": ["phase"],
            "runnable": True,
        },
    )
    if is_local():
        monkeypatch.setattr(agent_status, "statuses", lambda: [])
        with pytest.raises(HTTPException) as failure:
            await execute_planned_task(vid, task["id"])
        assert failure.value.detail["code"] == "build_tool_needs_setup"
        assert "Settings > Keys and connections > Coding tools" in failure.value.detail["message"]
        assert get_task(vid, task["id"])["status"] == "open"
    else:
        result = await execute_planned_task(vid, task["id"])
        assert result["status"] == "done"
        assert result.get("completion", {}).get("mode") == "site"


def test_refill_skips_when_product_work_is_parked(client: TestClient):
    vid = _venture(client)
    from app.planning.cycle import refill_unused_work
    from app.planning.store import enqueue_task

    enqueue_task(
        vid,
        {
            "title": "Publish a lobbylock apartment problem landing page",
            "detail": "Need a real page",
            "why_now": "Site is a stub",
            "tags": ["phase", "venture.discovery"],
            "runnable": False,
        },
    )
    assert refill_unused_work(vid) == []


def test_refill_mints_product_not_generic_discovery(client: TestClient):
    vid = _venture(client)
    from app.planning.cycle import refill_unused_work
    from app.planning.execute import is_product_work

    filled = refill_unused_work(vid)
    assert filled, "expected unused product playbook items"
    for row in filled:
        assert is_product_work(row["title"], row.get("tags") or []), row["title"]
        blob = row["title"].lower()
        assert "alternatives" not in blob
        assert "compare" not in blob
        assert "demand probe" not in blob
