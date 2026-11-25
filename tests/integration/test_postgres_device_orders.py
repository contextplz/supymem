"""Real PostgreSQL claim contention and tenant access for device work orders."""
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from fastapi import HTTPException

from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db


@pytest.fixture(autouse=True)
def explicit_native_admission(monkeypatch, journey_db):
    # Native durability tests opt in; default-off coverage clears this explicitly.
    monkeypatch.setenv("DEVICE_DESKTOP_EXECUTION_ENABLED", "1")
    from app.build.device_desktop_pilot import ALLOWLIST_ENV
    from app.db.supymem_models import DesktopDevice
    d = journey_db
    monkeypatch.setenv(ALLOWLIST_ENV, json.dumps([[d["actor"], "dev_one"]]))
    with d["factory"]() as session:
        row = session.get(DesktopDevice, "dev_one")
        if row is None:
            row = DesktopDevice(id="dev_one", person_id=d["actor"], name="Synthetic owned Mac", platform="macos")
            session.add(row)
        row.person_id, row.revoked_at = d["actor"], None
        row.app_version = "0.1.84"
        session.commit()


def prepare(d, *, credit_ceiling_micro=1000, actions=None, plan_state="approved",
            terminal=False, desktop=False, coding=None, duration_seconds=60):
    from app.admin.authn import CallerContext
    from app.build import device_orders as svc
    caller = CallerContext(d["actor"], "", "user", "/test", "POST", 9999999999, "test", device_id="dev_one")
    manifest = svc.Manifest(task_id="test_task", title="Read approved page", instruction="Read page",
        applications=["com.companyos.app"] if terminal or coding else ["com.google.Chrome"] if desktop else ["com.apple.Safari"],
        origins=[] if terminal or desktop or coding else ["https://example.test"],
        actions=["editor.edit", "terminal.execute"] if coding else ["terminal.execute"] if terminal else ["desktop.read", "desktop.interact"] if desktop else (actions or ["browser.read"]),
        terminal_verification={"operation": "git_status_v1", "workspace_slug": "test-venture"}
        if terminal else None,
        desktop_verification={"operation": "desktop_goal_v1", "application_bundle": "com.google.Chrome",
            "process_id": 42, "process_start_identity": "123456", "window_id": 77,
            "window_title": "New Tab", "delivery_mode": "background", "max_steps": 4,
            "reviewed_text": ["google.com"]} if desktop else None,
        coding_verification={"operation": "workspace_edit_v1", "provider": coding,
            "workspace_slug": "test-venture", "read_scope": "device_filesystem",
            "write_scope": "venture_workspace", "command_network": "disabled",
            "offline_profile": "static_html_v1"} if coding else None,
        credit_ceiling_micro=credit_ceiling_micro, duration_seconds=duration_seconds)
    from app.db.supymem_models import ExecutionPlan, PlanMilestone, PlanTask
    from app.db.models import CreditAccount
    from core.clock import utcnow
    from core.ids import new_id
    pid, mid, tid = new_id("pln"), new_id("plm"), new_id("plt")
    structured = None
    if coding:
        import json
        from app.builder.expectations import compile_inline
        acceptance = ["Reviewed page visible"]
        expectations = compile_inline({"product_name": "Reviewed page", "evidence": [{
            "summary": acceptance[0], "required_text": acceptance, "actions": []}]}, acceptance)
        structured = json.dumps({"proposal": {"milestones": [{"key": "landing", "tasks": [{
            "key": "page", "description": "Reviewed static page", "acceptance": acceptance,
            "landing_expectations": expectations}]}]}, "entity_ids": {"tasks": {"landing:page": tid}}})
    with d["factory"]() as session:
        session.add(CreditAccount(id=new_id("cra"), workspace_id=d["wid"], venture_id=d["vid"],
            scope="venture", granted_micro=10000, spent_micro=0, reserved_micro=0, status="active",
            created_at=utcnow(), updated_at=utcnow()))
        session.add(ExecutionPlan(id=pid, workspace_id=d["wid"], venture_id=d["vid"], version=1,
            state=plan_state, approved_at=utcnow(), approved_by=d["actor"], created_at=utcnow(), structured=structured or "{}"))
        session.flush()
        session.add(PlanMilestone(id=mid, plan_id=pid, workspace_id=d["wid"], venture_id=d["vid"],
            order=1, title="Review", kind="landing" if coding else "validation", scope="in",
            acceptance='["Reviewed page visible"]' if coding else None))
        session.flush()
        session.add(PlanTask(id=tid, milestone_id=mid, workspace_id=d["wid"], venture_id=d["vid"],
            order=1, title="Review page", state="pending", created_at=utcnow()))
        session.commit()
    manifest.task_id = tid
    order = svc.prepare(d["vid"], caller, manifest)
    return svc, caller, order


def test_two_device_claims_admit_only_one_execution(journey_db):
    d = journey_db
    svc, caller, order = prepare(d)
    args = (d["vid"], caller, order["id"], order["review_digest"])
    svc.transition(*args, "approve")
    barrier = Barrier(2)
    def claim(_):
        barrier.wait(timeout=5)
        try:
            return svc.transition(*args, "claim")["state"]
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, [1, 2]))
    assert outcomes.count("claimed") == 1
    assert outcomes.count("device_order_transition_invalid") == 1


def test_signed_native_pilot_rechecks_pair_device_review_account_and_hold(journey_db, monkeypatch):
    from fastapi.testclient import TestClient
    from core.assertion import sign_assertion
    from app.build import device_desktop_pilot as pilot, device_invocations as calls
    from app.db.models import CreditAccount, CreditReservation, VentureDocument
    from app.db.supymem_models import DesktopDevice
    d = journey_db
    svc, caller, order = prepare(d, desktop=True)
    secret = "signed-native-pilot-fixture-more-than-thirty-two-bytes"
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    monkeypatch.setenv("ADMIN_REQUIRE_TOTP", "1")
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "ops@example.test")
    monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", "native-pilot-fixture-vault-more-than-thirty-two-bytes")
    monkeypatch.setenv("DEVICE_BROWSER_EXECUTION_ENABLED", "1")
    from app import main
    client = TestClient(main.create_app())
    base = f"/ventures/{d['vid']}/device-work-orders"
    path = base + "/" + order["id"]
    def request(method, target, *, device="dev_one", actor=d["actor"], body=None):
        token = sign_assertion(secret, sub=actor, email="", role="user", path=target, method=method, device_id=device)
        return client.request(method, target, headers={"X-COS-Assertion": token}, json=body)
    assert client.get(path).status_code == 401
    assert request("GET", path, actor="usr_foreign").status_code == 404
    assert request("GET", path + "/capabilities").json()["available"] is True
    assert request("GET", path + "/capabilities", device="dev_other").json()["available"] is False
    for pair in ([], [[d["actor"], "dev_other"]], [["usr_foreign", "dev_one"]]):
        monkeypatch.setenv(pilot.ALLOWLIST_ENV, json.dumps(pair))
        refused = request("POST", base, body=order["manifest"])
        assert refused.status_code == 409 and refused.json()["detail"]["code"] == svc.DESKTOP_EXECUTION_UNAVAILABLE
        assert request("GET", path + "/capabilities").json()["available"] is False
        for action in ("approve", "claim"):
            assert request("POST", path + "/" + action, body={"review_digest": order["review_digest"]}).status_code == 409
        with d["factory"]() as session:
            assert session.get(VentureDocument, order["id"]).provenance_json == json.dumps(order)
            assert not session.query(CreditReservation).filter_by(request_id=order["id"]).first()
    monkeypatch.setenv(pilot.ALLOWLIST_ENV, json.dumps([[d["actor"], "dev_one"]]))
    approved = request("POST", path + "/approve", body={"review_digest": order["review_digest"]})
    assert approved.status_code == 200, approved.text
    digest = approved.json()["review_digest"]
    assert request("POST", path + "/claim", body={"review_digest": "0" * 64}).status_code == 409
    claimed = request("POST", path + "/claim", body={"review_digest": digest})
    assert claimed.status_code == 200, claimed.text
    args = (d["vid"], caller, order["id"])
    calls.admit(*args, invocation_id="already_admitted", request_digest="a" * 64, quote_micro=100)
    with d["factory"]() as session:
        saved = json.loads(session.get(VentureDocument, order["id"]).provenance_json)
        hold = session.get(CreditReservation, saved["reservation_id"])
        account_id = hold.account_id
        hold.status = "settled"
        session.commit()
    assert request("GET", path + "/authority").status_code == 409
    with d["factory"]() as session:
        session.get(CreditReservation, saved["reservation_id"]).status = "reconciling"
        session.get(CreditAccount, account_id).status = "suspended"
        session.commit()
    assert request("GET", path + "/authority").status_code == 409
    with d["factory"]() as session:
        session.get(CreditAccount, account_id).status = "active"
        session.get(DesktopDevice, "dev_one").revoked_at = svc.utcnow()
        session.commit()
    assert request("GET", path + "/authority").status_code == 409
    assert request("GET", path + "/capabilities").json()["available"] is False
    with d["factory"]() as session:
        session.get(DesktopDevice, "dev_one").revoked_at = None
        row = session.get(VentureDocument, order["id"])
        changed = json.loads(row.provenance_json)
        changed["manifest"]["instruction"] += " changed after review"
        row.provenance_json = json.dumps(changed)
        session.commit()
    assert request("GET", path + "/authority").status_code == 409
    with d["factory"]() as session:
        row = session.get(VentureDocument, order["id"])
        # Restore the real admitted receipt, not a fabricated settled result.
        row.provenance_json = json.dumps(saved)
        session.commit()
    monkeypatch.delenv(svc.DESKTOP_EXECUTION_ENV)
    assert request("GET", path + "/authority").status_code == 409
    calls.record_result(*args, invocation_id="already_admitted", actual_micro=42)
    assert request("POST", path + "/cancel", body={"review_digest": digest}).status_code == 200


