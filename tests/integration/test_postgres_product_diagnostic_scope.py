"""Migrated DB exact first-app scope; unavailable capability/order are synthetic."""

import json
import uuid

import pytest
from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401
from tests.integration.test_postgres_planning import planning as planning  # noqa: F401
from tests.integration.test_postgres_build_intents import ready_plan as ready_plan  # noqa: F401
from tests.unit.product import test_native_product_source as foundation


@pytest.fixture
def first_product(ready_plan):
    from app.build import (
        device_orders,
        native_product_source as native,
        subscription_product_scope as scope,
    )
    from app.db.supymem_models import ExecutionPlan, PlanMilestone, PlanTask

    d = ready_plan
    with d["factory"]() as session:
        plan = session.get(ExecutionPlan, d["plan_id"])
        task = session.get(PlanTask, d["plan_task_id"])
        milestone = session.get(PlanMilestone, task.milestone_id)
        document = json.loads(plan.structured)
        row = next(
            row
            for row in document["proposal"]["milestones"]
            if document["entity_ids"]["milestones"][row["key"]] == milestone.id
        )
        saved = next(
            item
            for item in row["tasks"]
            if document["entity_ids"]["tasks"][row["key"] + ":" + item["key"]]
            == task.id
        )
        contract = foundation.task.__wrapped__()
        saved.update(
            {
                **contract,
                "key": saved["key"],
                "title": task.title,
                "description": task.description,
            }
        )
        row.update(kind="core", included=True)
        milestone.kind, milestone.scope = "core", "in"
        task.tool_hints = json.dumps([native.CAPABILITY])
        plan.structured = json.dumps(document)
        # This synthetic DB setup is not an approval/runtime availability path.
        session.flush()
        selected = native.snapshot(
            plan, device_orders._task_snapshot(session, d["wid"], d["vid"], task.id)
        )
        bound = scope.review_first_app(
            session, plan, selected, app_id=d["app_id"], actor=d["actor"]
        )
        session.commit()
    return {**d, "selected": selected, "bound": bound}


def test_first_product_binds_exact_explicit_founder_created_app_without_mutation(
    first_product,
):
    from app.build import subscription_product_scope as scope
    from app.db.supymem_models import ExecutionPlan, FounderApp, BuildRun
    from sqlalchemy import select
    from app.infra.apps import create_draft

    d = first_product
    other = create_draft(
        workspace_id=d["wid"],
        venture_id=d["vid"],
        actor=d["actor"],
        name="Second exact product fixture",
        slug="second-" + uuid.uuid4().hex,
    )
    with d["factory"]() as session:
        plan = session.get(ExecutionPlan, d["plan_id"])
        bound = scope.review_first_app(
            session, plan, d["selected"], app_id=d["app_id"], actor=d["actor"]
        )
        assert bound == d["bound"] and bound["app_id"] != other["id"]
        assert bound["product_app_baseline"]["prior_deployment_id"] is None
        assert session.get(FounderApp, d["app_id"]).state == "draft"
        assert not session.scalar(
            select(BuildRun.id).where(BuildRun.app_id == d["app_id"])
        )
        assert (
            bound["runtime_qa_verified"] is False
            and bound["hosting_authorized"] is False
        )


