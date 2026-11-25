"""Founder chat should lead to saved review work, including after a blocker."""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.company_os_harness import append_idea_revision, load_company_os_app


@pytest.mark.parametrize("document,question", [
    ("Supplier audit worksheet for warehouse procurement teams", "What did we do so far?"),
    ("Packing checklist for families planning camping trips", "Catch me up on our saved work."),
])
def test_return_recap_includes_saved_chat_work_from_your_earlier_private_chat(tmp_path, monkeypatch, document, question):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/recap.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.chat import conversations
    from app.chat.journey import guided_answer
    from app.chat.store import append_message
    from app.db.models import Person, Venture, Workspace, WorkspaceMember
    from app.entry import service
    from app.state import db
    from core.clock import utcnow

    now = utcnow()
    with db()() as session:
        session.add(Person(id="recap_owner", display_name="Owner", email="recap@example.invalid", created_at=now))
        session.add(Person(id="recap_other", display_name="Other", email="other@example.invalid", created_at=now))
        session.add(Workspace(id="recap_workspace", owner_id="recap_owner", kind="venture", name="Your workspace",
                              entry_mode="idea", created_at=now, updated_at=now))
        session.add(Venture(id="ven_recap", workspace_id="recap_workspace", name="Your project", raw_idea=document,
                            stage="idea", created_at=now, updated_at=now))
        session.add(WorkspaceMember(id="recap_member", workspace_id="recap_workspace", person_id="recap_other",
                                   role="contributor", created_at=now))
        session.commit()
    with service.venture_scope("ven_recap", workspace_id="recap_workspace", actor_id="recap_other"):
        private = conversations.create("ven_recap", idempotency_key="other")
        with conversations.bind_conversation("ven_recap", private["id"]):
            append_message("ven_recap", role="assistant", body_md="Private material that must stay hidden.")
    with service.venture_scope("ven_recap", workspace_id="recap_workspace", actor_id="recap_owner"):
        earlier = conversations.create("ven_recap", idempotency_key="earlier")
        with conversations.bind_conversation("ven_recap", earlier["id"]):
            append_message("ven_recap", role="user", body_md=f"Write {document}.")
            append_message("ven_recap", role="assistant", body_md=f"{document}\n1. Review what you need.\n2. Check what is missing.")
        returning = conversations.create("ven_recap", idempotency_key="returning")
        with conversations.bind_conversation("ven_recap", returning["id"]):
            reply = guided_answer("ven_recap", {"idea": {}, "attention": {}}, {"state": "idle"}, question=question)
    assert document in reply["body_md"]
    assert "Nothing has been completed" not in reply["body_md"]
    assert "Private material" not in reply["body_md"]


@pytest.mark.parametrize("audience", [
    "warehouse procurement teams",
    "families planning camping trips",
])
def test_recap_excludes_status_and_previous_recaps_using_saved_request_metadata(tmp_path, monkeypatch, audience):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/recap_metadata.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.chat.journey import _saved_chat_work, guided_answer
    from app.chat.store import append_message, list_messages
    from app.db.models import Venture
    from app.state import db
    from core.clock import utcnow

    venture_id = "ven_recap_metadata"
    now = utcnow()
    with db()() as session:
        session.add(Venture(id=venture_id, workspace_id="wsp_recap_metadata", name="Your project",
                            raw_idea=audience, stage="idea", created_at=now, updated_at=now))
        session.commit()
    brief = {"idea": {}, "attention": {}}
    workflow = {"state": "idle"}
    append_message(venture_id, role="user", body_md="What have you done so far?")
    stale = guided_answer(venture_id, brief, workflow, question="What have you done so far?")
    append_message(venture_id, role="assistant", body_md=stale["body_md"], kind=stale["kind"])
    document = f"Interview document for {audience}: ask about your last difficult purchase."
    append_message(venture_id, role="user", body_md=f"Write an interview document for {audience}.")
    append_message(venture_id, role="assistant", body_md=document)
    assert _saved_chat_work(venture_id, "Recap") == [document]
    for question in ("Where are we?", "What did we do so far?", "Recap", "Catch me up"):
        append_message(venture_id, role="user", body_md=question)
        reply = guided_answer(venture_id, brief, workflow, question=question)
        append_message(venture_id, role="assistant", body_md=reply["body_md"], kind=reply["kind"])
    before = list_messages(venture_id)
    reply = guided_answer(venture_id, brief, workflow, question="Recap")
    assert document in reply["body_md"]
    assert "Nothing has been completed" not in reply["body_md"]
    assert reply["body_md"].count("Your saved chat includes these replies") == 1
    assert reply["body_md"].count("Where it stands now:") == 1
    assert _saved_chat_work(venture_id, "Recap") == [document]
    assert list_messages(venture_id) == before


