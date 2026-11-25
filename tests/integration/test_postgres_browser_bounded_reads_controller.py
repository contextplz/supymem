"""Actual PG admission/effect/controller integration; all provider replies are local stubs."""
from datetime import timedelta
import importlib.util
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401
from tests.integration.test_postgres_browser_qualification import seeded_trial
from tests.integration.test_postgres_browser_qualification_hooks import signed_client
from tests.unit.product.test_device_browser_ai import configure, response, v2_request


def device(d, monkeypatch):
    from app.admin.authn import CallerContext
    from app.build import browser_qualification as qualification, device_orders as orders
    from app.db.models import CreditAccount
    from app.db.supymem_models import DesktopDevice
    profile = configure(monkeypatch)
    device_id = "dev_" + d["actor"].rsplit("_", 1)[1]
    monkeypatch.setenv(qualification.ENABLED_ENV, "1")
    monkeypatch.setenv(qualification.ALLOWLIST_ENV, d["actor"])
    now = orders.utcnow()
    with d["factory"]() as session:
        session.add(DesktopDevice(id=device_id, person_id=d["actor"], name="Synthetic bounded read Mac", platform="macos", app_version="0.1.85"))
        session.add(CreditAccount(id="cra_" + device_id, workspace_id=d["wid"], venture_id=d["vid"], scope="venture",
            granted_micro=20_000_000, spent_micro=0, reserved_micro=0, status="active", created_at=now, updated_at=now))
        session.commit()
    return CallerContext(d["actor"], "", "user", "/test", "POST", 9999999999, "synthetic", device_id=device_id), profile


def read_order(d, monkeypatch, *, trial_bound=False):
    from app.build import browser_qualification as qualification, device_orders as orders
    from core.browser_qualification_catalog import CATALOG_DIGEST
    from core.canonical import digest
    caller, profile = device(d, monkeypatch)
    if trial_bound:
        trial = qualification.start_trial(d["vid"], caller, catalog_digest=CATALOG_DIGEST,
            seed_digest=digest("bounded-public-source"), expires_at=int((orders.utcnow() + timedelta(hours=1)).timestamp()))
        order = qualification.prepare_task(d["vid"], caller, trial["trial_id"], "l01_research_synthesis")
    else:
        manifest = orders.Manifest(task_id="founder_browser_" + d["vid"], title="Synthetic read only request",
            instruction="Open https://pypi.org/ and read the source title. Read only; do not submit anything.",
            applications=["com.google.Chrome"], origins=["https://pypi.org"], actions=["browser.read"],
            credit_ceiling_micro=500_000, duration_seconds=900, general_browser_verification={
                "operation": "general_goal_v1", "start_url": "https://pypi.org/", "max_steps": 8,
                "allow_type": False, "fill_values": {}, "execution_profile": profile.model_dump()})
        order = orders.prepare(d["vid"], caller, manifest)
    args = (d["vid"], caller, order["id"])
    for action in ("approve", "claim"):
        order = orders.transition(*args, order["review_digest"], action)
    return orders, caller, order, args


def marked_request(order, *, marker=True):
    request = v2_request(order)
    request.observation.location = order["manifest"]["general_browser_verification"]["start_url"]
    request.current_load.location = request.observation.location
    request.current_load.response.url = request.observation.location
    request.current_load.observation_digest = request.observation.digest()
    notes = json.loads(request.context.notes)
    notes["current"].update(location=request.current_load.location, observation_digest=request.observation.digest())
    request.context.notes = json.dumps(notes)
    if marker:
        request.context.bounded_reads = True
    return request