@pytest.mark.parametrize(
    "change",
    [
        "foreign_scope",
        "unreviewed_app",
        "prior_run",
        "deployed_state",
        "creation_changed",
    ],
)
def test_first_product_rejects_foreign_unreviewed_or_preexisting_app(
    first_product, change
):
    from app.build import subscription_product_scope as scope
    from app.db.supymem_models import (
        ExecutionPlan,
        FounderApp,
        FounderRequest,
        BuildRun,
    )
    from app.credits._txn import naive_utc
    from sqlalchemy import select

    d = first_product
    with d["factory"]() as session:
        plan = session.get(ExecutionPlan, d["plan_id"])
        app = session.get(FounderApp, d["app_id"])
        if change == "foreign_scope":
            app.venture_id = "other-venture-synthetic"
        elif change == "unreviewed_app":
            request = session.scalar(
                select(FounderRequest).where(
                    FounderRequest.related_id == app.id,
                    FounderRequest.source == "app_create",
                )
            )
            request.classification = "unsupported"
        elif change == "prior_run":
            session.add(
                BuildRun(
                    id="fixture_prior_" + uuid.uuid4().hex,
                    app_id=app.id,
                    workspace_id=d["wid"],
                    venture_id=d["vid"],
                    trigger="fixture",
                    agent="fixture",
                    state="failed",
                    created_at=naive_utc(),
                    closed_at=naive_utc(),
                    spec="{}",
                )
            )
        elif change == "deployed_state":
            app.state = "live"
        else:
            app.slug += "-changed"
        # Avoid writing a nonexistent foreign FK; pure identity check uses row.
        if change != "foreign_scope":
            session.flush()
        with pytest.raises(ValueError, match="subscription_product_first_app"):
            with session.no_autoflush:
                scope.review_first_app(
                    session, plan, d["selected"], app_id=d["app_id"], actor=d["actor"]
                )


def test_settled_product_scope_binds_order_execution_provider_receipt_and_current_scenario(
    first_product,
):
    from types import SimpleNamespace
    from app.build import device_orders, subscription_product_scope as scope
    from app.db.models import VentureDocument
    from app.db.supymem_models import CodingSourceReceipt
    from app.credits._txn import naive_utc

    d = first_product
    caller = SimpleNamespace(
        sub=d["actor"], role="user", legacy=False, device_id="dev_product_fixture"
    )
    suffix = uuid.uuid4().hex
    oid, eid = "dwo_product_" + suffix, "dex_product_" + suffix
    with d["factory"]() as session:
        now = naive_utc()
        body = {
            "id": oid,
            "workspace_id": d["wid"],
            "venture_id": d["vid"],
            "actor_id": d["actor"],
            "device_id": caller.device_id,
            "state": "stopped",
            "claimed_at": now.isoformat(),
            "closed_at": now.isoformat(),
            "settlement_ledger_id": "synthetic-device-settlement",
            "execution_id": eid,
            "native_stop": {"processes_stopped": True, "execution_id": eid},
            "task_snapshot": d["bound"],
            "manifest": {
                "task_id": d["plan_task_id"],
                "coding_verification": {
                    "operation": "workspace_edit_v1",
                    "provider": "codex",
                },
            },
        }
        order = VentureDocument(
            id=oid,
            venture_id=d["vid"],
            kind=device_orders.KIND,
            title="Unavailable product fixture",
            provenance_json=json.dumps(body),
            created_at=now,
            updated_at=now,
        )
        source = CodingSourceReceipt(
            order_id=oid,
            workspace_id=d["wid"],
            venture_id=d["vid"],
            app_id=d["app_id"],
            actor_id=d["actor"],
            device_id=caller.device_id,
            plan_task_id=d["plan_task_id"],
            execution_id=eid,
            provider="codex",
            state="stored",
            sha256="a" * 64,
            manifest_sha256="b" * 64,
            size_bytes=3,
            receipt_json=json.dumps(
                {
                    "version_id": "synthetic-not-s3-readback",
                    "sha256": "a" * 64,
                    "manifest_digest": "b" * 64,
                    "size_bytes": 3,
                }
            ),
            created_at=now,
            updated_at=now,
        )
        session.add_all([order, source])
        session.flush()
        identity = scope.current(session, d["vid"], oid, caller)
        assert identity["scope"]["app_id"] == d["app_id"]
        assert (
            identity["scope"]["execution_id"] == eid
            and identity["scope"]["provider"] == "codex"
        )
        assert (
            not identity["runtime_qa_verified"] and not identity["hosting_authorized"]
        )
        source.provider = "claude"
        session.flush()
        with pytest.raises(ValueError, match="custody_changed"):
            scope.current(session, d["vid"], oid, caller)
        source.provider = "codex"
        body["task_snapshot"]["product_source_contract"]["scenario"]["steps"][0][
            "status"
        ] = 200
        order.provenance_json = json.dumps(body)
        session.flush()
        with pytest.raises(ValueError, match="scope_changed"):
            scope.current(session, d["vid"], oid, caller)
