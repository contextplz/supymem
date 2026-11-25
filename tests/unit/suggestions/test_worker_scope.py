"""Synthetic worker cards use persisted founder authority and conservative expiry."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from tests.unit.suggestions.test_suggestions import env as env


def _identity(venture_id):
    from app.db.models import Venture, Workspace
    from app.state import db
    with db()() as session:
        workspace = session.get(Workspace, session.get(Venture, venture_id).workspace_id)
        return workspace.id, workspace.owner_id


def _other(client):
    from app.core.local_mode import is_local

    response = client.post("/ventures", json={"raw_idea": "Synthetic scheduling product for tutors"},
                           headers={"X-Subject": "usr_suggestions_other"})
    assert response.status_code == 201, response.text
    if is_local():
        from app.authz.workspace import ensure_subject_person
        from app.db.models import Workspace, WorkspaceMember
        from app.state import db

        ensure_subject_person("usr_suggestions_other")
        with db()() as session:
            workspace_id = response.json()["workspace_id"]
            workspace = session.get(Workspace, workspace_id)
            workspace.owner_id = "usr_suggestions_other"
            for member in session.scalars(select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id)):
                member.person_id = "usr_suggestions_other"
            session.commit()
    return response.json()["id"]


def _proposal():
    return {"key": "research.start", "title": "Synthetic research card", "why": "Fixture",
            "plan_text": "Synthetic proposed work", "est_credits": 1,
            "action_kind": "research.start", "payload": {}, "requires_mac": False}


def _idea_cards(venture_id):
    from app.db.supymem_models import Suggestion
    from app.state import db
    from core.clock import utcnow
    with db()() as session:
        for suffix, state, kind in (("new", "new", "idea.advance"), ("saved", "saved", "idea.answer"),
                                    ("current", "new", "plan.generate")):
            identity = f"sug_worker_{suffix}"
            session.add(Suggestion(id=identity, venture_id=venture_id, title="Synthetic saved card",
                action_kind=kind, status=state, dedupe_key=identity, created_at=utcnow()))
        session.commit()


def _statuses():
    from app.db.supymem_models import Suggestion
    from app.state import db
    with db()() as session:
        return {row.id: row.status for row in session.scalars(select(Suggestion))}


def test_global_worker_binds_each_persisted_owner_and_restores_scheduler_scope(env, monkeypatch):
    from app.suggestions import service
    from app.tenancy import Scope, bind, current, current_or_none
    client, first = env
    second = _other(client)
    expected = {vid: _identity(vid) for vid in (first, second)}
    seen = {}
    def propose(vid, limit=5):
        scope = current()
        assert (scope.workspace_id, scope.actor) == expected[vid]
        assert scope.venture_id == vid
        seen[vid] = scope
        return [_proposal()]
    monkeypatch.setattr(service.proposals, "propose_next_actions", propose)
    assert current_or_none() is None
    with bind(workspace_id="none", venture_id="", actor="system"):
        assert service.generate_all() == 2
        assert current() == Scope("none", "", "system")
    assert current_or_none() is None and set(seen) == {first, second}
    from app.db.supymem_models import Suggestion
    from app.state import db
    with db()() as session:
        for row in session.scalars(select(Suggestion)):
            assert row.person_id == expected[row.venture_id][1]


def test_foreign_existing_scope_cannot_rebind_to_an_owner(env, monkeypatch):
    from app.suggestions import service
    from app.tenancy import Scope, bind, current
    client, first = env
    second = _other(client)
    wid, _ = _identity(first)
    _, foreign = _identity(second)
    monkeypatch.setattr(service.proposals, "propose_next_actions", lambda *args: pytest.fail("foreign scope generated a card"))
    with bind(workspace_id=wid, venture_id=first, actor=foreign):
        assert service.generate_all() == 0
        assert current() == Scope(wid, first, foreign)
    assert _statuses() == {}


@pytest.mark.parametrize("state", ["blocked", "venture_archived", "stage_archived", "workspace_archived", "revoked"])
def test_global_worker_skips_unavailable_authority_and_continues_other_founder(env, monkeypatch, state):
    from app.db.models import Venture, Workspace, WorkspaceMember
    from app.state import db
    from app.suggestions import service
    from app.tenancy import current
    from core.clock import utcnow
    client, first = env
    second = _other(client)
    wid, owner = _identity(first)
    with db()() as session:
        if state == "blocked":
            session.get(Workspace, wid).settings_json = json.dumps({"blocked": True})
        elif state == "venture_archived":
            session.get(Venture, first).archived_at = utcnow()
        elif state == "stage_archived":
            session.get(Venture, first).stage = "archived"
        elif state == "workspace_archived":
            session.get(Workspace, wid).archived_at = utcnow()
        else:
            member = session.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == wid,
                                                                  WorkspaceMember.person_id == owner))
            member.role = "viewer"
        session.commit()
    seen = []
    def propose(vid, limit=5):
        assert vid == second and current().venture_id == second
        seen.append(vid)
        return [_proposal()]
    monkeypatch.setattr(service.proposals, "propose_next_actions", propose)
    assert service.generate_all() == 1 and set(seen) == {second}


def test_unscoped_guidance_generates_nothing_and_keeps_live_idea_cards(env):
    from app.suggestions import service
    from app.tenancy import current_or_none
    from app.ventures.next_step import next_step
    _, vid = env
    _idea_cards(vid)
    assert current_or_none() is None and next_step(vid)["key"] == "venture.review"
    assert isinstance(service.generate(vid), list)
    service._expire_stale({vid})
    assert all(_statuses()[identity] == "expired" for identity in ("sug_worker_new", "sug_worker_saved", "sug_worker_current"))


@pytest.mark.parametrize("key", [None, "", "unknown", "venture.review", "work.running", "idea.clarify",
                                 "unsupported", "research.unsupported", "site.audit", "venture.paused", "research.running"])
def test_unknown_or_unfinished_guidance_cannot_expire_idea_cards(env, monkeypatch, key):
    from app.suggestions import service
    _, vid = env
    _idea_cards(vid)
    monkeypatch.setattr("app.ventures.next_step.next_step", lambda _: {"key": key})
    service._expire_stale({vid})
    assert _statuses() == {"sug_worker_new": "expired", "sug_worker_saved": "expired", "sug_worker_current": "expired"}


def test_actual_confirmed_idea_and_completed_research_expire_only_past_idea_cards(env):
    from app.autopilot.authority import founder_scope
    from app.core.local_mode import is_local
    from app.db.models import Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession, ResearchDossier, ResearchFinding
    from app.state import db
    from app.suggestions import service
    from app.ventures import startup_review
    from app.ventures.next_step import next_step
    from core.clock import utcnow
    _, first = env
    wid, owner = _identity(first)
    vid, sid, rid, now = "ven_worker_past_idea", "ids_worker_review", "idr_worker_confirmed", utcnow()
    with db()() as session:
        session.add(Venture(id=vid, workspace_id=wid, name="Synthetic product", raw_idea="A booking product for tutors",
                            stage="draft", created_at=now, updated_at=now))
        session.flush()
        session.add(IdeaSession(id=sid, workspace_id=wid, venture_id=vid, case="know", state="confirmed",
            current_revision_id=rid, confirmed_revision_id=rid, confirmed_by_person_id=owner,
            confirmed_at=now, created_at=now, updated_at=now))
        session.flush()
        session.add(IdeaRevision(id=rid, session_id=sid, workspace_id=wid, venture_id=vid, rev=1,
                                source="founder", structured='{"_questions_skipped": true}', created_at=now))
        session.add(ResearchDossier(id="rsd_worker_completed", workspace_id=wid, venture_id=vid,
            revision_id=rid, kind="deep", state="complete", summary="{}", created_at=now, completed_at=now))
        session.add(ResearchFinding(id="finding_worker", dossier_id="rsd_worker_completed", workspace_id=wid,
            venture_id=vid, category="competitor", source="builtin", title="Public alternative", url="https://example.org/alternative",
            summary="Observed competitor", evidence=json.dumps({"observed_at": now.isoformat(), "evidence_kind": "search_excerpt"}),
            created_at=now))
        session.commit()
    _idea_cards(vid)
    with founder_scope(vid, autonomous=True):
        if is_local():
            review = startup_review.get(vid)
            while question := review["question"]:
                review = startup_review.skip_question(vid, review["revision"], question["field"])
            startup_review._mutate(vid, review["revision"], "priorities", value="Learn about my market")
            assert next_step(vid)["key"] == "research.review"
        else:
            assert next_step(vid)["key"] in {"plan.generate", "site.build"}
        service._expire_stale({vid})
    assert _statuses() == {"sug_worker_new": "expired", "sug_worker_saved": "expired", "sug_worker_current": "expired"}
