"""The per-venture page choice is authorized, durable and cleared by a real build."""
import json

import pytest
from fastapi import HTTPException

from tests.unit.pagebuilder.test_api import setup as setup


def choice_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.ventures import router

    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app)


@pytest.mark.parametrize("status", ["scheduled", "pending", "retry_wait", "leased", "running"])
@pytest.mark.parametrize("choice", ["build", "skip"])
def test_active_venture_work_rejects_page_choice_without_mutation(setup, status, choice):
    from app.db.models import Job, VentureDocument
    from core.clock import utcnow

    d = setup
    prior = "skip" if choice == "build" else "build"
    url, headers = f"/api/ventures/{d['vid']}/page-choice", {"X-Subject": d["actor"]}
    with choice_client() as client:
        assert client.post(url, headers=headers, json={"choice": prior}).status_code == 200
        with d["factory"]() as session:
            session.add(Job(id="active-page-choice-work", kind="research.deep", workspace_id=d["wid"],
                venture_id=d["vid"], status=status, payload_json=json.dumps({"revision_id": d["rid"]}),
                created_at=utcnow()))
            session.commit()
            saved = session.get(VentureDocument, f"page_choice:{d['vid']}")
            original = (saved.body_md, saved.updated_at)
        for _ in range(2):
            denied = client.post(url, headers=headers, json={"choice": choice})
            assert denied.status_code == 409
            assert denied.json()["detail"] == "Wait for the current work to finish before choosing your next step."
        with d["factory"]() as session:
            from sqlalchemy import select
            saved = session.get(VentureDocument, f"page_choice:{d['vid']}")
            assert (saved.body_md, saved.updated_at) == original
            jobs = session.scalars(select(Job).where(Job.workspace_id == d["wid"], Job.venture_id == d["vid"])).all()
            assert [(job.id, job.status, json.loads(job.payload_json)) for job in jobs] == [
                ("active-page-choice-work", status, {"revision_id": d["rid"]})]


def test_actual_research_start_refuses_stale_choice_and_retains_request_binding(setup):
    from app.db.models import Job, VentureDocument
    from app.db.supymem_models import IdeaSession
    from app.research.service import start
    from app.tenancy import bind
    from sqlalchemy import select

    d = setup
    url, headers = f"/api/ventures/{d['vid']}/page-choice", {"X-Subject": d["actor"]}
    with choice_client() as client:
        assert client.post(url, headers=headers, json={"choice": "skip"}).status_code == 200
        with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
            first = start(d["vid"], expected_revision_id=d["rid"], request_key="page-choice-research-start")
        assert client.post(url, headers=headers, json={"choice": "build"}).status_code == 409
        with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
            repeated = start(d["vid"], expected_revision_id=d["rid"], request_key="page-choice-research-start")
            with pytest.raises(RuntimeError, match="research_inputs_changed"):
                start(d["vid"], expected_revision_id="stale-revision", request_key="page-choice-stale-revision")
        assert repeated["job_id"] == first["job_id"]
        with d["factory"]() as session:
            assert json.loads(session.get(VentureDocument, f"page_choice:{d['vid']}").body_md) == {"choice": "skip"}
            idea = session.get(IdeaSession, d["sid"])
            assert (idea.current_revision_id, idea.confirmed_revision_id, idea.state) == (d["rid"], d["rid"], "confirmed")
            jobs = session.scalars(select(Job).where(Job.workspace_id == d["wid"], Job.venture_id == d["vid"])).all()
            assert len(jobs) == 1 and jobs[0].id == first["job_id"] and jobs[0].status == "scheduled"


def test_active_work_cannot_create_a_new_page_choice(setup):
    from app.db.models import Job, VentureDocument
    from core.clock import utcnow

    d = setup
    with d["factory"]() as session:
        session.add(Job(id="active-without-page-choice", kind="research.deep", workspace_id=d["wid"],
            venture_id=d["vid"], status="scheduled", payload_json="{}", created_at=utcnow()))
        session.commit()
    with choice_client() as client:
        assert client.post(f"/api/ventures/{d['vid']}/page-choice", headers={"X-Subject": d["actor"]}, json={"choice": "skip"}).status_code == 409
    with d["factory"]() as session:
        assert session.get(VentureDocument, f"page_choice:{d['vid']}") is None


