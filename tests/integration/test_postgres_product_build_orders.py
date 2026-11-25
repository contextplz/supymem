"""Migrated PostgreSQL serializes device reviews and exact receipt replays."""
import json
from dataclasses import replace

import pytest
from fastapi import HTTPException

from tests.integration.test_postgres_auth_security import pg_database as pg_database, concurrent
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db
from tests.unit.product.test_product_build_orders import manifest, plan_packet, iteration


@pytest.fixture
def product_claim(journey_db, monkeypatch):
    from app.admin.authn import CallerContext
    from app.build import device_orders as svc
    from app.db.models import CreditAccount
    from core.ids import new_id
    d = journey_db
    monkeypatch.setenv("SUPYMEM_CODING_LOOP_ENABLED", "1")
    monkeypatch.setenv("SUPYMEM_CODING_LOOP_ALLOWLIST", json.dumps([d["actor"]]))
    caller = CallerContext(d["actor"], "", "user", "/test", "POST", 9999999999, "test", device_id="dev_one")
    with d["factory"]() as session:
        session.add(CreditAccount(id=new_id("cra"), workspace_id=d["wid"], venture_id=d["vid"],
            scope="venture", granted_micro=10000, spent_micro=0, reserved_micro=0, status="active",
            created_at=svc.utcnow(), updated_at=svc.utcnow()))
        session.commit()
    order = svc.prepare(d["vid"], caller, manifest(svc, d["vid"]))
    approved = svc.transition(d["vid"], caller, order["id"], order["review_digest"], "approve")
    claimed = svc.transition(d["vid"], caller, order["id"], approved["review_digest"], "claim")
    return d, svc, caller, claimed


def test_duplicate_plan_and_iteration_writers_commit_one_exact_receipt(product_claim):
    d, svc, caller, body = product_claim
    args = (d["vid"], caller, body["id"])
    plan = svc.ProductPlanReceipt.model_validate(plan_packet(body))
    plans = concurrent(lambda: svc.record_product_plan(*args, plan))
    assert plans[0] == plans[1] == svc.product_plan(*args)["plan"]
    rows = concurrent(lambda: svc.record_coding_iteration(*args, iteration(svc, body)))
    assert rows[0] == rows[1]
    assert svc.coding_iterations(*args)["iterations"] == [rows[0]]


def test_postgres_replay_conflict_foreign_identity_and_zero_settlement(product_claim, monkeypatch):
    from app.build import device_stop, device_invocations
    from app.db.models import CreditAccount, CreditReservation
    d, svc, caller, body = product_claim
    args = (d["vid"], caller, body["id"])
    saved = svc.record_product_plan(*args, svc.ProductPlanReceipt.model_validate(plan_packet(body)))
    with pytest.raises(ValueError, match="conflict"):
        svc.record_product_plan(*args, svc.ProductPlanReceipt.model_validate(plan_packet(body, spec="Altered /api/health plan")))
    for other in (replace(caller, device_id="other"), replace(caller, sub="usr_foreign")):
        with pytest.raises((PermissionError, KeyError, HTTPException)):
            svc.record_product_plan(d["vid"], other, body["id"], svc.ProductPlanReceipt.model_validate(plan_packet(body)))
    stopped = device_stop.record(*args, execution_id=body["execution_id"], evidence_digest="e" * 64,
        processes_stopped=True)
    monkeypatch.delenv("SUPYMEM_CODING_LOOP_ENABLED")
    assert svc.record_product_plan(*args, svc.ProductPlanReceipt.model_validate(plan_packet(body))) == saved
    svc.record_coding_iteration(*args, iteration(svc, body))
    repeated = device_stop.record(*args, execution_id=body["execution_id"], evidence_digest="e" * 64,
        processes_stopped=True)
    assert repeated["settlement_ledger_id"] == stopped["settlement_ledger_id"]
    with pytest.raises(ValueError):
        device_invocations.check_current(*args)
    with d["factory"]() as session:
        hold = session.get(CreditReservation, body["reservation_id"])
        account = session.get(CreditAccount, hold.account_id)
        assert hold.status == "settled" and hold.amount_micro == 0
        assert account.spent_micro == account.reserved_micro == 0


def test_postgres_current_confirmed_idea_is_bound_to_plan_review(product_claim):
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from core.ids import new_id
    d, svc, caller, body = product_claim
    with d["factory"]() as session:
        rid = new_id("idv")
        session.add(IdeaRevision(id=rid, session_id=d["sid"], workspace_id=d["wid"], venture_id=d["vid"],
            rev=2, source="founder", structured=json.dumps({"offer": "A changed confirmed product"}),
            created_at=svc.utcnow()))
        session.flush()
        idea = session.get(IdeaSession, d["sid"])
        idea.current_revision_id = idea.confirmed_revision_id = rid
        session.commit()
    with pytest.raises(ValueError, match="device_order_task_changed"):
        from app.build import device_invocations
        device_invocations.check_current(d["vid"], caller, body["id"])


