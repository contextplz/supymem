from __future__ import annotations

from tests.company_os_harness import load_company_os_app


def test_approved_build_work_keeps_plan_and_task_binding():
    load_company_os_app()
    from app.planning.compiler import _approved_work

    doc = {
        "entity_ids": {"tasks": {"landing:page": "tsk_plan_1"}},
        "proposal": {
            "milestones": [{
                "key": "landing",
                "kind": "landing",
                "title": "Landing page",
                "description": "Publish the approved offer.",
                "included": True,
                "tasks": [{
                    "key": "page",
                    "title": "Build the landing page",
                    "description": "Create a static page.",
                    "acceptance": ["Offer is visible"],
                    "capability_id": "build.run",
                    "implementation": "existing",
                }],
            }],
        },
    }

    projected = _approved_work("pln_1", doc)

    assert len(projected) == 1
    assert "plan:pln_1" in projected[0]["tags"]
    assert "plan_task:tsk_plan_1" in projected[0]["tags"]
    assert "capability:build.run" in projected[0]["tags"]
    assert projected[0]["runnable"] is False


def test_out_of_scope_approved_work_is_explained_but_not_runnable():
    load_company_os_app()
    from app.planning.compiler import _approved_work

    doc = {
        "entity_ids": {"tasks": {"research:scan": "tsk_out"}},
        "scope_details": {"tsk_out": {"scope": "out", "reason": "No integrated executor for this milestone."}},
        "proposal": {"milestones": [{"key": "research", "kind": "research", "title": "Research",
            "included": True, "tasks": [{"key": "scan", "title": "Scan the market", "description": "Collect evidence.",
            "acceptance": [], "capability_id": "research.deep", "implementation": "internal"}]}]},
    }

    projected = _approved_work("pln_1", doc)
    assert len(projected) == 1
    assert projected[0]["runnable"] is False
    assert "No integrated executor" in projected[0]["detail"]