@pytest.mark.parametrize("journey_skipped", [False, True])
def test_research_gate_preserves_direction_correction_and_confirm_guards(setup, journey_skipped):
    from app.api.ventures import PageChoiceBody, save_page_choice
    from app.db.models import Job, VentureDocument
    from app.db.supymem_models import IdeaSession
    from app.idea.service import confirm, start_direction_correction
    from app.research.service import ResearchPreflightBlocked, start
    from app.tenancy import bind
    from sqlalchemy import select

    d = setup
    save_page_choice(d["vid"], PageChoiceBody(choice="skip"), x_subject=d["actor"])
    if journey_skipped:
        from core.clock import utcnow
        with d["factory"]() as session:
            row = session.get(VentureDocument, f"journey_overrides:{d['vid']}")
            if row is None:
                row = VentureDocument(id=f"journey_overrides:{d['vid']}", venture_id=d["vid"],
                    kind="journey_overrides", title="Journey choices", created_at=utcnow())
                session.add(row)
            row.body_md = json.dumps({"skipped": {"landing_page": {}}})
            row.updated_at = utcnow()
            session.commit()
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        correction = start_direction_correction(d["sid"], "know", venture_id=d["vid"], revision_id=d["rid"])
        assert correction["state"] == "clarifying" and correction["id"] != d["sid"]
        with pytest.raises(ResearchPreflightBlocked, match="research_confirmation_required"):
            start(d["vid"], expected_revision_id=d["rid"], request_key="page-choice-old-confirmation")
        with pytest.raises(RuntimeError, match="not_ready_to_confirm"):
            confirm(correction["id"], revision_id=d["rid"], person_id=d["actor"])
        with pytest.raises(RuntimeError, match="not_ready_to_confirm"):
            confirm(correction["id"], revision_id=correction["current_revision"]["id"], person_id=d["actor"])
    with d["factory"]() as session:
        original = session.get(IdeaSession, d["sid"])
        pending = session.get(IdeaSession, correction["id"])
        assert (original.state, original.current_revision_id, original.confirmed_revision_id) == ("confirmed", d["rid"], d["rid"])
        assert pending.state == "clarifying" and pending.confirmed_revision_id is None
        assert not session.scalar(select(Job.id).where(Job.workspace_id == d["wid"], Job.venture_id == d["vid"]))
        assert json.loads(session.get(VentureDocument, f"page_choice:{d['vid']}").body_md) == {"choice": "skip"}


def test_research_gate_rechecks_latest_session_after_waiting_for_correction(setup, monkeypatch):
    from sqlalchemy.orm import Session
    from app.db.models import Job
    from app.db.supymem_models import IdeaSession
    from app.idea.service import start_direction_correction
    from app.research.service import ResearchPreflightBlocked, start
    from app.tenancy import bind
    from sqlalchemy import select

    d = setup
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        correction = start_direction_correction(d["sid"], "know", venture_id=d["vid"], revision_id=d["rid"])
    real_scalar, returned_stale = Session.scalar, []

    def correction_after_lock(session, statement, *args, **kwargs):
        result = real_scalar(session, statement, *args, **kwargs)
        if statement._for_update_arg is not None and isinstance(result, IdeaSession) and not returned_stale:
            returned_stale.append(True)
            return session.get(IdeaSession, d["sid"])
        return result

    monkeypatch.setattr(Session, "scalar", correction_after_lock)
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        with pytest.raises(ResearchPreflightBlocked, match="research_confirmation_required"):
            start(d["vid"], expected_revision_id=d["rid"], request_key="page-choice-concurrent-correction")
    assert returned_stale and correction["state"] == "clarifying"
    with d["factory"]() as session:
        assert not session.scalar(select(Job.id).where(Job.venture_id == d["vid"]))


@pytest.mark.parametrize("scope", ["other_workspace", "other_venture", "terminal"])
def test_page_choice_active_fence_is_tenant_scoped_and_ignores_terminal_work(setup, scope):
    from app.db.models import Job, VentureDocument
    from core.clock import utcnow

    d = setup
    wid = "other-workspace" if scope == "other_workspace" else d["wid"]
    vid = "other-venture" if scope == "other_venture" else d["vid"]
    status = "succeeded" if scope == "terminal" else "running"
    with d["factory"]() as session:
        session.add(Job(id="unrelated-page-choice-work", kind="research.deep", workspace_id=wid,
            venture_id=vid, status=status, payload_json="{}", created_at=utcnow()))
        session.commit()
    with choice_client() as client:
        accepted = client.post(f"/api/ventures/{d['vid']}/page-choice", headers={"X-Subject": d["actor"]}, json={"choice": "skip"})
        assert accepted.status_code == 200 and accepted.json() == {"choice": "skip"}
    with d["factory"]() as session:
        assert json.loads(session.get(VentureDocument, f"page_choice:{d['vid']}").body_md) == {"choice": "skip"}
        assert session.get(Job, "unrelated-page-choice-work").status == status