def completed_parent(args, order, *, marker=True, invocation_id="actor_parent", phase="actor"):
    from app.build import device_browser_ai as ai, device_invocations as calls
    request = marked_request(order, marker=marker)
    source = {"phase": phase, "profile_digest": ai.profile_digest(order["manifest"]["general_browser_verification"]["execution_profile"]),
        "checkpoint_digest": "c" * 64, "load": request.current_load.model_dump()}
    if marker:
        source["bounded_reads"] = True
    result = calls.admit(*args, invocation_id=invocation_id, request_digest="a" * 64, quote_micro=100, browser_phase=source)
    assert result["admitted_now"] is True
    calls.record_result(*args, invocation_id=invocation_id, actual_micro=1,
        usage={"model": order["manifest"]["general_browser_verification"]["execution_profile"]["roles"][phase]["model"],
            "input_tokens": 1, "output_tokens": 1})
    return invocation_id, request


@pytest.mark.parametrize("operation", ["navigate", "scroll", "extract"])
def test_signed_v1_marked_source_reads_have_real_one_use_parent_child_effect(journey_db, monkeypatch, operation):
    from app.build import device_browser_ai as ai, device_effects as effects
    d = journey_db
    orders, caller, order, args = read_order(d, monkeypatch)
    request = marked_request(order)
    action = {"op": operation, "text": "Source title"} if operation == "extract" else {
        "op": "scroll", "direction": "down", "amount": 1} if operation == "scroll" else {
        "op": "navigate", "text": "https://pypi.org/project/pip/"}
    if operation == "navigate":
        request.context.links = [ai.NavigationTarget(id="c1", href=action["text"], tag="A", form=False)]
    sent = []
    monkeypatch.setattr(ai, "_provider_call", lambda body: (sent.append(body), response(json.dumps(
        {"actions": [action], "memory": "", "subgoal_done": False})))[1])
    client, headers = signed_client(monkeypatch, caller)
    path = f"/ventures/{d['vid']}/device-work-orders/{order['id']}/browser-ai-call"
    result = client.post(path, json=request.model_dump(), headers=headers(path))
    assert result.status_code == 200, result.text
    assert result.json()["task_verified"] is False and len(sent) == 1
    proposal = result.json()["effect_proposals"][0]
    child = proposal["invocation_id"]
    assert effects.admit(*args, invocation_id=child, observation_digest=request.observation.digest(), effect_digest="e" * 64)
    assert not effects.admit(*args, invocation_id=child, observation_digest=request.observation.digest(), effect_digest="e" * 64)
    effects.receipt(*args, invocation_id=child, effect_digest="e" * 64, outcome="returned")
    saved = orders.retrieve(*args)["invocations"]
    assert saved[request.invocation_id]["bounded_reads"] is True and saved[request.invocation_id]["actual_micro"] > 0
    assert saved[child]["no_provider_io"] is True and saved[child]["actual_micro"] == 0
    assert saved[child]["effect"]["state"] == "returned"


@pytest.mark.parametrize("operation", ["navigate", "scroll", "extract"])
def test_unmarked_v1_cannot_save_new_read_effects(journey_db, monkeypatch, operation):
    from app.build import device_invocations as calls
    from coding_agents.jev_decision import Candidate
    d = journey_db
    orders, _, order, args = read_order(d, monkeypatch)
    parent, _ = completed_parent(args, order, marker=False)
    with pytest.raises(ValueError, match="device_effect_proposal_unavailable"):
        calls.save_browser_actor_proposal(*args, invocation_id=parent, candidate=Candidate(operation, "document_" + "b" * 64, "c" * 64))
    assert "bounded_reads" not in orders.retrieve(*args)["invocations"][parent]


@pytest.mark.parametrize("operation", ["click", "type", "hover", "new_tab"])
def test_bounded_read_marker_does_not_widen_read_only_effect_authority(journey_db, monkeypatch, operation):
    from app.build import device_invocations as calls
    from coding_agents.jev_decision import Candidate
    d = journey_db
    _, _, order, args = read_order(d, monkeypatch)
    parent, _ = completed_parent(args, order)
    with pytest.raises(ValueError, match="device_effect_proposal_unavailable"):
        calls.save_browser_actor_proposal(*args, invocation_id=parent, candidate=Candidate(operation, "c1", "c" * 64))