@pytest.mark.parametrize("stage", ["pending", "approved", "claimed"])
def test_native_default_off_retires_old_postgres_cards_without_hiding_cancel_or_receipts(journey_db, monkeypatch, stage):
    from app.build import device_invocations as calls
    from app.db.models import CreditAccount
    d = journey_db
    svc, caller, order = prepare(d, desktop=True)
    args = (d["vid"], caller, order["id"])
    if stage in {"approved", "claimed"}:
        svc.transition(*args, order["review_digest"], "approve")
    if stage == "claimed":
        svc.transition(*args, order["review_digest"], "claim")
        calls.admit(*args, invocation_id="owned_receipt", request_digest="a" * 64, quote_micro=100)
    before = svc.retrieve(*args)
    with d["factory"]() as session:
        balance = [(a.reserved_micro, a.spent_micro) for a in session.query(CreditAccount).all()]
    monkeypatch.delenv(svc.DESKTOP_EXECUTION_ENV)
    for action in ("approve", "claim"):
        with pytest.raises(ValueError, match="^device_desktop_execution_unavailable$"):
            svc.transition(*args, order["review_digest"], action)
    if stage == "claimed":
        with pytest.raises(ValueError, match="^device_desktop_execution_unavailable$"):
            calls.check_current(*args)
        with pytest.raises(ValueError, match="^device_desktop_execution_unavailable$"):
            calls.admit(*args, invocation_id="restart", request_digest="b" * 64, quote_micro=100)
    assert svc.retrieve(*args) == before
    with d["factory"]() as session:
        assert balance == [(a.reserved_micro, a.spent_micro) for a in session.query(CreditAccount).all()]
    if stage == "claimed":
        calls.record_result(*args, invocation_id="owned_receipt", actual_micro=42)
        assert svc.retrieve(*args)["invocations"]["owned_receipt"]["actual_micro"] == 42
    assert svc.transition(*args, order["review_digest"], "cancel")["state"] == "cancelled"


def test_chat_style_recovery_returns_same_claimed_review(journey_db):
    d = journey_db
    svc, caller, order = prepare(d)
    args = (d["vid"], caller, order["id"], order["review_digest"])
    svc.transition(*args, "approve")
    svc.transition(*args, "claim")
    manifest = svc.Manifest(**order["manifest"])
    assert svc.prepare(d["vid"], caller, manifest, recover_active=True)["id"] == order["id"]
    with pytest.raises(ValueError, match="active_order"):
        svc.prepare(d["vid"], caller, manifest)


def test_prepare_does_not_enqueue_review_notice(journey_db):
    from app.desktop_notices import pending

    d = journey_db
    svc, caller, order = prepare(d)
    notices = [item for item in pending(workspace_ids={d["wid"]})
               if item["kind"] == "device_work_order_review"]
    assert notices == []

    recovered = svc.prepare(
        d["vid"], caller, svc.Manifest(**order["manifest"]), recover_active=True
    )
    assert recovered["id"] == order["id"]
    notices = [item for item in pending(workspace_ids={d["wid"]})
               if item["kind"] == "device_work_order_review"]
    assert notices == []


def test_unclaimed_cancel_enqueues_cancelled_notice(journey_db):
    from app.desktop_notices import pending

    d = journey_db
    svc, caller, order = prepare(d)
    svc.transition(d["vid"], caller, order["id"], order["review_digest"], "cancel")
    notices = [item for item in pending(workspace_ids={d["wid"]})
               if item["kind"] == "device_task_cancelled"]
    assert len(notices) == 1
    assert notices[0]["target_path"] == (
        f"/v/{d['vid']}/device-work-orders/{order['id']}"
    )
    assert notices[0]["title"] == "Order board: Saved notice: Mac task cancelled: Read approved page"


def test_native_stop_settles_known_usage_once_under_contention(journey_db):
    from app.build import device_invocations as calls, device_stop
    from app.credits.accounts import get_account
    from app.db.models import CreditLedgerEntry
    from sqlalchemy import select
    d = journey_db
    svc, caller, order = prepare(d)
    args = (d["vid"], caller, order["id"])
    svc.transition(*args, order["review_digest"], "approve")
    claimed = svc.transition(*args, order["review_digest"], "claim")
    calls.admit(*args, invocation_id="decision1", request_digest="a" * 64, quote_micro=500)
    calls.record_result(*args, invocation_id="decision1", actual_micro=300)
    svc.transition(*args, order["review_digest"], "cancel")
    barrier = Barrier(2)
    def stop(_):
        barrier.wait(timeout=5)
        return device_stop.record(*args, execution_id=claimed["execution_id"],
            evidence_digest="b" * 64, processes_stopped=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(stop, [1, 2]))
    assert all(value["state"] == "stopped" for value in results)
    assert results[0]["settlement_ledger_id"] == results[1]["settlement_ledger_id"]
    account = get_account(d["vid"])
    assert account.reserved_micro == 0 and account.spent_micro == 300
    with d["factory"]() as session:
        entries = session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.request_id == order["id"])).all()
        assert len(entries) == 1 and entries[0].amount_micro == -300
    with pytest.raises(ValueError, match="conflict"):
        device_stop.record(*args, execution_id=claimed["execution_id"], evidence_digest="c" * 64, processes_stopped=True)
    next_order = svc.prepare(d["vid"], caller, svc.Manifest(**order["manifest"]))
    assert next_order["id"] != order["id"]


def test_verified_native_terminal_receipt_is_bound_and_idempotent(journey_db):
    from app.build import device_stop
    from app.ventures.run_status import snapshot

    d = journey_db
    svc, caller, order = prepare(d, terminal=True)
    args = (d["vid"], caller, order["id"])
    svc.transition(*args, order["review_digest"], "approve")
    claimed = svc.transition(*args, order["review_digest"], "claim")
    receipt = dict(execution_id=claimed["execution_id"], evidence_digest="b" * 64,
                   processes_stopped=True, task_verified=True)
    stopped = device_stop.record(*args, **receipt)
    assert stopped["state"] == "stopped"
    assert stopped["native_stop"]["task_verified"] is True
    assert stopped["native_stop"]["verification_kind"] == "git_status_v1"
    assert device_stop.record(*args, **receipt)["settlement_ledger_id"] == stopped["settlement_ledger_id"]
    run = snapshot(d["vid"], d["wid"])
    assert run["state"] == "completed"
    assert run["action"]["href"].endswith(order["id"])
    with pytest.raises(ValueError, match="device_stop_conflict"):
        device_stop.record(*args, execution_id=claimed["execution_id"],
                           evidence_digest="b" * 64, processes_stopped=True)


def test_native_verification_cannot_exceed_reviewed_scope(journey_db):
    from app.build import device_stop

    d = journey_db
    svc, caller, order = prepare(d)
    args = (d["vid"], caller, order["id"])
    svc.transition(*args, order["review_digest"], "approve")
    claimed = svc.transition(*args, order["review_digest"], "claim")
    with pytest.raises(ValueError, match="device_stop_verification_invalid"):
        device_stop.record(*args, execution_id=claimed["execution_id"],
                           evidence_digest="b" * 64, processes_stopped=True, task_verified=True)
    with pytest.raises(ValueError, match="device_stop_verification_invalid"):
        device_stop.record(*args, execution_id=claimed["execution_id"],
                           evidence_digest="b" * 64, processes_stopped=False, task_verified=True)
    stopped = device_stop.record(*args, execution_id=claimed["execution_id"],
                                 evidence_digest="b" * 64, processes_stopped=True)
    assert "task_verified" not in stopped["native_stop"]


def test_native_stop_retains_unknown_usage_until_late_receipt(journey_db):
    from app.build import device_invocations as calls, device_stop
    from app.credits.accounts import get_account
    d = journey_db
    svc, caller, order = prepare(d)
    args = (d["vid"], caller, order["id"])
    svc.transition(*args, order["review_digest"], "approve")
    claimed = svc.transition(*args, order["review_digest"], "claim")
    calls.admit(*args, invocation_id="decision1", request_digest="a" * 64, quote_micro=500)
    receipt = dict(execution_id=claimed["execution_id"], evidence_digest="b" * 64, processes_stopped=True)
    pending = device_stop.record(*args, **receipt)
    assert pending["stop_reconciliation_required"] and "closed_at" not in pending
    assert get_account(d["vid"]).reserved_micro == 1000
    with pytest.raises(ValueError, match="budget_unavailable"):
        calls.check_current(*args)
    with pytest.raises(ValueError, match="active_order"):
        svc.prepare(d["vid"], caller, svc.Manifest(**order["manifest"]))
    calls.record_result(*args, invocation_id="decision1", actual_micro=125)
    closed = device_stop.record(*args, **receipt)
    assert closed["state"] == "stopped" and not closed.get("stop_reconciliation_required")
    assert get_account(d["vid"]).reserved_micro == 0
    assert get_account(d["vid"]).spent_micro == 125


@pytest.mark.parametrize("safe_code", ["desktop_fresh_observation_required",
                                       "desktop_pre_dispatch_unknown",
                                       "desktop_pre_dispatch_target_changed"])