def test_old_saved_work_is_projected_as_blocked_by_current_plan(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/blocked-work.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app import state
    from app.api.tasks_api import list_venture_tasks
    from app.db.models import Venture
    from app.db.supymem_models import ExecutionPlan, PlanMilestone, PlanTask
    from app.planning.store import save_tasks
    from core.clock import utcnow

    state.reset_db_factory()
    now = utcnow()
    with state.db()() as session:
        session.add(Venture(id="ven_blocked", workspace_id="wsp_blocked", name="Blocked",
            raw_idea="A venture", stage="BUILDING", created_at=now, updated_at=now))
        session.add(ExecutionPlan(id="pln_blocked", workspace_id="wsp_blocked", venture_id="ven_blocked",
            version=1, state="approved", structured=json.dumps({"scope_details": {
                "pt_blocked": {"scope": "out", "reason": "Capability not installed."}}}), created_at=now))
        session.flush()
        session.add(PlanMilestone(id="mls_blocked", plan_id="pln_blocked", workspace_id="wsp_blocked",
            venture_id="ven_blocked", order=1, title="Research", kind="research", depends_on="[]"))
        session.flush()
        session.add(PlanTask(id="pt_blocked", milestone_id="mls_blocked", workspace_id="wsp_blocked",
            venture_id="ven_blocked", order=1, title="Research task", state="pending", created_at=now))
        session.commit()
    save_tasks("ven_blocked", [{"title": "Research task", "why_now": "Old approved queue",
        "tags": ["plan_task:pt_blocked", "capability:research.deep"], "runnable": True}],
        source="approved_plan:pln_blocked")

    work = list_venture_tasks("ven_blocked")["items"]
    assert len(work) == 1
    assert work[0]["runnable"] is False
    assert work[0]["plan_scope"] == "out"
    assert work[0]["plan_scope_reason"] == "Capability not installed."


def test_replan_restores_approved_work_and_skips_completed_tasks(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/approved-work.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app import state
    from app.api.tasks_api import _refresh_approved_plan_work, list_venture_tasks
    from app.db.models import Venture
    from app.db.supymem_models import BuildRun, ExecutionPlan, FounderApp, PlanMilestone, PlanTask
    from app.planning.store import list_open_tasks, save_tasks
    from app.ventures.state_brief import _plan_section
    from core.clock import utcnow

    state.reset_db_factory()
    state.db()
    now = utcnow()
    doc = {
        "entity_ids": {"tasks": {"landing:page": "plt_done", "product:app": "plt_build"}},
        "proposal": {"milestones": [
            {"key": "landing", "kind": "landing", "title": "Landing", "description": "Published site",
             "included": True, "tasks": [{"key": "page", "title": "Build landing page",
                 "description": "Static page", "acceptance": [], "capability_id": "build.run",
                 "implementation": "existing"}]},
            {"key": "product", "kind": "product", "title": "Product", "description": "Build product",
             "included": True, "tasks": [{"key": "app", "title": "Build authenticated product",
                 "description": "Durable app", "acceptance": [], "capability_id": "build.run",
                 "implementation": "existing"}]},
        ]},
    }
    import json
    with state.db()() as session:
        session.add(Venture(id="ven_approved", workspace_id="wsp_approved", name="Example",
            raw_idea="An application assistant", stage="BUILDING", created_at=now, updated_at=now))
        session.add(ExecutionPlan(id="pln_approved", workspace_id="wsp_approved",
            venture_id="ven_approved", version=1, state="approved", structured=json.dumps(doc),
            approved_at=now, approved_by="founder", created_at=now))
        session.flush()
        session.add_all([
            PlanMilestone(id="mls_landing", plan_id="pln_approved", workspace_id="wsp_approved",
                venture_id="ven_approved", order=1, title="Landing", kind="landing"),
            PlanMilestone(id="mls_product", plan_id="pln_approved", workspace_id="wsp_approved",
                venture_id="ven_approved", order=2, title="Product", kind="product"),
        ])
        session.flush()
        session.add_all([
            PlanTask(id="plt_done", milestone_id="mls_landing", workspace_id="wsp_approved",
                venture_id="ven_approved", order=1, title="Build landing page", state="done", created_at=now),
            PlanTask(id="plt_build", milestone_id="mls_product", workspace_id="wsp_approved",
                venture_id="ven_approved", order=1, title="Build authenticated product", state="pending", created_at=now),
        ])
        session.commit()
    save_tasks("ven_approved", [
        {"title": "Build landing page", "why_now": "Approved plan", "runnable": False,
         "tags": ["plan:pln_approved", "plan_task:plt_done", "capability:build.run"]},
        {"title": "Build authenticated product", "why_now": "Approved plan", "runnable": False,
         "tags": ["plan:pln_approved", "plan_task:plt_build", "capability:build.run"]},
    ], source="approved_plan:pln_approved")
    with state.db()() as session:
        assert _plan_section(session, ExecutionPlan, PlanTask, "ven_approved")["current_step"] == "Build authenticated product"
    # Work is a saved snapshot; reading it must reflect later plan completion
    # and must not call a pending build a reviewable result.
    assert [row["title"] for row in list_open_tasks("ven_approved")] == [
        "Build landing page", "Build authenticated product"]
    current = list_venture_tasks("ven_approved")["items"]
    assert [row["title"] for row in current] == ["Build authenticated product"]
    assert current[0]["plan_task_state"] == "pending"
    assert current[0]["build_review_ready"] is False
    with state.db()() as session:
        session.add(FounderApp(id="app_approved", workspace_id="wsp_approved",
            venture_id="ven_approved", slug="approved-app", name="Approved app",
            kind="web", created_at=now, updated_at=now))
        session.flush()
        session.add(BuildRun(id="run_approved", app_id="app_approved", workspace_id="wsp_approved",
            venture_id="ven_approved", plan_task_id="plt_build", trigger="founder",
            agent="builder", state="succeeded", spec="{}", closed_at=now))
        plan_task = session.get(PlanTask, "plt_build")
        plan_task.state = "waiting_for_founder"
        plan_task.build_run_id = "run_approved"
        session.commit()
    assert list_venture_tasks("ven_approved")["items"][0]["build_review_ready"] is True
    # A stale saved "running" row follows the finished build: it is reviewable work, not running work.
    with state.db()() as session:
        from app.db.models import PlannedTask
        row = next(r for r in session.query(PlannedTask).all() if "plan_task:plt_build" in (r.tags_json or ""))
        row.status = "running"
        session.commit()
    projected = list_venture_tasks("ven_approved")
    assert projected["running"] == []
    assert projected["items"][0]["status"] == "open" and projected["items"][0]["build_review_ready"] is True
    save_tasks("ven_approved", [{"title": "Test a paid offer", "why_now": "Generic slate"}], source="replan")

    result = _refresh_approved_plan_work("ven_approved")

    assert result["plan_id"] == "pln_approved"
    assert [row["title"] for row in result["tasks"]] == ["Build authenticated product"]
    assert [row["title"] for row in list_open_tasks("ven_approved")] == ["Build authenticated product"]


def test_publication_evidence_is_scoped_and_does_not_complete_plan_work(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/publication-work.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    import json
    from app import state
    from app.api.tasks_api import list_venture_tasks
    from app.db.models import Venture
    from app.db.supymem_models import Deployment, ExecutionPlan, FounderApp, PlanMilestone, PlanTask
    from app.planning.compiler import get_plan
    from app.planning.store import save_tasks
    from app.ventures.run_status import snapshot
    from core.clock import utcnow

    state.reset_db_factory()
    state.db()
    now = utcnow()
    proposal = {"milestones": [{"key": "landing", "kind": "landing", "title": "Landing",
        "description": "Public page", "included": True, "depends_on": [], "tasks": [
            {"key": "static", "title": "Build static page", "description": "Create page",
             "acceptance": [], "capability_id": "build.run", "implementation": "new_code"},
            {"key": "publish", "title": "Founder reviews and publishes", "description": "Review page",
             "acceptance": [], "capability_id": "founder.action", "implementation": "founder_action"},
        ]}]}
    with state.db()() as session:
        session.add_all([
            Venture(id="ven_a", workspace_id="wsp_a", name="A", raw_idea="A", stage="BUILDING", created_at=now, updated_at=now),
            Venture(id="ven_b", workspace_id="wsp_b", name="B", raw_idea="B", stage="BUILDING", created_at=now, updated_at=now),
            FounderApp(id="app_a", workspace_id="wsp_a", venture_id="ven_a", slug="app-a", name="A", kind="landing", created_at=now, updated_at=now),
        ])
        session.add(ExecutionPlan(id="pln_a", workspace_id="wsp_a", venture_id="ven_a", version=1,
            state="approved", structured=json.dumps({"proposal": proposal,
                "entity_ids": {"milestones": {"landing": "mls_a"},
                               "tasks": {"landing:static": "plt_static", "landing:publish": "plt_publish"}},
                "scope_details": {"plt_static": {"scope": "conditional"}, "plt_publish": {"scope": "conditional"}}}),
            approved_at=now, approved_by="founder", created_at=now))
        session.flush()
        session.add(PlanMilestone(id="mls_a", plan_id="pln_a", workspace_id="wsp_a", venture_id="ven_a",
            order=1, title="Landing", kind="landing", depends_on="[]"))
        session.flush()
        session.add_all([
            PlanTask(id="plt_static", milestone_id="mls_a", workspace_id="wsp_a", venture_id="ven_a",
                order=1, title="Build static page", state="pending", created_at=now),
            PlanTask(id="plt_publish", milestone_id="mls_a", workspace_id="wsp_a", venture_id="ven_a",
                order=2, title="Founder reviews and publishes", state="pending", created_at=now),
            Deployment(id="dep_a", app_id="app_a", workspace_id="wsp_a", venture_id="ven_a",
                target="supymem_static", state="live", version=1, url="https://a.example.com", live_at=now),
        ])
        session.commit()
    save_tasks("ven_a", [
        {"title": "Build static page", "why_now": "Landing", "runnable": False,
         "tags": ["plan_task:plt_static", "capability:build.run"]},
        {"title": "Founder reviews and publishes", "why_now": "Landing", "runnable": False,
         "tags": ["plan_task:plt_publish", "capability:founder.action"]},
    ], source="approved_plan:pln_a")

    work = list_venture_tasks("ven_a")["items"]
    assert len(work) == 2
    assert all(item["plan_task_state"] == "pending" for item in work)
    assert all(item["public_site_evidence"]["url"] == "https://a.example.com" for item in work)
    assert get_plan("pln_a")["milestones"][0]["public_site_evidence"]["source"] == "deployment"
    assert all(task["state"] == "pending" for task in get_plan("pln_a")["milestones"][0]["tasks"])
    assert list_venture_tasks("ven_b")["items"] == []
    status = snapshot("ven_a", "wsp_a")
    assert status["stage"] == "Published site"
    assert "public landing page is live" in status["activity"]
    assert "Build static page is waiting" not in status["activity"]
    assert snapshot("ven_b", "wsp_b")["stage"] != "Published site"

    # A newer approved plan retires its old Work projection even when its
    # planned-task rows remain open for historical review.
    with state.db()() as session:
        session.get(ExecutionPlan, "pln_a").superseded_by = "pln_next"
        session.commit()
    assert list_venture_tasks("ven_a")["items"] == []
    assert snapshot("ven_a", "wsp_a")["stage"] != "Published site"


def test_legacy_publication_requires_unambiguous_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/legacy-publication.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app import state
    from app.db.models import SiteVersion, Venture
    from app.planning.publication_evidence import public_site_evidence
    from core.clock import utcnow

    state.reset_db_factory()
    state.db()
    now = utcnow()
    with state.db()() as session:
        session.add(Venture(id="ven_one", workspace_id="wsp_shared", name="One",
            raw_idea="One", stage="BUILDING", created_at=now, updated_at=now))
        session.add(SiteVersion(id="site_one", workspace_id="wsp_shared", version=1,
            stage="first_look", template="simple", archetype="simple", brandkit_hash="x",
            copy_hash="x", path="/tmp/site", sha256="x", published=1,
            url="https://one.example.com", created_at=now))
        session.commit()
        assert public_site_evidence(session, workspace_id="wsp_shared", venture_id="ven_one")["source"] == "site_version"
        session.add(Venture(id="ven_two", workspace_id="wsp_shared", name="Two",
            raw_idea="Two", stage="BUILDING", created_at=now, updated_at=now))
        session.commit()
        assert public_site_evidence(session, workspace_id="wsp_shared", venture_id="ven_two") is None