@pytest.mark.parametrize("offer", [
    "Shift handover logs for commercial laundry supervisors",
    "A keepsake journal for amateur birdwatchers",
])
def test_return_guidance_distinguishes_skipped_questions_from_saved_proposals(tmp_path, monkeypatch, offer):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/guidance.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.chat.journey import guided_answer
    from app.db.models import Job, Venture, VentureDocument
    from app.db.supymem_models import ClarifierTurn, IdeaRevision, IdeaSession
    from app.state import db
    from core.clock import utcnow

    now = utcnow()
    with db()() as session:
        session.add(Venture(id="ven_return", workspace_id="wsp_return", name="Your project", raw_idea=offer,
                            stage="idea", created_at=now, updated_at=now))
        session.add(IdeaSession(id="ids_return", workspace_id="wsp_return", venture_id="ven_return", case="know",
                               state="reviewing", current_revision_id="idv_return", created_at=now, updated_at=now))
        session.flush()
        session.add(IdeaRevision(id="idv_return", session_id="ids_return", workspace_id="wsp_return",
                                 venture_id="ven_return", rev=1, source="founder", summary=offer,
                                 structured=json.dumps({"offer": offer, "_questions_skipped": True}), created_at=now))
        session.add(ClarifierTurn(id="turn_return", session_id="ids_return", workspace_id="wsp_return",
                                 venture_id="ven_return", turn=1, role="agent", kind="proposal", text=offer,
                                 created_at=now))
        session.commit()

    brief = {"workspace_id": "wsp_return", "idea": {"name": "Your project", "confirmed": False}}
    workflow = {"state": "queued", "activity": "Waiting for a worker", "action": None}
    for state, kind, role, skipped in [
        ("reviewing", "proposal", "agent", True),
        ("reviewing", "proposal", "agent", False),
        ("clarifying", "question", "agent", False),
        ("clarifying", "question", "agent", True),
        ("clarifying", "question", "founder", False),
        ("clarifying", "opening", "agent", False),
    ]:
        fields = {"offer": offer, "_questions_skipped": skipped}
        with db()() as session:
            idea = session.get(IdeaSession, "ids_return")
            idea.state = state
            append_idea_revision(session, idea.current_revision_id, structured=json.dumps(fields))
            turn = session.get(ClarifierTurn, "turn_return")
            turn.kind, turn.role = kind, role
            turn.text = "What would make this useful to you?" if kind == "question" else offer
            session.commit()

        reply = guided_answer("ven_return", brief, workflow, question="Where do we stand?")
        body = reply["body_md"]
        assert "I need your answer" not in body
        assert "question has not appeared" not in body
        assert "required" not in body.lower()
        assert "worker" not in body.lower()
        assert "skip" in body.lower()
        assert "No new work was started" in body
        assert not any(action.get("confirmation") for action in reply["actions"])
        if kind == "question" and role == "agent" and not skipped:
            assert "What would make this useful to you?" in body
        else:
            assert "What would make this useful to you?" not in body
            assert "review" in body.lower()
            assert reply["actions"] == [{"label": "Review idea", "href": "/v/ven_return/idea", "style": "primary"}]
        if state == "reviewing":
            assert "ready to review" in body.lower()
        with db()() as session:
            assert session.get(IdeaSession, "ids_return").state == state
            idea = session.get(IdeaSession, "ids_return")
            assert json.loads(session.get(IdeaRevision, idea.current_revision_id).structured) == fields
            assert session.query(Job).count() == 0

    with db()() as session:
        session.get(ClarifierTurn, "turn_return").kind = "question"
        session.add(VentureDocument(id="journey_overrides:ven_return", venture_id="ven_return",
            kind="journey_overrides", title="Your choices", body_md=json.dumps({"skipped": {"clarification": {}}}),
            created_at=now, updated_at=now))
        session.commit()
    workflow = {"state": "needs_you", "activity": "You have saved work to review.",
                "action": {"label": "Review saved work", "href": "/v/ven_return/tasks"}}
    reply = guided_answer("ven_return", brief, workflow)
    assert "idea question" not in reply["body_md"]
    assert reply["actions"] == [{"label": "Review saved work", "href": "/v/ven_return/tasks", "style": "primary"}]


def test_guided_status_for_new_venture_uses_idea_review(monkeypatch):
    load_company_os_app()
    from app.chat import assistant
    from app.ventures import run_status

    monkeypatch.setattr(assistant, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "workspace_id": "wsp_new", "idea": {"name": "NewCo"},
        "plan": {"present": False}, "attention": {"needs_you": []},
    })
    monkeypatch.setattr(run_status, "snapshot", lambda *_args, **_kwargs: {
        "state": "idle", "activity": "No active background job recorded.",
        "next_step": "Open the venture's saved work.", "action": None,
    })
    monkeypatch.setattr(assistant, "_complete", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("Broad venture status should use saved workflow")))

    reply = asyncio.run(assistant.answer_question("ven_new", "Where are we, and what should I do next?"))
    assert "no company job is in progress" in reply["body_md"]
    assert reply["actions"] == [{"label": "Review idea", "href": "/v/ven_new/idea", "style": "primary"}]