def test_rejected_desktop_effect_settles_billed_decision_without_claiming_success(journey_db, safe_code):
    from app.build import device_effects, device_invocations as calls, device_stop
    from app.credits.accounts import get_account
    from coding_agents.jev_decision import Candidate

    d = journey_db
    svc, caller, order = prepare(d, desktop=True)
    args = (d["vid"], caller, order["id"])
    svc.transition(*args, order["review_digest"], "approve")
    claimed = svc.transition(*args, order["review_digest"], "claim")
    calls.admit(*args, invocation_id="decision1", request_digest="a" * 64, quote_micro=500)
    calls.record_result(*args, invocation_id="decision1", actual_micro=125)
    device_effects.save_proposal(*args, invocation_id="decision1", candidate=Candidate(
        operation="click", target="c1", observation_digest="b" * 64))
    assert device_effects.admit(*args, invocation_id="decision1",
        observation_digest="b" * 64, effect_digest="c" * 64)
    device_effects.receipt(*args, invocation_id="decision1", effect_digest="c" * 64,
                           outcome="uncertain")
    pending = device_stop.record(*args, execution_id=claimed["execution_id"],
        evidence_digest="d" * 64, processes_stopped=True)
    assert pending["state"] == "claimed"
    svc.transition(*args, order["review_digest"], "cancel")
    with pytest.raises(ValueError, match="pre_dispatch_code_invalid"):
        device_effects.receipt(*args, invocation_id="decision1", effect_digest="c" * 64,
            outcome="not_dispatched", pre_dispatch_code="desktop_driver_transport_unavailable")
    device_effects.receipt(*args, invocation_id="decision1", effect_digest="c" * 64,
        outcome="not_dispatched", pre_dispatch_code=safe_code)
    closed = device_stop.record(*args, execution_id=claimed["execution_id"],
        evidence_digest="d" * 64, processes_stopped=True)
    assert closed["state"] == "stopped" and closed["settled_micro"] == 125
    assert not closed["native_stop"].get("task_verified")
    assert get_account(d["vid"]).reserved_micro == 0
    assert svc.prepare(d["vid"], caller, svc.Manifest(**order["manifest"]))["id"] != order["id"]


def test_stopped_desktop_text_uncertainty_closes_unverified_and_releases_hold(journey_db):
    from app.build import device_effects as effects, device_invocations as calls, device_stop
    from app.credits.accounts import get_account
    from coding_agents.jev_decision import Candidate

    d = journey_db
    svc, caller, order = prepare(d, desktop=True)
    args = (d["vid"], caller, order["id"])
    for action in ("approve", "claim"):
        claimed = svc.transition(*args, order["review_digest"], action)
    for invocation_id, outcome in (("first", "returned"), ("second", "uncertain")):
        calls.admit(*args, invocation_id=invocation_id, request_digest="a" * 64, quote_micro=100)
        calls.record_result(*args, invocation_id=invocation_id, actual_micro=42)
        effects.save_proposal(*args, invocation_id=invocation_id,
            candidate=Candidate("type", "c1", "b" * 64, value_index=0))
        assert effects.admit(*args, invocation_id=invocation_id,
            observation_digest="b" * 64, effect_digest="c" * 64)
        effects.receipt(*args, invocation_id=invocation_id,
            effect_digest="c" * 64, outcome=outcome)
    closed = device_stop.record(*args, execution_id=claimed["execution_id"],
        evidence_digest="d" * 64, processes_stopped=True)
    assert closed["state"] == "stopped" and closed["settled_micro"] == 84
    assert closed["desktop_text_effect_reconciliation"]["effect_replayed"] is False
    assert closed["native_stop"].get("task_verified") is None
    assert get_account(d["vid"]).reserved_micro == 0
    assert svc.prepare(d["vid"], caller, svc.Manifest(**order["manifest"]))["id"] != order["id"]


def test_founder_ack_closes_uncertain_desktop_click_without_replay(journey_db):
    from app.build import device_effects as effects, device_invocations as calls, device_stop
    from app.credits.accounts import get_account
    from coding_agents.jev_decision import Candidate

    d = journey_db
    svc, caller, order = prepare(d, desktop=True)
    args = (d["vid"], caller, order["id"])
    svc.transition(*args, order["review_digest"], "approve")
    claimed = svc.transition(*args, order["review_digest"], "claim")
    calls.admit(*args, invocation_id="decision1", request_digest="a" * 64, quote_micro=100)
    calls.record_result(*args, invocation_id="decision1", actual_micro=42)
    effects.save_proposal(*args, invocation_id="decision1",
        candidate=Candidate("click", "c1", "b" * 64))
    assert effects.admit(*args, invocation_id="decision1",
        observation_digest="b" * 64, effect_digest="c" * 64)
    effects.receipt(*args, invocation_id="decision1", effect_digest="c" * 64,
                    outcome="uncertain")
    pending = device_stop.record(*args, execution_id=claimed["execution_id"],
        evidence_digest="d" * 64, processes_stopped=True)
    assert pending["state"] == "claimed"
    svc.transition(*args, order["review_digest"], "cancel")
    still_held = device_stop.record(*args, execution_id=claimed["execution_id"],
        evidence_digest="d" * 64, processes_stopped=True)
    assert still_held["state"] == "cancelled"
    with pytest.raises(svc.ActiveDeviceOrder):
        svc.prepare(d["vid"], caller, svc.Manifest(**order["manifest"]))
    acknowledged = device_stop.acknowledge_uncertain_desktop_click(*args)
    assert acknowledged["uncertain_desktop_click_ack"]["effect_digest"] == "c" * 64
    assert device_stop.acknowledge_uncertain_desktop_click(*args)["uncertain_desktop_click_ack"] == \
        acknowledged["uncertain_desktop_click_ack"]
    closed = device_stop.record(*args, execution_id=claimed["execution_id"],
        evidence_digest="d" * 64, processes_stopped=True)
    assert closed["state"] == "stopped" and closed["settled_micro"] == 42
    assert closed["desktop_click_effect_reconciliation"]["effect_replayed"] is False
    assert not closed["native_stop"].get("task_verified")
    assert get_account(d["vid"]).reserved_micro == 0
    assert svc.prepare(d["vid"], caller, svc.Manifest(**order["manifest"]))["id"] != order["id"]


def test_native_stop_accepts_stronger_late_process_cleanup_receipt(journey_db):
    from app.build import device_stop
    from app.credits.accounts import get_account
    d = journey_db
    svc, caller, order = prepare(d)
    args = (d["vid"], caller, order["id"])
    svc.transition(*args, order["review_digest"], "approve")
    claimed = svc.transition(*args, order["review_digest"], "claim")
    pending = device_stop.record(*args, execution_id=claimed["execution_id"],
        evidence_digest="a" * 64, processes_stopped=False)
    assert pending["stop_reconciliation_required"] is True
    closed = device_stop.record(*args, execution_id=claimed["execution_id"],
        evidence_digest="b" * 64, processes_stopped=True)
    assert closed["state"] == "stopped" and closed["settled_micro"] == 0
    assert get_account(d["vid"]).reserved_micro == 0


def test_cancelled_pre_inference_startup_failure_can_settle_zero(journey_db):
    from app.build import device_stop
    from app.credits.accounts import get_account
    d = journey_db
    svc, caller, order = prepare(d)
    args = (d["vid"], caller, order["id"])
    svc.transition(*args, order["review_digest"], "approve")
    claimed = svc.transition(*args, order["review_digest"], "claim")
    pending = device_stop.record(*args, execution_id=claimed["execution_id"],
        evidence_digest="b" * 64, processes_stopped=False)
    assert pending["stop_reconciliation_required"]
    svc.transition(*args, order["review_digest"], "cancel")
    closed = device_stop.reconcile_startup_failure(*args, execution_id=claimed["execution_id"],
        evidence_digest="b" * 64)
    assert closed["state"] == "stopped" and closed["settled_micro"] == 0
    assert closed["startup_reconciliation"]["process_cleanup_proven"] is False
    assert get_account(d["vid"]).reserved_micro == 0
    assert device_stop.reconcile_startup_failure(*args, execution_id=claimed["execution_id"],
        evidence_digest="b" * 64)["settlement_ledger_id"] == closed["settlement_ledger_id"]


def test_rotated_device_can_only_recover_same_actor_cancelled_expired_startup(journey_db, monkeypatch):
    from app.build import device_orders, device_stop
    from app.credits.accounts import get_account
    from datetime import timedelta
    from core.clock import utcnow
    d = journey_db
    svc, original, order = prepare(d)
    args = (d["vid"], original, order["id"])
    svc.transition(*args, order["review_digest"], "approve")
    claimed = svc.transition(*args, order["review_digest"], "claim")
    device_stop.record(*args, execution_id=claimed["execution_id"],
                       evidence_digest="b" * 64, processes_stopped=False)
    svc.transition(*args, order["review_digest"], "cancel")
    rotated = replace(original, device_id="dev_two")
    stranger = replace(original, sub="usr_stranger", device_id="dev_two")
    assert device_orders.retrieve(d["vid"], rotated, order["id"])["state"] == "cancelled"
    with pytest.raises(HTTPException) as denied:
        device_orders.retrieve(d["vid"], stranger, order["id"])
    assert denied.value.status_code == 404
    with pytest.raises(KeyError):
        svc.transition(d["vid"], rotated, order["id"], order["review_digest"], "claim")
    with pytest.raises(ValueError, match="unavailable"):
        device_stop.reconcile_startup_failure(d["vid"], rotated, order["id"],
            execution_id=claimed["execution_id"], evidence_digest="b" * 64)
    monkeypatch.setattr(device_orders, "utcnow", lambda: utcnow() + timedelta(seconds=121))
    closed = device_stop.reconcile_startup_failure(d["vid"], rotated, order["id"],
        execution_id=claimed["execution_id"], evidence_digest="b" * 64)
    assert closed["state"] == "stopped" and closed["settled_micro"] == 0
    assert get_account(d["vid"]).reserved_micro == 0
    assert device_orders.retrieve(d["vid"], rotated, order["id"])["state"] == "stopped"
    assert device_stop.reconcile_startup_failure(d["vid"], rotated, order["id"],
        execution_id=claimed["execution_id"], evidence_digest="b" * 64)["settlement_ledger_id"] == closed["settlement_ledger_id"]


