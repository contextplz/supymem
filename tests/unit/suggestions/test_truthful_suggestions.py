import asyncio

import pytest

from tests.unit.suggestions.test_suggestions import env as shared_env


@pytest.fixture(name="env")
def suggestions_env(tmp_path, monkeypatch):
    yield from shared_env.__wrapped__(tmp_path, monkeypatch)


def proposal(kind="research.start", payload=None):
    return {"action_kind": kind, "title": "Start research", "why": "Current next step", "plan_text": "Start research",
            "payload": payload or {}, "est_credits": 20, "requires_mac": False}


@pytest.mark.parametrize("domain", ["warehouse-planning.example", "family-recipes.example"])
@pytest.mark.parametrize("local", [True, False])
def test_hidden_suggestions_are_not_offered_or_counted_but_saved_data_survives(env, monkeypatch, domain, local):
    from core.ids import new_id
    from core.clock import utcnow
    from app.db.models import Venture
    from app.db.supymem_models import Suggestion
    from app.state import db
    from app.suggestions import proposals, service

    _, venture_id = env
    monkeypatch.setenv("SUPYMEM_MODE", "local" if local else "hosted")
    candidate = proposal("browser.task.prepare", {"url": f"https://{domain}/pricing", "name": domain})
    assert (proposals._shape(candidate) is None) is local
    monkeypatch.setattr(service, "_expire_stale", lambda *args: None)
    monkeypatch.setattr(service, "_reconcile", lambda *args: None)
    with db()() as session:
        workspace_id = session.get(Venture, venture_id).workspace_id
        row = Suggestion(id=new_id("sug"), venture_id=venture_id, title=domain, why="Your saved competitor review",
                         plan_text="Review your saved competitor", est_credits=0, action_kind="browser.task.prepare",
                         payload_json="{}", dedupe_key=domain, requires_mac=True, status="new", created_at=utcnow())
        session.add(row)
        session.commit()
        suggestion_id = row.id
    listed = service.list_for({workspace_id}, venture_id=venture_id)
    assert len(listed["items"]) == (0 if local else 1)
    assert listed["new"] == (0 if local else 1)
    with db()() as session:
        assert session.get(Suggestion, suggestion_id).status == "new"


def test_registry_rejects_navigation_and_scopes_outreach(env):
    from app.suggestions import proposals

    for kind in ("gtm.diagnosis", "content.drafts", "human", "pmf.iterate", "build.prepare", "mac.task.prepare", "site.publish"):
        assert proposals._shape(proposal(kind)) is None
    outreach = proposals._shape(proposal("outreach.draft", {"batch": True, "approved_by_founder": True}))
    assert outreach["title"] == "Draft 3 outreach emails for review"
    assert outreach["payload"] == {"run": True, "review_only": True}
    waitlist = proposals._shape(proposal("waitlist.enable"))
    assert waitlist["title"] == "Enable and test the waitlist"
    for kind in proposals.EXECUTABLE_KINDS:
        assert proposals.CAPABILITIES[kind]["completes_fully"] is True
        assert proposals.CAPABILITIES[kind]["executor"]


@pytest.mark.parametrize("kind,path", [
    ("outreach.draft", "/gtm?venture=ven_result&step=review"),
    ("research.start", "/v/ven_result/research"),
    ("idea.answer", "/v/ven_result/idea"),
    ("idea.advance", "/v/ven_result/idea"),
    ("waitlist.enable", "/v/ven_result/site"),
    ("recovery.plan_template", "/v/ven_result/plan"),
])
def test_history_uses_result_destination_not_arbitrary_action_link(kind, path):
    import json
    from app.db.supymem_models import Suggestion
    from app.suggestions import service
    from core.clock import utcnow

    row = Suggestion(id="result", venture_id="ven_result", action_kind=kind, status="done", created_at=utcnow(),
        result_ref=json.dumps({"href": "/v/ven_result/now", "message": "```text\nDraft 1\nDraft 2\nDraft 3\n```"}))
    result = service.public(row)
    assert result["result_href"] == path
    assert "```" not in result["result_message"] and "\n" not in result["result_message"]
    if kind == "outreach.draft":
        assert result["result_message"] == "3 drafts ready for review"


def test_exception_retains_committed_job_identity(env, monkeypatch):
    from app.autopilot import actions, receipts, service
    from app.chat import execute
    from app.core.jobs import enqueue
    from app.db.models import Venture
    from app.state import db

    _, vid = env
    async def partial(venture_id, name, **kwargs):
        with db()() as session:
            venture = session.get(Venture, venture_id)
            identifier = enqueue(session, "research.deep", {}, workspace_id=venture.workspace_id,
                                 venture_id=venture_id)
            session.commit()
        receipts.queued_job(identifier)
        raise RuntimeError("after_commit")

    monkeypatch.setattr(execute, "execute_action", partial)
    result = asyncio.run(actions.execute_proposed_action(vid, {
        "kind": "research.start", "title": "Start research", "payload": {}}, service._owner(vid)))
    assert result["reason"] == "held" and len(result["effects"]["jobs"]) == 1
    assert result["completion_verified"] is False


