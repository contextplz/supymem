"""Boot crash-sweep must clear leftover running onboarding rows."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/sweep.db")
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "wm.sqlite3"))
    monkeypatch.setenv("PROVISIONING_MODE", "local")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("LLM_OFFLINE", "true")
    monkeypatch.setenv("CODING_AGENT", "inhouse_stub")
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    return main


def _seed_running(venture_name: str = "Sweep Co"):
    from app.db.models import OnboardingRun, OnboardingStep, Venture
    from app.state import db
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
                name=venture_name,
                raw_idea="apartment compost pickup",
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
                status="running",
                phase="validate",
                phases_done=3,
                phases_total=10,
                flags_json="{}",
                started_at=now,
                updated_at=now,
            )
        )
        session.add(
            OnboardingStep(
                id=new_id("ost"),
                run_id=run_id,
                step_id="validate.demand_read",
                phase="validate",
                attempt=1,
                status="running",
                idempotency_key="k",
                started_at=now,
            )
        )
        session.commit()
    return vid, run_id


def test_crash_sweep_zero_clears_fresh_running_row(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    from app.onboarding.resume import crash_sweep
    from app.onboarding.state import list_steps, load_run

    _vid, run_id = _seed_running()
    touched = crash_sweep(stale_after_s=0)
    assert run_id in touched
    run = load_run(run_id)
    assert run.status == "paused"
    assert run.paused_reason == "interrupted"
    steps = list_steps(run_id)
    demand = next(s for s in steps if s.step_id == "validate.demand_read")
    assert demand.status == "failed"
    assert demand.error_class == "interrupted"


def test_crash_sweep_90s_leaves_fresh_running(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    from app.onboarding.resume import crash_sweep
    from app.onboarding.state import load_run

    _vid, run_id = _seed_running()
    touched = crash_sweep(stale_after_s=90)
    assert run_id not in touched
    run = load_run(run_id)
    assert run.status == "running"


def test_crash_sweep_clears_orphan_running_step_on_done_run(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    from app.db.models import OnboardingRun, OnboardingStep, Venture
    from app.onboarding.resume import crash_sweep
    from app.onboarding.state import list_steps, load_run
    from app.state import db
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
                name="Ghost Step Co",
                raw_idea="apartment compost pickup",
                stage="live",
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
                status="done",
                phase="handoff",
                phases_done=10,
                phases_total=10,
                flags_json="{}",
                started_at=now,
                updated_at=now,
                finished_at=now,
            )
        )
        session.commit()
        session.add(
            OnboardingStep(
                id=new_id("ost"),
                run_id=run_id,
                step_id="validate.demand_read",
                phase="validate",
                attempt=1,
                status="running",
                idempotency_key="k",
                started_at=now,
            )
        )
        session.commit()

    crash_sweep(stale_after_s=0)
    run = load_run(run_id)
    assert run.status == "done"
    demand = next(s for s in list_steps(run_id) if s.step_id == "validate.demand_read")
    assert demand.status == "failed"
    assert demand.error_class == "interrupted"


def test_bootstrap_calls_crash_sweep_with_zero(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    import inspect

    from app.bootstrap import bootstrap
    from app.onboarding.resume import crash_sweep
    from app.onboarding.state import load_run

    src = inspect.getsource(bootstrap)
    assert "crash_sweep(stale_after_s=0)" in src
    _vid, run_id = _seed_running("Boot Sweep Co")
    touched = crash_sweep(stale_after_s=0)
    assert run_id in touched
    run = load_run(run_id)
    assert run.status == "paused"
    assert run.paused_reason == "interrupted"


@pytest.mark.parametrize("domain", ["vendor-inventory.example", "weekend-hiking.example"])
def test_worker_bootstrap_does_not_interrupt_live_onboarding(tmp_path, monkeypatch, domain):
    _setup(tmp_path, monkeypatch)
    from app.bootstrap import bootstrap
    from app.onboarding.state import load_run

    _venture_id, run_id = _seed_running(domain)
    assert bootstrap(recover_onboarding=False) == []
    assert load_run(run_id).status == "running"