def test_startup_reconciliation_rejects_any_admitted_provider_call(journey_db):
    from app.build import device_invocations as calls, device_stop
    d = journey_db
    svc, caller, order = prepare(d)
    args = (d["vid"], caller, order["id"])
    svc.transition(*args, order["review_digest"], "approve")
    claimed = svc.transition(*args, order["review_digest"], "claim")
    calls.admit(*args, invocation_id="decision1", request_digest="a" * 64, quote_micro=100)
    calls.record_result(*args, invocation_id="decision1", actual_micro=0)
    device_stop.record(*args, execution_id=claimed["execution_id"],
        evidence_digest="b" * 64, processes_stopped=False)
    svc.transition(*args, order["review_digest"], "cancel")
    with pytest.raises(ValueError, match="unavailable"):
        device_stop.reconcile_startup_failure(*args, execution_id=claimed["execution_id"],
            evidence_digest="b" * 64)


def test_mounted_signed_http_order_lifecycle_uses_real_membership_and_credits(journey_db, monkeypatch):
    from fastapi.testclient import TestClient
    from core.assertion import sign_assertion
    from app.credits.accounts import get_account
    d = journey_db
    svc, caller, order = prepare(d)
    secret = "mounted-device-order-test-secret-more-than-32-bytes"
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    monkeypatch.setenv("ADMIN_REQUIRE_TOTP", "1")
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "ops@example.test")
    monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", "test-vault-secret-with-more-than-32-bytes")
    from app import main
    client = TestClient(main.create_app())
    base = f"/ventures/{d['vid']}/device-work-orders"
    path = base + "/" + order["id"]
    def request(method, target, *, actor=d["actor"], device="dev_one", body=None):
        token = sign_assertion(secret, sub=actor, email="", role="user", path=target, method=method, device_id=device)
        return client.request(method, target, headers={"X-COS-Assertion": token}, json=body)
    assert client.get(path, headers={"X-Subject": d["actor"]}).status_code == 401
    assert request("GET", path, actor="usr_foreign").status_code == 404
    # A reconnect may assign a new Mac device id. The same founder may inspect
    # a pending review for recovery, but the new device cannot approve it.
    assert request("GET", path, device="dev_other").status_code == 200
    assert request("GET", path, device=None).status_code == 403
    saved = request("POST", base, body=order["manifest"])
    assert saved.status_code == 200, saved.text
    assert saved.json()["id"] == order["id"]
    body = {"review_digest": order["review_digest"]}
    approved = request("POST", path + "/approve", body=body)
    assert approved.status_code == 200, approved.text
    assert get_account(d["vid"]).reserved_micro == 1000
    claimed = request("POST", path + "/claim", body=body)
    assert claimed.status_code == 200, claimed.text
    assert request("POST", path + "/claim", body=body).status_code == 409
    cancelled = request("POST", path + "/cancel", body=body)
    assert cancelled.status_code == 200, cancelled.text
    assert request("GET", path).json()["state"] == "cancelled"
    # A cancellation request cannot prove an external process stopped or refund
    # ambiguous usage; this must stay held for verified reconciliation.
    assert get_account(d["vid"]).reserved_micro == 1000


def test_real_membership_and_mac_binding_apply_to_saved_order(journey_db):
    d = journey_db
    svc, caller, order = prepare(d)
    assert svc.retrieve(d["vid"], caller, order["id"]) == order
    with pytest.raises(HTTPException) as foreign_read:
        svc.retrieve(d["vid"], replace(caller, sub="usr_foreign"), order["id"])
    assert foreign_read.value.status_code == 404
    assert svc.retrieve(d["vid"], replace(caller, device_id="dev_other"), order["id"]) == order
    with pytest.raises(HTTPException) as foreign:
        svc.transition(d["vid"], replace(caller, sub="usr_foreign"), order["id"], order["review_digest"], "approve")
    assert foreign.value.status_code == 404
    with pytest.raises(KeyError):
        svc.transition(d["vid"], replace(caller, device_id="dev_other"), order["id"], order["review_digest"], "approve")
    from app.db.models import Workspace, WorkspaceMember
    from app.authz.workspace import ensure_subject_person
    from sqlalchemy import delete
    ensure_subject_person("usr_new_owner")
    with d["factory"]() as session:
        session.get(Workspace, d["wid"]).owner_id = "usr_new_owner"
        session.execute(delete(WorkspaceMember).where(WorkspaceMember.workspace_id == d["wid"], WorkspaceMember.person_id == d["actor"]))
        session.commit()
    with pytest.raises(HTTPException) as revoked:
        svc.transition(d["vid"], caller, order["id"], order["review_digest"], "approve")
    assert revoked.value.status_code == 404
    with pytest.raises(HTTPException) as revoked_read:
        svc.retrieve(d["vid"], caller, order["id"])
    assert revoked_read.value.status_code == 404


def test_changed_or_superseded_plan_cannot_be_claimed(journey_db):
    d = journey_db
    svc, caller, order = prepare(d)
    args = (d["vid"], caller, order["id"], order["review_digest"])
    svc.transition(*args, "approve")
    from app.db.supymem_models import ExecutionPlan, PlanTask
    with d["factory"]() as session:
        session.get(PlanTask, order["manifest"]["task_id"]).description = "Different task"
        session.commit()
    with pytest.raises(ValueError, match="task_changed"):
        svc.transition(*args, "claim")
    with d["factory"]() as session:
        session.get(ExecutionPlan, order["task_snapshot"]["plan_id"]).state = "superseded"
        session.commit()
    with pytest.raises(ValueError, match="not_ready"):
        svc.transition(*args, "claim")
    assert svc.transition(*args, "cancel")["state"] == "cancelled"


def test_simultaneous_preparation_reuses_one_new_review(journey_db):
    d = journey_db
    svc, caller, old = prepare(d)
    svc.transition(d["vid"], caller, old["id"], old["review_digest"], "cancel")
    manifest = svc.Manifest.model_validate(old["manifest"])
    barrier = Barrier(2)
    def propose(_):
        barrier.wait(timeout=5)
        return svc.prepare(d["vid"], caller, manifest)["id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(propose, [1, 2]))
    assert len(set(ids)) == 1 and ids[0] != old["id"]


def test_competing_device_approvals_cannot_overspend(journey_db):
    from sqlalchemy import select
    from app.credits.errors import InsufficientCredits
    from app.db.models import CreditAccount, CreditReservation
    from app.db.supymem_models import PlanTask
    from core.clock import utcnow
    from core.ids import new_id
    d = journey_db
    svc, caller, old = prepare(d)
    svc.transition(d["vid"], caller, old["id"], old["review_digest"], "cancel")
    other_task = new_id("plt")
    with d["factory"]() as session:
        session.add(PlanTask(id=other_task, milestone_id=old["task_snapshot"]["milestone_id"],
            workspace_id=d["wid"], venture_id=d["vid"], order=2, title="Second task",
            state="pending", created_at=utcnow()))
        session.commit()
    orders = [svc.prepare(d["vid"], caller, svc.Manifest.model_validate({
        **old["manifest"], "task_id": task, "credit_ceiling_micro": 6000}))
        for task in (old["manifest"]["task_id"], other_task)]
    barrier = Barrier(2)
    def approve(order):
        barrier.wait(timeout=5)
        try:
            return svc.transition(d["vid"], caller, order["id"], order["review_digest"], "approve")["state"]
        except InsufficientCredits:
            return "insufficient"
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(approve, orders))
    assert sorted(outcomes) == ["approved", "insufficient"]
    with d["factory"]() as session:
        account = session.scalar(select(CreditAccount).where(CreditAccount.venture_id == d["vid"]))
        assert account.reserved_micro == 6000
        holds = list(session.scalars(select(CreditReservation).where(CreditReservation.account_id == account.id)))
        assert len(holds) == 1 and holds[0].amount_micro == 6000


def test_viewer_demotion_blocks_spending_but_permits_cancelling_own_review(journey_db):
    from sqlalchemy import select
    from app.db.models import Workspace, WorkspaceMember
    from app.authz.workspace import ensure_subject_person
    d = journey_db
    svc, caller, order = prepare(d)
    ensure_subject_person("usr_device_new_owner")
    with d["factory"]() as session:
        session.get(Workspace, d["wid"]).owner_id = "usr_device_new_owner"
        member = session.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == d["wid"],
                                                               WorkspaceMember.person_id == d["actor"]))
        member.role = "viewer"
        session.commit()
    args = (d["vid"], caller, order["id"], order["review_digest"])
    with pytest.raises(PermissionError):
        svc.transition(*args, "approve")
    with pytest.raises(PermissionError):
        svc.prepare(d["vid"], caller, svc.Manifest.model_validate(order["manifest"]))
    assert svc.retrieve(*args[:3])["state"] == "pending"
    assert svc.transition(*args, "cancel")["state"] == "cancelled"


def test_concurrent_invocations_share_the_reviewed_ceiling(journey_db):
    from app.build import device_invocations as calls
    d = journey_db
    svc, caller, order = prepare(d)
    args = (d["vid"], caller, order["id"])
    for action in ("approve", "claim"):
        svc.transition(*args, order["review_digest"], action)
    barrier = Barrier(2)
    def admit(index):
        barrier.wait(timeout=5)
        try:
            return calls.admit(*args, invocation_id=f"call_{index}", request_digest="a" * 64,
                               quote_micro=600)["admitted_now"]
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(admit, [1, 2]))
    assert results.count(True) == 1
    assert results.count("device_invocation_budget_exhausted") == 1
    saved = svc.retrieve(*args)
    assert len(saved["invocations"]) == 1