def test_approved_core_plan_expectations_join_the_review_snapshot(product_claim):
    from app.db.supymem_models import ExecutionPlan, PlanMilestone, PlanTask
    from core.ids import new_id
    d, svc, caller, body = product_claim
    pid, mid, tid = new_id("pln"), new_id("plm"), new_id("plt")
    with d["factory"]() as session:
        session.add(ExecutionPlan(id=pid, workspace_id=d["wid"], venture_id=d["vid"], version=1,
            state="approved", approved_at=svc.utcnow(), approved_by=d["actor"], created_at=svc.utcnow(), structured="{}"))
        session.flush()
        session.add(PlanMilestone(id=mid, plan_id=pid, workspace_id=d["wid"], venture_id=d["vid"],
            order=1, title="Core product", kind="core", scope="in", acceptance='["Split the total correctly"]'))
        session.flush()
        session.add(PlanTask(id=tid, milestone_id=mid, workspace_id=d["wid"], venture_id=d["vid"], order=1,
            title="Tip splitter", description="Saves recent splits between devices", state="pending",
            tool_hints='["app.iterate"]', created_at=svc.utcnow()))
        session.commit()
    with d["factory"]() as session:
        snapshot = svc._product_build_snapshot(session, d["wid"], d["vid"])
    assert snapshot["execution_plan"]["plan_id"] == pid
    assert snapshot["execution_plan"]["tasks"][0]["acceptance"] == ["Split the total correctly"]
    assert any("shared database" in line for line in snapshot["product_scope"]["not_in_this_build"])
    with pytest.raises(ValueError, match="device_order_task_changed"):
        from app.build import device_invocations
        device_invocations.check_current(d["vid"], caller, body["id"])


@pytest.mark.parametrize("journey_db", [{"offer": "Maintenance checklists for laboratories"},
    {"offer": "Exercise timer for runners"}], indirect=True, ids=["b2b", "consumer"])
def test_local_requested_build_is_approved_and_claimed_once_without_wallet(journey_db, monkeypatch):
    from desktop_runtime import agent_status
    from app.admin.authn import CallerContext
    from app.build import device_orders as svc
    from app.coding_agents_consent import CodingAgentConsent
    from app.core.local_secrets import update_settings
    from app.db.models import CreditAccount, CreditReservation, Workspace
    from app.workspaces.resolve import local_person
    from sqlalchemy import select

    d = journey_db
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    from app.core.local_owner import _ensure_proxy_secret
    _ensure_proxy_secret()
    update_settings("capabilities", {"background_work": False}, {})
    update_settings("spend", {"monthly_spend_cap": 0}, {})
    monkeypatch.setattr(agent_status, "statuses", lambda: [{"name": "codex", "installed": True,
        "signed_in": True, "subscription_login": True, "available": True}])
    with d["factory"]() as session:
        actor = local_person(session).id
        session.get(Workspace, d["wid"]).owner_id = actor
        if session.scalar(select(CodingAgentConsent).where(CodingAgentConsent.founder_id == actor,
                CodingAgentConsent.provider == "codex")) is None:
            session.add(CodingAgentConsent(id="local_consent", founder_id=actor, provider="codex", consented_at=svc.utcnow()))
        session.commit()
    caller = CallerContext(actor, "", "user", "/test", "POST", 0, "")
    plans = concurrent(lambda: svc.prepare_product_build_from_chat(d["vid"], caller, "Build the app with my Codex"))
    assert all(plan["id"] == plans[0]["id"] and plan["state"] == "approved" for plan in plans)
    plan = plans[0]

    def attempt_claim():
        try:
            return svc.transition(d["vid"], caller, plan["id"], plan["review_digest"], "claim")
        except ValueError as error:
            return str(error)

    claims = concurrent(attempt_claim)
    assert len([result for result in claims if isinstance(result, dict) and result["state"] == "claimed"]) == 1
    assert claims.count("device_order_transition_invalid") == len(claims) - 1
    for other in (replace(caller, sub="other_owner"), replace(caller, device_id="other_device")):
        with pytest.raises((KeyError, PermissionError, HTTPException)):
            svc.transition(d["vid"], other, plan["id"], plan["review_digest"], "claim")
    with d["factory"]() as session:
        assert session.scalar(select(CreditAccount.id).where(CreditAccount.venture_id == d["vid"])) is None
        assert session.scalar(select(CreditReservation.id).where(CreditReservation.venture_id == d["vid"])) is None
