"""Synthetic Mac leases and authoritative receipts; no native effects run."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tests.company_os_harness import load_company_os_app

NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)


@pytest.fixture()
def device_db(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", "sqlite:///:memory:")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.credits import _txn
    from app.db.models import Base, CreditReservation, Person, Venture, VentureDocument, Workspace
    from app.db.supymem_models import DesktopDevice, Notice
    from app.db.task_models import Task, TaskEvent, TaskRun, TaskStep
    from app.tasks import device
    from app.tasks.contracts import Budget
    from app.tenancy import bind

    monkeypatch.setattr(_txn, "utcnow", lambda: NOW)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[model.__table__ for model in (
        Person, Workspace, Venture, Task, TaskRun, TaskStep, TaskEvent, DesktopDevice,
        Notice, VentureDocument, CreditReservation)])
    with Session(engine) as session:
        moment = NOW.replace(tzinfo=None)
        session.add(Person(id="usr_device", display_name="Synthetic founder", created_at=moment))
        session.add(Workspace(id="ws_device", owner_id="usr_device", kind="venture", name="Synthetic",
            entry_mode="idea", created_at=moment, updated_at=moment))
        session.add(Venture(id="ven_device", workspace_id="ws_device", name="Synthetic",
            raw_idea="Synthetic task", stage="draft", created_at=moment, updated_at=moment))
        for identifier, owner in (("dev_one", "usr_device"), ("dev_two", "usr_device"), ("dev_other", "usr_other")):
            session.add(DesktopDevice(id=identifier, person_id=owner, name="Synthetic Mac", platform="macos"))
        session.add(Task(id="tsk_device", workspace_id="ws_device", venture_id="ven_device", owner_person_id="usr_device",
            title="Inspect this workspace", goal="Inspect this workspace", budget_json=Budget().model_dump_json(),
            state="working", deadline_at=moment + timedelta(minutes=20), created_at=moment, updated_at=moment))
        session.add(TaskRun(id="trn_device", task_id="tsk_device", run_key="initial", state="working",
            started_at=moment, deadline_at=moment + timedelta(minutes=20)))
        session.add(TaskStep(id="tst_device", run_id="trn_device", position=0, title="Inspect this workspace",
            skill="device.inspect", runs_on="device", state="submitted", deadline_at=moment + timedelta(minutes=20)))
        session.add(VentureDocument(id="doc_device", venture_id="ven_device", kind="device_evidence",
            title="Synthetic evidence", provenance_json="{}", created_at=moment, updated_at=moment))
        session.commit()
    yield SimpleNamespace(module=device, engine=engine, bind=bind)
    engine.dispose()


def invoke(d, function, *args, scope=None, **kwargs):
    scope = scope or {"workspace_id": "ws_device", "venture_id": "ven_device", "actor": "usr_device"}
    with Session(d.engine, expire_on_commit=False) as session, d.bind(**scope):
        result = function(session, "tsk_device", "tst_device", *args, **kwargs)
        session.commit()
        return result


def claim(d, **kwargs):
    return invoke(d, d.module.claim_step, "dev_one", **kwargs)


def fenced(d, function, lease, **kwargs):
    return invoke(d, function, "dev_one", lease["lease_token"], lease["lease_version"], **kwargs)


def saved(d):
    from app.db.task_models import Task, TaskEvent, TaskStep
    with Session(d.engine) as session:
        task, step = session.get(Task, "tsk_device"), session.get(TaskStep, "tst_device")
        return task.state, step.state, json.loads(task.blocker_json or "{}"), json.loads(step.evidence_json), [row.data_json for row in session.scalars(select(TaskEvent))]


def test_lease_token_is_private_hashed_and_heartbeat_renews(device_db):
    from app.db.task_models import Task, TaskStep
    from app.tasks.service import view
    d = device_db
    lease = claim(d)
    assert lease["accepted"] and lease["heartbeat_seconds"] == 15
    assert lease["lease_expires_at"] == "2026-10-08T12:01:00+00:00"
    with Session(d.engine) as session:
        step = session.get(TaskStep, "tst_device")
        assert step.lease_token != lease["lease_token"] and len(step.lease_token) == 64
        assert lease["lease_token"] not in json.dumps(view(session, session.get(Task, "tsk_device")))
    heartbeat = fenced(d, d.module.heartbeat_step, lease, now=NOW + timedelta(seconds=15))
    assert heartbeat["lease_expires_at"] == "2026-10-08T12:01:15+00:00"
    assert "lease_token" not in heartbeat
    assert all(lease["lease_token"] not in event for event in saved(d)[-1])


def test_duplicate_claim_and_second_mac_cannot_steal(device_db):
    d = device_db
    lease = claim(d)
    assert claim(d)["reason"] == "lease_active"
    with pytest.raises(PermissionError, match="move_requires_approval"):
        invoke(d, d.module.claim_step, "dev_two", now=NOW + timedelta(seconds=61))
    assert fenced(d, d.module.heartbeat_step, lease)["accepted"]


@pytest.mark.parametrize("field", ["token", "version", "owner"])
def test_exact_lease_fence_is_required(device_db, field):
    d = device_db
    lease = claim(d)
    owner, token, version = "dev_one", lease["lease_token"], lease["lease_version"]
    if field == "token":
        token = "incorrect-token-for-synthetic-test"
    elif field == "version":
        version += 1
    else:
        owner = "dev_two"
    with pytest.raises(PermissionError, match="fence_invalid"):
        invoke(d, d.module.heartbeat_step, owner, token, version)


@pytest.mark.parametrize("scope", [
    {"workspace_id": "ws_other", "venture_id": "ven_device", "actor": "usr_device"},
    {"workspace_id": "ws_device", "venture_id": "ven_other", "actor": "usr_device"},
    {"workspace_id": "ws_device", "venture_id": "ven_device", "actor": "usr_other"},
])
def test_parent_scope_and_owner_are_required(device_db, scope):
    with pytest.raises(PermissionError, match="scope_mismatch"):
        invoke(device_db, device_db.module.claim_step, "dev_one", scope=scope)


def test_registered_device_owner_and_revocation_are_checked(device_db):
    from app.db.supymem_models import DesktopDevice
    d = device_db
    with pytest.raises(PermissionError, match="scope_mismatch"):
        invoke(d, d.module.claim_step, "dev_other")
    with Session(d.engine) as session:
        session.get(DesktopDevice, "dev_one").revoked_at = NOW
        session.commit()
    with pytest.raises(PermissionError, match="scope_mismatch"):
        claim(d)


def test_expired_heartbeat_saves_waiting_blocker_and_new_claim_fences_old_token(device_db):
    d = device_db
    lease = claim(d)
    expired = fenced(d, d.module.heartbeat_step, lease, now=NOW + timedelta(seconds=60))
    assert not expired["accepted"] and expired["state"] == "waiting_for_device"
    assert saved(d)[:3] == ("input_required", "waiting_for_device", {
        "class": "needs_device_online", "reason": "Your Mac stopped responding.",
        "unblock_action": "Open Supymem on the same Mac to resume from saved progress."})
    resumed = claim(d, now=NOW + timedelta(seconds=61))
    assert resumed["lease_version"] > lease["lease_version"] and resumed["lease_token"] != lease["lease_token"]
    with pytest.raises(PermissionError, match="fence_invalid"):
        fenced(d, d.module.checkpoint_step, lease, now=NOW + timedelta(seconds=62))


def test_stale_sweep_is_idempotent_and_keeps_partial_reference(device_db):
    d = device_db
    lease = claim(d)
    fenced(d, d.module.checkpoint_step, lease, evidence_refs=["venture_document:doc_device"])
    with Session(d.engine) as session:
        assert d.module.sweep_stale_leases(session, now=NOW + timedelta(seconds=61)) == ["tst_device"]
        session.commit()
    with Session(d.engine) as session:
        assert d.module.sweep_stale_leases(session, now=NOW + timedelta(seconds=62)) == []
        session.commit()
    assert saved(d)[3]["refs"] == ["venture_document:doc_device"]


def test_sweep_reuses_callers_existing_sqlite_write_transaction(device_db):
    from app.db.task_models import Task
    d = device_db
    claim(d)
    with Session(d.engine) as session:
        session.get(Task, "tsk_device").updated_at = NOW + timedelta(seconds=61)
        session.flush()  # No serialize_writes info: the caller already wrote.
        assert d.module.sweep_stale_leases(session, now=NOW + timedelta(seconds=61)) == ["tst_device"]
        session.commit()
    assert saved(d)[1] == "waiting_for_device"


@pytest.mark.parametrize("reason,state", [("needs_auth", "auth_required"), ("needs_approval", "approval_required"),
                                         ("budget_exhausted", "input_required")])
def test_same_mac_does_not_clear_other_blockers(device_db, reason, state):
    from app.db.task_models import Task, TaskRun, TaskStep
    d = device_db
    claim(d)
    with Session(d.engine) as session:
        task, run, step = session.get(Task, "tsk_device"), session.get(TaskRun, "trn_device"), session.get(TaskStep, "tst_device")
        step.state, step.lease_token, step.lease_expires_at = "waiting_for_device", None, None
        task.state = run.state = state
        task.blocker_json = json.dumps({"class": reason, "reason": "Synthetic blocker", "unblock_action": "Synthetic action"})
        session.commit()
    assert claim(d)["reason"] == "task_not_ready"
    assert saved(d)[0] == state


@pytest.mark.parametrize("outcome", ["uncertain", "calling"])
def test_unknown_effect_never_auto_reclaims(device_db, outcome):
    d = device_db
    lease = claim(d)
    fenced(d, d.module.checkpoint_step, lease, effect_outcome=outcome)
    if outcome == "calling":
        with Session(d.engine) as session:
            d.module.sweep_stale_leases(session, now=NOW + timedelta(seconds=61))
            session.commit()
    assert saved(d)[1] == "reconciliation_required"
    assert saved(d)[2]["reconciliation_required"] is True
    assert claim(d, now=NOW + timedelta(seconds=62))["reason"] == "reconciliation_required"


def test_client_completion_cannot_clear_an_unbound_calling_effect(device_db):
    d = device_db
    lease = claim(d)
    fenced(d, d.module.checkpoint_step, lease, effect_outcome="calling")
    result = fenced(d, d.module.checkpoint_step, lease, effect_outcome="completed")
    assert result["state"] == "reconciliation_required"
    assert saved(d)[3]["effect_outcome"] == "uncertain"


@pytest.mark.parametrize("refs", [[{"body": "free text"}], ["https://example.test/result"],
    ["venture_document:" + "sk" + "-" + "fake-test-secret-value-1234567890"]])
def test_checkpoint_accepts_only_bounded_metadata_refs(device_db, refs):
    d = device_db
    lease = claim(d)
    with pytest.raises(ValueError, match="evidence_invalid"):
        fenced(d, d.module.checkpoint_step, lease, evidence_refs=refs)
    assert saved(d)[3] == {}


def _order(d, *, settled=True, verified=True, uncertain=False):
    from app.build import device_orders
    from app.db.models import CreditReservation, VentureDocument
    from app.db.task_models import TaskStep
    moment = NOW.replace(tzinfo=None)
    body = {"id": "dwo_device", "workspace_id": "ws_device", "venture_id": "ven_device",
        "actor_id": "usr_device", "device_id": "dev_one", "manifest": {"browser_verification": {
            "expected_locations": ["https://example.test"]}},
        "task_snapshot": {}, "expires_at": (NOW + timedelta(minutes=10)).isoformat(), "state": "stopped",
        "claimed_at": NOW.isoformat(), "closed_at": NOW.isoformat(), "execution_id": "dex_device",
        "reservation_id": "res_device", "invocations": {"call_device": {"state": "completed",
            "request_digest": "b" * 64, "actual_micro": 0}}, "native_stop": {"execution_id": "dex_device",
            "evidence_digest": "a" * 64, "processes_stopped": True, "task_verified": verified,
            "verification_kind": "expected_browser_locations_v1"}, "stop_reconciliation_required": uncertain}
    body["review_digest"] = device_orders._digest(body)
    with Session(d.engine) as session:
        session.get(TaskStep, "tst_device").inputs_json = json.dumps({"device_order_id": "dwo_device"})
        session.add(VentureDocument(id="dwo_device", venture_id="ven_device", kind=device_orders.KIND, title="Synthetic order",
            provenance_json=json.dumps(body), created_at=moment, updated_at=moment))
        session.add(CreditReservation(id="res_device", account_id="acct_device", amount_micro=0,
            workspace_id="ws_device", request_id="dwo_device", purpose="device.work_order", status="settled" if settled else "reconciling",
            expires_at=moment + timedelta(minutes=10), created_at=moment))
        session.commit()


def test_attached_authoritative_stop_completes_once_without_admission(device_db):
    d = device_db
    _order(d)
    invoke(d, d.module.attach_order, "dev_one", "dwo_device")
    first = invoke(d, d.module.attach_stop_receipt, "dev_one", "dwo_device")
    second = invoke(d, d.module.attach_stop_receipt, "dev_one", "dwo_device")
    assert first["state"] == second["state"] == "completed"
    assert saved(d)[:2] == ("completed", "completed")
    assert len([event for event in saved(d)[-1] if "device_order:dwo_device" in event]) == 1


@pytest.mark.parametrize("invocations", [{}, {"empty": {}}])
def test_asserted_native_verification_without_execution_evidence_cannot_complete(device_db, invocations):
    from app.db.models import VentureDocument

    d = device_db
    _order(d)
    with Session(d.engine) as session:
        row = session.get(VentureDocument, "dwo_device")
        body = json.loads(row.provenance_json)
        body["invocations"] = invocations
        row.provenance_json = json.dumps(body)
        session.commit()
    invoke(d, d.module.attach_order, "dev_one", "dwo_device")
    result = invoke(d, d.module.attach_stop_receipt, "dev_one", "dwo_device")
    assert not result["accepted"] and result["state"] == "reconciliation_required"
    assert saved(d)[:2] == ("input_required", "reconciliation_required")


def test_real_native_stop_cannot_prove_an_unrelated_source_count(device_db):
    from app.db.task_models import TaskStep
    d = device_db
    _order(d)
    with Session(d.engine) as session:
        session.get(TaskStep, "tst_device").done_test_json = json.dumps({
            "type": "deterministic", "spec": {"predicate": "at_least", "metric": "source_count", "count": 1}})
        session.commit()
    invoke(d, d.module.attach_order, "dev_one", "dwo_device")
    invoke(d, d.module.attach_stop_receipt, "dev_one", "dwo_device")
    assert saved(d)[:2] == ("failed", "failed")


@pytest.mark.parametrize("options", [{"settled": False}, {"verified": False}, {"uncertain": True}])
def test_unsettled_or_unverified_stop_requires_reconciliation(device_db, options):
    d = device_db
    _order(d, **options)
    invoke(d, d.module.attach_order, "dev_one", "dwo_device")
    result = invoke(d, d.module.attach_stop_receipt, "dev_one", "dwo_device")
    assert not result["accepted"] and result["state"] == "reconciliation_required"
    assert saved(d)[0] == "input_required"


@pytest.mark.parametrize("invocation", [
    {"state": "responded", "effect": {"state": "admitted", "digest": "b" * 64}},
    {"state": "completed", "task_usage_projection": "deferred"},
    {"state": "completed", "task_usage_projection": "uncertain"},
])
def test_native_unresolved_effect_or_task_usage_cannot_prove_completion_or_authorize_resume(device_db, invocation):
    from app.db.models import VentureDocument
    d = device_db
    _order(d)
    with Session(d.engine) as session:
        row = session.get(VentureDocument, "dwo_device")
        body = json.loads(row.provenance_json)
        # Native admission and Task projection persist these source-owned states.
        body["invocations"] = {"call_device": invocation}
        row.provenance_json = json.dumps(body)
        session.commit()
    invoke(d, d.module.attach_order, "dev_one", "dwo_device")
    result = invoke(d, d.module.attach_stop_receipt, "dev_one", "dwo_device")
    assert not result["accepted"] and result["state"] == "reconciliation_required"
    assert saved(d)[0] == "input_required" and saved(d)[3]["effect_outcome"] == "uncertain"
    assert claim(d)["reason"] == "reconciliation_required"


def test_order_must_match_original_step_input_and_device(device_db):
    from app.db.task_models import TaskStep
    d = device_db
    _order(d)
    with pytest.raises(KeyError, match="device_order_not_found"):
        invoke(d, d.module.attach_order, "dev_two", "dwo_device")
    with Session(d.engine) as session:
        session.get(TaskStep, "tst_device").inputs_json = "{}"
        session.commit()
    with pytest.raises(PermissionError, match="order_unbound"):
        invoke(d, d.module.attach_order, "dev_one", "dwo_device")


@pytest.mark.parametrize("state,reason", [("paused", None), ("approval_required", "needs_approval"),
                                        ("input_required", "budget_exhausted")])
def test_verified_stop_cannot_resume_paused_or_other_blocked_work(device_db, state, reason):
    from app.db.task_models import Task, TaskRun
    d = device_db
    _order(d)
    with Session(d.engine) as session:
        task = session.get(Task, "tsk_device")
        task.state = session.get(TaskRun, "trn_device").state = state
        task.blocker_json = json.dumps({"class": reason}) if reason else None
        session.commit()
    invoke(d, d.module.attach_order, "dev_one", "dwo_device")
    assert invoke(d, d.module.attach_stop_receipt, "dev_one", "dwo_device")["state"] == "completed"
    assert saved(d)[0] == state


def test_uncertain_stop_cannot_resurrect_canceled_task(device_db):
    from app.db.task_models import Task, TaskRun, TaskStep
    d = device_db
    _order(d, uncertain=True)
    invoke(d, d.module.attach_order, "dev_one", "dwo_device")
    with Session(d.engine) as session:
        session.get(Task, "tsk_device").state = "canceled"
        session.get(TaskRun, "trn_device").state = "canceled"
        session.get(TaskStep, "tst_device").state = "canceled"
        session.commit()
    result = invoke(d, d.module.attach_stop_receipt, "dev_one", "dwo_device")
    assert not result["accepted"] and saved(d)[:2] == ("canceled", "canceled")


def test_checkpoint_reference_cannot_cross_venture(device_db):
    from app.db.models import VentureDocument
    d = device_db
    lease = claim(d)
    with Session(d.engine) as session:
        session.add(VentureDocument(id="doc_other", venture_id="ven_other", kind="device_evidence", title="Synthetic",
            provenance_json="{}", created_at=NOW, updated_at=NOW))
        session.commit()
    with pytest.raises(PermissionError, match="evidence_scope_mismatch"):
        fenced(d, d.module.checkpoint_step, lease, evidence_refs=["venture_document:doc_other"])