def test_jev_controller_meters_once_with_real_plan_membership_and_reservation(journey_db, monkeypatch):
    from app.build import device_jev as jev
    from app.db.models import CreditAccount
    from coding_agents.jev_decision import Observation
    from sqlalchemy import select
    d = journey_db
    svc, caller, order = prepare(d, credit_ceiling_micro=8000)
    args = (d["vid"], caller, order["id"])
    for action in ("approve", "claim"):
        svc.transition(*args, order["review_digest"], action)
    monkeypatch.setenv("JEV_DEVICE_DECISIONS_ENABLED", "1")
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-key")
    observation = Observation(revision="1", application="com.apple.Safari", location="https://example.test/page",
        goal="Untrusted caller goal", context="Expected page loaded", controls=[], permitted=True,
        confirmation_pending=False, prior_effect_uncertain=False)
    sent = []
    def provider(body):
        sent.append(body)
        assert svc.retrieve(*args)["invocations"]["once"]["state"] == "calling"
        return {"model": jev.decision.MODEL, "usage": {"input_tokens": 1000, "output_tokens": 40},
            "answers": {"operation": {"type": "choice", "choice": "done",
                "probabilities": {"done": 1.0, "wait": 0.0, "blocked": 0.0}}}}
    monkeypatch.setattr(jev, "_provider_call", provider)
    result = jev.decide_browser(*args, invocation_id="once", observation=observation)
    assert result.operation == "done" and result.requires_outcome_verification
    with pytest.raises(ValueError, match="already_attempted"):
        jev.decide_browser(*args, invocation_id="once", observation=observation)
    assert len(sent) == 1 and sent[0]["state"]["goal"] == "Read page"
    assert svc.retrieve(*args)["invocations"]["once"]["actual_micro"] == 42
    with d["factory"]() as session:
        account = session.scalar(select(CreditAccount).where(CreditAccount.venture_id == d["vid"]))
        assert account.reserved_micro == 8000 and account.spent_micro == 0
    # A model's done proposal does not finish the task or settle the native hold.
    assert svc.retrieve(*args)["state"] == "claimed"


def test_jev_controller_uses_top_choice_for_bounded_navigation(journey_db, monkeypatch):
    from app.build import device_jev as jev
    from coding_agents.jev_decision import Observation
    d = journey_db
    svc, caller, order = prepare(d, actions=["browser.read", "browser.interact"], credit_ceiling_micro=8000)
    args = (d["vid"], caller, order["id"])
    for action in ("approve", "claim"):
        svc.transition(*args, order["review_digest"], action)
    monkeypatch.setenv("JEV_DEVICE_DECISIONS_ENABLED", "1")
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("JEV_FIXED_ADAPTER", "0")  # this covers the unfixed (kill-switch) reply shape with blocked/none heads
    observation = Observation(revision="one", application="com.apple.Safari",
        location="https://example.test", goal="Caller goal", context="The reviewed link is visible",
        controls=[{"id": "c0", "label": "Open reviewed destination", "operations": ["click"],
            "visible": True, "enabled": True}], permitted=True, confirmation_pending=False,
        prior_effect_uncertain=False)

    def provider(_body):
        return {"model": jev.decision.MODEL, "usage": {"input_tokens": 1000, "output_tokens": 40},
            "answers": {
                "operation": {"type": "choice", "choice": "click",
                    "probabilities": {"blocked": .08, "wait": .08, "done": .10, "click": .74}},
                "click_target": {"type": "choice", "choice": "c0",
                    "probabilities": {"none": .26, "c0": .74}},
            }}

    monkeypatch.setattr(jev, "_provider_call", provider)
    result = jev.decide_browser(*args, invocation_id="fast_nav", observation=observation)
    assert result.operation == "click" and result.target == "c0"
    assert svc.retrieve(*args)["invocations"]["fast_nav"]["state"] == "completed"


def test_cancelled_stopped_unknown_model_call_settles_at_admitted_ceiling(journey_db, monkeypatch):
    from datetime import timedelta
    from app.build import device_invocations as calls, device_stop
    from app.credits.accounts import get_account

    d = journey_db
    svc, caller, order = prepare(d, credit_ceiling_micro=8000, desktop=True)
    args = (d["vid"], caller, order["id"])
    for action in ("approve", "claim"):
        svc.transition(*args, order["review_digest"], action)
    calls.admit(*args, invocation_id="din_unknown", request_digest="a" * 64, quote_micro=2000)
    calls.record_result(*args, invocation_id="din_unknown", actual_micro=None)
    svc.transition(*args, order["review_digest"], "cancel")
    claimed = svc.retrieve(*args)
    stopped = device_stop.record(*args, execution_id=claimed["execution_id"],
        evidence_digest="b" * 64, processes_stopped=True, task_verified=False)
    assert stopped["state"] == "cancelled" and stopped["stop_reconciliation_required"] is True
    assert get_account(d["vid"]).reserved_micro == 8000
    settled_at = device_stop.orders.utcnow() + timedelta(seconds=120)
    monkeypatch.setattr(device_stop.orders, "utcnow", lambda: settled_at)
    result = device_stop.settle_cancelled_unknown_usage(*args,
        execution_id=claimed["execution_id"], evidence_digest="b" * 64)
    assert result["state"] == "stopped" and result["settled_micro"] == 2000
    assert result["unknown_usage_reconciliation"]["actual_usage_unconfirmed"] is True
    account = get_account(d["vid"])
    assert account.reserved_micro == 0 and account.spent_micro == 2000
    assert device_stop.settle_cancelled_unknown_usage(*args,
        execution_id=claimed["execution_id"], evidence_digest="b" * 64)["settled_micro"] == 2000


def test_cancelled_stopped_desktop_effect_stays_unverified_when_settled(journey_db):
    from coding_agents.jev_decision import Candidate
    from app.build import device_effects, device_invocations as calls, device_stop
    from app.credits.accounts import get_account

    d = journey_db
    svc, caller, order = prepare(d, credit_ceiling_micro=8000, desktop=True)
    args = (d["vid"], caller, order["id"])
    for action in ("approve", "claim"):
        svc.transition(*args, order["review_digest"], action)
    calls.admit(*args, invocation_id="din_effect", request_digest="a" * 64, quote_micro=2000)
    calls.record_result(*args, invocation_id="din_effect", actual_micro=300)
    device_effects.save_proposal(*args, invocation_id="din_effect",
        candidate=Candidate("type", "c1", "b" * 64, value_index=0))
    assert device_effects.admit(*args, invocation_id="din_effect",
        observation_digest="b" * 64, effect_digest="c" * 64)
    svc.transition(*args, order["review_digest"], "cancel")
    claimed = svc.retrieve(*args)
    stopped = device_stop.record(*args, execution_id=claimed["execution_id"],
        evidence_digest="d" * 64, processes_stopped=True, task_verified=False)
    assert stopped["state"] == "cancelled"
    result = device_stop.settle_cancelled_unknown_usage(*args,
        execution_id=claimed["execution_id"], evidence_digest="d" * 64)
    assert result["state"] == "stopped" and result["settled_micro"] == 300
    assert result["unknown_usage_reconciliation"]["effect_outcome_unconfirmed"] is True
    assert result["unknown_usage_reconciliation"]["effect_replayed"] is False
    assert result["invocations"]["din_effect"]["effect"]["state"] == "admitted"
    account = get_account(d["vid"])
    assert account.reserved_micro == 0 and account.spent_micro == 300


def test_mounted_signed_jev_decision_preserves_tenant_and_credit_boundaries(journey_db, monkeypatch):
    from fastapi.testclient import TestClient
    from core.assertion import sign_assertion
    from app.build import device_jev
    from app.credits.accounts import get_account
    d = journey_db
    svc, caller, order = prepare(d, credit_ceiling_micro=8000)
    for action in ("approve", "claim"):
        svc.transition(d["vid"], caller, order["id"], order["review_digest"], action)
    secret = "mounted-device-decision-test-secret-more-than-32-bytes"
    for key, value in {"COS_INTERNAL_ASSERTION_SECRET": secret, "ADMIN_REQUIRE_TOTP": "1",
        "ADMIN_BOOTSTRAP_EMAILS": "ops@example.test", "COMPANY_OS_VAULT_SECRET": "test-vault-secret-with-more-than-32-bytes",
        "JEV_DEVICE_DECISIONS_ENABLED": "1", "TYPESAFE_API_KEY": "synthetic-test-key"}.items():
        monkeypatch.setenv(key, value)
    from app import main
    client = TestClient(main.create_app())
    path = f"/ventures/{d['vid']}/device-work-orders/{order['id']}/browser-decision"
    body = {"invocation_id": "once", "observation": {"revision": "one", "application": "com.apple.Safari",
        "location": "https://example.test", "goal": "Caller goal", "context": "Page is ready", "controls": [],
        "permitted": True, "confirmation_pending": False, "prior_effect_uncertain": False}}
    sent = []
    def provider(value):
        sent.append(value)
        return {"model": "jev-1.13.0", "usage": {"input_tokens": 1000, "output_tokens": 40},
            "answers": {"operation": {"type": "choice", "choice": "done",
                "probabilities": {"done": 1.0, "wait": 0.0, "blocked": 0.0}}}}
    monkeypatch.setattr(device_jev, "_provider_call", provider)
    def request(actor=d["actor"], device="dev_one"):
        assertion = sign_assertion(secret, sub=actor, email="", role="user", path=path, method="POST", device_id=device)
        return client.post(path, json=body, headers={"X-COS-Assertion": assertion})
    assert request(actor="usr_foreign").status_code == 404
    assert request(device="dev_foreign").status_code == 404
    assert request(device=None).status_code == 403
    assert sent == []
    result = request()
    assert result.status_code == 200, result.text
    assert result.json()["authority"] == "proposal_only"
    assert request().status_code == 409
    assert len(sent) == 1
    account = get_account(d["vid"])
    assert account.reserved_micro == 8000 and account.spent_micro == 0
    assert svc.retrieve(d["vid"], caller, order["id"])["invocations"]["once"]["usage"]["input_tokens"] == 1000


