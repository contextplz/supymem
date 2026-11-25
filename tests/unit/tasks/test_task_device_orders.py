"""Synthetic saved orders use real task projection/leases; no native effects."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from tests.company_os_harness import load_company_os_app

NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
SCOPE = {"workspace_id": "ws_adapter", "venture_id": "ven_adapter", "actor": "usr_adapter"}


def synthetic_model_limits(manifest):
    """Published S06 metadata ABI only; no live configuration/provider claim."""
    roles = []
    desktop, browser = manifest.get("desktop_verification"), manifest.get("general_browser_verification") or {}
    if desktop:
        kinds = ["plan", "step", "verify"] + (["vision"] if desktop.get("visual_review") is True else [])
        roles = [{"purpose": "device.ai_step." + kind, "provider": "synthetic_provider", "model": "synthetic_model",
            "catalog_model": "synthetic_catalog", "api_mode": "responses", "endpoint_host": "synthetic.example.test",
            "max_output_tokens": 1500 if kind == "vision" else 2500} for kind in kinds]
    elif browser.get("execution_profile"):
        for phase, role in browser["execution_profile"]["roles"].items():
            roles.append({"purpose": "device.browser_ai." + phase, "provider": role["provider"], "model": role["model"],
                "catalog_model": role["model"], "api_mode": "responses", "endpoint_host": "api.openai.com",
                "max_output_tokens": role["max_output_tokens"]})
    content = {"input_tokens": 200_000 if roles else 0,
        "max_output_tokens": max((role["max_output_tokens"] for role in roles), default=0), "roles": roles}
    return {**content, "route_digest": hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}


@pytest.fixture()
def orders(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", "sqlite:///:memory:")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.build import device_ai_step, device_orders as native
    from app.db.models import Base, Job, Person, Venture, VentureDocument, Workspace
    from app.db.supymem_models import DesktopDevice, Notice
    from app.db.task_models import Task, TaskEvent, TaskRun, TaskStep
    from app.tasks import device, device_orders
    from app.tenancy import bind
    from core.clock import set_clock

    clock = [NOW]
    set_clock(lambda: clock[0])
    monkeypatch.setattr(device_ai_step, "native_model_limits", synthetic_model_limits, raising=False)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[model.__table__ for model in (
        Person, Workspace, Venture, VentureDocument, DesktopDevice, Job, Notice, Task, TaskRun, TaskStep, TaskEvent)])
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        moment = NOW.replace(tzinfo=None)
        session.add(Person(id="usr_adapter", display_name="Synthetic founder", created_at=moment))
        session.add(Workspace(id="ws_adapter", owner_id="usr_adapter", kind="venture", name="Synthetic",
            entry_mode="idea", created_at=moment, updated_at=moment))
        session.add(Venture(id="ven_adapter", workspace_id="ws_adapter", name="Synthetic",
            raw_idea="Synthetic", stage="draft", created_at=moment, updated_at=moment))
        session.add(DesktopDevice(id="dev_adapter", person_id="usr_adapter", name="Synthetic Mac", platform="macos"))
        session.flush()
        manifest = {"title": "Inspect the synthetic workspace", "task_id": "founder_mac_ven_adapter",
            "instruction": "Inspect the synthetic workspace and report its name.",
            "actions": ["desktop.read", "desktop.interact"], "credit_ceiling_micro": 800_000, "duration_seconds": 900,
            "desktop_verification": {"synthetic": True, "max_steps": 48}}
        body = {"id": "dwo_adapter", **SCOPE, "actor_id": SCOPE["actor"], "device_id": "dev_adapter",
            "manifest": manifest, "state": "claimed", "execution_id": "dex_synthetic_adapter",
            "claimed_at": (NOW - timedelta(minutes=1)).isoformat(), "expires_at": (NOW + timedelta(hours=1)).isoformat(),
            "task_snapshot": native._review_task_snapshot(session, "ws_adapter", "ven_adapter", manifest,
                actor="usr_adapter", device="dev_adapter")}
        body.pop("actor")
        body["review_digest"] = native._digest(body)
        session.add(VentureDocument(id=body["id"], venture_id="ven_adapter", kind=native.KIND,
            title="Synthetic reviewed order", provenance_json=json.dumps(body), created_at=moment, updated_at=moment))
        session.commit()
    yield SimpleNamespace(factory=factory, adapter=device_orders, native=native, device=device, bind=bind, clock=clock)
    set_clock(None)
    engine.dispose()


def body(d, session):
    from app.db.models import VentureDocument
    return json.loads(session.get(VentureDocument, "dwo_adapter").provenance_json)


def write_body(session, value):
    from app.db.models import VentureDocument
    session.get(VentureDocument, "dwo_adapter").provenance_json = json.dumps(value)
    session.flush()  # Canonical native claim is saved before the adapter reloads it.


def typed(d, *, state="working", step_state="submitted", blocker=None, evidence=None, bound=False, unused=False):
    from app.db.models import Job
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.tasks.contracts import Budget
    with d.factory() as session:
        value = body(d, session)
        started = NOW.replace(tzinfo=None) - timedelta(minutes=4)
        due = started + timedelta(minutes=25)
        session.add(Job(id="job_adapter", kind="tasks.execute", workspace_id="ws_adapter", venture_id="ven_adapter",
            payload_json="{}", status="completed", created_at=started))
        session.add(Task(id="tsk_adapter", workspace_id="ws_adapter", venture_id="ven_adapter", owner_person_id="usr_adapter",
            title="Existing typed task", goal="Existing typed task", budget_json=Budget().model_dump_json(), state=state,
            blocker_json=json.dumps(blocker) if blocker else None, deadline_at=due, created_at=started, updated_at=started))
        session.add(TaskRun(id="trn_adapter", task_id="tsk_adapter", run_key="initial", state=state,
            started_at=started, deadline_at=due, control_job_id="job_adapter", cost_micro=91,
            tokens_used=200, attempts=2, tool_calls=3, allowance_json='{"synthetic":"unchanged"}'))
        inputs = {"device_order_id": value["id"]}
        if bound:
            inputs.update(review_digest=value["review_digest"], execution_id=value["execution_id"])
        session.add(TaskStep(id="tst_adapter", run_id="trn_adapter", position=1, title="Existing Mac step",
            skill="device.work_order", runs_on="device", state=step_state, inputs_json=json.dumps(inputs),
            evidence_json=json.dumps(evidence or {}), deadline_at=due, cost_micro=0 if unused else 91))
        session.commit()
    return {"task_id": "tsk_adapter", "run_id": "trn_adapter", "step_id": "tst_adapter"}


def count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def test_legacy_projection_is_single_durable_binding_and_uses_real_fenced_lease(orders):
    from app.db.models import Job
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.tasks.contracts import Budget
    d = orders
    with d.factory() as session, d.bind(**SCOPE):
        value = body(d, session)
        assert d.adapter.resolve_order_step(session, value) is None
        # Real native admission may already have written the canonical order.
        write_body(session, value)
        binding = d.adapter.ensure_order_step(session, value)
        task, run, step = (session.get(model, binding[key]) for model, key in
            ((Task, "task_id"), (TaskRun, "run_id"), (TaskStep, "step_id")))
        assert task.create_key == "device_order:dwo_adapter"
        assert (task.title, task.goal, step.title) == (value["manifest"]["title"], value["manifest"]["instruction"], value["manifest"]["title"])
        assert Budget.model_validate_json(task.budget_json) == Budget()
        from app.tasks.budgets import effective_limits
        limits = effective_limits(task, run)
        assert (limits.cost_micro, limits.tokens, limits.tool_calls) == (800_000, 400 * 202_500, 48)
        assert limits.attempts == 1
        assert limits.tokens > Budget.model_json_schema()["properties"]["tokens"]["maximum"]
        assert limits.wall_until == (NOW + timedelta(minutes=14)).replace(tzinfo=None)
        assert json.loads(task.scope_json)["_native_legacy"]["model_limits"] == synthetic_model_limits(value["manifest"])
        from app.tasks.budgets import is_legacy_profile
        assert is_legacy_profile(task, run)
        assert run.run_key == value["execution_id"]
        assert run.started_at == (NOW - timedelta(minutes=1)).replace(tzinfo=None)
        assert run.deadline_at == task.deadline_at == step.deadline_at
        assert run.control_job_id is None and count(session, Job) == 0
        assert (run.cost_micro, run.tokens_used, run.attempts, run.tool_calls) == (0, 0, 0, 0)
        assert step.position == 0 and step.lease_token is None and step.lease_version == 0
        assert json.loads(step.inputs_json) == {"device_order_id": value["id"],
            "review_digest": value["review_digest"], "execution_id": value["execution_id"]}
        value.update(binding)
        write_body(session, value)
        session.commit()
    with d.factory() as session, d.bind(**SCOPE):
        value = body(d, session)
        assert d.adapter.resolve_order_step(session, value) == binding
        assert d.adapter.ensure_order_step(session, value) == binding
        assert [count(session, model) for model in (Task, TaskRun, TaskStep, Job)] == [1, 1, 1, 0]
        lease = d.device.claim_step(session, binding["task_id"], binding["step_id"], "dev_adapter")
        assert lease["accepted"] and lease["heartbeat_seconds"] == 15
        assert session.get(TaskStep, binding["step_id"]).lease_token != lease["lease_token"]
        from app.tasks import service, usage
        assert service.guard(session, session.get(Task, binding["task_id"]), session.get(TaskRun, binding["run_id"]), tokens=202_500)
        reserved = usage.reserve_native_call(session, task_id=binding["task_id"], run_id=binding["run_id"],
            step_id=binding["step_id"], order_id=value["id"], execution_id=value["execution_id"],
            review_digest=value["review_digest"], invocation_id="synthetic_profile_call", request_digest="b" * 64,
            estimate_micro=10, max_tokens=202_500)
        assert reserved["admitted_now"]
        assert session.get(TaskRun, binding["run_id"]).reserved_tokens == 202_500
        session.commit()


@pytest.mark.parametrize("source", ["founder_goal", "instruction", "long_instruction_short_founder_goal"])
def test_legacy_projection_preserves_exact_reviewed_title_and_actual_founder_goal(orders, source):
    from app.db.task_models import Task, TaskStep
    d = orders
    with d.factory() as session, d.bind(**SCOPE):
        value = body(d, session)
        value["manifest"]["title"] = "Read my project status exactly"
        instruction = "Open my project, read its status, and report the original wording."
        goal = "Read the project status and report its original wording."
        value["manifest"]["instruction"] = "x" * 9000 if source == "long_instruction_short_founder_goal" else instruction
        if source != "instruction":
            value["manifest"]["desktop_verification"]["founder_goal"] = goal
        value["review_digest"] = d.native._digest(value)
        write_body(session, value)
        original = body(d, session)
        binding = d.adapter.ensure_order_step(session, value)
        task, step = session.get(Task, binding["task_id"]), session.get(TaskStep, binding["step_id"])
        assert task.title == step.title == value["manifest"]["title"]
        assert task.goal == (instruction if source == "instruction" else goal)
        assert body(d, session) == original


def test_oversized_actual_native_goal_is_refused_without_task_or_native_body_changes(orders, monkeypatch):
    from app.build import device_ai_step
    from app.db.task_models import Task, TaskRun, TaskStep
    d = orders
    with d.factory() as session, d.bind(**SCOPE):
        value = body(d, session)
        value["manifest"]["instruction"] = "x" * 8001
        value["review_digest"] = d.native._digest(value)
        write_body(session, value)
        original = body(d, session)
        monkeypatch.setattr(device_ai_step, "native_model_limits", lambda *args: pytest.fail("Goal refusal must precede cap capture"))
        with pytest.raises(ValueError, match="reviewed goal is too long") as error:
            d.adapter.ensure_order_step(session, value)
        assert len(str(error.value)) < 150 and value["manifest"]["instruction"] not in str(error.value)
        assert body(d, session) == original
        assert [count(session, model) for model in (Task, TaskRun, TaskStep)] == [0, 0, 0]


def test_same_execution_reclaims_many_lease_generations_without_new_attempt_or_wall_extension(orders):
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.tasks import budgets, usage
    d = orders
    with d.factory() as session, d.bind(**SCOPE):
        value = body(d, session)
        binding = d.adapter.ensure_order_step(session, value)
        lease = d.device.claim_step(session, binding["task_id"], binding["step_id"], value["device_id"])
        assert lease["accepted"]
        original_invocation = {**binding, "order_id": value["id"], "execution_id": value["execution_id"],
            "review_digest": value["review_digest"], "invocation_id": "synthetic_known_call", "request_digest": "d" * 64}
        usage.reserve_native_call(session, **original_invocation, estimate_micro=20, max_tokens=100)
        assert usage.settle_native_call(session, **original_invocation, actual_micro=7,
            prompt_tokens=4, completion_tokens=6, cached_tokens=2)
        task, run, step = session.get(Task, binding["task_id"]), session.get(TaskRun, binding["run_id"]), session.get(TaskStep, binding["step_id"])
        run.tool_calls = 5  # Synthetic already-counted effects; no native effect is executed.
        original = (run.run_key, run.started_at, run.deadline_at, budgets.wall_deadline(task, run),
            run.cost_micro, run.tokens_used, run.tool_calls, step.started_at)
        assert original[4:7] == (7, 10, 5) and run.attempts == 1
        duplicate = d.device.claim_step(session, task.id, step.id, value["device_id"])
        assert not duplicate["accepted"] and duplicate["reason"] == "lease_active"
        assert run.attempts == 1
        session.commit()
    versions = [lease["lease_version"]]
    for _ in range(13):
        prior = lease
        d.clock[0] += timedelta(seconds=61)  # Both admission moment and server guard clock advance.
        with d.factory() as session, d.bind(**SCOPE):
            lease = d.device.claim_step(session, binding["task_id"], binding["step_id"], "dev_adapter")
            assert lease["accepted"] and lease["lease_version"] > prior["lease_version"]
            versions.append(lease["lease_version"])
            with pytest.raises(PermissionError, match="fence_invalid"):
                d.device.heartbeat_step(session, binding["task_id"], binding["step_id"], "dev_adapter",
                    prior["lease_token"], prior["lease_version"])
            task, run, step = session.get(Task, binding["task_id"]), session.get(TaskRun, binding["run_id"]), session.get(TaskStep, binding["step_id"])
            assert run.attempts == 1
            assert (run.run_key, run.started_at, run.deadline_at, budgets.wall_deadline(task, run),
                run.cost_micro, run.tokens_used, run.tool_calls, step.started_at) == original
            session.commit()
    assert len(versions) == len(set(versions)) == 14
    d.clock[0] = original[3].replace(tzinfo=timezone.utc) + timedelta(seconds=1)
    with d.factory() as session, d.bind(**SCOPE):
        stopped = d.device.heartbeat_step(session, binding["task_id"], binding["step_id"], "dev_adapter",
            lease["lease_token"], lease["lease_version"])
        assert not stopped["accepted"] and stopped["reason"] == "budget_exhausted"
        task, run, step = session.get(Task, binding["task_id"]), session.get(TaskRun, binding["run_id"]), session.get(TaskStep, binding["step_id"])
        assert (run.attempts, run.cost_micro, run.tokens_used, run.tool_calls) == (1, 7, 10, 5)
        assert budgets.wall_deadline(task, run) == original[3] and step.lease_token is None
        assert json.loads(task.blocker_json)["class"] == "budget_exhausted"
        assert not d.device.claim_step(session, task.id, step.id, "dev_adapter")["accepted"]


@pytest.mark.parametrize("change", ["forged_profile", "changed_execution"])
def test_invalid_legacy_execution_cannot_receive_attempt_exception_or_lease(orders, change):
    from app.db.task_models import Task, TaskRun, TaskStep
    d = orders
    with d.factory() as session, d.bind(**SCOPE):
        binding = d.adapter.ensure_order_step(session, body(d, session))
        task, run, step = session.get(Task, binding["task_id"]), session.get(TaskRun, binding["run_id"]), session.get(TaskStep, binding["step_id"])
        if change == "forged_profile":
            scope = json.loads(task.scope_json)
            scope["_native_legacy"]["limits"]["provider_calls"] += 1
            task.scope_json = json.dumps(scope)
        else:
            run.run_key = "dex_changed_execution"
        with pytest.raises(ValueError, match="original binding"):
            d.device.claim_step(session, task.id, step.id, "dev_adapter")
        assert run.attempts == 0 and step.lease_token is None and step.lease_version == 0


@pytest.mark.parametrize("attempts", [2, True])
def test_legacy_attempt_profile_rejects_nonlogical_counter_without_reset(orders, attempts):
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.tasks import budgets
    from app.tasks.contracts import Budget
    d = orders
    with d.factory() as session, d.bind(**SCOPE):
        binding = d.adapter.ensure_order_step(session, body(d, session))
        task, run, step = session.get(Task, binding["task_id"]), session.get(TaskRun, binding["run_id"]), session.get(TaskStep, binding["step_id"])
        task.budget_json = Budget(attempts=100).model_dump_json()
        assert budgets.effective_limits(task, run).attempts == 1
        run.attempts = attempts
        with pytest.raises(ValueError, match="original binding"):
            budgets.is_legacy_profile(task, run)
        with pytest.raises(ValueError, match="original binding"):
            d.device.claim_step(session, task.id, step.id, "dev_adapter")
        assert run.attempts is attempts and step.lease_token is None and step.lease_version == 0


def test_client_legacy_flag_does_not_change_existing_typed_attempt_budget(orders):
    from app.tasks import budgets
    from app.tasks.locks import lock_run
    d = orders
    binding = typed(d, bound=True)
    with d.factory() as session, d.bind(**SCOPE):
        value = body(d, session)
        value["legacy_profile"] = True  # Ignored client-shaped data is not a server Task profile.
        write_body(session, value)
        task, run = lock_run(session, binding["run_id"])
        run.allowance_json = None
        assert d.adapter.ensure_order_step(session, value) == binding
        assert budgets.is_legacy_profile(task, run) is False and budgets.effective_limits(task, run).attempts == 12
        claim = d.device.claim_step(session, task.id, binding["step_id"], "dev_adapter")
        assert claim["accepted"] and run.attempts == 3


@pytest.mark.parametrize("unknown", ["canonical_call", "task_reservation", "prior_history"])
def test_unknown_or_historical_legacy_execution_never_mints_a_lease(orders, unknown):
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.tasks import usage
    d = orders
    with d.factory() as session, d.bind(**SCOPE):
        value = body(d, session)
        if unknown == "prior_history":
            value["iterations"] = [{"outcome": "checks_passed"}]
            write_body(session, value)
            with pytest.raises(ValueError, match="history_requires_reconciliation"):
                d.adapter.ensure_order_step(session, value)
            assert [count(session, model) for model in (Task, TaskRun, TaskStep)] == [0, 0, 0]
            return
        binding = d.adapter.ensure_order_step(session, value)
        if unknown == "canonical_call":
            value["invocations"] = {"synthetic_unknown": {"state": "calling"}}
            write_body(session, value)
        else:
            usage.reserve_native_call(session, **binding, order_id=value["id"], execution_id=value["execution_id"],
                review_digest=value["review_digest"], invocation_id="synthetic_pending", request_digest="e" * 64,
                estimate_micro=20, max_tokens=100)
            session.flush()
        with pytest.raises(ValueError, match="reconciliation_required"):
            d.adapter.ensure_order_step(session, value)
        run, step = session.get(TaskRun, binding["run_id"]), session.get(TaskStep, binding["step_id"])
        assert run.attempts == 0 and step.lease_token is None and step.lease_version == 0
        if unknown == "task_reservation":
            assert (run.reserved_cost_micro, run.reserved_tokens) == (20, 100)


@pytest.mark.parametrize("change", ["revoked_device", "changed_custody"])
def test_legacy_reclaim_still_requires_current_registered_device_and_custody(orders, change):
    from app.db.models import Workspace
    from app.db.supymem_models import DesktopDevice
    from app.db.task_models import TaskRun, TaskStep
    d = orders
    with d.factory() as session, d.bind(**SCOPE):
        binding = d.adapter.ensure_order_step(session, body(d, session))
        lease = d.device.claim_step(session, binding["task_id"], binding["step_id"], "dev_adapter")
        assert lease["accepted"]
        session.commit()
    with d.factory() as session:
        if change == "revoked_device":
            session.get(DesktopDevice, "dev_adapter").revoked_at = NOW
        else:
            session.get(Workspace, "ws_adapter").owner_id = "usr_new_owner"
        session.commit()
    d.clock[0] += timedelta(seconds=61)
    with d.factory() as session, d.bind(**SCOPE):
        with pytest.raises(PermissionError, match="scope_mismatch"):
            d.device.claim_step(session, binding["task_id"], binding["step_id"], "dev_adapter")
        assert session.get(TaskRun, binding["run_id"]).attempts == 1
        assert session.get(TaskStep, binding["step_id"]).lease_version == lease["lease_version"]


def test_legacy_profile_is_frozen_once_and_has_no_public_extension_or_reset(orders, monkeypatch):
    from app.build import device_ai_step
    from app.db.task_models import Task, TaskRun
    from app.tasks import budgets, service
    d = orders
    with d.factory() as session, d.bind(**SCOPE):
        value = body(d, session)
        binding = d.adapter.ensure_order_step(session, value)
        session.commit()
    monkeypatch.setattr(device_ai_step, "native_model_limits", lambda *args: pytest.fail("Frozen profile was recaptured"))
    with d.factory() as session, d.bind(**SCOPE):
        assert d.adapter.ensure_order_step(session, body(d, session)) == binding
        task, run = session.get(Task, binding["task_id"]), session.get(TaskRun, binding["run_id"])
        scope, budget, started, deadline = task.scope_json, task.budget_json, run.started_at, run.deadline_at
        run.cost_micro, run.tokens_used = 800_001, 350
        task.state = run.state = "input_required"
        task.blocker_json = json.dumps({"class": "budget_exhausted"})
        assert "cannot be extended" in budgets.eligibility(session, task, run)
        with pytest.raises(ValueError, match="cannot be extended"):
            budgets.grant(session, task, run, "synthetic_continue")
        assert (task.scope_json, task.budget_json, run.started_at, run.deadline_at) == (scope, budget, started, deadline)
        assert (run.cost_micro, run.tokens_used, run.allowance_json) == (800_001, 350, None)
        public = json.dumps(service.view(session, task))
        assert not any(value in public for value in ("native_task_budget_v1", "provider_calls", "800001", "route_digest", "synthetic_model"))
        assert budgets.effective_limits(task, run).wall_until == deadline


@pytest.mark.parametrize("change", ["missing_profile", "hash", "scope", "start_time", "control_job"])
def test_legacy_profile_missing_or_tampered_binding_never_falls_back_to_generic_caps(orders, change):
    from app.db.task_models import Task, TaskRun
    from app.tasks import budgets
    d = orders
    with d.factory() as session, d.bind(**SCOPE):
        binding = d.adapter.ensure_order_step(session, body(d, session))
        task, run = session.get(Task, binding["task_id"]), session.get(TaskRun, binding["run_id"])
        if change == "start_time":
            run.started_at = NOW.replace(tzinfo=None)
        elif change == "control_job":
            run.control_job_id = "job_hidden"
        else:
            value = json.loads(task.scope_json)
            if change == "missing_profile":
                value.pop("_native_legacy")
            elif change == "hash":
                value["_native_legacy"]["limits"]["cost_micro"] += 1
            else:
                profile = value["_native_legacy"]
                profile["binding"]["actor_id"] = "usr_foreign"
                profile["sha256"] = hashlib.sha256(json.dumps({key: item for key, item in profile.items() if key != "sha256"},
                    sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            task.scope_json = json.dumps(value)
        with pytest.raises(ValueError, match="original binding"):
            budgets.effective_limits(task, run)
        with pytest.raises(ValueError, match="original binding"):
            budgets.is_legacy_profile(task, run)
        assert budgets.eligibility(session, task, run) is not None


@pytest.mark.parametrize("change", ["purpose", "provider", "model", "catalog_model", "api_mode", "max_output_tokens", "endpoint_host", "extra_field", "bool_cap"])
def test_paid_legacy_role_requires_exact_frozen_route_and_output_cap(orders, change):
    from app.db.task_models import Task, TaskRun
    from app.tasks import budgets
    d = orders
    with d.factory() as session, d.bind(**SCOPE):
        binding = d.adapter.ensure_order_step(session, body(d, session))
        task, run = session.get(Task, binding["task_id"]), session.get(TaskRun, binding["run_id"])
        original = task.scope_json
        role = json.loads(original)["_native_legacy"]["model_limits"]["roles"][0]
        assert budgets.require_frozen_native_role(task, run, role) is True
        if change == "max_output_tokens":
            role[change] += 1
        elif change == "extra_field":
            role["unreviewed_option"] = True
        elif change == "bool_cap":
            role["max_output_tokens"] = True
        else:
            role[change] = "other_value"
        with pytest.raises(ValueError, match="model route changed"):
            budgets.require_frozen_native_role(task, run, role)
        assert task.scope_json == original
        assert (run.cost_micro, run.tokens_used, run.reserved_cost_micro, run.reserved_tokens) == (0, 0, 0, 0)


def test_operator_cap_increase_does_not_expand_an_existing_frozen_native_route(orders, monkeypatch):
    from app.build import device_ai_step
    from app.db.task_models import Task, TaskRun
    from app.tasks import budgets
    d = orders
    def original_limits(manifest):
        captured = synthetic_model_limits(manifest)
        for role in captured["roles"]:
            role["max_output_tokens"] = 700
        captured["max_output_tokens"] = 700
        captured["route_digest"] = hashlib.sha256(json.dumps({key: item for key, item in captured.items() if key != "route_digest"},
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return captured
    monkeypatch.setattr(device_ai_step, "native_model_limits", original_limits)
    with d.factory() as session, d.bind(**SCOPE):
        binding = d.adapter.ensure_order_step(session, body(d, session))
        task, run = session.get(Task, binding["task_id"]), session.get(TaskRun, binding["run_id"])
        role = json.loads(task.scope_json)["_native_legacy"]["model_limits"]["roles"][0]
        assert role["max_output_tokens"] == 700 and budgets.require_frozen_native_role(task, run, role)
        role["max_output_tokens"] = 2500
        with pytest.raises(ValueError, match="model route changed"):
            budgets.require_frozen_native_role(task, run, role)
        assert budgets.effective_limits(task, run).tokens == 400 * 200_700


def test_cli_profile_cannot_authorize_a_paid_role_and_typed_task_keeps_own_route(orders):
    from app.db.task_models import Task, TaskRun
    from app.tasks import budgets
    d = orders
    binding = typed(d)
    with d.factory() as session, d.bind(**SCOPE):
        typed_task, typed_run = session.get(Task, binding["task_id"]), session.get(TaskRun, binding["run_id"])
        assert budgets.require_frozen_native_role(typed_task, typed_run, {"malformed": "ignored for typed route"}) is False
        value = body(d, session)
        value["manifest"].pop("desktop_verification")
        value["manifest"]["coding_loop_verification"] = {"max_iterations": 6}
        value["manifest"]["credit_ceiling_micro"] = 0
        profile = d.adapter._capture_profile(value)
        # Pure CLI profile check only. Native CLI snapshot/admission remains
        # owned by S07; this does not fabricate a completion receipt.
        legacy_task = SimpleNamespace(id="tsk_cli_profile", create_key="device_order:" + value["id"], kind="one_time",
            workspace_id=value["workspace_id"], venture_id=value["venture_id"], owner_person_id=value["actor_id"],
            scope_json=json.dumps({"_native_legacy": profile}), budget_json=typed_task.budget_json)
        legacy_run = SimpleNamespace(id="trn_cli_profile", task_id=legacy_task.id, run_key=value["execution_id"],
            started_at=datetime.fromisoformat(value["claimed_at"]), control_job_id=None, allowance_json=None, attempts=0)
        assert budgets.is_legacy_profile(legacy_task, legacy_run)
        paid = synthetic_model_limits({"desktop_verification": {"synthetic": True}})["roles"][0]
        with pytest.raises(ValueError, match="model route changed"):
            budgets.require_frozen_native_role(legacy_task, legacy_run, paid)


@pytest.mark.parametrize("change", ["missing", "key_field", "userinfo", "bad_digest", "tokens", "max_output"])
def test_source_cap_metadata_unavailable_or_malformed_prevents_projection(orders, monkeypatch, change):
    from app.build import device_ai_step
    from app.db.task_models import Task, TaskRun, TaskStep
    d = orders
    if change == "missing":
        monkeypatch.delattr(device_ai_step, "native_model_limits")
    else:
        def invalid(manifest):
            captured = synthetic_model_limits(manifest)
            if change == "key_field":
                captured["roles"][0]["api_key"] = "synthetic_forbidden_field"
            elif change == "userinfo":
                captured["roles"][0]["endpoint_host"] = "someone:password@api.example.test"
            elif change == "bad_digest":
                captured["route_digest"] = "0" * 64
                return captured
            elif change == "tokens":
                captured["input_tokens"] = 1_000_000
            else:
                captured["max_output_tokens"] += 1
            content = {key: item for key, item in captured.items() if key != "route_digest"}
            captured["route_digest"] = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            return captured
        monkeypatch.setattr(device_ai_step, "native_model_limits", invalid)
    with d.factory() as session, d.bind(**SCOPE):
        with pytest.raises(ValueError, match="limits"):
            d.adapter.ensure_order_step(session, body(d, session))
        assert [count(session, model) for model in (Task, TaskRun, TaskStep)] == [0, 0, 0]


def test_profile_capture_keeps_reviewed_browser_calls_effects_and_cli_iterations(orders):
    """Pure published-cap capture only; this does not qualify a native CLI run."""
    d = orders
    with d.factory() as session:
        value = body(d, session)
        value["manifest"].pop("desktop_verification")
        roles = {phase: {"provider": "openai", "model": "gpt-5.6-luna", "service_tier": "default",
            "reasoning": "none", "max_output_tokens": 700 if phase == "plan" else 2500} for phase in ("plan", "actor", "verify")}
        value["manifest"]["general_browser_verification"] = {"max_steps": 32, "execution_profile": {
            "version": "browser_cuai_text_v1", "backend": "cuai", "policy_version": 1, "roles": roles,
            "pricing_digest": "a" * 64, "max_provider_calls": 20, "max_request_bytes": 2048,
            "completion_policy": "browser_semantic_review_v1"}}
        browser = d.adapter._capture_profile(value)
        assert (browser["limits"]["provider_calls"], browser["limits"]["tool_calls"], browser["limits"]["output_tokens"]) == (20, 20, 2500)
        value["manifest"].pop("general_browser_verification")
        value["manifest"]["coding_loop_verification"] = {"max_iterations": 6}
        value["manifest"]["credit_ceiling_micro"] = 0
        cli = d.adapter._capture_profile(value)
        assert (cli["limits"]["provider_calls"], cli["limits"]["tool_calls"], cli["limits"]["input_tokens"], cli["limits"]["output_tokens"]) == (0, 6, 0, 0)
        assert cli["model_limits"]["roles"] == []


@pytest.mark.parametrize("history", ["invocations", "iterations"])
def test_first_legacy_projection_keeps_prior_native_history_for_reconciliation(orders, history):
    from app.db.task_models import Task, TaskRun, TaskStep
    d = orders
    with d.factory() as session, d.bind(**SCOPE):
        value = body(d, session)
        value[history] = {"synthetic_paid_call": {"state": "completed", "actual_micro": 100}} if history == "invocations" else [{"outcome": "checks_passed"}]
        write_body(session, value)
        with pytest.raises(ValueError, match="history_requires_reconciliation"):
            d.adapter.ensure_order_step(session, value)
        assert body(d, session) == value
        assert [count(session, model) for model in (Task, TaskRun, TaskStep)] == [0, 0, 0]


@pytest.mark.parametrize("change", ["revoked", "custody", "stopped"])
def test_receipt_only_discovery_keeps_original_parent_after_admission_is_withdrawn(orders, change):
    from app.db.models import Workspace
    from app.db.supymem_models import DesktopDevice
    from app.db.task_models import Task, TaskRun, TaskStep
    d = orders
    binding = typed(d, bound=True)
    with d.factory() as session:
        value = body(d, session)
        value.update(binding)
        if change == "revoked":
            session.get(DesktopDevice, "dev_adapter").revoked_at = NOW
        elif change == "custody":
            session.get(Workspace, "ws_adapter").owner_id = "usr_new_owner"
        else:
            value["state"] = "stopped"
        write_body(session, value)
        session.commit()
    with d.factory() as session, d.bind(**SCOPE):
        value = body(d, session)
        # The caller's independent signed native receipt check is out of scope
        # for this scalar bridge test. Discovery itself grants no lease/effect.
        assert d.adapter.resolve_order_step(session, value, receipt_only=True) == binding
        assert not session.dirty and not session.new
        with pytest.raises((ValueError, PermissionError)):
            d.adapter.resolve_order_step(session, value)
        with pytest.raises((ValueError, PermissionError)):
            d.adapter.ensure_order_step(session, value)
        assert session.get(TaskStep, binding["step_id"]).lease_token is None
        assert [count(session, model) for model in (Task, TaskRun, TaskStep)] == [1, 1, 1]


@pytest.mark.parametrize("change", ["forged_body", "retargeted_saved_body"])
def test_receipt_only_discovery_denies_forged_or_retargeted_binding(orders, change):
    from app.db.task_models import TaskRun, TaskStep
    d = orders
    binding = typed(d, bound=True)
    with d.factory() as session:
        value = body(d, session)
        if change == "forged_body":
            value["execution_id"] = "dex_forged"
        else:
            value["actor_id"] = "usr_foreign"
            value["review_digest"] = d.native._digest(value)
            write_body(session, value)
        with pytest.raises((ValueError, PermissionError), match="changed|binding"):
            d.adapter.resolve_order_step(session, value, receipt_only=True)
        assert session.get(TaskRun, binding["run_id"]).cost_micro == 91
        assert session.get(TaskStep, binding["step_id"]).lease_token is None


def test_existing_typed_step_retains_run_identity_usage_deadlines_and_control_job(orders, monkeypatch):
    from app.db.models import Job
    from app.db.task_models import Task, TaskRun, TaskStep
    d = orders
    binding = typed(d, bound=True)
    with d.factory() as session, d.bind(**SCOPE):
        run = session.get(TaskRun, binding["run_id"])
        original = (run.run_key, run.started_at, run.deadline_at, run.control_job_id, run.cost_micro,
            run.tokens_used, run.attempts, run.tool_calls, run.allowance_json)
        from app.tasks.budgets import is_legacy_profile
        assert is_legacy_profile(session.get(Task, binding["task_id"]), run) is False
        order = body(d, session)
        trace = []
        actual_lock, actual_saved = d.adapter.lock_step, d.native._saved_order
        def parent_lock(*args, **kwargs):
            trace.append("parent")
            return actual_lock(*args, **kwargs)
        def order_read(*args, **kwargs):
            if kwargs.get("lock"):
                trace.append("order")
            return actual_saved(*args, **kwargs)
        monkeypatch.setattr(d.adapter, "lock_step", parent_lock)
        monkeypatch.setattr(d.native, "_saved_order", order_read)
        assert d.adapter.resolve_order_step(session, order) == binding
        assert d.adapter.ensure_order_step(session, order) == binding
        assert trace.index("parent") < trace.index("order")
        assert (run.run_key, run.started_at, run.deadline_at, run.control_job_id, run.cost_micro,
            run.tokens_used, run.attempts, run.tool_calls, run.allowance_json) == original
        assert session.get(TaskStep, binding["step_id"]).position == 1
        assert [count(session, model) for model in (Task, TaskRun, TaskStep, Job)] == [1, 1, 1, 1]
        session.commit()


@pytest.mark.parametrize("case", ["fresh_unsealed", "used_invocations", "used_iterations", "foreign_actor", "execution_fork"])
def test_existing_generic_step_without_exact_original_lineage_cannot_reuse_order(orders, case):
    from app.db.task_models import Task, TaskRun, TaskStep
    d = orders
    binding = typed(d, state="approval_required", step_state="approval_required",
        blocker={"class": "needs_approval"}, bound=case == "execution_fork")
    with d.factory() as session:
        value = body(d, session)
        if case == "used_invocations":
            value["invocations"] = {"synthetic_prior_call": {"state": "completed", "actual_micro": 100,
                "usage": {"input_tokens": 10, "output_tokens": 20}}}
        elif case == "used_iterations":
            value["iterations"] = [{"outcome": "checks_passed"}]
        elif case == "execution_fork":
            step = session.get(TaskStep, binding["step_id"])
            inputs = json.loads(step.inputs_json)
            inputs["execution_id"] = "dex_unrelated_execution"
            step.inputs_json = json.dumps(inputs)
        if case in {"used_invocations", "used_iterations"}:
            # A zero-counter generic run must not adopt another order's past
            # usage/effects. No historical Task reservations are fabricated.
            run = session.get(TaskRun, binding["run_id"])
            run.cost_micro = run.tokens_used = 0
        write_body(session, value)
        session.commit()
    with d.factory() as session, d.bind(**SCOPE):
        canonical = body(d, session)
        task, run, step = session.get(Task, binding["task_id"]), session.get(TaskRun, binding["run_id"]), session.get(TaskStep, binding["step_id"])
        original = (task.state, task.blocker_json, run.state, run.run_key, run.started_at, run.deadline_at,
            run.cost_micro, run.tokens_used, run.attempts, run.tool_calls, step.state, step.inputs_json, step.evidence_json)
        proposed = dict(canonical)
        if case == "foreign_actor":
            proposed["actor_id"] = "usr_foreign"
        if case in {"fresh_unsealed", "used_invocations", "used_iterations"}:
            assert d.adapter.resolve_order_step(session, proposed) == binding  # Lock discovery only.
            with pytest.raises(ValueError, match="binding_unsealed"):
                d.adapter.ensure_order_step(session, proposed)
        else:
            with pytest.raises(PermissionError, match="scope_mismatch|binding_mismatch"):
                d.adapter.ensure_order_step(session, proposed)
        assert (task.state, task.blocker_json, run.state, run.run_key, run.started_at, run.deadline_at,
            run.cost_micro, run.tokens_used, run.attempts, run.tool_calls, step.state, step.inputs_json, step.evidence_json) == original
        assert body(d, session) == canonical and [count(session, model) for model in (Task, TaskRun, TaskStep)] == [1, 1, 1]
        assert step.lease_token is None and step.lease_version == 0
        assert not session.new and not session.dirty


@pytest.mark.parametrize("with_review", [False, True])
def test_trusted_fresh_typed_sealer_preserves_parent_limits_and_freezes_step_roles_once(orders, monkeypatch, with_review):
    from app.build import device_ai_step
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.tasks import budgets
    from app.tasks.locks import lock_step
    d = orders
    binding = typed(d, state="approval_required", step_state="approval_required", blocker={"class": "needs_approval"}, unused=True)
    with d.factory() as session, d.bind(**SCOPE):
        task, run, step = lock_step(session, binding["step_id"])
        run.allowance_json = None
        value = body(d, session)
        if with_review:
            step.inputs_json = json.dumps({"device_order_id": value["id"], "review_digest": value["review_digest"]})
        original = (task.goal, task.scope_json, task.budget_json, task.state, task.blocker_json, run.run_key,
            run.started_at, run.deadline_at, run.cost_micro, run.tokens_used, run.attempts, run.tool_calls,
            run.control_job_id, run.allowance_json)
        limits = vars(budgets.effective_limits(task, run)).copy()
        assert d.adapter.seal_first_order_step(session, value) == binding
        seal = json.loads(step.evidence_json)[budgets.NATIVE_STEP_SEAL]
        assert seal["binding"] == {**binding, **d.adapter._profile_binding(value)}
        assert json.loads(step.inputs_json) == {"device_order_id": value["id"], "review_digest": value["review_digest"], "execution_id": value["execution_id"]}
        assert budgets.require_frozen_native_role(task, run, seal["model_limits"]["roles"][0], step=step)
        assert (task.goal, task.scope_json, task.budget_json, task.state, task.blocker_json, run.run_key,
            run.started_at, run.deadline_at, run.cost_micro, run.tokens_used, run.attempts, run.tool_calls,
            run.control_job_id, run.allowance_json) == original
        assert vars(budgets.effective_limits(task, run)) == limits and not budgets.is_legacy_profile(task, run)
        session.flush()
        monkeypatch.setattr(device_ai_step, "native_model_limits", lambda *args: pytest.fail("Typed seal recaptured its original route"))
        assert d.adapter.seal_first_order_step(session, value) == binding
        assert json.loads(step.evidence_json)[budgets.NATIVE_STEP_SEAL] == seal
        assert d.adapter.ensure_order_step(session, value) == binding
        assert step.lease_token is None and [count(session, model) for model in (Task, TaskRun, TaskStep)] == [1, 1, 1]


def test_fresh_typed_sealer_requires_prelocked_parent_and_never_creates_one(orders):
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.tasks.locks import TaskLockRequired
    d = orders
    with d.factory() as session, d.bind(**SCOPE):
        assert d.adapter.seal_first_order_step(session, body(d, session)) is None
        assert [count(session, model) for model in (Task, TaskRun, TaskStep)] == [0, 0, 0]
    binding = typed(d, unused=True)
    with d.factory() as session, d.bind(**SCOPE):
        with pytest.raises(TaskLockRequired, match="parent_lock_required"):
            d.adapter.seal_first_order_step(session, body(d, session))
        assert json.loads(session.get(TaskStep, binding["step_id"]).inputs_json) == {"device_order_id": "dwo_adapter"}


def test_typed_sealer_never_reseals_native_legacy_parent(orders):
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.tasks.locks import lock_step
    d = orders
    with d.factory() as session, d.bind(**SCOPE):
        value = body(d, session)
        binding = d.adapter.ensure_order_step(session, value)
        session.flush()
        task, run, step = lock_step(session, binding["step_id"])
        original = (task.scope_json, task.budget_json, run.run_key, run.started_at, run.deadline_at,
            run.cost_micro, run.tokens_used, step.inputs_json, step.evidence_json)
        with pytest.raises(ValueError, match="legacy_reseal_refused"):
            d.adapter.seal_first_order_step(session, value)
        assert (task.scope_json, task.budget_json, run.run_key, run.started_at, run.deadline_at,
            run.cost_micro, run.tokens_used, step.inputs_json, step.evidence_json) == original
        assert [count(session, model) for model in (Task, TaskRun, TaskStep)] == [1, 1, 1]


@pytest.mark.parametrize("used", ["lease", "started", "finished", "attempt", "job", "cost", "evidence", "call_event", "tool_event", "native_history", "stale_review"])
def test_typed_sealer_refuses_used_step_or_native_history_without_any_parent_reset(orders, used):
    from app.db.task_models import TaskEvent
    from app.tasks.locks import lock_step
    d = orders
    binding = typed(d, unused=True)
    with d.factory() as session, d.bind(**SCOPE):
        task, run, step = lock_step(session, binding["step_id"])
        value = body(d, session)
        if used == "lease":
            step.lease_version = 1
        elif used in {"started", "finished"}:
            setattr(step, used + "_at", NOW.replace(tzinfo=None))
        elif used in {"attempt", "cost"}:
            setattr(step, "attempt" if used == "attempt" else "cost_micro", 1)
        elif used == "job":
            step.job_id = "job_adapter"
        elif used == "evidence":
            step.evidence_json = json.dumps({"effect_outcome": "uncertain"})
        elif used in {"call_event", "tool_event"}:
            session.add(TaskEvent(id="tev_typed_seal_used", task_id=task.id, run_id=run.id, step_id=step.id,
                kind="call_reserved" if used == "call_event" else "native_tool_admitted", message="Synthetic prior activity",
                data_json="{}", created_at=NOW.replace(tzinfo=None)))
        elif used == "native_history":
            value["invocations"] = {"synthetic_previous": {"state": "completed"}}
            write_body(session, value)
        else:
            step.inputs_json = json.dumps({"device_order_id": value["id"], "review_digest": "0" * 64})
        original = (task.goal, task.scope_json, task.budget_json, run.run_key, run.started_at, run.deadline_at,
            run.cost_micro, run.tokens_used, run.attempts, run.tool_calls, run.control_job_id, run.allowance_json,
            step.inputs_json, step.evidence_json, step.lease_token, step.lease_version)
        with pytest.raises((ValueError, PermissionError), match="used|binding"):
            d.adapter.seal_first_order_step(session, value)
        assert (task.goal, task.scope_json, task.budget_json, run.run_key, run.started_at, run.deadline_at,
            run.cost_micro, run.tokens_used, run.attempts, run.tool_calls, run.control_job_id, run.allowance_json,
            step.inputs_json, step.evidence_json, step.lease_token, step.lease_version) == original
        assert body(d, session) == value


@pytest.mark.parametrize("change", ["role_cap", "scope", "execution", "hash", "unsealed", "no_provider"])
def test_typed_paid_role_requires_exact_seal_identity_scope_and_original_route(orders, change):
    from app.tasks import budgets
    from app.tasks.locks import lock_step
    d = orders
    binding = typed(d, unused=True)
    with d.factory() as session, d.bind(**SCOPE):
        task, run, step = lock_step(session, binding["step_id"])
        d.adapter.seal_first_order_step(session, body(d, session))
        evidence = json.loads(step.evidence_json)
        seal = evidence[budgets.NATIVE_STEP_SEAL]
        role = dict(seal["model_limits"]["roles"][0])
        if change == "role_cap":
            role["max_output_tokens"] += 1
        elif change == "execution":
            inputs = json.loads(step.inputs_json)
            inputs["execution_id"] = "dex_changed"
            step.inputs_json = json.dumps(inputs)
        elif change == "unsealed":
            step.evidence_json = "{}"
        else:
            if change == "scope":
                seal["binding"]["actor_id"] = "usr_foreign"
            elif change == "hash":
                seal["sha256"] = "0" * 64
            else:
                model_limits = {"input_tokens": 0, "max_output_tokens": 0, "roles": []}
                model_limits["route_digest"] = hashlib.sha256(json.dumps(model_limits, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                seal["model_limits"] = model_limits
            if change != "hash":
                seal["sha256"] = hashlib.sha256(json.dumps({key: item for key, item in seal.items() if key != "sha256"},
                    sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            step.evidence_json = json.dumps(evidence)
        with pytest.raises(ValueError, match="binding|route"):
            budgets.require_frozen_native_role(task, run, role, step=step)


@pytest.mark.parametrize("state,reason", [("paused", "needs_approval"), ("canceled", "needs_approval"),
    ("auth_required", "needs_auth"), ("input_required", "needs_input"), ("input_required", "budget_exhausted")])
def test_projection_does_not_clear_other_task_stops(orders, state, reason):
    from app.db.task_models import Task, TaskRun, TaskStep
    d = orders
    binding = typed(d, state=state, step_state="approval_required", blocker={"class": reason}, bound=True)
    with d.factory() as session, d.bind(**SCOPE):
        assert d.adapter.ensure_order_step(session, body(d, session)) == binding
        assert session.get(Task, binding["task_id"]).state == state
        assert session.get(TaskRun, binding["run_id"]).state == state
        assert session.get(TaskStep, binding["step_id"]).state == "approval_required"
        assert json.loads(session.get(Task, binding["task_id"]).blocker_json)["class"] == reason


def test_only_bound_step_approval_is_cleared_after_exact_native_claim(orders):
    from app.db.task_models import Task, TaskRun, TaskStep
    d = orders
    binding = typed(d, state="approval_required", step_state="approval_required", blocker={"class": "needs_approval"}, bound=True)
    with d.factory() as session, d.bind(**SCOPE):
        d.adapter.ensure_order_step(session, body(d, session))
        assert session.get(Task, binding["task_id"]).state == "working"
        assert session.get(TaskRun, binding["run_id"]).state == "working"
        assert session.get(TaskStep, binding["step_id"]).state == "submitted"
        assert session.get(Task, binding["task_id"]).blocker_json is None


@pytest.mark.parametrize("change", ["caller_manifest", "stored_digest", "snapshot", "revoked", "ownership", "foreign_scope"])
def test_saved_review_current_owner_device_and_snapshot_are_authoritative(orders, change):
    from app.db.models import Venture, Workspace
    from app.db.supymem_models import DesktopDevice
    from app.db.task_models import Task
    d = orders
    with d.factory() as session:
        value = body(d, session)
        scope = SCOPE
        if change == "caller_manifest":
            value["manifest"]["title"] = "Caller supplied replacement"
        elif change == "stored_digest":
            value["review_digest"] = "0" * 64
            write_body(session, value)
        elif change == "snapshot":
            session.get(Venture, "ven_adapter").created_at += timedelta(seconds=1)
            session.flush()
        elif change == "revoked":
            session.get(DesktopDevice, "dev_adapter").revoked_at = NOW
            session.flush()
        elif change == "ownership":
            session.get(Workspace, "ws_adapter").owner_id = "usr_foreign"
            session.flush()
        else:
            scope = {**SCOPE, "actor": "usr_foreign"}
        with d.bind(**scope), pytest.raises((ValueError, PermissionError)):
            d.adapter.ensure_order_step(session, value)
        assert count(session, Task) == 0


def test_approval_without_canonical_execution_cannot_create_a_run(orders):
    from app.db.task_models import Task
    d = orders
    with d.factory() as session, d.bind(**SCOPE):
        value = body(d, session)
        value["state"] = "approved"
        value.pop("execution_id")
        value.pop("claimed_at")
        write_body(session, value)
        assert d.adapter.resolve_order_step(session, value) is None
        with pytest.raises(ValueError, match="order_not_claimed"):
            d.adapter.ensure_order_step(session, value)
        assert count(session, Task) == 0


def test_explicit_fresh_pending_discovery_is_read_only_and_cannot_admit_or_create_task(orders):
    from app.db.task_models import Task, TaskRun, TaskStep
    d = orders
    with d.factory() as session, d.bind(**SCOPE):
        value = body(d, session)
        value["state"] = "pending"
        value.pop("claimed_at")
        value.pop("execution_id")
        write_body(session, value)
        original = body(d, session)
        assert d.adapter.resolve_order_step(session, value, preclaim=True) is None
        assert not session.new and not session.dirty
        with pytest.raises(ValueError, match="order_not_ready"):
            d.adapter.resolve_order_step(session, value)
        with pytest.raises(ValueError, match="order_not_ready"):
            d.adapter.ensure_order_step(session, value)
        assert body(d, session) == original
        assert [count(session, model) for model in (Task, TaskRun, TaskStep)] == [0, 0, 0]


def test_pending_parent_discovery_does_not_seal_inputs_clear_blocker_or_change_counters(orders):
    from app.db.task_models import Task, TaskRun, TaskStep
    d = orders
    binding = typed(d, state="approval_required", step_state="approval_required", blocker={"class": "needs_approval"})
    with d.factory() as session, d.bind(**SCOPE):
        value = body(d, session)
        value["state"] = "pending"
        value.pop("claimed_at")
        value.pop("execution_id")
        write_body(session, value)
        task, run, step = session.get(Task, binding["task_id"]), session.get(TaskRun, binding["run_id"]), session.get(TaskStep, binding["step_id"])
        original = (task.state, task.blocker_json, run.state, run.run_key, run.cost_micro, run.tokens_used, run.attempts,
            step.state, step.inputs_json, step.evidence_json, step.lease_token, step.lease_version)
        assert d.adapter.resolve_order_step(session, value, preclaim=True) == binding
        assert (task.state, task.blocker_json, run.state, run.run_key, run.cost_micro, run.tokens_used, run.attempts,
            step.state, step.inputs_json, step.evidence_json, step.lease_token, step.lease_version) == original
        with pytest.raises(ValueError, match="order_not_ready"):
            d.adapter.ensure_order_step(session, value)
        assert body(d, session) == value and not session.new and not session.dirty


@pytest.mark.parametrize("case", ["claimed_at", "execution_id", "invocations", "iterations", "expired"])
def test_preclaim_discovery_refuses_used_or_expired_pending_order(orders, case):
    from app.db.task_models import Task
    d = orders
    with d.factory() as session, d.bind(**SCOPE):
        value = body(d, session)
        value["state"] = "pending"
        value.pop("claimed_at")
        value.pop("execution_id")
        if case == "expired":
            value["expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
            value["review_digest"] = d.native._digest(value)
        else:
            value[case] = {"claimed_at": None, "execution_id": "dex_prior", "invocations": {}, "iterations": []}[case]
        write_body(session, value)
        with pytest.raises(ValueError, match="preclaim_not_fresh"):
            d.adapter.resolve_order_step(session, value, preclaim=True)
        assert body(d, session) == value and count(session, Task) == 0


@pytest.mark.parametrize("case", ["foreign_actor", "retargeted_body", "changed_custody"])
def test_pending_preclaim_still_requires_exact_saved_body_and_current_custody(orders, case):
    from app.db.models import Workspace
    from app.db.task_models import Task
    d = orders
    with d.factory() as session, d.bind(**SCOPE):
        value = body(d, session)
        value["state"] = "pending"
        value.pop("claimed_at")
        value.pop("execution_id")
        write_body(session, value)
        original = body(d, session)
        proposed = json.loads(json.dumps(value))
        if case == "foreign_actor":
            proposed["actor_id"] = "usr_foreign"
        elif case == "retargeted_body":
            proposed["manifest"]["instruction"] = "Caller supplied replacement goal"
        else:
            session.get(Workspace, "ws_adapter").owner_id = "usr_new_owner"
            session.flush()
        with pytest.raises((PermissionError, ValueError), match="scope_mismatch|order_changed"):
            d.adapter.resolve_order_step(session, proposed, preclaim=True)
        assert body(d, session) == original and count(session, Task) == 0


def test_pending_preclaim_cannot_be_combined_with_receipt_cleanup_or_public_flag(orders):
    d = orders
    with d.factory() as session, d.bind(**SCOPE):
        value = body(d, session)
        value["state"] = "pending"
        value.pop("claimed_at")
        value.pop("execution_id")
        value["preclaim"] = True  # A body field never chooses an internal mode.
        write_body(session, value)
        with pytest.raises(ValueError, match="discovery_mode_invalid"):
            d.adapter.resolve_order_step(session, value, preclaim=True, receipt_only=True)
        with pytest.raises(ValueError, match="order_not_ready"):
            d.adapter.resolve_order_step(session, value)
        assert not session.new and not session.dirty


def test_claimed_order_can_reuse_binding_after_its_review_window_expires(orders):
    from app.db.task_models import TaskRun
    d = orders
    binding = typed(d, bound=True)
    with d.factory() as session, d.bind(**SCOPE):
        value = body(d, session)
        value["expires_at"] = (NOW - timedelta(seconds=30)).isoformat()
        value["review_digest"] = d.native._digest(value)
        from app.db.task_models import TaskStep
        step = session.get(TaskStep, binding["step_id"])
        inputs = json.loads(step.inputs_json)
        inputs["review_digest"] = value["review_digest"]
        step.inputs_json = json.dumps(inputs)
        write_body(session, value)
        started = session.get(TaskRun, binding["run_id"]).started_at
        assert d.adapter.ensure_order_step(session, value) == binding
        assert session.get(TaskRun, binding["run_id"]).started_at == started


@pytest.mark.parametrize("outcome", ["calling", "uncertain"])
def test_canonical_unknown_call_prevents_reopening_blank_typed_checkpoint(orders, outcome):
    from app.db.task_models import Task, TaskStep
    d = orders
    binding = typed(d, state="approval_required", step_state="approval_required", bound=True,
        blocker={"class": "needs_approval"})
    with d.factory() as session, d.bind(**SCOPE):
        value = body(d, session)
        value["invocations"] = {"synthetic_paid_call": {"state": outcome}}
        write_body(session, value)
        with pytest.raises(ValueError, match="reconciliation_required"):
            d.adapter.ensure_order_step(session, value)
        assert session.get(Task, binding["task_id"]).state == "approval_required"
        assert session.get(TaskStep, binding["step_id"]).lease_token is None
        assert json.loads(session.get(TaskStep, binding["step_id"]).evidence_json) == {}


@pytest.mark.parametrize("hold", ["cost", "tokens", "zero_estimate_event"])
def test_unsettled_task_usage_prevents_new_order_binding_or_approval_clear(orders, hold):
    from app.db.task_models import Task, TaskEvent, TaskRun, TaskStep
    d = orders
    binding = typed(d, state="approval_required", step_state="approval_required", blocker={"class": "needs_approval"}, bound=True)
    with d.factory() as session, d.bind(**SCOPE):
        run = session.get(TaskRun, binding["run_id"])
        if hold == "cost":
            run.reserved_cost_micro = 1
        elif hold == "tokens":
            run.reserved_tokens = 1
        else:
            session.add(TaskEvent(id="tev_unsettled_adapter", task_id=binding["task_id"], run_id=run.id,
                step_id=binding["step_id"], kind="call_reserved", message="Synthetic zero-estimate pending call",
                data_json=json.dumps({"invocation": "synthetic_unsettled", "cost_micro": 0, "tokens": 0}),
                created_at=NOW.replace(tzinfo=None)))
        session.commit()
        with pytest.raises(ValueError, match="reconciliation_required"):
            d.adapter.ensure_order_step(session, body(d, session))
        assert session.get(Task, binding["task_id"]).state == "approval_required"
        step = session.get(TaskStep, binding["step_id"])
        assert json.loads(step.inputs_json)["execution_id"] == body(d, session)["execution_id"] and step.lease_token is None


@pytest.mark.parametrize("change", ["execution", "public_ids", "duplicate_step", "foreign_parent"])
def test_existing_binding_cannot_be_retargeted_or_duplicated(orders, change):
    from app.db.task_models import Task, TaskStep
    d = orders
    binding = typed(d, bound=True)
    with d.factory() as session, d.bind(**SCOPE):
        value = body(d, session)
        if change == "execution":
            value["execution_id"] = "dex_other_synthetic"
            write_body(session, value)
        elif change == "public_ids":
            value.update(binding, step_id="tst_other")
            write_body(session, value)
        elif change == "duplicate_step":
            original = session.get(TaskStep, "tst_adapter")
            session.add(TaskStep(id="tst_duplicate", run_id=original.run_id, position=2, title="Duplicate",
                skill=original.skill, runs_on="device", inputs_json=original.inputs_json,
                state="submitted", deadline_at=original.deadline_at))
            session.flush()
        else:
            session.get(Task, "tsk_adapter").owner_person_id = "usr_foreign"
            session.flush()
        with pytest.raises((ValueError, PermissionError), match="binding"):
            d.adapter.ensure_order_step(session, value)
        assert count(session, Task) == 1


@pytest.mark.parametrize("outcome", ["calling", "uncertain"])
def test_binding_never_replays_or_rebinds_unknown_effects(orders, outcome):
    from app.db.task_models import TaskStep
    d = orders
    binding = typed(d, evidence={"effect_outcome": outcome}, bound=True)
    with d.factory() as session, d.bind(**SCOPE):
        with pytest.raises(ValueError, match="reconciliation_required"):
            d.adapter.ensure_order_step(session, body(d, session))
        step = session.get(TaskStep, binding["step_id"])
        assert json.loads(step.evidence_json) == {"effect_outcome": outcome}
        assert json.loads(step.inputs_json)["execution_id"] == body(d, session)["execution_id"]
        assert step.lease_token is None


def test_projection_race_requires_parent_discovery_retry_before_order_lock(orders, monkeypatch):
    from app.db.task_models import Task
    d = orders
    original = d.adapter._resolve
    calls = []
    def discovered(*args):
        calls.append(True)
        return original(*args) if len(calls) == 1 else {"task_id": "tsk_concurrent", "run_id": "trn_concurrent", "step_id": "tst_concurrent"}
    monkeypatch.setattr(d.adapter, "_resolve", discovered)
    with d.factory() as session, d.bind(**SCOPE):
        with pytest.raises(ValueError, match="parent_lock_required"):
            d.adapter.ensure_order_step(session, body(d, session))
        assert count(session, Task) == 0