@pytest.mark.parametrize("marker", [False, 0, 1, "true", "false"])
def test_marker_requires_literal_true_in_dto_and_durable_admission(journey_db, monkeypatch, marker):
    from app.build import device_browser_ai as ai, device_invocations as calls
    d = journey_db
    orders, _, order, args = read_order(d, monkeypatch)
    with pytest.raises(ValidationError):
        ai.BrowserContext(bounded_reads=marker)
    request = marked_request(order)
    with pytest.raises(ValueError, match=ai.INVALID):
        calls.admit(*args, invocation_id="bad_marker", request_digest="a" * 64, quote_micro=100,
            browser_phase={"phase": "actor", "profile_digest": request.execution_profile_digest,
                "checkpoint_digest": "c" * 64, "load": request.current_load.model_dump(), "bounded_reads": marker})
    assert not orders.retrieve(*args).get("invocations")


def test_bounded_marker_requires_source_notes_v2_before_provider_or_invocation(journey_db, monkeypatch):
    from app.build import device_browser_ai as ai
    d = journey_db
    orders, _, order, args = read_order(d, monkeypatch)
    request = marked_request(order)
    notes = json.loads(request.context.notes)
    notes["schema_version"] = 1
    request.context.notes = json.dumps(notes)
    monkeypatch.setattr(ai, "_provider_call", lambda _: pytest.fail("opaque source reached provider"))
    with pytest.raises(ValueError, match=ai.INVALID):
        ai.run(*args, request=request)
    assert not orders.retrieve(*args).get("invocations")


@pytest.mark.parametrize("change", ["latest", "state", "phase", "profile"])
def test_source_read_parent_must_remain_latest_completed_actor_on_current_profile(journey_db, monkeypatch, change):
    from app.build import device_invocations as calls
    from coding_agents.jev_decision import Candidate
    d = journey_db
    _, _, order, args = read_order(d, monkeypatch)
    parent, _ = completed_parent(args, order)
    with d["factory"]() as session:
        row, body, _ = calls._locked(session, *args)
        if change == "latest":
            body["browser_ai_latest_invocation_id"] = "newer_actor"
        else:
            body["invocations"][parent][{"state": "state", "phase": "phase", "profile": "profile_digest"}[change]] = {
                "state": "uncertain", "phase": "plan", "profile": "f" * 64}[change]
        row.provenance_json = json.dumps(body)
        session.commit()
    with pytest.raises(ValueError, match="device_effect_proposal_unavailable"):
        calls.save_browser_actor_proposal(*args, invocation_id=parent, candidate=Candidate("extract", "document_" + "b" * 64, "c" * 64))


@pytest.mark.parametrize("change", ["latest", "parent_state", "parent_phase", "profile"])
def test_existing_read_child_rechecks_parent_and_profile_before_effect_admission(journey_db, monkeypatch, change):
    from app.build import device_effects as effects, device_invocations as calls
    from coding_agents.jev_decision import Candidate
    d = journey_db
    _, _, order, args = read_order(d, monkeypatch)
    parent, _ = completed_parent(args, order)
    child = calls.save_browser_actor_proposal(*args, invocation_id=parent,
        candidate=Candidate("extract", "document_" + "b" * 64, "c" * 64))
    with d["factory"]() as session:
        row, body, _ = calls._locked(session, *args)
        if change == "latest":
            body["browser_ai_latest_invocation_id"] = "newer_actor"
        elif change == "profile":
            for key in (parent, child):
                body["invocations"][key]["profile_digest"] = "f" * 64
        else:
            body["invocations"][parent]["state" if change == "parent_state" else "phase"] = "uncertain" if change == "parent_state" else "plan"
        row.provenance_json = json.dumps(body)
        session.commit()
    with pytest.raises(ValueError, match="device_effect_proposal_unavailable"):
        effects.admit(*args, invocation_id=child, observation_digest="c" * 64, effect_digest="e" * 64)