def test_failed_build_guidance_names_recovery_and_keeps_route_in_venture(monkeypatch):
    load_company_os_app()
    from app.chat import assistant
    from app.ventures import run_status

    monkeypatch.setattr(assistant, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "workspace_id": "wsp_one", "idea": {"name": "Example"},
        "plan": {"present": True, "state": "approved"},
        "builds": {"last": {"state": "failed", "failure_reason": "QA timed out"}},
        "attention": {"needs_you": ["Last build failed: QA timed out"]},
    })
    monkeypatch.setattr(run_status, "snapshot", lambda *_args, **_kwargs: {
        "state": "failed", "activity": "The build did not complete successfully.",
        "next_step": "Review the saved result before deciding whether to retry.",
        "action": {"label": "Review build", "href": "/v/ven_other/builds"},
    })

    reply = asyncio.run(assistant.answer_question("ven_one", "How do we get unstuck?"))
    assert "QA timed out" in reply["body_md"]
    assert "do not retry automatically" in reply["body_md"]
    assert reply["actions"] == [{"label": "Review build result", "href": "/v/ven_one/builds", "style": "primary"}]


def test_status_of_specific_build_stays_on_existing_build_answer():
    load_company_os_app()
    from app.chat.journey import asks_for_guidance
    from app.chat.intent import classify_intent

    assert not asks_for_guidance("What is the status of the latest product build?")
    assert classify_intent("What is the status of the latest product build?") == "assistant"


@pytest.mark.parametrize("question", [
    "Why is Execution blocked?",
    "What’s blocking Execution?",
    "Why is Execution blocked? Do not approve this plan or start a build.",
])
def test_execution_blocker_question_uses_saved_next_action(monkeypatch, question):
    load_company_os_app()
    from app.chat import assistant
    from app.chat.intent import classify_intent
    from app.ventures import run_status

    assert classify_intent(question) == "assistant"
    monkeypatch.setattr(assistant, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "workspace_id": "wsp_shift", "idea": {"name": "ShiftLens"},
        "plan": {"present": True, "state": "founder_review"},
        "attention": {"needs_you": ["Review execution plan"]},
    })
    monkeypatch.setattr(run_status, "snapshot", lambda *_args, **_kwargs: {
        "state": "needs_you", "activity": "The execution plan is ready for review.",
        "next_step": "Review execution plan",
        "action": {"label": "Review execution plan", "href": "/v/ven_shift/plan#plan-review"},
    })
    monkeypatch.setattr(assistant, "_complete", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("Execution blocker guidance must use current saved workflow")))

    reply = asyncio.run(assistant.answer_question("ven_shift", question))
    assert "execution plan is ready for review" in reply["body_md"].lower()
    assert reply["actions"] == [{"label": "Review execution plan", "href": "/v/ven_shift/plan#plan-review", "style": "primary"}]


def test_personal_next_step_with_explicit_denial_uses_current_review_link(monkeypatch):
    load_company_os_app()
    from app.chat import assistant
    from app.chat.journey import asks_for_guidance
    from app.ventures import run_status

    question = "What should I do next for this personal project? Do not start a build or approve anything."
    assert asks_for_guidance(question)
    assert not asks_for_guidance("What is the status of the latest product build?")
    monkeypatch.setattr(assistant, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "workspace_id": "wsp_one", "idea": {"name": "Personal Bookmark QA", "project_intent": "personal"},
        "plan": {"present": True, "current_step": "Build personal project with Codex"},
        "attention": {"needs_you": []},
    })
    monkeypatch.setattr(run_status, "snapshot", lambda *_args, **_kwargs: {
        "state": "needs_you", "activity": "Your confirmed personal project is ready for a reviewed local build.",
        "next_step": "Review local build",
        "action": {"label": "Review local build", "href": "/v/ven_one/idea#journey-action"},
    })
    monkeypatch.setattr(assistant, "_complete", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("Read-only status must use the saved workflow")))

    reply = asyncio.run(assistant.answer_question("ven_one", question))
    assert "build task waiting" not in reply["body_md"]
    assert "reviewed local build" in reply["body_md"]
    assert reply["actions"] == [{"label": "Review local build", "href": "/v/ven_one/idea#journey-action", "style": "primary"}]