@pytest.mark.parametrize("audience", ["Warehouse supervisors", "Families planning holidays"])
def test_choice_is_scoped_and_building_clears_skip(setup, audience):
    from app.api.ventures import PageChoiceBody, save_page_choice
    from app.autopilot.authority import founder_scope
    from app.db.models import VentureDocument
    from app.db.supymem_models import ResearchDossier, ResearchFinding
    from app.pagebuilder import api
    from app.ventures.next_step import _facts
    from app.ventures.next_step import next_step
    from core.clock import utcnow

    d = setup
    with d["factory"]() as session:
        now = utcnow()
        session.add(ResearchDossier(id="page-choice-complete-research", workspace_id=d["wid"],
            venture_id=d["vid"], revision_id=d["rid"], kind="deep", state="complete",
            summary=json.dumps({"review": {"overview": f"Saved alternatives for {audience}."}}),
            created_at=now, completed_at=now))
        session.add(ResearchFinding(id="page-choice-source", dossier_id="page-choice-complete-research",
            workspace_id=d["wid"], venture_id=d["vid"], category="source", title=audience, source="web",
            url="https://example.test/alternatives", evidence=json.dumps({"evidence_kind": "web", "observed_at": now.isoformat()}),
            created_at=now))
        session.commit()
    save_page_choice(d["vid"], PageChoiceBody(choice="skip"), x_subject=d["actor"])
    assert _facts(d["vid"])["page_skipped"] is True
    with founder_scope(d["vid"], d["actor"]):
        assert _facts(d["vid"])["dossier_state"] == "complete"
        assert next_step(d["vid"])["key"] == "gtm.outreach"
    with pytest.raises(HTTPException):
        save_page_choice(d["vid"], PageChoiceBody(choice="build"), x_subject="another-founder")
    # An unavailable provider leaves the founder's choice intact.
    original = api.resources
    api.resources = lambda *_a: {"waitlist_error": None, "provider_error": "pagebuilder_provider_unavailable"}
    try:
        with pytest.raises(HTTPException):
            api.build(d["vid"], d["actor"])
        assert _facts(d["vid"])["page_skipped"] is True
    finally:
        api.resources = original
    started = api.build(d["vid"], d["actor"])
    with d["factory"]() as session:
        row = session.get(VentureDocument, f"page_choice:{d['vid']}")
        assert row.venture_id == d["vid"] and row.kind == "page_choice"
        assert json.loads(row.body_md) == {"choice": "build"}
    assert _facts(d["vid"])["page_skipped"] is False
    assert api.build(d["vid"], d["actor"])["job_id"] == started["job_id"]


def test_research_milestone_offers_two_equal_routes_without_requiring_a_plan(setup, monkeypatch):
    from app.chat import milestone_hooks
    from app.research import service
    from app.pagebuilder import api

    posts = []
    monkeypatch.setattr(api, "enabled", lambda: True)
    monkeypatch.setattr(service, "get_dossier", lambda *_a: {"summary": {"verdict": {
        "source_count": 2, "why": ["Related pages are ready to review."], "why_not": ["Demand is unverified."]}}})
    monkeypatch.setattr(milestone_hooks, "post_milestone", lambda *_a, **kw: posts.append(kw))
    milestone_hooks.research_done({"venture_id": setup["vid"], "_job_id": "synthetic-research-job"})
    assert len(posts) == 1
    choices = posts[0]["actions"][1:]
    assert [a["label"] for a in choices] == ["Build the landing page and explore customers", "Find customers first, skip the landing page"]
    assert all(a["style"] == "primary" for a in choices)
    assert choices[1]["href"].endswith("&skip_page=1")
    assert not any(a.get("action") == "plan.generate" for a in posts[0]["actions"])


@pytest.mark.parametrize("intake_path,revision_personal", [
    (None, True), ("personal_build", True), ("repo_build", True),
    ("personal_build", False), ("repo_build", False),
])
def test_saved_skip_preserves_personal_local_build_for_ui_and_background(setup, intake_path, revision_personal):
    from app.api.ventures import PageChoiceBody, save_page_choice
    from app.autopilot.authority import founder_scope
    from app.db.models import VentureDocument
    from app.db.supymem_models import IdeaRevision, ResearchDossier
    from app.ventures.next_step import _facts, founder_next_step, next_step
    from core.clock import utcnow

    d = setup
    personal = revision_personal or intake_path == "personal_build"
    with d["factory"]() as session:
        revision = session.get(IdeaRevision, d["rid"])
        fields = json.loads(revision.structured or "{}")
        fields["project_intent"] = "personal" if revision_personal else "startup"
        revision.structured = json.dumps(fields)
        if intake_path:
            session.add(VentureDocument(id=f"intake_decision:{d['vid']}",
                venture_id=d["vid"], kind="intake_decision", title="Verified intake",
                body_md=json.dumps({"path": intake_path}), created_at=utcnow(), updated_at=utcnow()))
        if not personal:
            session.add(ResearchDossier(id="page-choice-current-research", workspace_id=d["wid"],
                venture_id=d["vid"], revision_id=d["rid"], kind="deep", state="complete",
                summary="{}", created_at=utcnow(), completed_at=utcnow()))
        session.commit()
    save_page_choice(d["vid"], PageChoiceBody(choice="skip"), x_subject=d["actor"])
    with founder_scope(d["vid"], d["actor"]):
        assert _facts(d["vid"])["page_skipped"] is True
        assert _facts(d["vid"])["personal"] is personal
        assert _facts(d["vid"])["gtm_entry"] is (not personal)
        for resolver in (next_step, founder_next_step):
            step = resolver(d["vid"])
            assert step["key"] == ("build.first" if personal else "gtm.outreach")
            assert step["href"] == (f"/v/{d['vid']}/idea" if personal else f"/gtm?venture={d['vid']}")