def test_budget_only_l01_trial_binding_preserves_reviewed_public_read_permission(journey_db, monkeypatch):
    from app.build import device_effects as effects, device_invocations as calls
    from app.db.models import VentureDocument
    from coding_agents.jev_decision import Candidate
    d = journey_db
    orders, _, order, args = read_order(d, monkeypatch, trial_bound=True)
    binding = orders._qualification_binding(order)
    assert binding.task_id == "l01_research_synthesis" and not hasattr(binding, "allowed_operations")
    parent, _ = completed_parent(args, order)
    child = calls.save_browser_actor_proposal(*args, invocation_id=parent,
        candidate=Candidate("extract", "document_" + "b" * 64, "c" * 64))
    assert effects.admit(*args, invocation_id=child, observation_digest="c" * 64, effect_digest="e" * 64)
    effects.receipt(*args, invocation_id=child, effect_digest="e" * 64, outcome="returned")
    with d["factory"]() as session:
        trial = json.loads(session.get(VentureDocument, binding.trial_id).provenance_json)
        assert trial["attempts"][order["id"] + ":" + parent]["state"] == "known"
        assert len(trial["attempts"]) == 1 and not trial["requests"]


def test_fixture_grant_still_narrows_bounded_read_marker(journey_db, monkeypatch):
    from dataclasses import replace
    from app.build import browser_qualification as qualification, device_invocations as calls, device_orders as orders
    from app.db.models import VentureDocument
    from coding_agents.jev_decision import Candidate
    d = journey_db
    _, _, _, order, grant, args = seeded_trial(d, monkeypatch, task_id="read_fact")
    # Trusted seeded scope deliberately lacks extract. All review/trial seals
    # are updated together; the explicit marker cannot widen this grant.
    with d["factory"]() as session:
        row, body, _ = calls._locked(session, *args)
        narrowed = replace(grant, allowed_operations=("navigate", "wait"), review_digest="0" * 64)
        body["browser_qualification_scope"] = qualification.scope_policy(narrowed)
        body["review_digest"] = orders._digest(body)
        narrowed = replace(narrowed, review_digest=body["review_digest"])
        body["browser_qualification_grant"] = narrowed.to_dict()
        trial_row = session.get(VentureDocument, grant.trial_id)
        trial = json.loads(trial_row.provenance_json)
        trial["grants"][body["id"]] = narrowed.to_dict()
        trial_row.provenance_json, row.provenance_json = json.dumps(trial), json.dumps(body)
        session.commit()
        order = body
    parent, _ = completed_parent(args, order)
    with pytest.raises(ValueError, match="device_effect_proposal_unavailable"):
        calls.save_browser_actor_proposal(*args, invocation_id=parent,
            candidate=Candidate("extract", "document_" + "b" * 64, "c" * 64))


@pytest.mark.parametrize("change", ["parent_phase", "parent_source", "child_state"])
def test_fixture_click_child_keeps_completed_actor_lineage_at_effect_admission(journey_db, monkeypatch, change):
    from app.build import device_effects as effects, device_invocations as calls
    from coding_agents.jev_decision import Candidate
    d = journey_db
    _, _, _, order, _, args = seeded_trial(d, monkeypatch)
    parent, _ = completed_parent(args, order)
    child = calls.save_browser_actor_proposal(*args, invocation_id=parent,
        candidate=Candidate("click", "c1", "c" * 64))
    with d["factory"]() as session:
        row, body, _ = calls._locked(session, *args)
        if change == "parent_phase":
            body["invocations"][parent]["phase"] = "plan"
        elif change == "parent_source":
            body["invocations"][parent]["source_kind"] = "foreign_source"
        else:
            body["invocations"][child]["state"] = "retired"
        row.provenance_json = json.dumps(body)
        session.commit()
    with pytest.raises(ValueError, match="device_effect_proposal_unavailable"):
        effects.admit(*args, invocation_id=child, observation_digest="c" * 64, effect_digest="e" * 64)


