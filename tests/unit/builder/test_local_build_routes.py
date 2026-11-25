import json
import sys
from types import ModuleType, SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from tests.company_os_harness import load_company_os_app


@pytest.fixture(params=["Inventory inspections for factories", "Family trail planning"])
def route(request, monkeypatch, tmp_path):
    load_company_os_app()
    from app.builder import ctl, execution
    from app.build import device_orders
    from app.core import local_owner
    from app.db.models import Base
    from app.db.supymem_models import BuildRun, FounderApp
    from app.ventures import lifecycle

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    engine = create_engine(f"sqlite:///{tmp_path}/builds.db")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(execution, "db", lambda: factory)
    app = FounderApp(id="app", workspace_id="workspace", venture_id="venture", slug="project", name=request.param,
                     kind="service", state="draft", created_at=execution.naive_utc(), updated_at=execution.naive_utc())
    with factory() as session:
        session.add(app)
        session.commit()
    accesses, orders = [], []

    def authorize(session, app_id, actor, mutate=False):
        assert app_id == app.id and actor == "owner"
        accesses.append(mutate)
        return app

    monkeypatch.setattr(execution, "_app", authorize)
    monkeypatch.setattr(local_owner, "current_owner", lambda: SimpleNamespace(id="owner", email=""))
    monkeypatch.setattr(ctl, "local_tool", lambda *args, **kwargs: "codex")
    monkeypatch.setattr(lifecycle, "is_paused", lambda *args: False)
    inputs = {"plan_id": "plan", "plan_task_id": "task", "milestone": {"kind": "core"},
              "task": {"title": request.param, "description": "Implement only the approved task."},
              "product_context": {"idea": request.param}, "source": {"kind": "empty", "sha256": "a" * 64}}
    monkeypatch.setattr(execution, "_inputs", lambda *args, **kwargs: inputs)

    def prepare(venture_id, caller, instruction, **kwargs):
        assert venture_id == app.venture_id and caller.sub == "owner"
        order = {"id": f"order_{len(orders)}", "venture_id": venture_id, "state": "pending", "review_digest": "review",
                 "review_path": "/builds", "manifest": {"instruction": instruction}, "task_snapshot": {}}
        orders.append(order)
        return dict(order)

    def transition(venture_id, caller, order_id, review_digest, decision):
        order = next(item for item in orders if item["id"] == order_id)
        order["state"] = "approved" if decision == "approve" else "cancelled"
        return dict(order)

    def product(*args, **kwargs):
        order = prepare(*args, **kwargs)
        orders[-1]["tool_kind"] = "product"
        return order

    def coding(*args, **kwargs):
        order = prepare(*args, **kwargs)
        orders[-1]["tool_kind"] = "coding"
        return order

    monkeypatch.setattr(device_orders, "prepare_product_build_from_chat", product)
    monkeypatch.setattr(device_orders, "prepare_coding_task_from_chat", coding)
    monkeypatch.setattr(device_orders, "transition", transition)
    monkeypatch.setattr(device_orders, "retrieve", lambda venture_id, caller, order_id: dict(next(
        item for item in orders if item["id"] == order_id)))

    def internal(*args, **kwargs):
        pytest.fail("An internal builder or hosted runtime must not run locally")

    monkeypatch.setattr(execution.budgets, "runtime_quote", internal)
    monkeypatch.setattr(ctl, "run_build", internal)
    monkeypatch.setattr(ctl, "watch_build", internal)
    from app.builder import orchestration
    monkeypatch.setattr(orchestration, "run", internal)
    monkeypatch.setattr(orchestration, "watch", internal)
    yield SimpleNamespace(execution=execution, factory=factory, inputs=inputs, app=app, orders=orders,
                          accesses=accesses, domain=request.param, internal=internal, BuildRun=BuildRun)
    engine.dispose()


