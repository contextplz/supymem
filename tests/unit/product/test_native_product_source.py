"""Disabled native source contracts preserve reviewed acceptance and scenario identity."""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE = Path(__file__).resolve().parents[3] / "services/company_os/app/build/native_product_source.py"
SPEC = importlib.util.spec_from_file_location("native_product_source_qa", MODULE)
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


@pytest.fixture
def task():
    journey = {"schema": "local_product_journey_v1", "steps": [
        {"name": "create_booking", "actor": 0, "method": "POST", "path": "/api/product/bookings",
         "body": {"label": "Synthetic booking"}, "status": 201, "capture": "id"},
        {"name": "other_customer", "actor": 1, "method": "GET", "path": "/api/product/bookings/{{create_booking}}",
         "status": 404},
        {"name": "anonymous_denied", "actor": None, "method": "GET", "path": "/api/product/bookings",
         "status": 401},
    ]}
    return {"key": "t1", "title": "Prepare booking source", "description": "A bounded proposed booking workflow.",
            "capability_id": module.CAPABILITY, "implementation": "new_code",
            "acceptance": ["An authenticated customer creates a synthetic booking.", "Other customers cannot read it."],
            "product_source_expectations": {"profile": module.PROFILE, "journey_json": json.dumps(journey),
                "criteria": [{"criterion": 0, "step_names": ["create_booking"]},
                             {"criterion": 1, "step_names": ["other_customer", "anonymous_denied"]}]}}


@pytest.fixture
def scope(task):
    plan = SimpleNamespace(id="plan_one", version=2, workspace_id="w_one", venture_id="v_one",
        state="approved", approved_by="person_one", approved_at=datetime(2026, 10, 2), superseded_by=None,
        structured=json.dumps({"proposal": {"milestones": [{"key": "m3", "kind": "core", "included": True, "acceptance": ["Milestone-level summary only"], "tasks": [task]}]},
                               "entity_ids": {"tasks": {"m3:t1": "task_one"}, "milestones": {"m3": "mile_one"}}}))
    selected = {"plan_id": plan.id, "plan_version": 2, "milestone_id": "mile_one", "task_id": "task_one",
                "title": task["title"], "description": task["description"], "acceptance": '["Milestone-level summary only"]',
                "tool_hints": json.dumps([module.CAPABILITY]),
                "approved_by": plan.approved_by, "approved_at": plan.approved_at.isoformat()}
    return plan, selected


def test_contract_binds_exact_task_acceptance_and_canonical_reviewed_scenario(task):
    before = copy.deepcopy(task)
    bound = module.contract(task)
    assert bound["task_acceptance"] == task["acceptance"]
    assert bound["runtime_contract"]["record_identity"] == "platform_authenticated_customer"
    assert bound["runtime_contract"]["required_files"] == ["product/__init__.py", "product/routes.py", "product/static/index.html"]
    assert bound["runtime_contract"]["hosting_authorized"] is False
    task["product_source_expectations"]["journey_json"] = json.dumps(json.loads(task["product_source_expectations"]["journey_json"]), indent=1)
    assert module.contract(task) == bound
    assert before["acceptance"] == task["acceptance"]
    bound["runtime_contract"]["required_files"].clear()
    assert len(module.RUNTIME_CONTRACT["required_files"]) == 3


@pytest.mark.parametrize("change", ["missing", "foreign_step", "duplicate", "boolean_index", "malformed_scenario", "remote_path", "boolean_actor"])
def test_contract_rejects_ambiguous_or_unreviewed_scenario_coverage(task, change):
    expected = task["product_source_expectations"]
    if change == "missing":
        expected["criteria"].pop()
    elif change == "foreign_step":
        expected["criteria"][0]["step_names"] = ["unknown_step"]
    elif change == "duplicate":
        expected["criteria"][0]["step_names"] *= 2
    elif change == "boolean_index":
        expected["criteria"][0]["criterion"] = False
    elif change == "malformed_scenario":
        expected["journey_json"] = "not-json"
    else:
        journey = json.loads(expected["journey_json"])
        journey["steps"][0]["path" if change == "remote_path" else "actor"] = "//outside.example" if change == "remote_path" else True
        expected["journey_json"] = json.dumps(journey)
    with pytest.raises(ValueError, match="native_product_source"):
        module.contract(task)


def test_snapshot_uses_selected_task_not_milestone_summary_or_client_contract(scope, task):
    plan, selected = scope
    selected["product_source_contract"] = {"scenario_sha256": "client-forged"}
    bound = module.snapshot(plan, selected)
    assert bound["product_source_contract"]["task_acceptance"] == task["acceptance"]
    assert bound["product_source_contract"]["scenario_sha256"] != "client-forged"
    assert bound["product_source_contract"]["venture_id"] == "v_one"
    assert bound["artifact_profile"] == "founder_product_v1"
    assert not bound["verified_execution"] and not bound["runtime_qa_verified"] and not bound["hosting_authorized"]
    module.assert_current(plan, bound)


@pytest.mark.parametrize("change", ["acceptance", "scenario", "criterion"])
def test_exact_saved_acceptance_or_scenario_drift_invalidates_snapshot(scope, change):
    plan, selected = scope
    saved = module.snapshot(plan, selected)
    document = json.loads(plan.structured)
    task = document["proposal"]["milestones"][0]["tasks"][0]
    if change == "acceptance":
        task["acceptance"][0] += " and cancel it"
    elif change == "scenario":
        journey = json.loads(task["product_source_expectations"]["journey_json"])
        journey["steps"][0]["status"] = 200
        task["product_source_expectations"]["journey_json"] = json.dumps(journey)
    else:
        task["product_source_expectations"]["criteria"][0]["step_names"] = ["anonymous_denied"]
    plan.structured = json.dumps(document)
    with pytest.raises(ValueError, match="native_product_source_snapshot_changed"):
        module.assert_current(plan, saved)


@pytest.mark.parametrize("change", ["unapproved", "superseded", "task_id", "milestone", "version", "boolean_version", "wrong_kind"])
def test_snapshot_rejects_unapproved_foreign_and_superseded_scope(scope, change):
    plan, selected = scope
    if change == "unapproved":
        plan.approved_by = None
    elif change == "superseded":
        plan.superseded_by = "new_plan"
    elif change == "task_id":
        selected["task_id"] = "foreign_task"
    elif change == "milestone":
        selected["milestone_id"] = "foreign_milestone"
    elif change == "version":
        selected["plan_version"] = 1
    elif change == "boolean_version":
        plan.version = 1
        selected["plan_version"] = True
    else:
        document = json.loads(plan.structured)
        document["proposal"]["milestones"][0]["kind"] = "landing"
        plan.structured = json.dumps(document)
    with pytest.raises(ValueError, match="native_product_source_snapshot_invalid"):
        module.snapshot(plan, selected)


@pytest.mark.parametrize("change", ["milestone_acceptance", "tool_hints", "excluded_milestone"])
def test_selected_metadata_cannot_survive_changed_saved_scope(scope, change):
    plan, selected = scope
    saved = module.snapshot(plan, selected)
    if change == "milestone_acceptance":
        saved["acceptance"] = '["Client-changed acceptance"]'
    elif change == "tool_hints":
        saved["tool_hints"] = '["build.run"]'
    else:
        document = json.loads(plan.structured)
        document["proposal"]["milestones"][0]["included"] = False
        plan.structured = json.dumps(document)
    with pytest.raises(ValueError, match="native_product_source_snapshot_invalid"):
        module.assert_current(plan, saved)