@pytest.mark.parametrize("known_usage", [True, False])
def test_revocation_during_provider_call_retains_receipt_without_continuing(journey_db, monkeypatch, known_usage):
    import json
    from sqlalchemy import delete
    from app.build import device_jev as jev, device_invocations as calls
    from app.db.models import Workspace, WorkspaceMember, VentureDocument
    from app.authz.workspace import ensure_subject_person
    from coding_agents.jev_decision import Observation
    d = journey_db
    svc, caller, order = prepare(d, credit_ceiling_micro=8000)
    args = (d["vid"], caller, order["id"])
    for action in ("approve", "claim"):
        svc.transition(*args, order["review_digest"], action)
    monkeypatch.setenv("JEV_DEVICE_DECISIONS_ENABLED", "1")
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-key")
    ensure_subject_person("usr_new_owner")
    sent = []
    def provider(body):
        sent.append(body)
        with d["factory"]() as session:
            session.get(Workspace, d["wid"]).owner_id = "usr_new_owner"
            session.execute(delete(WorkspaceMember).where(WorkspaceMember.workspace_id == d["wid"],
                WorkspaceMember.person_id == d["actor"]))
            session.commit()
        if not known_usage:
            raise TimeoutError("synthetic provider timeout")
        return {"model": jev.decision.MODEL, "usage": {"input_tokens": 1000, "output_tokens": 40},
            "answers": {"operation": {"type": "choice", "choice": "done",
                "probabilities": {"done": 1.0, "wait": 0.0, "blocked": 0.0}}}}
    monkeypatch.setattr(jev, "_provider_call", provider)
    observation = Observation(revision="one", application="com.apple.Safari", location="https://example.test",
        goal="Read page", context="Ready", controls=[], permitted=True,
        confirmation_pending=False, prior_effect_uncertain=False)
    with pytest.raises(HTTPException if known_usage else ValueError):
        jev.decide_browser(*args, invocation_id="once", observation=observation)
    with d["factory"]() as session:
        saved = json.loads(session.get(VentureDocument, order["id"]).provenance_json)
        receipt = saved["invocations"]["once"]
        assert receipt["state"] == ("completed" if known_usage else "uncertain")
        assert saved["state"] == "claimed"  # No fabricated stop or refund.
        if known_usage:
            assert receipt["actual_micro"] == 42
    with pytest.raises(HTTPException):
        calls.admit(*args, invocation_id="another", request_digest="b" * 64, quote_micro=100)
    with pytest.raises(KeyError):
        calls.record_result(d["vid"], replace(caller, device_id="dev_foreign"), order["id"],
            invocation_id="once", actual_micro=42)
    with pytest.raises(KeyError):
        calls.record_result(d["vid"], replace(caller, sub="usr_foreign"), order["id"],
            invocation_id="once", actual_micro=42)
    assert len(sent) == 1


def test_native_effect_proposal_consumed_once_and_uncertainty_stops_new_work(journey_db):
    from coding_agents.jev_decision import Candidate
    from app.build import device_invocations as calls, device_effects as effects
    d = journey_db
    svc, caller, order = prepare(d, actions=["browser.read", "browser.interact"])
    args = (d["vid"], caller, order["id"])
    for action in ("approve", "claim"):
        svc.transition(*args, order["review_digest"], action)
    calls.admit(*args, invocation_id="known_proposal", request_digest="a" * 64, quote_micro=100)
    calls.record_result(*args, invocation_id="known_proposal", actual_micro=42)
    effects.save_proposal(*args, invocation_id="known_proposal", candidate=Candidate("click", "c0", "b" * 64))
    barrier = Barrier(2)
    def consume(_):
        barrier.wait(timeout=5)
        return effects.admit(*args, invocation_id="known_proposal", observation_digest="b" * 64, effect_digest="c" * 64)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(consume, [1, 2]))
    assert sorted(outcomes) == [False, True]
    effects.receipt(*args, invocation_id="known_proposal", effect_digest="c" * 64, outcome="uncertain")
    assert effects.admit(*args, invocation_id="known_proposal", observation_digest="b" * 64, effect_digest="c" * 64) is False
    with pytest.raises(ValueError, match="reconciliation"):
        calls.admit(*args, invocation_id="next", request_digest="d" * 64, quote_micro=100)
    with pytest.raises(ValueError, match="conflict"):
        effects.receipt(*args, invocation_id="known_proposal", effect_digest="c" * 64, outcome="returned")


def test_reviewed_desktop_type_and_scroll_are_admitted_once(journey_db):
    from coding_agents.jev_decision import Candidate
    from app.build import device_invocations as calls, device_effects as effects
    d = journey_db
    svc, caller, base = prepare(d)
    svc.transition(d["vid"], caller, base["id"], base["review_digest"], "cancel")
    body = {**base["manifest"], "applications": ["com.google.Chrome"], "origins": [],
            "actions": ["desktop.read", "desktop.interact"],
            "desktop_verification": {"operation": "desktop_goal_v1",
                "application_bundle": "com.google.Chrome", "process_id": 123,
                "process_start_identity": "123456789", "window_id": 44,
                "window_title": "Search", "delivery_mode": "background",
                "max_steps": 8, "reviewed_text": ["websites that sell pets"]}}
    order = svc.prepare(d["vid"], caller, svc.Manifest(**body))
    args = (d["vid"], caller, order["id"])
    for action in ("approve", "claim"):
        svc.transition(*args, order["review_digest"], action)
    calls.admit(*args, invocation_id="typed", request_digest="a" * 64, quote_micro=100)
    calls.record_result(*args, invocation_id="typed", actual_micro=42)
    effects.save_proposal(*args, invocation_id="typed",
        candidate=Candidate("type", "c1", "b" * 64, value_index=0))
    assert effects.admit(*args, invocation_id="typed", observation_digest="b" * 64,
                         effect_digest="c" * 64) is True
    assert effects.admit(*args, invocation_id="typed", observation_digest="b" * 64,
                         effect_digest="c" * 64) is False
    effects.receipt(*args, invocation_id="typed", effect_digest="c" * 64, outcome="returned")
    calls.admit(*args, invocation_id="scrolled", request_digest="d" * 64, quote_micro=100)
    calls.record_result(*args, invocation_id="scrolled", actual_micro=42)
    effects.save_proposal(*args, invocation_id="scrolled",
        candidate=Candidate("scroll", "c2", "e" * 64))
    assert effects.admit(*args, invocation_id="scrolled", observation_digest="e" * 64,
                         effect_digest="f" * 64) is True
    assert effects.admit(*args, invocation_id="scrolled", observation_digest="e" * 64,
                         effect_digest="f" * 64) is False
    effects.receipt(*args, invocation_id="scrolled", effect_digest="f" * 64, outcome="returned")