@pytest.mark.parametrize("domain", ["maple-ledger.test", "winter-picnic.test"])
@pytest.mark.parametrize("profile,state", [("static_landing_v1", "stopped"), ("web_app_vercel_v1", "stopped"), ("web_app_vercel_v1", "failed")])
def test_build_activity_retries_only_the_bound_request(monkeypatch, domain, profile, state):
    import hashlib
    load_company_os_app()
    from app.api import device_orders_api
    from app.build import device_orders
    from app.pagebuilder import api

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    caller = SimpleNamespace(sub="owner", device_id="device")
    old = {"id": "dwo_original", "venture_id": "venture", "workspace_id": "workspace", "actor_id": "owner",
        "expires_at": "2099-01-01", "device_id": "device", "state": state,
        "claimed_at": "started" if state == "stopped" else None, "native_stop": {"processes_stopped": True},
        "failure_code": "coding_sign_in_required" if state == "failed" else None,
        "task_snapshot": {"pagebuilder_job_id": "page", "requirements": domain},
        "requested_build": {"request_sha256": hashlib.sha256(domain.encode()).hexdigest()},
        "manifest": {"instruction": f"Keep the original requirements for {domain}",
            "coding_loop_verification": {"profile": profile, "phase": "implement", "provider": "codex", "plan_order_id": "plan"}}}
    old["review_digest"] = device_orders._digest(old)
    monkeypatch.setattr(device_orders_api, "verify_caller", lambda *args, **kwargs: caller)
    monkeypatch.setattr(device_orders, "retrieve", lambda *_: old)
    monkeypatch.setattr(device_orders, "prepare_product_build_from_chat", lambda *args, **kwargs: pytest.fail("Generic product plan"))
    monkeypatch.setattr(device_orders.Manifest, "model_validate", lambda value: value)
    seen = []

    def prepare(venture, actor, manifest, **kwargs):
        seen.append(manifest)
        assert kwargs["requested_build"] == old["requested_build"]
        assert kwargs["retry_of"] == old["id"]
        return {"id": "replacement", "state": "approved"}

    def retry_page(venture, actor, **kwargs):
        seen.append(kwargs)
        return {"job_id": "replacement_page", "order_id": "replacement"}

    monkeypatch.setattr(device_orders, "prepare", prepare)
    monkeypatch.setattr(api, "_start", retry_page)
    result = device_orders_api.renew("venture", "dwo_original", None)
    if profile == "static_landing_v1":
        assert seen == [{"new_version_of": "page", "retry_job_id": "page"}]
        assert result["review_path"] == "/v/venture/device-work-orders/replacement"
    else:
        assert seen == [old["manifest"]]
        assert result["id"] == "replacement" and result["state"] == "approved"


def test_local_history_does_not_import_hosted_diagnostic(route, monkeypatch):
    monkeypatch.setitem(sys.modules, "app.build.subscription_product_diagnostic_admission", None)
    with route.factory() as session:
        session.add(route.BuildRun(
            id="old-run", app_id=route.app.id, workspace_id=route.app.workspace_id,
            venture_id=route.app.venture_id, agent="diagnostic", state="failed", runtime_version=1,
            trigger="subscription_product_diagnostic", closed_at=route.execution.naive_utc(),
            error_code="diagnostic_failed", spec=json.dumps({"diagnostic": {"phase": "source_check"}}),
        ))
        session.commit()

    detail = route.execution.read("old-run", actor="owner")
    assert route.execution.listing(route.app.id, actor="owner") == [detail]
    assert (detail["build_run_id"], detail["state"], detail["phase"]) == ("old-run", "failed", "source_check")
    assert detail["closed"] and not detail["verified"] and not detail["hosting_authorized"]
    assert detail["error_code"] == "diagnostic_failed"
    assert not any("credit" in key or "ceiling" in key for key in detail)
    with route.factory() as session:
        session.get(route.BuildRun, "old-run").spec = "not json"
        session.commit()
    assert route.execution.read("old-run", actor="owner")["phase"] == "failed"


def disconnect(monkeypatch):
    from app.builder import ctl

    def missing(*args, **kwargs):
        raise HTTPException(409, detail={"code": "build_tool_needs_setup", "message": "Open Connections > Coding tools."})

    monkeypatch.setattr(ctl, "local_tool", missing)


@pytest.mark.parametrize("connected", [True, False])
@pytest.mark.parametrize("revision", [True, False])
def test_local_quote_uses_tool_readiness_not_managed_runtime(route, monkeypatch, connected, revision):
    from app.hosting import revisions
    if revision:
        route.inputs["source"]["id"] = "source"
    monkeypatch.setattr(revisions, "build_inputs", lambda *args, **kwargs: route.inputs)
    if not connected:
        disconnect(monkeypatch)
    options = {"actor": "owner", "revision_request_id": "revision"} if revision else {
        "actor": "owner", "plan_id": "plan", "plan_task_id": "task"}
    quote = (route.execution.estimate_revision if revision else route.execution.estimate)("app", **options)
    assert quote["ready"] is connected
    assert not route.orders
    assert not any("micro" in name for name in quote)
    if not connected:
        assert "Settings > Keys and connections > Coding tools" in quote["next_step"]