def test_scope_bound_on_list_and_state_change_rejected_on_accept(env, monkeypatch):
    from app.db.models import Venture
    from app.state import db
    from app.suggestions import service
    from app.tenancy import bind, current

    client, vid = env
    with db()() as session:
        wid = session.get(Venture, vid).workspace_id
    seen = []
    def step(identifier):
        scope = current()
        seen.append((identifier, scope.workspace_id, scope.venture_id))
        return {"key": "research.start"}
    monkeypatch.setattr("app.ventures.next_step.next_step", step)
    monkeypatch.setattr(service.proposals, "propose_next_actions", lambda *_: [proposal()])
    sid = service.generate(vid)[0]
    with bind(workspace_id="none", venture_id="", actor="system"):
        assert service.list_for({wid})["items"][0]["id"] == sid
    assert seen and all(item == (vid, wid, vid) for item in seen)
    with db()() as session:
        session.get(Venture, vid).raw_idea = "Changed idea"
        session.commit()
    monkeypatch.setattr(service, "_execute_accepted", lambda *_: pytest.fail("stale card executed"))
    accepted = asyncio.run(service.accept(sid, None))
    assert accepted["status"] == "accepted" and accepted["result_state"] == "noop"
    assert service.list_for({wid})["items"] == []


@pytest.mark.parametrize("reason,completed,status", [("noop", False, "noop"), ("needs_you", False, "needs_you"),
                                                     ("", True, "done"), ("in_progress", False, "needs_you")])
def test_truthful_result_status_and_link(env, monkeypatch, reason, completed, status):
    from app.suggestions import service

    _, vid = env
    monkeypatch.setattr(service.proposals, "propose_next_actions", lambda *_: [proposal()])
    sid = service.generate(vid)[0]
    service._set(sid, ("new",), "executing")
    class Executor:
        @staticmethod
        async def execute_proposed_action(*args, **kwargs):
            return {"ok": True, "reason": reason, "completion_verified": completed,
                    "done": ["research.start"] if completed else [],
                    "effects": {"links": [f"/v/{vid}/research"], "message": "Review the research credit quote."}}
    result = asyncio.run(service._accept_with_god(Executor, service.get(sid), None))
    assert result["result_state"] == status
    assert result["status"] == ("done" if status == "done" else "accepted")
    assert result["result_href"] == f"/v/{vid}/research"
    assert "Previously started" not in result["result_message"]


def test_card_approval_does_not_follow_hidden_actions(env, monkeypatch):
    from app.autopilot import service

    calls = []
    async def execute(vid, kind, payload, why, owner, done, human, **kwargs):
        calls.append(kind)
        done.append(kind)
        return {"actions": [{"action": "plan.resources_none", "payload": {}}]}, None
    monkeypatch.setattr(service, "_execute", execute)
    token = service._effects.set({})
    try:
        result = asyncio.run(service._run_step_with_budget(env[1], {"chat_action": "research.start", "title": "Research"},
            None, [], True, never=frozenset(), budget=service._ApprovedCardBudget(20000)))
    finally:
        service._effects.reset(token)
    assert result is None and calls == ["research.start"]


def test_nested_dispatch_inherits_publication_fence(env, monkeypatch):
    from app.autopilot import actions, service
    from app.suggestions.proposals import BLOCKED_KINDS

    async def execute(vid, name, payload, why, owner, done, human, **kwargs):
        assert BLOCKED_KINDS <= service._forbidden.get()
        reply, stop = await service._execute_scoped(vid, "site.publish", {}, "Nested PMF", owner, done, human)
        assert reply is None and stop == "needs_you"
        return {"kind": "wait", "actions": []}, "waiting"
    monkeypatch.setattr(service, "_execute", execute)
    result = asyncio.run(actions.execute_proposed_action(env[1], {"kind": "pmf.iterate", "title": "PMF", "payload": {}},
                                                         service._owner(env[1]), approved_ceiling_micro=0))
    assert result["reason"] == "noop"
    assert not service._forbidden.get()


def test_reviewed_question_identity_reaches_driver_and_changed_question_cannot_be_answered(env, monkeypatch):
    from app.autopilot import lifecycle
    from app.db.supymem_models import ClarifierTurn
    from app.state import db
    from core.clock import utcnow

    _, vid = env
    idea = lifecycle._idea_session(vid)
    with db()() as session:
        session.add(ClarifierTurn(id="current_question", session_id=idea.id, venture_id=vid,
            workspace_id=idea.workspace_id, turn=999, role="agent", kind="question", text="Who pays?",
            options='["Owners"]', created_at=utcnow()))
        session.commit()
    monkeypatch.setattr("app.idea.service.post_message", lambda *args, **kwargs: pytest.fail("Changed question answered"))
    result = asyncio.run(lifecycle.run_driver(vid, "idea.answer", {"question_id": "old_question"}, None, True))
    assert result["kind"] == "needs_you" and "changed" in result["body_md"]


@pytest.mark.parametrize("options,expected", [('[]', []), ('["Owners"]', ["idea.answer"])])
def test_answer_card_requires_an_answer_not_a_proposal_request(env, monkeypatch, options, expected):
    from app.autopilot import lifecycle
    from app.db.supymem_models import ClarifierTurn
    from app.state import db
    from app.suggestions import proposals
    from core.clock import utcnow

    _, vid = env
    idea = lifecycle._idea_session(vid)
    with db()() as session:
        session.add(ClarifierTurn(id="answerable_question", session_id=idea.id, venture_id=vid,
            workspace_id=idea.workspace_id, turn=999, role="agent", kind="question", text="Who pays?",
            options=options, created_at=utcnow()))
        session.commit()
    monkeypatch.setattr(proposals, "_propose_scoped", lambda *_: [
        proposals._shape(proposal("idea.answer", {"question_id": "answerable_question"}))])
    assert [item["action_kind"] for item in proposals.propose_next_actions(vid)] == expected
