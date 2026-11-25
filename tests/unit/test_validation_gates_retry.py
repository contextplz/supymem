"""Leftover durable MEM must not skip founder quiz / validation on a new or failed run."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/gates.db")
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "wm.sqlite3"))
    monkeypatch.setenv("PROVISIONING_MODE", "local")
    monkeypatch.setenv("ONBOARDING_SYNC", "1")
    monkeypatch.setenv("LLM_OFFLINE", "true")
    monkeypatch.setenv("CODING_AGENT", "inhouse_stub")
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    return main


def _fake_ctx(venture_id: str, run_id: str, mode: str = "guided"):
    from app.onboarding.state import Ctx

    class _Ven:
        id = venture_id

    class _Run:
        id = run_id
        flags_json = "{}"

    ctx = Ctx.__new__(Ctx)
    ctx.venture = _Ven()
    ctx.run = _Run()
    ctx.mode = mode
    ctx.out = {}
    ctx.narrator = None
    return ctx


@pytest.mark.asyncio
async def test_leftover_venture_mem_does_not_skip_validation_gates(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    from app.onboarding.steps.validate import decision, review_choice
    from app.state import MEM

    vid = "ven_prior"
    old_run = "obr_old"
    new_run = "obr_new"
    MEM[f"validation_review:{vid}"] = "summary"
    MEM[f"validation_decision:{vid}"] = {"action": "proceed", "actor": "founder"}
    MEM[f"validation_review:{vid}:{old_run}"] = "summary"
    MEM[f"validation_decision:{vid}:{old_run}"] = {"action": "proceed", "actor": "founder"}

    ctx = _fake_ctx(vid, new_run)
    reviewed = await review_choice(ctx)
    assert reviewed.pause is not None
    assert reviewed.pause.reason == "awaiting_validation_review"

    decided = await decision(ctx)
    assert decided.pause is not None
    assert decided.pause.reason == "awaiting_validation_decision"


def test_failed_resume_rewinds_to_validate_and_voids_gates(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    from app.db.models import OnboardingRun, OnboardingStep, Venture
    from app.onboarding.gates import prepare_failed_resume
    from app.onboarding.state import list_steps, load_run
    from app.state import MEM, db
    from core.clock import utcnow
    from core.ids import new_id

    now = utcnow()
    vid = new_id("ven")
    run_id = new_id("obr")
    factory = db()
    with factory() as session:
        session.add(
            Venture(
                id=vid,
                name="Retry Co",
                raw_idea="compost pickup for apartment plants",
                stage="draft",
                autonomy_level="L1",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
        session.add(
            OnboardingRun(
                id=run_id,
                venture_id=vid,
                entry_mode="idea",
                mode="guided",
                status="failed",
                phase="commit",
                phases_done=5,
                phases_total=10,
                error="network",
                flags_json="{}",
                started_at=now,
                updated_at=now,
                finished_at=now,
            )
        )
        session.commit()
        for step_id, phase in (
            ("validate.ask", "validate"),
            ("validation.review_choice", "validation_gate"),
            ("validation.decision", "validation_gate"),
            ("orient.read_brief", "orient"),
        ):
            session.add(
                OnboardingStep(
                    id=new_id("ost"),
                    run_id=run_id,
                    step_id=step_id,
                    phase=phase,
                    attempt=1,
                    status="done",
                    idempotency_key="k",
                    output_json="{}",
                    started_at=now,
                    finished_at=now,
                )
            )
        session.commit()

    MEM[f"validation_review:{vid}"] = "summary"
    MEM[f"validation_decision:{vid}"] = {"action": "proceed"}
    MEM[f"answers:{vid}"] = {"audience": "anyone"}

    run = load_run(run_id)
    prepare_failed_resume(run)
    refreshed = load_run(run_id)
    assert refreshed.phase == "validate"
    assert refreshed.phases_done == 3
    remaining = {s.step_id for s in list_steps(run_id)}
    assert "validate.ask" not in remaining
    assert "validation.review_choice" not in remaining
    assert "validation.decision" not in remaining
    assert "orient.read_brief" in remaining
    assert MEM.get(f"validation_review:{vid}") is None
    assert MEM.get(f"validation_decision:{vid}") is None
    assert MEM.get(f"answers:{vid}") is None