@pytest.mark.parametrize("connected", [True, False])
@pytest.mark.parametrize("revision", [True, False])
def test_local_quoted_start_routes_brief_and_replays_order(route, monkeypatch, connected, revision):
    from app.db.supymem_models import FounderRequest
    from app.hosting import revisions
    if revision:
        route.inputs["revision"] = {"request_id": "revision"}
        route.inputs["product_context"]["revision_instruction"] = "Fix the submitted form without changing its scope."
        monkeypatch.setattr(revisions, "build_inputs", lambda *args, **kwargs: route.inputs)
    quote_digest = route.execution.digest({"inputs": route.inputs, "mode": "connected_tool"})
    with route.factory() as session:
        session.add(FounderRequest(id="quote", workspace_id="workspace", venture_id="venture", source="build_quote",
            related_id="app", outcome="pending", created_at=route.execution.naive_utc(), text=json.dumps({
                "actor": "owner", "inputs": route.inputs, "source_artifact_id": None,
                "revision_request_id": "revision" if revision else None,
                "quote": {"ready": True, "input_digest": quote_digest, "mode": "connected_tool"}})))
        session.commit()
    if not connected:
        disconnect(monkeypatch)
    options = {"actor": "owner", "quote_id": "quote", "input_digest": quote_digest, "idempotency_key": "request"}
    if connected:
        result = route.execution.start("app", **options)
        assert result["device_work_order_id"] == route.orders[0]["id"]
        assert route.domain in route.orders[0]["manifest"]["instruction"]
        assert route.execution.start("app", **options) == result
        assert len(route.orders) == 1
        with pytest.raises(route.execution.BuildError, match="build_idempotency_conflict"):
            route.execution.start("app", **{**options, "input_digest": "b" * 64})
    else:
        with pytest.raises(HTTPException) as failure:
            route.execution.start("app", **options)
        assert "Settings > Keys and connections > Coding tools" in failure.value.detail["message"]
        assert not route.orders


@pytest.mark.parametrize("connected", [True, False])
def test_apps_start_preserves_quote_and_caller(route, monkeypatch, connected):
    from app.api import apps_api
    monkeypatch.setattr(apps_api, "_app_or_404", lambda app_id: route.app)
    monkeypatch.setattr(apps_api, "assert_venture_access", lambda *args: None)
    monkeypatch.setattr(apps_api, "subject_person_id", lambda subject: subject)
    if not connected:
        disconnect(monkeypatch)
    if connected:
        result = apps_api.start_build("app", apps_api.StartBuildBody(instruction=route.domain), "request", "owner")
        assert result["state"] == "approved" and len(route.orders) == 1
        assert route.domain in route.orders[0]["manifest"]["instruction"]
        assert apps_api.start_build("app", apps_api.StartBuildBody(instruction=route.domain), "request", "owner") == result
        assert len(route.orders) == 1
    else:
        with pytest.raises(HTTPException) as failure:
            apps_api.start_build("app", apps_api.StartBuildBody(instruction=route.domain), "request", "owner")
        assert "Settings > Keys and connections > Coding tools" in failure.value.detail["message"]
        assert not route.orders


@pytest.mark.parametrize("connected", [True, False])
@pytest.mark.parametrize("managed", [True, False])
def test_queued_build_routes_once_and_watch_never_rebuilds(route, monkeypatch, connected, managed):
    from app.core.jobs import FatalError, LOCAL_MANAGED_BUILD_CODE
    from app.worker import handlers
    with route.factory() as session:
        session.add(route.BuildRun(id="build", app_id="app", workspace_id="workspace", venture_id="venture",
            plan_task_id="task", trigger="founder_approved", agent="openhands", state="queued", runtime_version=1 if managed else 0,
            spec=json.dumps({"actor": "owner", "inputs": route.inputs}), created_at=route.execution.naive_utc()))
        session.commit()
    payload = {"build_run_id": "build", "runtime_version": 1 if managed else 0}
    if managed:
        with pytest.raises(FatalError, match=LOCAL_MANAGED_BUILD_CODE):
            handlers._build_run(payload)
        assert not route.orders
        return
    if not connected:
        disconnect(monkeypatch)
        with pytest.raises(HTTPException) as failure:
            handlers._build_run(payload)
        assert "Settings > Keys and connections > Coding tools" in failure.value.detail["message"]
        assert not route.orders
        return
    handlers._build_run(payload)
    handlers._build_run(payload)
    handlers._build_watch(payload)
    assert len(route.orders) == 1 and route.domain in route.orders[0]["manifest"]["instruction"]
    with route.factory() as session:
        run = session.get(route.BuildRun, "build")
        assert json.loads(run.spec)["device_work_order_id"] == route.orders[0]["id"]
        assert run.state != "succeeded"


