"""Disabled native product-source foundation; bindings grant no execution or QA.

The scenario is separately reviewed data, never loaded from the captured archive.
Structural coverage is not proof that a scenario tests the stated requirements.
"""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from core.product_qa_contract import validate_product_journey
from pydantic import BaseModel, ConfigDict, Field

CAPABILITY = "coding.subscription_product_source"
PROFILE = "native_python_product_source_v1"
RUNTIME_CONTRACT = {
    "schema_version": 1,
    "runtime": "python_product_v1",
    "required_files": ["product/__init__.py", "product/routes.py", "product/static/index.html"],
    "router_export": "product.routes.router",
    "router_mount": "/api/product",
    "record_api": "/api/records",
    "record_identity": "platform_authenticated_customer",
    "external_integrations": False,
    "hosting_authorized": False,
}


class ProductSourceCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    criterion: int = Field(ge=0, le=5)
    step_names: list[Annotated[str, Field(min_length=3, max_length=64, pattern=r"^[a-z][a-z0-9_]{2,63}$")]] = Field(min_length=1, max_length=60)


class ProductSourceExpectations(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    profile: Literal["native_python_product_source_v1"] = Field(json_schema_extra={"enum": [PROFILE]})
    journey_json: str = Field(min_length=1, max_length=16384, description=(
        'Complete JSON, never prose: {"schema":"local_product_journey_v1","steps":[...]}. '
        'Root keys are exactly schema and steps; include 2-60 steps. Each step requires '
        'name (unique, matching ^[a-z][a-z0-9_]{2,63}$), actor (integer 0 or 1, or null '
        'for anonymous), method (GET/POST/PUT/PATCH/DELETE), path (local /api/product/... '
        'or /api/records/...; no query string, URL or traversal), and status (integer '
        '200-499). Optional keys only: body, expect, origin (same/foreign), contains '
        'and excludes (lists of strings), capture (field name, not null). Omit unused '
        'optional keys. criteria indices must match checks in order, and each '
        'step_names list must name existing steps without duplicates.'
    ))
    criteria: list[ProductSourceCriterion] = Field(min_length=1, max_length=6)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode()).hexdigest()


def contract(task: dict) -> dict:
    """Bind exact saved task acceptance to a bounded reviewed diagnostic scenario."""
    acceptance = task.get("acceptance")
    if (not isinstance(acceptance, list) or not 1 <= len(acceptance) <= 6
            or any(not isinstance(item, str) or not item.strip() or len(item) > 1800 for item in acceptance)):
        raise ValueError("native_product_source_acceptance_invalid")
    try:
        expected = ProductSourceExpectations.model_validate(task.get("product_source_expectations"))
        journey, _canonical, scenario_digest = validate_product_journey(expected.journey_json.encode())
    except ValueError as error:
        raise ValueError("native_product_source_contract_invalid") from error
    names = {step["name"] for step in journey["steps"]}
    if (len(expected.criteria) != len(acceptance)
            or [item.criterion for item in expected.criteria] != list(range(len(acceptance)))
            or any(len(item.step_names) != len(set(item.step_names))
                   or not set(item.step_names) <= names for item in expected.criteria)):
        raise ValueError("native_product_source_coverage_invalid")
    body = {"schema_version": 1, "profile": PROFILE,
            "runtime_contract": RUNTIME_CONTRACT,
            "task_acceptance": list(acceptance),
            "criteria": [item.model_dump() for item in expected.criteria],
            "scenario": journey, "scenario_sha256": scenario_digest}
    # Round-trip so callers cannot mutate the module's runtime contract.
    return {**json.loads(json.dumps(body)), "contract_sha256": _digest(body)}


def snapshot(plan, selected: dict) -> dict:
    """Recover one selected approved task through server-owned entity IDs.

    Deliberately not an executor route: the capability remains unavailable and
    the native coding allowlist does not admit it until the full QA chain exists.
    """
    try:
        if (plan.state not in {"approved", "executing"} or not plan.approved_by
                or not plan.approved_at or plan.superseded_by
                or selected.get("approved_by") != plan.approved_by
                or selected.get("approved_at") != plan.approved_at.isoformat()):
            raise ValueError("plan_not_approved")
        document = json.loads(plan.structured)
        pairs = [(milestone, task) for milestone in document["proposal"]["milestones"]
                 for task in milestone["tasks"]
                 if document["entity_ids"]["tasks"].get(milestone["key"] + ":" + task["key"])
                 == selected["task_id"]]
        if len(pairs) != 1:
            raise ValueError("ambiguous_task")
        milestone, task = pairs[0]
        if (milestone["kind"] != "core" or milestone.get("included") is not True
                or json.loads(selected["acceptance"]) != milestone["acceptance"]
                or json.loads(selected["tool_hints"]) != [CAPABILITY]
                or task["capability_id"] != CAPABILITY
                or task["implementation"] != "new_code"
                or selected["plan_id"] != plan.id or type(selected["plan_version"]) is not int
                or selected["plan_version"] != plan.version
                or selected["milestone_id"] != document["entity_ids"]["milestones"][milestone["key"]]
                or selected["title"] != task["title"] or selected["description"] != task["description"]):
            raise ValueError("task_scope")
        bound = contract(task)
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise ValueError("native_product_source_snapshot_invalid") from error
    return {**selected, "artifact_profile": "founder_product_v1",
            "product_source_contract": {**bound, "workspace_id": plan.workspace_id,
                                        "venture_id": plan.venture_id},
            "verified_execution": False, "runtime_qa_verified": False,
            "hosting_authorized": False}


def assert_current(plan, saved: dict) -> None:
    """Compare authoritative regenerated bindings; never accept client scenario data."""
    if snapshot(plan, saved) != saved:
        raise ValueError("native_product_source_snapshot_changed")
