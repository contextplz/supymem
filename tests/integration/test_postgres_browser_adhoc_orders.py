"""Signed, migrated browser reviews without fabricated planning authority.

Model calls are forbidden. These tests qualify server review/credit/receipt
custody, not installed browser execution or semantic goal completion.
"""

from dataclasses import replace
import json
import secrets

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401
from tests.unit.product.test_device_browser_ai import configure, request_for, response


GOAL = "Open https://example.test/ and report the main headline text on the page. Read only; do not click any buttons or submit anything."


@pytest.fixture
def browser_review(journey_db, monkeypatch):
    from app.admin.authn import CallerContext
    from app.api.device_orders_api import router
    from app.build import device_browser_ai as ai, device_orders as orders
    from app.db.models import CreditAccount
    from core.assertion import sign_assertion
    from core.clock import utcnow
    from core.ids import new_id

    d = journey_db
    profile = configure(monkeypatch)
    monkeypatch.delenv("DEVICE_DESKTOP_EXECUTION_ENABLED", raising=False)
    monkeypatch.setattr(ai, "_provider_call", lambda *_: pytest.fail("Review must not call a model"))
    secret = secrets.token_hex(32)
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    caller = CallerContext(d["actor"], "", "user", "/test", "POST", 9999999999,
                           "test", device_id="dev_browser")
    with d["factory"]() as session:
        session.add(CreditAccount(id=new_id("cra"), workspace_id=d["wid"], venture_id=d["vid"],
            scope="venture", granted_micro=1_000_000, spent_micro=0, reserved_micro=0,
            status="active", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        def request(method, suffix, body=None, *, actor=None, device="dev_browser", vid=None):
            path = f"/ventures/{vid or d['vid']}/{suffix}"
            assertion = sign_assertion(secret, sub=actor or d["actor"], email="", role="user",
                                      path=path, method=method, device_id=device)
            return client.request(method, path, json=body, headers={"X-COS-Assertion": assertion})

        d.update(orders=orders, profile=profile, caller=caller, request=request,
                 body={"instruction": GOAL, "browser_execution_profile_digest": ai.profile_digest(profile)})
        yield d


def prepared(d):
    result = d["request"]("POST", "device-work-orders/from-chat", d["body"])
    assert result.status_code == 200, result.json()
    return result.json()


def moved(d, order, action):
    result = d["request"]("POST", f"device-work-orders/{order['id']}/{action}",
                          {"review_digest": order["review_digest"],
                           **({"read_only_parser_supported": True} if action == "approve" else {})})
    assert result.status_code == 200, result.json()
    return result.json()


def test_signed_no_plan_review_is_idempotent_and_claim_requires_permission_and_credits(browser_review):
    from app.chat.store import idempotent_message
    from app.db.models import CreditAccount, CreditReservation
    from app.db.supymem_models import ExecutionPlan, FounderAction, PlanTask

    d = browser_review
    order = prepared(d)
    assert order["manifest"]["task_id"] == f"founder_browser_{d['vid']}"
    assert order["task_snapshot"] == {
        "kind": "founder_browser_request", "task_id": order["manifest"]["task_id"],
        "venture_id": d["vid"], "workspace_id": d["wid"],
        "venture_created_at": order["task_snapshot"]["venture_created_at"],
    }
    assert GOAL in order["manifest"]["instruction"]
    assert prepared(d)["id"] == order["id"]
    assert idempotent_message(d["vid"], "user", f"mac-review:{order['id']}:user")["body_md"] == GOAL
    assert d["request"]("POST", f"device-work-orders/{order['id']}/claim",
                         {"review_digest": order["review_digest"]}).status_code == 409
    with d["factory"]() as session:
        assert session.query(CreditReservation).filter_by(request_id=order["id"]).count() == 0
        assert all(session.query(model).filter_by(venture_id=d["vid"]).count() == 0
                   for model in (ExecutionPlan, PlanTask, FounderAction))
    approved = moved(d, order, "approve")
    with d["factory"]() as session:
        account = session.query(CreditAccount).filter_by(venture_id=d["vid"], scope="venture").one()
        hold = session.query(CreditReservation).filter_by(request_id=order["id"]).one()
        assert account.reserved_micro == hold.amount_micro == 500_000
        assert hold.status == "active" and hold.request_id == approved["id"]
    claimed = moved(d, approved, "claim")
    assert claimed["execution_id"] and claimed["task_snapshot"] == order["task_snapshot"]
    assert d["request"]("POST", f"device-work-orders/{order['id']}/claim",
                         {"review_digest": claimed["review_digest"]}).status_code == 409
    with d["factory"]() as session:
        assert session.query(CreditReservation).filter_by(request_id=order["id"]).one().status == "reconciling"
    assert d["request"]("POST", f"device-work-orders/{order['id']}/renew").status_code == 409


def test_cancelled_unstarted_review_renews_saved_goal_and_releases_old_hold(browser_review):
    from app.db.models import CreditAccount, CreditReservation

    d = browser_review
    old = moved(d, prepared(d), "approve")
    moved(d, old, "cancel")
    result = d["request"]("POST", f"device-work-orders/{old['id']}/renew")
    assert result.status_code == 200, result.json()
    new = result.json()
    assert new["id"] != old["id"] and new["state"] == "pending"
    assert new["manifest"] == old["manifest"] and new["task_snapshot"] == old["task_snapshot"]
    assert new["review_digest"] != old["review_digest"]
    with d["factory"]() as session:
        assert session.query(CreditAccount).filter_by(venture_id=d["vid"], scope="venture").one().reserved_micro == 0
        assert session.query(CreditReservation).filter_by(request_id=old["id"]).one().status == "released"


@pytest.mark.parametrize("mutation", ["foreign_actor", "other_device", "archived", "changed_review"])
def test_saved_adhoc_review_cannot_borrow_foreign_or_retired_authority(browser_review, mutation):
    from app.db.models import CreditReservation, Venture, VentureDocument
    from core.clock import utcnow

    d = browser_review
    order = prepared(d)
    actor, device = None, "dev_browser"
    if mutation == "foreign_actor":
        from app.authz.workspace import ensure_subject_person
        actor = "usr_adhoc_other"
        ensure_subject_person(actor)
    elif mutation == "other_device":
        device = "dev_other"
    else:
        with d["factory"]() as session:
            if mutation == "archived":
                session.get(Venture, d["vid"]).archived_at = utcnow()
            else:
                row = session.get(VentureDocument, order["id"])
                value = json.loads(row.provenance_json)
                value["task_snapshot"]["workspace_id"] = "foreign_workspace"
                row.provenance_json = json.dumps(value)
            session.commit()
    result = d["request"]("POST", f"device-work-orders/{order['id']}/approve",
                         {"review_digest": order["review_digest"],
                          "read_only_parser_supported": True}, actor=actor, device=device)
    assert result.status_code in {403, 404, 409}
    with d["factory"]() as session:
        assert session.query(CreditReservation).filter_by(request_id=order["id"]).count() == 0


def test_credit_refusal_keeps_unapproved_review_without_charge(browser_review):
    from app.db.models import CreditAccount, CreditReservation

    d = browser_review
    order = prepared(d)
    with d["factory"]() as session:
        session.query(CreditAccount).filter_by(venture_id=d["vid"], scope="venture").one().granted_micro = 499_999
        session.commit()
    result = d["request"]("POST", f"device-work-orders/{order['id']}/approve",
                         {"review_digest": order["review_digest"],
                          "read_only_parser_supported": True})
    assert result.status_code == 402
    assert d["orders"].retrieve(d["vid"], d["caller"], order["id"])["state"] == "pending"
    with d["factory"]() as session:
        assert session.query(CreditReservation).filter_by(request_id=order["id"]).count() == 0


@pytest.mark.parametrize("unknown", [False, True])
def test_adhoc_stop_settles_known_usage_or_keeps_unknown_without_plan_completion(browser_review, monkeypatch, unknown):
    from app.build import device_browser_ai as ai, device_invocations as calls, device_stop
    from app.db.models import CreditAccount, CreditReservation
    from app.db.supymem_models import ExecutionPlan, FounderAction, PlanTask

    d = browser_review
    order = moved(d, moved(d, prepared(d), "approve"), "claim")
    args = (d["vid"], d["caller"], order["id"])
    attempts = []

    def provider(_body):
        attempts.append(1)
        if unknown:
            raise TimeoutError("synthetic unknown transport")
        return response('{"subgoals":["Read headline"]}')

    monkeypatch.setattr(ai, "_provider_call", provider)
    phase = request_for(order, phase="plan", invocation="one_call")
    phase_result = d["request"]("POST", f"device-work-orders/{order['id']}/browser-ai-call", phase.model_dump())
    assert phase_result.status_code == (409 if unknown else 200), phase_result.json()
    if unknown:
        assert phase_result.json()["detail"]["code"] == "device_browser_ai_provider_or_usage_unconfirmed"
    else:
        assert phase_result.json()["task_verified"] is False
        assert phase_result.json()["effect_proposals"] == []
    assert attempts == [1]
    value = d["orders"].retrieve(*args)["invocations"]["one_call"]
    assert value["state"] == ("uncertain" if unknown else "completed")
    actual = 0 if unknown else value["actual_micro"]
    with pytest.raises(ValueError, match="device_stop_verification_invalid"):
        device_stop.record(*args, execution_id=order["execution_id"], evidence_digest="b" * 64,
                           processes_stopped=True, task_verified=True)
    result = device_stop.record(*args, execution_id=order["execution_id"], evidence_digest="b" * 64,
                                processes_stopped=True, task_verified=False, reason_code="cu_outcome_review_required")
    assert not result["native_stop"].get("task_verified")
    assert result["state"] == ("claimed" if unknown else "stopped")
    with d["factory"]() as session:
        hold = session.query(CreditReservation).filter_by(request_id=order["id"]).one()
        account = session.query(CreditAccount).filter_by(venture_id=d["vid"], scope="venture").one()
        assert hold.status == ("reconciling" if unknown else "settled")
        assert account.reserved_micro == (500_000 if unknown else 0)
        assert account.spent_micro == actual
        assert all(session.query(model).filter_by(venture_id=d["vid"]).count() == 0
                   for model in (ExecutionPlan, PlanTask, FounderAction))
    if unknown:
        with pytest.raises(ValueError, match="device_invocation_budget_unavailable"):
            calls.admit(*args, invocation_id="no_retry", request_digest="c" * 64, quote_micro=100)


@pytest.mark.parametrize("open_action", [False, True])
def test_existing_approved_plan_still_requires_its_real_current_task(browser_review, open_action):
    from app.db.supymem_models import ExecutionPlan, FounderAction, PlanMilestone, PlanTask
    from core.clock import utcnow
    from core.ids import new_id

    d = browser_review
    pid, mid, tid = new_id("pln"), new_id("plm"), new_id("plt")
    with d["factory"]() as session:
        session.add(ExecutionPlan(id=pid, workspace_id=d["wid"], venture_id=d["vid"], version=1,
            state="approved", approved_at=utcnow(), approved_by=d["actor"], created_at=utcnow(), structured="{}"))
        session.flush()
        session.add(PlanMilestone(id=mid, plan_id=pid, workspace_id=d["wid"], venture_id=d["vid"],
            order=1, title="Review", kind="validation", scope="in"))
        session.flush()
        session.add(PlanTask(id=tid, milestone_id=mid, workspace_id=d["wid"], venture_id=d["vid"],
            order=1, title="Current review", state="pending", created_at=utcnow()))
        if open_action:
            session.add(FounderAction(id=new_id("fact"), workspace_id=d["wid"], venture_id=d["vid"],
                plan_task_id=tid, title="Review", steps="[]", required_secret_slots="[]", state="open",
                blocks_task=True, created_at=utcnow()))
        session.commit()
    result = d["request"]("POST", "device-work-orders/from-chat", d["body"])
    if open_action:
        assert result.status_code == 200, result.json()
        assert result.json()["manifest"]["task_id"] == tid
        assert result.json()["task_snapshot"]["plan_id"] == pid
    else:
        assert result.status_code == 409
        assert result.json()["detail"]["code"] == "device_terminal_task_unavailable"


def test_reserved_browser_identity_cannot_authorize_typed_or_native_work(browser_review):
    d = browser_review
    order = prepared(d)
    moved(d, order, "cancel")
    manifest = {**order["manifest"]}
    manifest.pop("general_browser_verification")
    manifest["browser_verification"] = {"expected_locations": ["https://example.test/"]}
    result = d["request"]("POST", "device-work-orders", manifest)
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "device_task_not_ready"
    with d["factory"]() as session:
        with pytest.raises(ValueError, match="device_task_not_ready"):
            d["orders"]._review_task_snapshot(session, d["wid"], d["vid"], {
                "task_id": manifest["task_id"], "desktop_verification": {},
                "actions": ["desktop.read", "desktop.interact"], "applications": ["com.google.Chrome"],
            })
    with pytest.raises(PermissionError, match="authenticated_device_required"):
        d["orders"].prepare_general_browser_from_chat(d["vid"], replace(d["caller"], device_id=None),
                                                     GOAL, d["profile"])