@pytest.mark.parametrize("connected", [True, False])
def test_legacy_deploy_admits_page_tool_order_never_generates(route, monkeypatch, connected):
    from app.worker import handlers
    from app.pagebuilder import api
    from app.landing import service
    monkeypatch.setattr(service, "generate_and_publish", route.internal)
    monkeypatch.setattr(api, "start", lambda venture_id, caller: {"job_id": "page", "brief": route.domain})
    if not connected:
        disconnect(monkeypatch)
        with pytest.raises(HTTPException) as failure:
            handlers._deploy_run({"venture_id": "venture", "workspace_id": "workspace", "_actor_subject": "owner"})
        assert "Settings > Keys and connections > Coding tools" in failure.value.detail["message"]
        return
    result = handlers._deploy_run({"venture_id": "venture", "workspace_id": "workspace", "_actor_subject": "owner"})
    assert result == {"job_id": "page", "brief": route.domain}


@pytest.mark.parametrize("connected", [True, False])
def test_page_worker_uses_existing_tool_job_not_internal_generator(route, monkeypatch, connected):
    from app.worker import handlers
    from app.pagebuilder import jobs, runtime
    from app.core.jobs import FatalError
    failures = []
    monkeypatch.setattr(runtime, "fail", lambda *args: failures.append(args))
    module = ModuleType("app.pagebuilder.tool")
    calls = []

    def run_job(payload):
        if not connected:
            raise HTTPException(409, detail={"message": "Open Settings > Keys and connections > Coding tools, connect your tool, then retry."})
        calls.append(payload)
        return {"device_work_order_id": "page_order", "brief": route.domain}

    module.run_job = run_job
    monkeypatch.setitem(sys.modules, module.__name__, module)
    payload = {"_job_id": "page"}
    if connected:
        assert handlers._pagebuilder_build(payload)["device_work_order_id"] == "page_order"
        assert calls == [payload]
    else:
        with pytest.raises(FatalError) as failure:
            handlers._pagebuilder_build(payload)
        assert "Settings > Keys and connections > Coding tools" in str(failure.value)
        assert not calls
        assert len(failures) == 1


@pytest.mark.parametrize("case", ["scope_changed", "cancelled", "already_started", "unrequested"])
def test_queued_build_keeps_scope_cancellation_and_request_guards(route, monkeypatch, case):
    from app.core.jobs import FatalError
    from app.worker import handlers
    spec = {"actor": "owner", "inputs": route.inputs}
    if case == "cancelled":
        spec["cancel_requested"] = True
    with route.factory() as session:
        session.add(route.BuildRun(id="build", app_id="app", workspace_id="workspace", venture_id="venture",
            trigger="plan_approved" if case == "unrequested" else "founder_approved", agent="openhands", state="queued",
            runtime_version=0, sandbox_ref="previous_runtime" if case == "already_started" else None,
            spec=json.dumps(spec), created_at=route.execution.naive_utc()))
        session.commit()
    if case == "scope_changed":
        monkeypatch.setattr(route.execution, "_inputs", lambda *args, **kwargs: {**route.inputs, "plan_version": 2})
        with pytest.raises(route.execution.BuildError, match="build_quote_inputs_changed"):
            handlers._build_run({"build_run_id": "build"})
    elif case in {"already_started", "unrequested"}:
        with pytest.raises(FatalError):
            handlers._build_run({"build_run_id": "build"})
    else:
        handlers._build_run({"build_run_id": "build"})
        with route.factory() as session:
            assert session.get(route.BuildRun, "build").state == "cancelled"
    assert not route.orders


