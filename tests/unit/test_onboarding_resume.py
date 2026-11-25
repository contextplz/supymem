"""Resume after a failed run continues that run — it does not start from orient."""

from __future__ import annotations

import inspect

from tests.company_os_harness import load_company_os_app


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/resume.db")
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "wm.sqlite3"))
    monkeypatch.setenv("PROVISIONING_MODE", "local")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("LLM_OFFLINE", "true")
    monkeypatch.setenv("CODING_AGENT", "inhouse_stub")
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    return main


def _seed_venture_and_run(*, status: str, phase: str, name: str = "Retry Co"):
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
                name=name,
                raw_idea="a waitlist for neighbourhood tool libraries",
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
                status=status,
                phase=phase,
                phases_done=3,
                phases_total=10,
                error="network" if status == "failed" else None,
                flags_json="{}",
                started_at=now,
                updated_at=now,
                finished_at=now if status != "running" else None,
            )
        )
        session.commit()
        session.add(
            OnboardingStep(
                id=new_id("ost"),
                run_id=run_id,
                step_id="orient.read_brief",
                phase="orient",
                attempt=1,
                status="done",
                idempotency_key="k",
                output_json='{"normalized":"idea"}',
                started_at=now,
                finished_at=now,
            )
        )
        session.commit()
    return vid, run_id


def test_resume_after_failed_continues_same_run(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    from app.api import onboarding as onboard_api
    from app.onboarding.gates import prepare_failed_resume
    from app.onboarding.spawn import start_onboarding
    from app.onboarding.state import load_run, resumable_run

    assert "resumable_run" in inspect.getsource(onboard_api.start_onboard)
    assert "resumable_run" in inspect.getsource(start_onboarding)

    vid, run_id = _seed_venture_and_run(status="failed", phase="validate")
    found = resumable_run(vid)
    assert found is not None
    assert found.id == run_id
    assert found.status == "failed"

    prepare_failed_resume(found)
    refreshed = load_run(run_id)
    assert refreshed.id == run_id
    assert refreshed.phase == "validate"
    assert refreshed.phase != "orient"


def test_resumable_run_ignores_completed_latest(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    from app.onboarding.state import resumable_run

    vid, _run_id = _seed_venture_and_run(status="done", phase="handoff", name="Done Co")
    assert resumable_run(vid) is None