def controller():
    path = Path(__file__).parents[2] / "scripts/desktop/cu_browser_qualification_controller.py"
    spec = importlib.util.spec_from_file_location("pg_qualification_controller", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_actual_controller_stages_one_pending_order_and_advances_only_after_real_stop_settlement(journey_db, monkeypatch):
    from app.build import device_orders as orders, device_stop
    from app.db.models import CreditAccount, VentureDocument
    d = journey_db
    caller, _ = device(d, monkeypatch)
    qa = controller()
    kwargs = {"seed_digest": "a" * 64, "expires_at": int((orders.utcnow() + timedelta(hours=1)).timestamp())}
    first = qa.prepare_next(d["vid"], caller, **kwargs)
    assert len(first["queue"]) == 36 and first["status"] == "pending_human_review" and first["human_clicks_required"] == 1
    assert first["task_id"] == qa.TASK_IDS[0] and sum(row["state"] == "pending" for row in first["queue"]) == 1
    assert first["provider_calls_dispatched"] == 0 and not first["order"].get("claimed_at")
    repeated = qa.prepare_next(d["vid"], caller, **kwargs)
    assert repeated["status"] == "existing_order" and repeated["order"] == first["order"]
    with d["factory"]() as session:
        account = session.scalar(select(CreditAccount).where(CreditAccount.venture_id == d["vid"]))
        assert account.spent_micro == account.reserved_micro == 0
        assert len(list(session.scalars(select(VentureDocument).where(
            VentureDocument.kind == orders.KIND, VentureDocument.venture_id == d["vid"])))) == 1
    args = (d["vid"], caller, first["order"]["id"])
    approved = orders.transition(*args, first["order"]["review_digest"], "approve")
    claimed = orders.transition(*args, approved["review_digest"], "claim")
    assert qa.prepare_next(d["vid"], caller, **kwargs)["order"]["id"] == claimed["id"]
    stopped = device_stop.record(*args, execution_id=claimed["execution_id"], evidence_digest="b" * 64,
        processes_stopped=False, task_verified=False)
    assert stopped["state"] == "claimed" and stopped["stop_reconciliation_required"]
    assert qa.prepare_next(d["vid"], caller, **kwargs)["task_id"] == qa.TASK_IDS[0]
    stopped = device_stop.record(*args, execution_id=claimed["execution_id"], evidence_digest="b" * 64,
        processes_stopped=True, task_verified=False)
    assert stopped["state"] == "stopped" and stopped["settlement_ledger_id"] and stopped["native_stop"]["processes_stopped"]
    next_task = qa.prepare_next(d["vid"], caller, **kwargs)
    assert next_task["task_id"] == qa.TASK_IDS[1] and next_task["status"] == "pending_human_review"
    assert next_task["order"]["id"] != claimed["id"] and next_task["order"]["state"] == "pending"


def test_controller_retains_expired_and_cancelled_seeds_without_reset(journey_db, monkeypatch):
    from app.build import device_orders as orders
    from app.db.models import VentureDocument
    d = journey_db
    caller, _ = device(d, monkeypatch)
    qa = controller()
    kwargs = {"seed_digest": "a" * 64, "expires_at": int((orders.utcnow() + timedelta(hours=1)).timestamp())}
    first = qa.prepare_next(d["vid"], caller, **kwargs)
    # Advance only the controller's wall clock; the saved review/grant is immutable.
    future = qa.date_ns(first["order"]["expires_at"]) + 1
    monkeypatch.setattr(qa.time, "time_ns", lambda: future)
    expired = qa.prepare_next(d["vid"], caller, **kwargs)
    assert expired["status"] == "expired_review_requires_cancel" and expired["human_clicks_required"] == 0
    assert expired["order"] == first["order"]
    args = (d["vid"], caller, first["order"]["id"])
    orders.transition(*args, first["order"]["review_digest"], "cancel")
    next_task = qa.prepare_next(d["vid"], caller, **kwargs)
    assert next_task["task_id"] == qa.TASK_IDS[1] and next_task["order"]["id"] != first["order"]["id"]
    with d["factory"]() as session:
        trial = json.loads(session.get(VentureDocument, first["trial"]["trial_id"]).provenance_json)
        assert trial["task_orders"][qa.TASK_IDS[0]] == first["order"]["id"]
        saved = json.loads(session.get(VentureDocument, first["order"]["id"]).provenance_json)
        assert saved["state"] == "cancelled"


def test_controller_waits_for_real_metered_result_and_settlement_after_stop(journey_db, monkeypatch):
    from app.build import device_invocations as calls, device_orders as orders, device_stop
    from app.db.models import VentureDocument
    d = journey_db
    caller, _ = device(d, monkeypatch)
    qa = controller()
    kwargs = {"seed_digest": "a" * 64, "expires_at": int((orders.utcnow() + timedelta(hours=1)).timestamp())}
    first = qa.prepare_next(d["vid"], caller, **kwargs)
    args = (d["vid"], caller, first["order"]["id"])
    approved = orders.transition(*args, first["order"]["review_digest"], "approve")
    claimed = orders.transition(*args, approved["review_digest"], "claim")
    request = marked_request(claimed)
    calls.admit(*args, invocation_id="awaiting_receipt", request_digest="a" * 64, quote_micro=100,
        browser_phase={"phase": "actor", "profile_digest": request.execution_profile_digest,
            "checkpoint_digest": "c" * 64, "load": request.current_load.model_dump(), "bounded_reads": True})
    stopped = device_stop.record(*args, execution_id=claimed["execution_id"], evidence_digest="b" * 64,
        processes_stopped=True, task_verified=False)
    assert stopped["state"] == "claimed" and stopped["stop_reconciliation_required"]
    held = qa.prepare_next(d["vid"], caller, **kwargs)
    assert held["task_id"] == first["task_id"] and held["order"]["id"] == claimed["id"]
    with d["factory"]() as session:
        trial = json.loads(session.get(VentureDocument, first["trial"]["trial_id"]).provenance_json)
        assert trial["attempts"][claimed["id"] + ":awaiting_receipt"]["state"] == "unknown"
    profile = claimed["manifest"]["general_browser_verification"]["execution_profile"]
    calls.record_result(*args, invocation_id="awaiting_receipt", actual_micro=1,
        usage={"model": profile["roles"]["actor"]["model"], "input_tokens": 1, "output_tokens": 1})
    stopped = device_stop.record(*args, execution_id=claimed["execution_id"], evidence_digest="b" * 64,
        processes_stopped=True, task_verified=False)
    assert stopped["state"] == "stopped" and stopped["settlement_ledger_id"] and stopped["settled_micro"] == 1
    assert qa.prepare_next(d["vid"], caller, **kwargs)["task_id"] == qa.TASK_IDS[1]


def test_error_document_verify_refuses_before_provider_admission(journey_db, monkeypatch):
    from app.build import device_browser_ai as ai
    from app.db.models import VentureDocument
    from tests.integration.test_postgres_browser_qualification_recovery import error_case, error_request
    d = journey_db
    _, _, order, grant, args, _, _, _, observation, proof, context = error_case(d, monkeypatch)
    request = error_request(order, context, observation, proof)
    request.phase = "verify"
    monkeypatch.setattr(ai, "_provider_call", lambda _: pytest.fail("failed source reached verifier"))
    with pytest.raises(ValueError, match=ai.INVALID):
        ai.run(*args, request=request)
    with d["factory"]() as session:
        saved = json.loads(session.get(VentureDocument, order["id"]).provenance_json)
        trial = json.loads(session.get(VentureDocument, grant.trial_id).provenance_json)
        assert not saved.get("invocations") and not trial["attempts"]
