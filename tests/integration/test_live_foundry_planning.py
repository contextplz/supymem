"""Opt-in planner schema/receipt acceptance with real Foundry, synthetic local data.

Research input is an explicit test fixture, not a claim of observed market demand.
No production account data, deployed products or outreach are changed.
"""

from __future__ import annotations

import json
import os

import pytest
from sqlalchemy import select

from tests.integration.test_live_foundry_research_validation import configure
from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db
from tests.integration.test_postgres_planning import run
from tests.planning_fixtures import seed_research

pytestmark = pytest.mark.skipif(
    os.environ.get("SUPYMEM_FOUNDRY_PLAN_SMOKE") != "1",
    reason="Live planning requires explicit opt-in and loopback PostgreSQL",
)


@pytest.mark.parametrize("synthetic_builder_ready", [False, True])
def test_real_planner_schema_credits_review_and_approval(journey_db, monkeypatch, synthetic_builder_ready):
    from app.credits.accounts import ensure_account, get_account
    from app.db.models import CreditLedgerEntry, Job
    from app.db.supymem_models import ExecutionPlan
    from app.planning import compiler, execution
    from app.planning import author
    from app.planning.resources import update_inventory
    from app.tenancy import bind

    data = journey_db
    configure(monkeypatch)
    if synthetic_builder_ready:
        # Exercise landing-contract generation, not real builder readiness.
        # Only synthetic local planning runs; no build/deploy is authorized.
        from app.builder import budgets
        monkeypatch.setattr(budgets, "runtime_quote", lambda _workspace_id: {
            "ready": True, "ceiling_micro": 120000,
        })
    # Observe the real provider/validator; do not replace either result. Diagnostics
    # contain synthetic test data only and never credentials or request headers.
    from llm_gateway import sole

    original_transmit, original_parse = sole.transmit, author.parse

    async def observe(*args, **kwargs):
        response = await original_transmit(*args, **kwargs)
        choice = response.choices[0]
        print(
            json.dumps(
                {
                    "finish_reason": choice.finish_reason,
                    "content_chars": len(choice.message.content or ""),
                    "usage": response.usage.model_dump() if response.usage else None,
                }
            )
        )
        return response

    def parse(raw, context):
        try:
            return original_parse(raw, context)
        except Exception as exc:
            from pathlib import Path

            target = (
                Path(os.environ.get("SUPYMEM_PLAN_DIAGNOSTIC_DIR", "/tmp"))
                / "supymem-synthetic-plan-invalid.json"
            )
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(raw)
            print(
                json.dumps(
                    {
                        "validation_failure": type(exc).__name__,
                        "detail": str(exc)[:1500],
                        "synthetic_response": str(target),
                    }
                )
            )
            raise

    monkeypatch.setattr(sole, "transmit", observe)
    monkeypatch.setattr(author, "parse", parse)
    seed_research(data)
    update_inventory(data["wid"], data["vid"], [], True)
    ensure_account(data["vid"], workspace_id=data["wid"], owner_person_id=data["actor"])
    quote = compiler.generation_quote(data["vid"], data["wid"])
    assert quote["model"] == "DeepSeek-V4-Pro" and quote["sufficient_now"]
    run(data)
    plan_id = execution.identifier(data["job_id"])
    with data["factory"]() as session:
        job = session.get(Job, data["job_id"])
        assert job.status == "succeeded", job.last_error
        row = session.get(ExecutionPlan, plan_id)
        receipt = json.loads(row.structured)["_runtime"]["operations"]["plan"]
        assert receipt["state"] == "completed"
        spends = session.scalars(
            select(CreditLedgerEntry).where(
                CreditLedgerEntry.account_id == get_account(data["vid"]).id,
                CreditLedgerEntry.kind == "spend",
                CreditLedgerEntry.amount_micro < 0,
            )
        ).all()
        assert len(spends) == 1
        charged = -spends[0].amount_micro
    plan = compiler.get_plan(plan_id)
    assert plan["reviewable"] and plan["generation_state"] == "completed"
    if synthetic_builder_ready:
        assert any(task["capability_id"] == "build.run" and task["implementation"] == "new_code"
                   and task.get("landing_expectations") for task in plan["milestones"][0]["tasks"])

    if synthetic_builder_ready:
        # The synthetic runtime quote contributes its bounded 12-credit estimate.
        assert plan["estimate"]["total_upfront_credits"] == 12.0
    else:
        assert plan["estimate"]["total_upfront_credits"] is None
    assert 0 < charged <= quote["ceiling_micro"]
    updated = compiler.patch_plan(
        plan_id, edit_version=plan["edit_version"], patch={"target_days": 21}
    )
    with bind(workspace_id=data["wid"], venture_id=data["vid"], actor=data["actor"]):
        approved = compiler.approve_plan(
            plan_id, person_id=data["actor"], review_token=updated["review_token"]
        )
    assert approved["execution_started"] is False
    assert get_account(data["vid"]).reserved_micro == 0
    print(
        json.dumps(
            {
                "workflow": "plan.compile",
                "job_state": "succeeded",
                "schema_valid": True,
                "provider": "azure_foundry",
                "model": "DeepSeek-V4-Pro",
                "charged_micro": charged,
                "milestones": len(plan["milestones"]),
                "manual_edit": "saved",
                "approval": "recorded",
                "execution_started": False,
                "research_input": "synthetic_fixture",
            }
        )
    )