def test_local_cancel_uses_native_stop_without_claiming_completion(route, monkeypatch):
    from app.build import device_orders
    result = route.execution.request_local("app", actor="owner", instruction=route.domain, idempotency_key="request")
    stops = []

    def stop(venture_id, caller, order_id):
        stops.append(order_id)
        return {"state": "stopping", "native_stop": {"processes_stopped": False}}

    monkeypatch.setattr(device_orders, "emergency_stop", stop)
    cancelled = route.execution.cancel(result["build_run_id"], actor="owner")
    assert cancelled["state"] == "stopping" and not cancelled["native_stop"]["processes_stopped"]
    assert stops == [result["device_work_order_id"]]
    with route.factory() as session:
        assert session.get(route.BuildRun, result["build_run_id"]).closed_at is None


def test_local_watch_reads_the_connected_tool_check_result(route, monkeypatch):
    import boto3

    from app.worker import handlers

    monkeypatch.setattr(boto3, "client", route.internal)
    result = route.execution.request_local("app", actor="owner", instruction=route.domain)
    receipts = [{"index": 0, "phase": "code", "outcome": "pass"}]
    route.orders[0]["iterations"] = receipts
    observed = handlers._build_watch({"build_run_id": result["build_run_id"]})
    assert observed["iterations"] == receipts and len(route.orders) == 1


@pytest.mark.parametrize("invalid", ["secret", "empty"])
def test_local_request_validates_before_persisting_plaintext(route, monkeypatch, invalid):
    from app.secrets import detect
    if invalid == "secret":
        monkeypatch.setattr(detect, "looks_like_secret", lambda instruction: True)
    with pytest.raises(HTTPException):
        route.execution.request_local("app", actor="owner", instruction=route.domain if invalid == "secret" else " ")
    with route.factory() as session:
        assert not session.query(route.BuildRun).all()
    assert not route.orders


@pytest.mark.parametrize("fields", [{"quote_id": "quote"}, {"input_digest": "a" * 64},
    {"quote_id": "quote", "input_digest": "a" * 64, "instruction": "Change the saved scope."}])
def test_local_api_never_silently_changes_a_quoted_request(route, fields):
    from app.api import apps_api
    with pytest.raises(ValueError):
        apps_api.StartBuildBody(**fields)
    assert not route.orders


@pytest.mark.parametrize("kind", ["core", "landing"])
def test_selected_task_keeps_its_scope_and_correct_tool_path(route, kind):
    from app.db.supymem_models import PlanMilestone, PlanTask
    route.inputs["milestone"]["kind"] = kind
    with route.factory() as session:
        session.add(PlanMilestone(id="milestone", plan_id="plan", workspace_id="workspace", venture_id="venture",
            order=0, title=route.domain, kind=kind))
        session.add(PlanTask(id="task", milestone_id="milestone", workspace_id="workspace", venture_id="venture",
            order=0, title=route.domain, created_at=route.execution.naive_utc()))
        session.commit()
    route.execution.request_local("app", actor="owner", instruction="Use only the requested scope.", plan_task_id="task")
    assert route.orders[0]["tool_kind"] == ("product" if kind == "core" else "coding")
    assert route.domain in route.orders[0]["manifest"]["instruction"]
    assert "Use only the requested scope." in route.orders[0]["manifest"]["instruction"]


def test_unknown_selected_task_starts_no_tool(route):
    with pytest.raises(KeyError):
        route.execution.request_local("app", actor="owner", instruction=route.domain, plan_task_id="foreign_task")
    assert not route.orders


def test_core_repair_routes_verified_source_and_rejection_to_coding_tool(route):
    route.inputs["source"] = {"kind": "artifact", "id": "prior-source", "sha256": "a" * 64, "size_bytes": 100}
    route.inputs["product_context"]["prior_founder_review"] = {
        "build_run_id": "prior", "source_artifact_id": "prior-source", "feedback": "Keep the approved workflow intact."}
    with route.factory() as session:
        session.add(route.BuildRun(id="build", app_id="app", workspace_id="workspace", venture_id="venture",
            plan_task_id="task", trigger="founder_approved", agent="connected_tool", state="queued", runtime_version=0,
            spec=json.dumps({"actor": "owner", "inputs": route.inputs}), created_at=route.execution.naive_utc()))
        session.commit()
    route.execution.run_local({"build_run_id": "build"})
    route.execution.run_local({"build_run_id": "build"})
    assert len(route.orders) == 1 and route.orders[0]["tool_kind"] == "coding"
    brief = route.orders[0]["manifest"]["instruction"]
    assert route.domain in brief and "Plan task: task" in brief
    assert "prior-source" in brief and "a" * 64 in brief
    assert "Keep the approved workflow intact." in brief
    with route.factory() as session:
        assert session.get(route.BuildRun, "build").state != "succeeded"