def test_founder_mac_request_is_reviewable_without_an_execution_plan_task(journey_db, monkeypatch):
    from app.admin.authn import CallerContext
    from app.build import device_orders as svc, device_invocations as calls
    from app.db.models import CreditAccount
    from core.clock import utcnow
    from core.ids import new_id

    d = journey_db
    caller = CallerContext(d["actor"], "", "user", "/test", "POST", 9999999999, "test", device_id="dev_one")
    with d["factory"]() as session:
        session.add(CreditAccount(id=new_id("cra"), workspace_id=d["wid"], venture_id=d["vid"],
            scope="venture", granted_micro=1000000, spent_micro=0, reserved_micro=0,
            status="active", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    monkeypatch.setattr(svc, "_current_open_task", lambda *_args: (_ for _ in ()).throw(
        AssertionError("general Mac control must not require a plan task")))
    order = svc.prepare_desktop_from_chat(
        d["vid"], caller, "Open Google Chrome and type a public search query",
        "com.google.Chrome", 123, "123456789", 44, "Search")
    assert order["task_snapshot"]["kind"] == "founder_mac_request"
    for action in ("approve", "claim"):
        svc.transition(d["vid"], caller, order["id"], order["review_digest"], action)
    calls.check_current(d["vid"], caller, order["id"])


def test_reviewed_navigation_uncertainty_settles_known_usage_without_claiming_success(journey_db):
    from coding_agents.jev_decision import Candidate
    from app.build import device_effects as effects, device_invocations as calls, device_stop
    from app.credits.accounts import get_account
    d = journey_db
    svc, caller, base = prepare(d, actions=["browser.read", "browser.interact"])
    manifest = svc.Manifest(**{**base["manifest"], "applications": ["com.google.Chrome"], "browser_verification": {
        "expected_locations": ["https://example.test/", "https://example.test/waitlist"]}})
    svc.transition(d["vid"], caller, base["id"], base["review_digest"], "cancel")
    order = svc.prepare(d["vid"], caller, manifest)
    args = (d["vid"], caller, order["id"])
    for action in ("approve", "claim"):
        claimed = svc.transition(*args, order["review_digest"], action)
    calls.admit(*args, invocation_id="once", request_digest="a" * 64, quote_micro=100)
    calls.record_result(*args, invocation_id="once", actual_micro=42)
    effects.save_proposal(*args, invocation_id="once", candidate=Candidate("click", "c0", "b" * 64))
    effects.admit(*args, invocation_id="once", observation_digest="b" * 64, effect_digest="c" * 64)
    effects.receipt(*args, invocation_id="once", effect_digest="c" * 64, outcome="uncertain")

    closed = device_stop.record(*args, execution_id=claimed["execution_id"],
        evidence_digest="d" * 64, processes_stopped=True)

    assert closed["state"] == "stopped" and closed["settled_micro"] == 42
    assert closed["navigation_effect_reconciliation"]["effect_replayed"] is False
    assert get_account(d["vid"]).reserved_micro == 0
    assert svc.prepare(d["vid"], caller, manifest)["id"] != order["id"]


def test_pending_device_task_remains_executable_after_approved_plan_starts(journey_db):
    d = journey_db
    svc, caller, order = prepare(d, plan_state="executing")
    for action in ("approve", "claim"):
        result = svc.transition(d["vid"], caller, order["id"], order["review_digest"], action)
    assert result["state"] == "claimed"
    from app.db.supymem_models import ExecutionPlan
    with d["factory"]() as session:
        plan = session.get(ExecutionPlan, order["task_snapshot"]["plan_id"])
        plan.superseded_by = "pln_replaced"
        session.commit()
    from app.build import device_invocations
    with pytest.raises(ValueError, match="task_not_ready"):
        device_invocations.check_current(d["vid"], caller, order["id"])


def test_public_site_review_is_derived_from_live_receipts_and_saved_task(journey_db):
    from app.db.supymem_models import (Deployment, ExecutionPlan, FounderAction,
        FounderApp, PlanMilestone, PlanTask)
    from core.clock import utcnow
    from core.ids import new_id
    d = journey_db
    svc, caller, unused = prepare(d)
    task_id = unused["manifest"]["task_id"]
    svc.transition(d["vid"], caller, unused["id"], unused["review_digest"], "cancel")
    app_id = new_id("app")
    with d["factory"]() as session:
        task = session.get(PlanTask, task_id)
        milestone = session.get(PlanMilestone, task.milestone_id)
        task.title = "Founder verifies the public launch"
        task.description = "Review the live public URL and managed waitlist."
        milestone.acceptance = '["Public URL loads", "Waitlist link reaches the managed page"]'
        build_task_id = new_id("plt")
        session.add(PlanTask(id=build_task_id, milestone_id=milestone.id,
            workspace_id=d["wid"], venture_id=d["vid"], order=0,
            title="Build public URL and waitlist artifact",
            description="Create the public URL and managed waitlist link.",
            tool_hints='["web_app.nextjs"]', state="pending", created_at=utcnow()))
        session.add(FounderAction(id=new_id("fact"), workspace_id=d["wid"], venture_id=d["vid"],
            plan_task_id=build_task_id, title="Resolve build blocker",
            steps='["Open public URL and waitlist configuration"]', required_secret_slots="[]",
            state="open", blocks_task=True, created_at=utcnow()))
        session.add(FounderAction(id=new_id("fact"), workspace_id=d["wid"], venture_id=d["vid"],
            plan_task_id=task_id, title="Review launch", steps='["Open Website", "Verify public URL and waitlist"]',
            required_secret_slots="[]", state="open", blocks_task=True, created_at=utcnow()))
        old_plan_id, old_milestone_id, old_task_id = new_id("pln"), new_id("plm"), new_id("plt")
        session.add(ExecutionPlan(id=old_plan_id, workspace_id=d["wid"], venture_id=d["vid"],
            version=0, state="executing", approved_at=utcnow(), approved_by=d["actor"],
            created_at=utcnow()))
        session.flush()
        session.add(PlanMilestone(id=old_milestone_id, plan_id=old_plan_id,
            workspace_id=d["wid"], venture_id=d["vid"], order=1, title="Old launch",
            kind="validation", scope="conditional",
            acceptance='["Public URL loads", "Waitlist link reaches managed page"]'))
        session.flush()
        session.add(PlanTask(id=old_task_id, milestone_id=old_milestone_id,
            workspace_id=d["wid"], venture_id=d["vid"], order=1,
            title="Founder verifies old public launch",
            description="Review the live public URL and managed waitlist.",
            state="pending", created_at=utcnow()))
        session.add(FounderAction(id=new_id("fact"), workspace_id=d["wid"], venture_id=d["vid"],
            plan_task_id=old_task_id, title="Review old launch",
            steps='["Verify public URL and waitlist"]', required_secret_slots="[]",
            state="open", blocks_task=True, created_at=utcnow()))
        session.add(FounderApp(id=app_id, workspace_id=d["wid"], venture_id=d["vid"], slug="derived-review",
            name="Derived review fixture", kind="service", state="live", build_mode="supymem",
            deploy_target="supymem", created_at=utcnow(), updated_at=utcnow()))
        session.flush()
        session.add(Deployment(id=new_id("dep"), app_id=app_id, workspace_id=d["wid"], venture_id=d["vid"],
            target="supymem_static", state="live", version=1, url="https://derived-review.supymem.com/",
            started_at=utcnow(), live_at=utcnow()))
        session.commit()
    order = svc.prepare_public_site_review(d["vid"], caller, task_id)
    assert order["manifest"]["origins"] == ["https://derived-review.supymem.com", "https://gateway.supymem.com"]
    assert "https://derived-review.supymem.com/" in order["manifest"]["instruction"]
    assert d["vid"] in order["manifest"]["instruction"]
    assert order["manifest"]["applications"] == ["com.google.Chrome"]
    assert order["manifest"]["actions"] == ["browser.read", "browser.interact"]
    assert order["manifest"]["browser_verification"] == {"expected_locations": [
        "https://derived-review.supymem.com/",
        f"https://gateway.supymem.com/public/waitlists/{d['vid']}",
    ]}
    assert "supymem_check" not in order["manifest"]["instruction"]
    assert all("supymem_check" not in loc for loc in order["manifest"]["browser_verification"]["expected_locations"])
    assert svc.prepare_public_site_review_from_chat(
        d["vid"], caller, "Open the live site in the browser and verify its waitlist"
    ) == order
    assert svc.prepare_public_site_review_from_chat(
        d["vid"], caller,
        "Open https://derived-review.supymem.com/ and verify its waitlist page"
    ) == order
    with pytest.raises(ValueError, match="device_chat_intent_unsupported"):
        svc.prepare_public_site_review_from_chat(
            d["vid"], caller,
            "Open https://www.reddit.com/r/cscareerquestions/ and research waitlist outreach rules"
        )
    with pytest.raises(ValueError, match="device_chat_intent_unsupported"):
        svc.prepare_public_site_review_from_chat(d["vid"], caller, "Delete the deployment")
    svc.transition(d["vid"], caller, order["id"], order["review_digest"], "cancel")
    terminal = svc.prepare_from_chat(
        d["vid"], caller, "Use the terminal to run git status and verify workspace readiness"
    )
    assert terminal["manifest"]["applications"] == ["com.companyos.app"]
    assert terminal["manifest"]["actions"] == ["terminal.execute"]
    assert terminal["manifest"]["origins"] == []
    assert terminal["manifest"]["terminal_verification"]["operation"] == "git_status_v1"
    assert terminal["manifest"]["terminal_verification"]["workspace_slug"].endswith(d["vid"][-6:])
    # Terminal readiness is bound to the deterministic current task, not to
    # whichever later task happens to mention a workspace, repository or Git.
    assert terminal["manifest"]["task_id"] == build_task_id
    svc.transition(d["vid"], caller, terminal["id"], terminal["review_digest"], "cancel")
    self_navigation = svc.prepare_from_chat(
        d["vid"], caller, "Use Supymem to open Builds in its own Mac app"
    )
    assert self_navigation["manifest"]["task_id"] == build_task_id
    assert self_navigation["manifest"]["applications"] == ["com.companyos.app"]
    assert self_navigation["manifest"]["actions"] == ["app.navigate"]
    assert self_navigation["manifest"]["app_navigation_verification"] == {
        "operation": "open_venture_screen_v1", "route": "builds",
    }
    with pytest.raises(ValueError, match="device_chat_intent_unsupported"):
        svc.prepare_from_chat(d["vid"], caller,
            "Use Supymem to open Builds and Settings in its own Mac app")
    svc.transition(d["vid"], caller, self_navigation["id"], self_navigation["review_digest"], "cancel")
    # An approved Next.js task still has no reviewed offline dependency supply.
    # Public-site/terminal evidence must not manufacture coding availability.
    with pytest.raises(ValueError, match="^device_coding_offline_profile_unavailable$"):
        svc.prepare_from_chat(
            d["vid"], caller, "Use Codex in the terminal to implement the current approved build task")
    with pytest.raises(ValueError, match="device_coding_provider_unsupported"):
        svc.prepare_from_chat(
            d["vid"], caller,
            "Use Cursor to implement the current approved build task",
        )


def test_coding_chat_does_not_turn_current_founder_review_into_code_access(journey_db):
    from app.db.supymem_models import FounderAction, PlanTask
    from core.clock import utcnow
    from core.ids import new_id
    d = journey_db
    svc, caller, order = prepare(d)
    task_id = order["manifest"]["task_id"]
    svc.transition(d["vid"], caller, order["id"], order["review_digest"], "cancel")
    with d["factory"]() as session:
        task = session.get(PlanTask, task_id)
        task.title = "Founder reviews build and publishes live"
        task.description = "Review the accepted artifact and decide whether to publish it."
        task.tool_hints = '["founder.action"]'
        session.add(FounderAction(
            id=new_id("fact"), workspace_id=d["wid"], venture_id=d["vid"],
            plan_task_id=task_id, title="Review accepted artifact",
            steps='["Review the public URL", "Confirm whether to publish"]',
            required_secret_slots="[]", state="open", blocks_task=True,
            created_at=utcnow(),
        ))
        session.commit()
    with pytest.raises(ValueError, match="device_coding_task_unavailable"):
        svc.prepare_from_chat(
            d["vid"], caller,
            "Use Codex to implement the current approved build task for this venture",
        )
    # Unsupported writable providers are rejected before plan-task admission;
    # their detected presence must never create an impossible review.
    with pytest.raises(ValueError, match="device_coding_provider_unsupported"):
        svc.prepare_from_chat(
            d["vid"], caller,
            "Use Cursor to edit the venture workspace",
        )
    inspected = svc.prepare_from_chat(
        d["vid"], caller,
        "Use Cursor to inspect the existing source read-only. Do not edit.",
    )
    assert inspected["manifest"]["task_id"] == task_id
    assert inspected["manifest"]["actions"] == ["editor.read"]
    assert inspected["manifest"]["applications"] == ["com.companyos.app"]
    assert inspected["manifest"]["origins"] == []
    assert inspected["manifest"]["inspection_verification"]["operation"] == "workspace_inspect_v1"
    assert inspected["manifest"]["inspection_verification"]["provider"] == "cursor"
    assert inspected["manifest"]["inspection_verification"]["write_scope"] == "none"
    assert inspected["manifest"]["inspection_verification"]["read_scope"] == "venture_workspace"
    assert "editing session" in inspected["manifest"]["instruction"]
    with pytest.raises(ValueError, match="device_inspection_provider_unsupported"):
        svc.prepare_from_chat(
            d["vid"], caller,
            "Use Codex to inspect the existing source read-only",
        )


def test_explicit_closed_core_task_still_requires_an_offline_source_profile(journey_db):
    """A named retry is reviewed without silently completing earlier founder work."""
    import json

    from app.db.models import CreditAccount
    from app.db.supymem_models import BuildRun, FounderAction, FounderApp, PlanMilestone, PlanTask
    from core.clock import utcnow
    from core.ids import new_id

    d = journey_db
    svc, caller, first = prepare(d)
    svc.transition(d["vid"], caller, first["id"], first["review_digest"], "cancel")
    core_id, task_id, app_id, run_id = (new_id(prefix) for prefix in ("plm", "plt", "app", "bld"))
    with d["factory"]() as session:
        session.query(CreditAccount).filter(CreditAccount.venture_id == d["vid"]).update(
            {"granted_micro": 2_000_000})
        first_task = session.get(PlanTask, first["manifest"]["task_id"])
        first_task.title = "Founder reviews published page"
        first_task.tool_hints = '["founder.action"]'
        session.add(FounderAction(id=new_id("fact"), workspace_id=d["wid"], venture_id=d["vid"],
            plan_task_id=first_task.id, title="Review page", steps='["Review"]',
            required_secret_slots="[]", state="open", blocks_task=True, created_at=utcnow()))
        session.add(PlanMilestone(id=core_id, plan_id=session.get(PlanMilestone, first_task.milestone_id).plan_id,
            workspace_id=d["wid"], venture_id=d["vid"], order=2, title="Product", kind="core",
            scope="in", acceptance=json.dumps(["Users can save a profile."])))
        session.flush()
        session.add(PlanTask(id=task_id, milestone_id=core_id, workspace_id=d["wid"],
            venture_id=d["vid"], order=1, title="Build the authenticated application",
            description="Create the functioning product.", tool_hints='["build.run"]',
            state="failed", build_run_id=run_id, created_at=utcnow()))
        session.add(FounderApp(id=app_id, workspace_id=d["wid"], venture_id=d["vid"],
            slug="explicit-core-" + app_id, name="Product", kind="web",
            created_at=utcnow(), updated_at=utcnow()))
        session.flush()
        session.add(BuildRun(id=run_id, app_id=app_id, workspace_id=d["wid"], venture_id=d["vid"],
            plan_task_id=task_id, trigger="founder", agent="builder", state="failed",
            closed_at=utcnow(), created_at=utcnow()))
        session.commit()
    with pytest.raises(ValueError, match="device_coding_task_unavailable"):
        svc.prepare_from_chat(d["vid"], caller, "Use Codex to build the current task")
    focus = "Use Codex to build the authenticated application. Repair its existing start script only; do not rerun the managed builder."
    with d["factory"]() as session:
        snapshot = svc._coding_task_snapshot(session, d["wid"], d["vid"], focus)
        assert snapshot["task_id"] == task_id
        assert snapshot["closed_failed_build_run_id"] == run_id
    with pytest.raises(ValueError, match="^device_coding_offline_profile_unavailable$"):
        svc.prepare_from_chat(d["vid"], caller, focus)
    with d["factory"]() as session:
        session.get(BuildRun, run_id).closed_at = None
        session.commit()
    with d["factory"]() as session, pytest.raises(ValueError, match="device_task_not_ready"):
        svc._coding_task_snapshot(session, d["wid"], d["vid"], focus)


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_zero_credit_coding_keeps_authority_and_exact_settlement(journey_db, provider):
    from app.build import device_invocations as calls, device_stop
    from app.db.models import CreditAccount, CreditReservation
    from sqlalchemy import select
    d = journey_db
    svc, caller, order = prepare(d, credit_ceiling_micro=0, coding=provider)
    args = (d["vid"], caller, order["id"])
    with d["factory"]() as session:
        account = session.scalar(select(CreditAccount).where(CreditAccount.venture_id == d["vid"]))
        account.granted_micro = 0
        session.commit()
    with pytest.raises(KeyError):
        svc.transition(d["vid"], replace(caller, device_id="other"), order["id"], order["review_digest"], "approve")
    approved = svc.transition(*args, order["review_digest"], "approve")
    claimed = svc.transition(*args, order["review_digest"], "claim")
    calls.check_current(*args)
    with pytest.raises(ValueError, match="budget_exhausted"):
        calls.admit(*args, invocation_id="paid", request_digest="a" * 64, quote_micro=1)
    with pytest.raises(ValueError, match="transition_invalid"):
        svc.transition(*args, order["review_digest"], "claim")
    stopped = device_stop.record(*args, execution_id=claimed["execution_id"],
        evidence_digest="b" * 64, processes_stopped=True, task_verified=False)
    assert stopped["state"] == "stopped" and stopped["native_stop"].get("task_verified") is not True
    repeated = device_stop.record(*args, execution_id=claimed["execution_id"],
        evidence_digest="b" * 64, processes_stopped=True, task_verified=False)
    assert repeated["settlement_ledger_id"] == stopped["settlement_ledger_id"]
    from sqlalchemy.exc import IntegrityError
    for changes in ({"purpose": "model_call"}, {"provider": "openai"}, {"amount_micro": -1}):
        with d["factory"]() as session, pytest.raises(IntegrityError):
            hold = session.get(CreditReservation, approved["reservation_id"])
            for key, value in changes.items():
                setattr(hold, key, value)
            session.flush()
    with d["factory"]() as session:
        hold = session.get(CreditReservation, approved["reservation_id"])
        account = session.get(CreditAccount, hold.account_id)
        assert hold.amount_micro == 0 and hold.status == "settled"
        assert account.reserved_micro == account.spent_micro == account.granted_micro == 0


def test_zero_credit_coding_preserves_suspended_account_and_cancel(journey_db):
    from app.db.models import CreditAccount, CreditReservation
    from app.credits.errors import AccountSuspended
    from sqlalchemy import select
    d = journey_db
    svc, caller, order = prepare(d, credit_ceiling_micro=0, coding="codex")
    args = (d["vid"], caller, order["id"], order["review_digest"])
    with d["factory"]() as session:
        account = session.scalar(select(CreditAccount).where(CreditAccount.venture_id == d["vid"]))
        aid = account.id
        account.status = "suspended"
        session.commit()
    with pytest.raises(AccountSuspended):
        svc.transition(*args, "approve")
    with d["factory"]() as session:
        account = session.get(CreditAccount, aid)
        account.status, account.granted_micro = "active", 0
        session.commit()
    approved = svc.transition(*args, "approve")
    svc.transition(*args, "cancel")
    with d["factory"]() as session:
        hold = session.get(CreditReservation, approved["reservation_id"])
        assert hold.amount_micro == 0 and hold.status == "released"
        assert session.get(CreditAccount, aid).reserved_micro == 0


@pytest.mark.parametrize('pause_at', ['approve', 'claim'])
def test_desktop_pause_is_durable_scoped_and_refuses_execution_grant(journey_db, pause_at):
    from app.build import device_desktop_pause as pause
    from app.db.models import VentureDocument
    from sqlalchemy import select
    d = journey_db
    svc, caller, body = prepare(d, desktop=True)
    if pause_at == 'claim':
        body = svc.transition(d['vid'], caller, body['id'], body['review_digest'], 'approve')
    assert pause.set_pause(d['vid'], caller, True)['paused'] is True
    assert pause.status(d['vid'], caller)['paused'] is True
    assert pause.status(d['vid'], replace(caller, device_id='other_device'))['paused'] is False
    with pytest.raises(ValueError, match='device_desktop_paused'):
        svc.transition(d['vid'], caller, body['id'], body['review_digest'], pause_at)
    with pytest.raises(ValueError, match='device_desktop_paused'):
        svc.prepare(d['vid'], caller, svc.Manifest(**body['manifest']))
    assert svc.retrieve(d['vid'], caller, body['id'])['state'] == body['state']
    assert svc.transition(d['vid'], caller, body['id'], body['review_digest'], 'cancel')['state'] == 'cancelled'
    with d['factory']() as session:
        docs = list(session.scalars(select(VentureDocument).where(VentureDocument.kind == pause.KIND)))
        assert len([row for row in docs if row.id == pause._id(d['wid'], d['actor'], caller.device_id)]) == 1
        pause.assert_available(session, d['wid'], d['actor'], caller.device_id, {'coding_verification': {}})
    assert pause.set_pause(d['vid'], caller, False)['paused'] is False
