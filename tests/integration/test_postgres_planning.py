"""Real migrated PostgreSQL plan journaling, lease races and credit settlement."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import select

from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db
from tests.planning_fixtures import install_planner, seed_research


@pytest.fixture()
def planning(journey_db, monkeypatch):
    from app.credits.accounts import ensure_account
    from app.planning.resources import update_inventory
    d = journey_db
    d["account_id"] = ensure_account(d["vid"], workspace_id=d["wid"], owner_person_id=d["actor"]).id
    d["dossier_id"], d["source_id"] = seed_research(d)
    update_inventory(d["wid"], d["vid"], [], True)
    d["calls"] = install_planner(monkeypatch)
    return d


def lease_plan(d):
    from app.core.jobs import lease
    with d["factory"]() as session:
        job = lease(session, "pg-plan", kinds=["plan.compile"], n=1)[0]
        session.commit()
        return job


def run(d):
    from app.worker.handlers import install_default_handlers
    from app.worker.main import _run_one
    install_default_handlers()
    job = lease_plan(d)
    asyncio.run(_run_one(d["factory"], job, {job.id}))
    return job


def requeue(d):
    from app.db.models import Job
    with d["factory"]() as session:
        row = session.get(Job, d["job_id"])
        row.status, row.run_after, row.scheduled_for = "retry_wait", None, None
        session.commit()


@pytest.mark.parametrize("named_stages", [False, True, "execution"])
def test_plan_response_recovery_settles_once_and_applies_once(planning, monkeypatch, named_stages):
    from app.db.models import CreditLedgerEntry, Job
    from app.db.supymem_models import ExecutionPlan, PlanMilestone
    from app.planning import compiler, execution as ex
    from llm_gateway import sole
    d = planning
    if named_stages:
        primary = sole._primary_chat
        async def named_response(*args, **kwargs):
            response = await primary(*args, **kwargs)
            message = response.choices[0].message
            value = json.loads(message.content)
            value["journey"] = {row["kind"]: {key: item for key, item in row.items() if key not in {"kind", "included"}}
                                for row in reversed(value.pop("milestones"))}
            if named_stages == "execution":
                for milestone in value["journey"].values():
                    for task in milestone["tasks"]:
                        selected = {name: task.pop(name) for name in
                                    ("capability_id", "implementation", "founder_action")}
                        if selected["implementation"] == "founder_action":
                            selected.pop("implementation")
                            selected["kind"] = "manual"
                        else:
                            selected["kind"] = "capability"
                        task["execution"] = selected
                        requirements = task.pop("acceptance")
                        expectations = task.get("landing_expectations")
                        task["checks"] = []
                        for index, requirement in enumerate(requirements):
                            evidence = None
                            if expectations:
                                criterion = next(item for item in expectations["criteria"] if item["criterion"] == index)
                                contract = expectations["contract"]
                                evidence = {"summary": criterion["summary"],
                                    "required_text": [contract["required_text"][i] for i in criterion["text_indices"]],
                                    "actions": [item for item in contract["actions"] if item["id"] in criterion["action_ids"]]}
                            task["checks"].append({"requirement": requirement, "landing_evidence": evidence})
                        if expectations:
                            task["landing_expectations"] = {"product_name": expectations["contract"]["product_name"]}
            message.content = json.dumps(value)
            return response
        monkeypatch.setattr(sole, "_primary_chat", named_response)
    original = compiler._finish
    def crash(*args):
        raise RuntimeError("isolated worker crash after response")
    monkeypatch.setattr(compiler, "_finish", crash)
    run(d)
    assert len(d["calls"]) == 1
    monkeypatch.setattr(compiler, "_finish", original)
    requeue(d)
    run(d)
    with d["factory"]() as session:
        assert session.get(Job, d["job_id"]).status == "succeeded"
        rows = session.scalars(select(ExecutionPlan).where(ExecutionPlan.venture_id == d["vid"])).all()
        assert len(rows) == 1 and rows[0].state == "founder_review"
        assert len(session.scalars(select(PlanMilestone).where(PlanMilestone.plan_id == rows[0].id)).all()) == 9
        spends = session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.purpose == "plan.compile", CreditLedgerEntry.account_id == d["account_id"], CreditLedgerEntry.kind == "spend")).all()
        assert len(spends) == 1 and spends[0].amount_micro < 0
    assert len(d["calls"]) == 1
    assert compiler.get_plan(ex.identifier(d["job_id"]))["usage"]["credits_micro"] > 0


def test_concurrent_delivery_cannot_release_active_reservation(planning, monkeypatch):
    from app.core.jobs import FatalError
    from app.credits.accounts import get_account
    from app.planning import compiler, execution as ex
    d = planning
    claimed = lease_plan(d)
    payload = {**claimed.payload, "_job_id": claimed.id, "_job_attempt": claimed.attempts, "_worker_id": claimed.worker_id}
    entered, release = Event(), Event()
    original = ex.journal.begin
    def pause(pid, key, payload, usage, token):
        entered.set()
        assert release.wait(10)
        return original(pid, key, payload, usage, token)
    monkeypatch.setattr(ex.journal, "begin", pause)
    def call():
        return compiler.run_compile(payload)
    with ThreadPoolExecutor(max_workers=2) as pool:
        primary = pool.submit(call)
        assert entered.wait(10)
        balance = get_account(d["vid"])
        assert balance.reserved_micro > 0
        duplicate = pool.submit(call)
        with pytest.raises(FatalError, match="plan_operation_in_progress"):
            duplicate.result(timeout=10)
        assert get_account(d["vid"]).reserved_micro == balance.reserved_micro
        release.set()
        primary.result(timeout=20)
    assert len(d["calls"]) == 1 and get_account(d["vid"]).reserved_micro == 0


def test_ambiguous_provider_is_not_redialled(planning, monkeypatch):
    from app.db.models import CreditAccount, CreditReservation, Job
    from llm_gateway import sole
    d = planning
    attempts = []
    async def timeout(*args, **kwargs):
        attempts.append(1)
        raise TimeoutError("private provider error")
    monkeypatch.setattr(sole, "_primary_chat", timeout)
    run(d)
    requeue(d)
    run(d)
    assert len(attempts) == 1  # timeout is uncertain; no redial or replay transport
    with d["factory"]() as session:
        assert session.get(Job, d["job_id"]).status == "succeeded"
        hold = session.scalar(select(CreditReservation).where(CreditReservation.purpose == "plan.compile", CreditReservation.workspace_id == d["wid"]))
        account = session.get(CreditAccount, hold.account_id)
        assert hold.status == "reconciling" and hold.amount_micro > 0
        assert account.reserved_micro == hold.amount_micro and hold.settled_micro is None


def test_inventory_changes_after_provider_are_not_applied(planning):
    from app.db.models import Job
    from app.db.supymem_models import ExistingAsset, ExecutionPlan
    from app.planning import execution as ex
    from tests.planning_fixtures import proposal
    from llm_gateway import sole
    from types import SimpleNamespace
    d = planning
    async def completion(messages, **kwargs):
        context = json.loads(messages[-1]["content"])
        with d["factory"]() as session:
            marker = session.scalar(select(ExistingAsset).where(ExistingAsset.venture_id == d["vid"], ExistingAsset.kind == "resource_inventory"))
            marker.detail = json.dumps({"complete": False})
            session.commit()
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(proposal(context))), finish_reason="stop")], usage=SimpleNamespace(prompt_tokens=500, completion_tokens=900, total_tokens=1400))
    old = sole._primary_chat
    sole._primary_chat = completion
    try:
        run(d)
    finally:
        sole._primary_chat = old
    with d["factory"]() as session:
        assert session.get(Job, d["job_id"]).status == "failed_dead"
        row = session.get(ExecutionPlan, ex.identifier(d["job_id"]))
        document = json.loads(row.structured)
        assert "proposal" not in document and document["_runtime"]["operations"]["plan"]["state"] == "completed"


def test_insufficient_exact_venture_credits_prevents_provider(planning):
    from app.db.models import CreditAccount, Job
    d = planning
    with d["factory"]() as session:
        account = session.scalar(select(CreditAccount).where(CreditAccount.venture_id == d["vid"]))
        account.granted_micro = 0
        session.commit()
    run(d)
    assert len(d["calls"]) == 0
    with d["factory"]() as session:
        row = session.get(Job, d["job_id"])
        assert row.status == "failed_dead" and "insufficient" in row.last_error


def test_concurrent_manual_edits_keep_one_version(planning):
    from app.planning import compiler, execution as ex
    d = planning
    run(d)
    pid = ex.identifier(d["job_id"])
    def change(summary):
        try:
            return compiler.patch_plan(pid, edit_version=1, patch={"summary": summary})["summary"]
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(change, ["First founder edit", "Second founder edit"]))
    assert outcomes.count("plan_edit_conflict") == 1
    assert compiler.get_plan(pid)["edit_version"] == 2


def test_plan_approval_preserves_later_venture_stage(planning):
    from app.db.models import Venture
    from app.planning import compiler, execution as ex

    d = planning
    run(d)
    pid = ex.identifier(d["job_id"])
    plan = compiler.get_plan(pid)
    with d["factory"]() as session:
        venture = session.get(Venture, d["vid"])
        venture.stage = "readiness_review"
        session.commit()

    approved = compiler.approve_plan(
        pid,
        person_id=d["actor"],
        review_token=plan["review_token"],
    )

    assert approved["plan"]["state"] == "approved"
    with d["factory"]() as session:
        assert session.get(Venture, d["vid"]).stage == "readiness_review"


def test_quote_request_and_durable_call_preserve_the_complete_context(planning, monkeypatch):
    from app.db.models import CreditReservation
    from app.planning import compiler, execution as ex, request_policy
    d = planning
    with d["factory"]() as session:
        original = ex.inputs(session, d["wid"], d["vid"])
    projected, source_map, asset_map = compiler._project(original["context"])
    request = request_policy.prepare(projected)
    quote = compiler.generation_quote(d["vid"], d["wid"])
    from llm_gateway import sole
    transmitted = []
    original_transmit = sole.transmit
    async def capture(*args, **kwargs):
        transmitted.append(kwargs)
        return await original_transmit(*args, **kwargs)
    monkeypatch.setattr(sole, "transmit", capture)
    run(d)
    messages, options = d["calls"][0]
    clock = [message for message in messages if message["role"] == "system" and message["content"].startswith("Now (server clock):")]
    assert len(clock) == 1 and "founder timezone:" in clock[0]["content"]
    assert [message for message in messages if message is not clock[0]] == request.messages
    assert len(messages) == len(request.messages) + 1
    assert json.loads(messages[-1]["content"]) == projected
    assert set(source_map.values()) == {row["id"] for row in original["context"]["sources"]}
    assert set(asset_map.values()) == {row["id"] for row in original["context"]["assets"]}
    assert options["max_tokens"] == quote["max_output_tokens"] == 16384
    assert transmitted[0]["response_schema"] == request.schema
    assert transmitted[0]["single_attempt"] is True and transmitted[0]["use_workspace_override"] is False
    assert options["captured_config"].deployment_pro == "DeepSeek-V4-Pro"
    assert options["captured_config"].timeout_seconds == request.provider_timeout_seconds == 240
    assert quote["generation_policy"] == request_policy.public_policy()
    with d["factory"]() as session:
        hold = session.scalar(select(CreditReservation).where(CreditReservation.purpose == "plan.compile", CreditReservation.workspace_id == d["wid"]))
        assert hold.amount_micro == quote["ceiling_micro"] == request.estimate("DeepSeek-V4-Pro")
    saved = json.loads(ex.read(ex.identifier(d["job_id"])).structured)
    assert saved["_runtime"]["context"] == original["context"]


@pytest.mark.parametrize("finish", [None, "length", "content_filter", "tool_calls"])
def test_incomplete_provider_output_is_repaired_once_then_charged_per_call_without_a_plan(planning, monkeypatch, finish):
    from types import SimpleNamespace
    from app.db.models import CreditLedgerEntry, Job
    from app.planning import execution as ex
    from tests.planning_fixtures import proposal
    from llm_gateway import sole
    d, calls = planning, []
    async def response(messages, **kwargs):
        calls.append(1)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish,
            message=SimpleNamespace(content=json.dumps(proposal(json.loads(messages[-1]["content"])))) )],
            usage=SimpleNamespace(prompt_tokens=900, completion_tokens=1600, total_tokens=2500))
    monkeypatch.setattr(sole, "_primary_chat", response)
    run(d)
    requeue(d)
    run(d)
    assert calls == [1, 1]
    doc = json.loads(ex.read(ex.identifier(d["job_id"])).structured)
    assert not doc.get("proposal")
    for key in ("plan", "plan-repair"):
        op = doc["_runtime"]["operations"][key]
        assert op["state"] == "completed" and op["usage"]["credits_micro"] > 0
    with d["factory"]() as session:
        assert session.get(Job, d["job_id"]).status == "succeeded"
        assert doc["_runtime"]["generation_failed"] is True
        assert len(session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.purpose == "plan.compile", CreditLedgerEntry.account_id == d["account_id"], CreditLedgerEntry.kind == "spend")).all()) == 2


def test_provider_usage_over_the_displayed_ceiling_cannot_charge_more_or_apply(planning, monkeypatch):
    from types import SimpleNamespace
    from app.db.models import CreditReservation, Job
    from app.planning import compiler, execution as ex
    from tests.planning_fixtures import proposal
    from llm_gateway import sole
    d = planning
    quote = compiler.generation_quote(d["vid"], d["wid"])
    async def response(messages, **kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
            message=SimpleNamespace(content=json.dumps(proposal(json.loads(messages[-1]["content"])))) )],
            usage=SimpleNamespace(prompt_tokens=900000, completion_tokens=160000, total_tokens=1060000))
    monkeypatch.setattr(sole, "_primary_chat", response)
    run(d)
    doc = json.loads(ex.read(ex.identifier(d["job_id"])).structured)
    usage = doc["_runtime"]["operations"]["plan"]["usage"]
    assert usage["credits_micro"] == quote["ceiling_micro"]
    assert usage["provider_actual_micro"] > quote["ceiling_micro"] and usage["provider_overage_micro"] > 0
    assert not doc.get("proposal")
    with d["factory"]() as session:
        hold = session.scalar(select(CreditReservation).where(CreditReservation.purpose == "plan.compile", CreditReservation.workspace_id == d["wid"]))
        assert hold.settled_micro == hold.amount_micro == quote["ceiling_micro"]
        assert session.get(Job, d["job_id"]).last_error == "plan_provider_usage_above_ceiling"


def test_priced_provider_cannot_switch_after_the_durable_call_begins(planning, monkeypatch):
    from app.planning import execution as ex
    d = planning
    original = ex.journal.begin
    def change_after_begin(*args):
        original(*args)
        monkeypatch.setenv("LLM_BASE_URL", "https://changed.example.test/openai/v1")
        monkeypatch.setenv("LLM_DEPLOYMENT_PRO", "unpriced-model")
    monkeypatch.setattr(ex.journal, "begin", change_after_begin)
    run(d)
    assert len(d["calls"]) == 1
    config = d["calls"][0][1]["captured_config"]
    assert config.deployment_pro == "DeepSeek-V4-Pro"
    assert config.base_url == "https://foundry.example.test/openai/v1"
    usage = json.loads(ex.read(ex.identifier(d["job_id"])).structured)["_runtime"]["operations"]["plan"]["usage"]
    assert usage["model"] == "DeepSeek-V4-Pro" and usage["provider_identity"]


def test_operator_probe_reads_the_failed_context_without_changing_its_hold_or_result(planning, monkeypatch):
    from types import SimpleNamespace
    from app.db.models import CreditAccount, CreditReservation
    from app.planning import execution as ex
    from llm_gateway import sole
    from scripts.infra import probe_preserved_plan as probe
    from tests.planning_fixtures import proposal
    d, network = planning, []
    async def timeout(*args, **kwargs):
        raise TimeoutError("synthetic failed operation")
    monkeypatch.setattr(sole, "_primary_chat", timeout)
    run(d)
    pid = ex.identifier(d["job_id"])
    saved = ex.read(pid).structured
    with d["factory"]() as session:
        before = session.get(CreditAccount, d["account_id"]).reserved_micro
    async def response(messages, **kwargs):
        network.append((messages, kwargs))
        value = proposal(json.loads(messages[-1]["content"]))
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(value)))],
            usage=SimpleNamespace(prompt_tokens=900, completion_tokens=1600, total_tokens=2500))
    monkeypatch.setattr(sole, "_primary_chat", response)
    monkeypatch.setenv("APP_ENV", "staging")
    args = SimpleNamespace(plan_id=pid, execute=False, expected_context_sha256=None, expected_request_sha256=None, max_ceiling_micro=None)
    check = probe.probe(args)
    assert not network and check["provider_attempted"] is False
    assert check["context_sha256"] == probe.digest(json.loads(saved)["_runtime"]["context"])
    args.execute, args.max_ceiling_micro = True, check["estimated_ceiling_micro"]
    with pytest.raises(ValueError, match="preflight_hash"):
        probe.probe(args)
    assert not network
    args.expected_context_sha256, args.expected_request_sha256 = check["context_sha256"], check["request_sha256"]
    result = probe.probe(args)
    assert len(network) == 1 and result["outcome"] == "valid_output" and result["failed_document_unchanged"], result
    assert result["usage"]["completion_tokens"] == 1600 and result["provider_usage_micro"] > 0
    assert result["billing_mode"] == "operator_diagnostic_no_founder_ledger_mutation"
    with d["factory"]() as session:
        from app.db.supymem_models import ExecutionPlan
        assert session.get(ExecutionPlan, pid).structured == saved
        assert session.get(CreditAccount, d["account_id"]).reserved_micro == before > 0
        hold = session.scalar(select(CreditReservation).where(CreditReservation.workspace_id == d["wid"], CreditReservation.purpose == "plan.compile"))
        assert hold.status == "reconciling" and hold.settled_micro is None


def test_schema_rejection_persists_safe_fields_and_settles_once(planning, monkeypatch):
    from types import SimpleNamespace
    from app.planning import execution as ex
    from tests.planning_fixtures import proposal
    from llm_gateway import sole
    d, calls = planning, []
    async def response(messages, **kwargs):
        calls.append(1)
        value = proposal(json.loads(messages[-1]["content"]))
        value["milestones"][0]["title"] = 999
        value["private_extra_field"] = "private response data"
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
            message=SimpleNamespace(content=json.dumps(value)))],
            usage=SimpleNamespace(prompt_tokens=900, completion_tokens=1600, total_tokens=2500))
    monkeypatch.setattr(sole, "_primary_chat", response)
    run(d)
    requeue(d)
    run(d)
    assert calls == [1, 1]
    doc = json.loads(ex.read(ex.identifier(d["job_id"])).structured)
    operation = doc["_runtime"]["operations"]["plan"]
    assert not doc.get("proposal") and operation["usage"]["credits_micro"] > 0
    diagnostics = operation["usage"]["output"]
    assert diagnostics["schema_issue_count"] == 2
    assert {"path": ["milestones", 0, "title"], "code": "string_type"} in diagnostics["schema_issues"]
    assert {"path": ["<field>"], "code": "extra_forbidden"} in diagnostics["schema_issues"]
    assert "private_extra_field" not in json.dumps(operation)
    assert "private response data" not in json.dumps(operation)


@pytest.mark.parametrize("field,repair", [("visual_name", None), ("required_text", True), ("required_text", False)])
def test_plan_copy_clips_only_descriptions_and_repairs_complete_exact_text(planning, monkeypatch, field, repair):
    from types import SimpleNamespace
    from app.db.models import CreditLedgerEntry
    from app.planning import execution as ex
    from tests.planning_fixtures import proposal
    from llm_gateway import sole
    from core.static_qa_contracts import RequiredText, Visual
    from pydantic import TypeAdapter
    d, calls = planning, []
    complete = "A proposed bakery ordering workflow with a counter, calendar and collection box."
    limit = (Visual.model_json_schema()["properties"]["name"]["maxLength"] if field == "visual_name"
             else TypeAdapter(RequiredText).json_schema()["maxLength"])
    overlong = complete * (limit // len(complete) + 1)
    assert len(complete) <= limit < len(overlong)
    async def response(messages, **kwargs):
        calls.append(1)
        value = proposal(json.loads(messages[-1]["content"]))
        expectations = value["milestones"][0]["tasks"][0]["landing_expectations"]
        if field == "visual_name":
            expectations["contract"]["visuals"] = [{"id": "workflow_preview", "name": overlong}]
            expectations["criteria"][0]["visual_ids"] = ["workflow_preview"]
        else:
            expectations["contract"]["required_text"][0] = complete if repair and len(calls) > 1 else overlong
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
            message=SimpleNamespace(content=json.dumps(value)))],
            usage=SimpleNamespace(prompt_tokens=900, completion_tokens=1600, total_tokens=2500))
    monkeypatch.setattr(sole, "_primary_chat", response)
    run(d)
    requeue(d)
    run(d)
    assert len(calls) == (1 if field == "visual_name" else 2)
    doc = json.loads(ex.read(ex.identifier(d["job_id"])).structured)
    if field == "visual_name":
        saved = doc["proposal"]["milestones"][0]["tasks"][0]["landing_expectations"]
        visual = saved["contract"]["visuals"][0]
        assert visual["id"] == "workflow_preview"
        assert len(visual["name"]) <= limit and visual["name"].endswith("…")
        assert saved["criteria"][0]["visual_ids"] == ["workflow_preview"]
    elif repair:
        saved = doc["proposal"]["milestones"][0]["tasks"][0]["landing_expectations"]["contract"]
        assert saved["required_text"][0] == complete
    else:
        assert not doc.get("proposal")
    with d["factory"]() as session:
        spending = session.scalars(select(CreditLedgerEntry).where(
            CreditLedgerEntry.purpose == "plan.compile", CreditLedgerEntry.account_id == d["account_id"],
            CreditLedgerEntry.kind == "spend")).all()
        assert len(spending) == len(calls)