@pytest.mark.parametrize("state,personal", [("confirmed", True), ("reviewing", False)])
def test_research_milestone_keeps_current_personal_or_unconfirmed_idea_route(setup, monkeypatch, state, personal):
    from app.chat import milestone_hooks
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.research import service

    d = setup
    with d["factory"]() as session:
        session.get(IdeaSession, d["sid"]).state = state
        revision = session.get(IdeaRevision, d["rid"])
        fields = json.loads(revision.structured or "{}")
        fields["project_intent"] = "personal" if personal else "startup"
        revision.structured = json.dumps(fields)
        session.commit()
    posts = []
    monkeypatch.setattr(service, "get_dossier", lambda *_a: {"summary": {"verdict": {
        "source_count": 2, "why": ["Saved related pages."], "why_not": []}}})
    monkeypatch.setattr(milestone_hooks, "post_milestone", lambda *_a, **kw: posts.append(kw))
    milestone_hooks.research_done({"venture_id": d["vid"], "_job_id": "synthetic-historical-report"})
    assert len(posts) == 1
    actions = posts[0]["actions"][1:]
    assert len(actions) == 1 and actions[0]["href"] == f"/v/{d['vid']}/idea"
    assert not any(a.get("action") == "plan.generate" or "skip_page" in a.get("href", "") for a in actions)


def test_research_milestone_with_disabled_builder_preserves_plan_route(setup, monkeypatch):
    from app.chat import milestone_hooks
    from app.pagebuilder import api
    from app.research import service

    posts = []
    monkeypatch.setattr(api, "enabled", lambda: False)
    monkeypatch.setattr(service, "get_dossier", lambda *_a: {"summary": {"verdict": {
        "source_count": 2, "why": ["Saved related pages."], "why_not": []}}})
    monkeypatch.setattr(milestone_hooks, "post_milestone", lambda *_a, **kw: posts.append(kw))
    milestone_hooks.research_done({"venture_id": setup["vid"], "_job_id": "synthetic-disabled-builder"})
    assert len(posts) == 1
    actions = posts[0]["actions"][1:]
    assert len(actions) == 1 and actions[0]["action"] == "plan.generate"
    assert not any("skip_page" in a.get("href", "") or "/site" in a.get("href", "") for a in actions)


@pytest.mark.parametrize("archive_mode", ["archived_at", "archived", "purged"])
def test_read_only_members_and_archived_ventures_cannot_change_the_choice(setup, archive_mode):
    from app.api.ventures import PageChoiceBody, save_page_choice
    from app.authz.workspace import ensure_subject_person
    from app.db.models import Venture, VentureDocument, WorkspaceMember
    from core.clock import utcnow

    d = setup
    viewer = "page-choice-viewer"
    ensure_subject_person(viewer)
    with d["factory"]() as session:
        session.add(WorkspaceMember(id="page-choice-viewer-member", workspace_id=d["wid"],
            person_id=viewer, role="viewer", can_approve=0, created_at=utcnow()))
        session.commit()
    with pytest.raises(HTTPException) as denied:
        save_page_choice(d["vid"], PageChoiceBody(choice="skip"), x_subject=viewer)
    assert denied.value.status_code == 403
    with d["factory"]() as session:
        assert session.get(VentureDocument, f"page_choice:{d['vid']}") is None

    save_page_choice(d["vid"], PageChoiceBody(choice="skip"), x_subject=d["actor"])
    with d["factory"]() as session:
        venture = session.get(Venture, d["vid"])
        if archive_mode == "archived_at":
            venture.archived_at = utcnow()
        else:
            venture.stage = archive_mode
        session.commit()
    with pytest.raises(HTTPException) as archived:
        save_page_choice(d["vid"], PageChoiceBody(choice="build"), x_subject=d["actor"])
    assert archived.value.status_code == 404
    with d["factory"]() as session:
        assert json.loads(session.get(VentureDocument, f"page_choice:{d['vid']}").body_md) == {"choice": "skip"}
