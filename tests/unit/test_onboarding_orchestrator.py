"""Orchestrator completes every phase of the idea registry with stubbed steps."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app


@pytest.mark.asyncio
async def test_orchestrator_seven_phases(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/orch.db")
    monkeypatch.setenv("PROVISIONING_MODE", "local")
    monkeypatch.setenv("ONBOARDING_SYNC", "1")
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()

    from app.db.models import Venture
    from app.onboarding import registry
    from app.onboarding.orchestrator import run_onboarding
    from app.onboarding.registry import StepDef, StepResult, PHASES
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    # Replace all steps with no-ops that still narrate
    async def noop(ctx):
        return StepResult(output={"ok": True})

    stubs = [
        StepDef(s.id, s.phase, noop, s.narrate, kind=s.kind, required=s.required, timeout_s=5)
        for s in registry.all_steps()
    ]
    # Skip pause in validate.ask
    async def ask_auto(ctx):
        return StepResult(output={"n": 0})

    stubs = [
        StepDef(s.id, s.phase, ask_auto if s.id == "validate.ask" else noop, s.narrate, kind=s.kind, timeout_s=5)
        for s in stubs
    ]
    registry._STEPS["idea"] = stubs

    factory = db()
    now = utcnow()
    vid = new_id("ven")
    with factory() as session:
        session.add(
            Venture(
                id=vid,
                name="Stub Co",
                raw_idea="a marketplace for restoring vintage synthesizers",
                stage="draft",
                autonomy_level="L1",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    run = await run_onboarding(vid, mode="auto")
    assert run.status == "done"
    assert run.phases_done == len(PHASES)
    from app.onboarding.state import list_steps

    steps = list_steps(run.id)
    assert len(steps) >= len(PHASES)
    phases_seen = {s.phase for s in steps}
    # Every phase that owns at least one step must have been entered.
    for p in {s.phase for s in stubs}:
        assert p in phases_seen

    # reset registry for other tests
    registry.reset_steps_cache()