def test_running_and_paused_guidance_use_real_next_checkpoint():
    load_company_os_app()
    from app.chat.journey import guided_answer

    brief = {"idea": {"name": "Example"}, "plan": {"present": False}, "attention": {"needs_you": []}}
    running = guided_answer("ven_one", brief, {
        "state": "working", "activity": "Building the approved product",
        "next_step": "Independent QA, usage settlement, then your build review.",
        "action": {"label": "View build progress", "href": "/v/ven_one/builds?run=bld_one"},
    })
    assert "Independent QA" in running["body_md"]
    assert running["actions"][0]["href"] == "/v/ven_one/builds?run=bld_one"

    paused = guided_answer("ven_one", brief, {"state": "paused"})
    assert "No work was restarted" in paused["body_md"]
    assert paused["actions"][0]["action"] == "resume" and paused["actions"][1]["href"] == "/"


def test_idle_startup_with_waitlist_gets_saved_gtm_pilot_step():
    load_company_os_app()
    from app.chat.journey import guided_answer

    reply = guided_answer("ven_one", {
        "idea": {"name": "Example"},
        "website": {"live": True, "waitlist_enabled": True, "waitlist_count": 3},
        "gtm": {"present": True, "bottleneck": "No evidence from target operations teams",
                "evidence_gaps": ["No customer interviews are recorded"]},
        "plan": {"present": True, "state": "approved"},
    }, {"state": "idle", "action": None})

    assert "3 recorded requests" in reply["body_md"]
    assert "not a verified product user" in reply["body_md"]
    assert "No evidence from target operations teams" in reply["body_md"]
    assert "No customer interviews are recorded" in reply["body_md"]
    assert "one narrow pilot cohort" in reply["body_md"]
    assert reply["actions"] == [{"label": "Review pilot in GTM",
                                 "href": "/gtm?venture=ven_one", "style": "primary"}]


def test_pilot_guidance_does_not_override_active_or_saved_required_work():
    load_company_os_app()
    from app.chat.journey import guided_answer

    brief = {"idea": {"name": "Example"},
             "website": {"live": True, "waitlist_enabled": True},
             "gtm": {"present": True, "bottleneck": "Unknown cohort"}}
    workflow = {"state": "needs_you", "activity": "Review the accepted source.",
                "next_step": "Review build evidence",
                "action": {"label": "Review build evidence", "href": "/v/ven_one/builds"}}
    reply = guided_answer("ven_one", brief, workflow)
    assert reply["actions"][0]["href"] == "/v/ven_one/builds"
    assert "Unknown cohort" not in reply["body_md"]

    personal = {**brief, "idea": {"name": "Hobby", "project_intent": "personal"}}
    reply = guided_answer("ven_one", personal, {"state": "idle", "action": None})
    assert reply["actions"][0]["href"] != "/gtm?venture=ven_one"


def test_live_page_with_open_saved_task_shows_reconciliation_and_pilot():
    load_company_os_app()
    from app.chat.journey import guided_answer

    reply = guided_answer("ven_one", {
        "idea": {"name": "Example"},
        "website": {"live": True, "waitlist_enabled": True, "waitlist_count": 2},
        "gtm": {"present": True, "evidence_gaps": ["No interviews with target buyers"]},
    }, {"state": "needs_you", "activity": "A public landing page is live.",
        "action": {"label": "Open saved work", "href": "/v/ven_one/tasks"}})

    assert "Reconcile the saved task" in reply["body_md"]
    assert "No interviews with target buyers" in reply["body_md"]
    assert [action["href"] for action in reply["actions"]] == [
        "/v/ven_one/tasks", "/gtm?venture=ven_one"]


def test_saved_gtm_text_is_plain_in_guidance():
    load_company_os_app()
    from app.chat.journey import guided_answer

    reply = guided_answer("ven_one", {
        "idea": {"name": "Example"},
        "website": {"live": True, "waitlist_enabled": True},
        "gtm": {"present": True, "bottleneck": "<script>bad</script>\n**Unknown cohort**"},
    }, {"state": "idle", "action": None})
    assert "<script>" not in reply["body_md"]
    assert "**Unknown cohort**" not in reply["body_md"]


def test_recap_shows_latest_mac_task_in_plain_words_and_collapses_repeats():
    from app.chat.journey import _done_list

    stopped = {"title": "Mac task in com.apple.Safari", "status": "stopped"}
    brief = {"device_work": {"recent": [{**stopped, "request": "Open fixture-grooming.supymem.com in Safari"}, stopped, stopped, stopped]}}
    lines = [d for d in _done_list(brief) if "Mac task" in d]
    assert lines == ["Mac task: Open fixture-grooming.supymem.com in Safari - result needs your review",
                     "3 earlier Mac tasks"]
    assert _done_list({"device_work": {"recent": [stopped]}}) == ["Mac task: A task in Safari - result needs your review"]
