"""Per-agent did / doing / next comes from assigned work, not invented copy."""

from __future__ import annotations

from sqlalchemy import select

from tests.company_os_harness import load_company_os_app


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/desk.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app import state
    from app.db.models import Venture
    from core.clock import utcnow

    state.reset_db_factory()
    now = utcnow()
    with state.db()() as session:
        session.add(
            Venture(
                id="ven_desk",
                workspace_id=None,
                name="Desk",
                raw_idea="Desk visibility",
                stage="org_planning",
                autonomy_level="L1",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()


def test_briefs_use_assigned_tasks(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    from app import state
    from app.chat.store import append_message
    from app.db.models import PlannedTask
    from app.org.characters import enrich_agent, stamp_speaker
    from app.org.desk import agent_desk, work_briefs
    from app.org.summary import persist_org, resolve_org
    from app.planning.store import save_tasks
    from core.clock import utcnow

    persist_org(
        "ven_desk",
        {
            "agents": [
                enrich_agent(
                    {
                        "id": "a1",
                        "role": "researcher",
                        "name": "Maya",
                        "status": "active",
                    }
                ),
                enrich_agent(
                    {
                        "id": "a2",
                        "role": "builder",
                        "name": "Cole",
                        "status": "quarantined",
                    }
                ),
            ]
        },
    )
    save_tasks(
        "ven_desk",
        [
            {
                "title": "Interview five buyers",
                "why_now": "Need demand evidence",
                "tags": ["research"],
                "owner_name": "Maya",
                "owner_role": "researcher",
                "owner_id": "a1",
            },
            {
                "title": "Ship the landing page",
                "why_now": "Need a URL",
                "tags": ["build"],
                "owner_name": "Cole",
                "owner_role": "builder",
                "owner_id": "a2",
            },
        ],
        source="plan",
    )
    now = utcnow()
    with state.db()() as session:
        for row in session.scalars(select(PlannedTask).where(PlannedTask.venture_id == "ven_desk")):
            if "Interview" in row.title:
                row.status = "done"
                row.finished_at = now
                row.updated_at = now
        session.commit()

    append_message(
        "ven_desk",
        role="assistant",
        kind="task_run",
        body_md=stamp_speaker(
            "Finished **Interview five buyers**.",
            {"name": "Maya", "title": "Researcher", "id": "a1"},
        ),
    )

    org = resolve_org("ven_desk")
    briefs = work_briefs("ven_desk", org.get("agents") or [])
    maya = briefs["a1"]
    cole = briefs["a2"]
    assert "Interview five buyers" in maya["did"]
    assert maya["next"] == "No queued task yet, the orchestrator may assign one."
    assert "shadow" in cole["doing"].lower() or "watching" in cole["doing"].lower()
    assert "landing page" in cole["next"].lower()
    assert "Interview" not in cole["did"]

    desk = agent_desk("ven_desk", "a1", org.get("agents") or [])
    assert desk is not None
    assert desk["work"]["today"]
    texts = " ".join(str(row.get("text") or "") for row in desk["work"]["today"])
    assert "Interview" in texts


def test_finished_research_job_is_credited_to_the_researcher(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    from app import state
    from app.db.models import Job
    from app.org.characters import enrich_agent
    from app.org.desk import work_briefs
    from core.clock import utcnow

    now = utcnow()
    with state.db()() as session:
        session.add(Job(id="job_r1", kind="research.deep", venture_id="ven_desk", payload_json="{}",
                        status="done", finished_at=now, created_at=now))
        session.commit()
    agents = [enrich_agent({"id": "a1", "role": "researcher", "name": "Maya", "status": "active"}),
              enrich_agent({"id": "a2", "role": "builder", "name": "Cole", "status": "active"})]
    briefs = work_briefs("ven_desk", agents)
    assert briefs["a1"]["did"] == "Ran market research."
    assert briefs["a2"]["did"] == "Nothing logged for them yet today."
