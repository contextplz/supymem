import json
from datetime import timedelta

import pytest


@pytest.mark.parametrize("audience", ["Freight coordinators", "Weekend hikers"])
def test_confirmation_explains_the_remaining_priorities_handoff(confirmation_saved, monkeypatch, audience):
    from app.chat import execute
    from app.db.models import CreditLedgerEntry, Job
    from app.db.supymem_models import ClarifierTurn, IdeaSession, ValidationRun
    from app.idea import service
    from app.idea.validation import METHODS
    from app.ventures import startup_review
    from core.clock import utcnow

    data = confirmation_saved
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(startup_review, "get", lambda _: {"complete": False, "question": None})
    with data["factory"]() as session:
        idea = IdeaSession(id="priority_session", workspace_id=data["wid"], venture_id=data["vid"],
            case="know", state="reviewing", created_at=utcnow(), updated_at=utcnow())
        session.add(idea)
        session.flush()
        revision = service.append_revision(session, idea, {"customer": audience, "offer": "A shared checklist",
            "problem": "Important tasks get missed", "workflow": "Record completed tasks"}, source="founder")
        session.add(ClarifierTurn(id="priority_proposal", session_id=idea.id, workspace_id=data["wid"],
            venture_id=data["vid"], turn=1, role="agent", kind="proposal", text=revision.summary, created_at=utcnow()))
        for method in METHODS:
            session.add(ValidationRun(id=f"priority_{method}", session_id=idea.id, revision_id=revision.id,
                workspace_id=data["wid"], venture_id=data["vid"], method=method, state="complete",
                founder_decision="accept", created_at=utcnow(), completed_at=utcnow()))
        session.commit()
        revision_id = revision.id
        jobs_before, charges_before = session.query(Job).count(), session.query(CreditLedgerEntry).count()
    reply = execute._confirm_idea(data["vid"], {"session_id": "priority_session", "revision_id": revision_id}, data["actor"])
    assert reply["kind"] == "startup_review"
    assert "Your idea is saved" in reply["body_md"]
    assert "What should come first for you?" in reply["body_md"]
    assert "Choose your priorities below before research or a build starts" in reply["body_md"]
    assert "charged" not in reply["body_md"] and "\u2014" not in reply["body_md"]
    with data["factory"]() as session:
        assert session.get(IdeaSession, "priority_session").state == "confirmed"
        assert session.query(Job).count() == jobs_before
        assert session.query(CreditLedgerEntry).count() == charges_before


@pytest.mark.parametrize("audience", ["Freight coordinators", "Weekend hikers"])
@pytest.mark.parametrize("status", ["failed", "failed_dead", "poison", "pending", "running"])
def test_local_idea_review_reports_failure_or_timeout(confirmation_saved, monkeypatch, audience, status):
    import asyncio
    from datetime import timedelta
    from app.chat import execute, router
    from app.db.models import Job
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.idea import service
    from core.clock import utcnow
    from core.ids import new_id

    data = confirmation_saved
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    with data["factory"]() as session:
        idea = IdeaSession(id=new_id("ids"), workspace_id=data["wid"], venture_id=data["vid"], case="know",
            state="validating", created_at=utcnow(), updated_at=utcnow())
        session.add(idea)
        session.flush()
        revision = service.append_revision(session, idea, {"customer": audience, "problem": "Missed plans"}, source="founder")
        session.add(ClarifierTurn(id=new_id("ict"), session_id=idea.id, workspace_id=data["wid"],
            venture_id=data["vid"], turn=1, role="agent", kind="proposal", text=revision.summary,
            options="{}", created_at=utcnow()))
        job_id = new_id("job")
        old = utcnow() - timedelta(minutes=20)
        session.add(Job(id=job_id, workspace_id=data["wid"], venture_id=data["vid"], kind="idea.validate",
            payload_json=json.dumps({"session_id": idea.id, "revision_id": revision.id}),
            status=status, last_error="provider_unavailable", created_at=old,
            started_at=old if status == "running" else None, timeout_s=60))
        session.commit()
        job_ids = {job.id for job in session.query(Job)}
    for result in (execute._review_idea(data["vid"]),
                   asyncio.run(router.handle_message(data["vid"], "How is my idea review going?", actor_subject=data["actor"]))):
        assert result["kind"] == "idea_recovery"
        assert "saved" in result["body_md"] and "Retry" in result["body_md"]
        assert any(action.get("label") == "Retry" for action in result["actions"])
        assert "still" not in result["body_md"] and "provider_unavailable" not in result["body_md"]
    if status in {"pending", "running"}:
        wakeups = []
        monkeypatch.setattr("app.core.jobs.publish_wake", lambda: wakeups.append(True))
        retry = asyncio.run(execute.execute_action(data["vid"], "job.retry", payload={"job_id": job_id}, actor_subject=data["actor"]))
        assert retry["kind"] == "idea_recovery" and wakeups == [True]
        with data["factory"]() as session:
            assert {job.id for job in session.query(Job)} == job_ids
            assert session.get(Job, job_id).status == status


@pytest.mark.parametrize("audience", ["Freight coordinators", "Weekend hikers"])
def test_local_idea_review_wait_is_bounded_not_immediate(confirmation_saved, monkeypatch, audience):
    from app.chat import execute
    from app.db.models import Job
    from app.db.supymem_models import IdeaSession
    from core.clock import utcnow
    from core.ids import new_id

    data = confirmation_saved
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    with data["factory"]() as session:
        idea = IdeaSession(id=new_id("ids"), workspace_id=data["wid"], venture_id=data["vid"], case="know",
            state="clarifying", created_at=utcnow(), updated_at=utcnow())
        session.add(idea)
        session.flush()
        job_id = new_id("job")
        session.add(Job(id=job_id, workspace_id=data["wid"], venture_id=data["vid"], kind="idea.clarify_turn",
            payload_json=json.dumps({"session_id": idea.id, "message": audience}), status="pending", created_at=utcnow()))
        session.commit()
    assert execute._idea_review_failure(data["vid"]) is None
    with data["factory"]() as session:
        session.get(Job, job_id).created_at = utcnow() - timedelta(seconds=121)
        session.commit()
    assert execute._idea_review_failure(data["vid"])["kind"] == "idea_recovery"
    with data["factory"]() as session:
        job = session.get(Job, job_id)
        job.status, job.last_error = "failed_dead", "clarification_invalid_output"
        session.commit()
        before = {row.id for row in session.query(Job)}
    assert execute._idea_review_failure(data["vid"]) is None
    recovery = execute._review_idea(data["vid"])
    from app.gstack.office_hours import SKIP

    assert recovery["kind"] == "idea_recovery"
    assert any(action.get("payload", {}).get("text") == SKIP for action in recovery["actions"])
    assert not any(action.get("action") == "job.retry" for action in recovery["actions"])
    assert not any(action.get("action") == "idea.review" for action in recovery["actions"])
    with data["factory"]() as session:
        assert {row.id for row in session.query(Job)} == before

from tests.unit.chat.test_round3_matrix import venture  # noqa: F401
from tests.integration.test_postgres_auth_security import pg_database  # noqa: F401


@pytest.mark.parametrize("audience,problem", [("cold storage operators", "Missed temperature checks"),
                                              ("families planning train holidays", "Missed travel connections")])
@pytest.mark.parametrize("confirmation_request", ["Confirm my idea using what I have told you. Skip any remaining optional questions. Do not start research yet.",
                                    "Skip the remaining questions and confirm my idea. Do not start research."])
def test_explicit_confirm_and_optional_skip_persists_without_research(confirmation_saved, monkeypatch, audience, problem, confirmation_request):
    import asyncio

    from app.chat import idea_proposal, router
    from app.db.models import CreditLedgerEntry, Job
    from app.db.supymem_models import ClarifierTurn, IdeaRevision, IdeaSession
    from app.idea import service
    from core.clock import utcnow
    from core.ids import new_id

    data = confirmation_saved
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    assert idea_proposal.clear_approval(confirmation_request, question_showing=True)
    with data["factory"]() as session:
        idea = IdeaSession(id=new_id("ids"), workspace_id=data["wid"], venture_id=data["vid"], case="know",
                           state="reviewing", created_at=utcnow(), updated_at=utcnow())
        session.add(idea)
        session.flush()
        revision = service.append_revision(session, idea, {"customer": audience, "problem": problem,
            "offer": "A private reminder checklist", "workflow": "Record your completed checks"}, source="founder")
        session.add(ClarifierTurn(id=new_id("ict"), session_id=idea.id, workspace_id=data["wid"],
            venture_id=data["vid"], turn=1, role="agent", kind="proposal", text=revision.summary,
            options="{}", created_at=utcnow()))
        sid = idea.id
        jobs_before, charges_before = session.query(Job).count(), session.query(CreditLedgerEntry).count()
        session.commit()
    reply = asyncio.run(router.handle_message(data["vid"], confirmation_request, actor_subject=data["actor"]))
    with data["factory"]() as session:
        saved = session.get(IdeaSession, sid)
        assert saved.state == "confirmed" and saved.confirmed_revision_id == saved.current_revision_id
        fields = json.loads(session.get(IdeaRevision, saved.confirmed_revision_id).structured)
        assert fields["customer"] == audience and fields["problem"] == problem
        assert session.query(Job).count() == jobs_before
        assert session.query(CreditLedgerEntry).count() == charges_before
    assert reply["kind"] != "confirmation" and not any(action.get("confirmation") for action in reply["actions"])


@pytest.fixture
def confirmation_saved(tmp_path, monkeypatch, request):
    if getattr(request, "param", None) == "hosted":
        monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    import os
    from tests.company_os_harness import load_company_os_app

    database_url = (request.getfixturevalue("pg_database")[0] if os.environ.get("TEST_POSTGRES_URL")
                    else f"sqlite:///{tmp_path}/confirmation.db")
    monkeypatch.setenv("COMPANY_OS_DB_URL", database_url)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "0")
    load_company_os_app()
    from app.authz.workspace import ensure_subject_person
    from app.core.local_mode import is_local
    from app.core.local_owner import ensure_owner
    from app.credits.accounts import ensure_account
    from app.credits.policy import WALLET_PHASE_KEY, person_wallet_enabled
    from app.db.models import CreditAccount, CreditPolicy, Venture
    from app.state import db
    from app.tenancy import bind
    from app.workspaces.service import create_workspace
    from core.clock import utcnow
    from core.ids import new_id

    with db()() as session:
        phase = session.get(CreditPolicy, WALLET_PHASE_KEY)
        if phase is not None and phase.value_json != json.dumps("person"):
            assert session.query(CreditAccount).count() == 0
            phase.value_json = json.dumps("person")
            session.commit()
    actor = ensure_owner().id if is_local() else new_id("usr")
    ensure_subject_person(actor)
    workspace = create_workspace(kind="company", name="Confirmation regression", owner_id=actor)
    venture_id = new_id("ven")
    with db()() as session:
        session.add(Venture(id=venture_id, workspace_id=workspace.id, name="Saved idea",
                            raw_idea="", stage="draft", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    with bind(workspace_id=workspace.id, venture_id=venture_id, actor=actor):
        ensure_account(venture_id, workspace_id=workspace.id, owner_person_id=actor)
        assert person_wallet_enabled()
        yield {"vid": venture_id, "wid": workspace.id, "actor": actor, "factory": db()}


@pytest.mark.parametrize("audience,problem", [
    ("Independent bakery owners", "Unfilled delivery shifts"),
    ("Weekend hikers", "Choosing unfamiliar trails"),
])
@pytest.mark.parametrize("phase", ["core", "gstack"])
def test_requested_review_shows_saved_summary_before_open_question(confirmation_saved, audience, problem, phase):
    from app.chat import idea_proposal
    from app.db.models import CreditLedgerEntry, Job
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.idea import service
    from core.clock import utcnow
    from core.ids import new_id

    data = confirmation_saved
    summary = f"You help {audience} with {problem.lower()}."
    with data["factory"]() as session:
        idea = IdeaSession(id=new_id("ids"), workspace_id=data["wid"], venture_id=data["vid"],
                           case="know", state="clarifying", created_at=utcnow(), updated_at=utcnow())
        session.add(idea)
        session.flush()
        revision = service.append_revision(session, idea, {"customer": audience, "problem": problem,
            "summary": summary}, source="founder")
        revision_id = revision.id
        options = {"field": "evidence", "options": [], "phase": phase}
        if phase == "gstack":
            options.update(gstack_id="Q1", gstack_name="Customer demand", index=1, total=1)
        question = ClarifierTurn(id=new_id("ict"), session_id=idea.id, workspace_id=data["wid"],
            venture_id=data["vid"], turn=1, role="agent", kind="question", text="What have you observed?",
            options=json.dumps(options), revision_id=revision_id, created_at=utcnow())
        session.add(question)
        session.commit()
        session_id = idea.id
        before = (session.query(Job).count(), session.query(CreditLedgerEntry).count())
    summary = service.get_session(session_id)["idea_summary"]
    reply = idea_proposal.review(data["vid"])
    assert reply["body_md"].startswith(summary + "\n\n")
    assert "What have you observed?" in reply["body_md"]
    assert reply["kind"] == "idea_clarification_ready"
    assert any(action["action"] == "chat.reply" for action in reply["actions"])
    saved = service.get_session(session_id)
    assert saved["state"] == "clarifying" and saved["current_revision"]["id"] == revision_id
    assert len(saved["turns"]) == 1
    with data["factory"]() as session:
        assert (session.query(Job).count(), session.query(CreditLedgerEntry).count()) == before


@pytest.mark.parametrize("audience,problem", [
    ("Freight dispatchers", "Missed delivery appointments"),
    ("Amateur gardeners", "Plants drying out during holidays"),
])
@pytest.mark.parametrize("message,claim,receipts,expected", [
    ("Please confirm my idea. Do not start research.", None, "complete", "confirmed"),
    ("Acknowledge the review and confirm the idea. Do not run an experiment.", None, "complete", "confirmed"),
    ("confirm", None, "complete", "confirmed"),
    ("Looks right. Confirm this idea.", None, "complete", "confirmed"),
    ("I confirm this idea.", None, "complete", "confirmed"),
    ("Summarize what we found.", "The confirmed idea is ready. Choose your next step.", "complete", "validating"),
    ("Summarize what we found.", "Your idea is confirmed. Choose your next step.", "complete", "validating"),
    ("Summarize what we found.", "Your idea is **confirmed**. Choose your next step.", "complete", "validating"),
    ("Do not confirm my idea.", None, "complete", "validating"),
    ('The note says "Leave things alone. Confirm this idea."', None, "complete", "validating"),
    ("Can you explain how to confirm my idea?", None, "complete", "validating"),
    ("Please confirm my idea.", None, "pending", "validating"),
    ("Please confirm my idea.", None, "active", "validating"),
    ("Summarize what we found.", "Your idea is confirmed. Choose your next step.", "confirmed", "confirmed"),
])
def test_chat_confirmation_matches_saved_state(confirmation_saved, monkeypatch, audience, problem, message, claim, receipts, expected):
    monkeypatch.setattr("app.core.local_mode.is_test_mode", lambda: True)
    import asyncio
    from app.chat import guard, router
    from app.chat.store import list_messages
    from app.db.models import ChatMessage, CreditLedgerEntry, Job
    from app.db.supymem_models import ClarifierTurn, IdeaSession, ValidationRun
    from app.idea import service
    from app.idea.validation import METHODS
    from app.ventures import startup_review
    from core.clock import utcnow
    from core.enums import JobStatus
    from core.ids import new_id

    data = confirmation_saved
    session_id = new_id("ids")
    with data["factory"]() as session:
        idea = IdeaSession(id=session_id, workspace_id=data["wid"], venture_id=data["vid"],
                           case="know", state="validating", created_at=utcnow(), updated_at=utcnow())
        session.add(idea)
        session.flush()
        revision = service.append_revision(session, idea, {"customer": audience, "problem": problem,
            "offer": "Reminders", "workflow": "Check upcoming tasks", "desired_outcome": "Avoid missed tasks",
            "first_user_path": "Try a reminder", "pricing_hypothesis": "Monthly subscription"}, source="founder")
        revision_id = revision.id
        session.add(ClarifierTurn(id=new_id("ict"), session_id=session_id, workspace_id=data["wid"],
            venture_id=data["vid"], turn=1, role="agent", kind="proposal", text="Review your saved idea.",
            options="{}", revision_id=revision_id, created_at=utcnow()))
        for method in METHODS:
            session.add(ValidationRun(id=new_id("ivr"), session_id=session_id, revision_id=revision_id,
                workspace_id=data["wid"], venture_id=data["vid"], method=method,
                state="pending" if receipts == "pending" else "complete",
                report=json.dumps({"summary": f"You help {audience} avoid {problem}."}), created_at=utcnow()))
        if receipts == "active":
            session.add(Job(id=new_id("job"), kind="idea.validate", workspace_id=data["wid"],
                venture_id=data["vid"], status=JobStatus.PENDING.value, created_at=utcnow(),
                payload_json=json.dumps({"session_id": session_id, "revision_id": revision_id})))
        if receipts == "confirmed":
            idea.state = "confirmed"
            idea.confirmed_revision_id = revision_id
        startup_review._document(session, data["vid"], "internal_new_user_flow", {"enabled": True})
        session.commit()
        jobs_before = session.query(Job).count()
        ledger_before = session.query(CreditLedgerEntry).count()

    async def answer(*args, **kwargs):
        return {"kind": "text", "body_md": claim or "Nothing changed. Review your saved idea next.",
                "actions": [], "citations": []}

    monkeypatch.setattr(router.general, "handle", answer)
    if claim:
        monkeypatch.setattr(router, "_dispatch_intent", answer)
    background_requests = []
    monkeypatch.setattr("app.entry.chat_start.enqueue_ready_exploration", background_requests.append)
    monkeypatch.setattr(guard, "pending_confirmation", lambda *_args: {"actions": [{"confirmation": {
        "action": "approve", "resulting_state": {"approval_id": "unrelated"}}}]})
    from app.state import MEM
    monkeypatch.setattr("app.ventures.state_brief.MEM", MEM)
    reply = asyncio.run(router.handle_message(data["vid"], message, actor_subject=data["actor"]))
    assert background_requests == []
    history = list_messages(data["vid"])
    assert history[-1]["id"] == reply["id"]
    assert history[-1]["body_md"] == reply["body_md"]
    saved = service.get_session(session_id)
    assert saved["state"] == expected, reply
    if expected == "confirmed":
        assert reply["kind"] != "confirmation"
        assert claim in reply["body_md"] if claim else "saved" in reply["body_md"].lower()
        assert not any(action.get("confirmation") for action in reply["actions"])
    elif claim:
        assert "not confirmed" in reply["body_md"].lower()
        assert any(action.get("action") == "idea.review" for action in reply["actions"])
    with data["factory"]() as session:
        assert session.get(ChatMessage, reply["id"]).body_md == reply["body_md"]
        if expected == "confirmed":
            assert session.get(IdeaSession, session_id).confirmed_revision_id == revision_id
        assert session.query(Job).count() == jobs_before
        assert session.query(CreditLedgerEntry).count() == ledger_before


@pytest.mark.parametrize("audience,problem,offer", [
    ("Warehouse managers", "Misplaced reusable pallets", "A manual pallet return log"),
    ("Apartment gardeners", "Forgetting when to water balcony plants", "A personal watering calendar"),
])
@pytest.mark.parametrize("confirmation_saved", ["hosted"], indirect=True)
def test_rejected_review_saves_confirmable_summary(confirmation_saved, monkeypatch, audience, problem, offer):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    import asyncio
    from app.chat import router
    from app.chat.store import list_messages
    from app.db.models import CreditLedgerEntry, Job
    from app.db.supymem_models import IdeaRevision, IdeaSession, ResearchDossier, ValidationRun
    from app.gbrain import think
    from app.gstack import office_hours
    from app.idea import service
    from app.research import execution, metered
    from app.ventures import startup_review
    from core.clock import utcnow
    from core.ids import new_id
    from tests.idea_fixtures import install_provider, validation_completion
    from tests.security.test_durable_journey_boundary import run_queued

    async def no_memory(**kwargs):
        return think.Gathered()

    def rejected_review(messages, kwargs):
        assert kwargs["purpose"] == "idea.validate"
        response = validation_completion(messages)
        report = json.loads(response.choices[0].message.content)
        report["summary"] = "Your product has verified paying customers and proven demand."
        for finding in report["findings"]:
            finding["source_ids"] = ["invented_evidence"]
        response.choices[0].message.content = json.dumps(report)
        return response

    monkeypatch.setattr(think, "gather", no_memory)
    monkeypatch.setattr("app.ventures.founder_facts.schedule", lambda *args, **kwargs: None)
    monkeypatch.setattr(metered, "REPAIR_ATTEMPTS", 1)
    calls = install_provider(monkeypatch, rejected_review)
    data = confirmation_saved
    session_id = new_id("ids")
    answer = f"{audience} handle {problem} with handwritten notes. I would replace those notes with {offer}. I have not tested demand."
    with data["factory"]() as session:
        idea = IdeaSession(id=session_id, workspace_id=data["wid"], venture_id=data["vid"],
                           case="know", state="validating", created_at=utcnow(), updated_at=utcnow())
        session.add(idea)
        session.flush()
        revision = service.append_revision(session, idea, {"project_intent": "startup", "customer": audience,
            "problem": problem, "offer": offer, "summary": f"You want to offer {offer} to {audience}."}, source="founder")
        revision_id, structured_before = revision.id, revision.structured
        question = office_hours.add_question_turn(session, idea, {"gstack_id": "Q2", "gstack_name": "Status quo",
            "text": "How do your users handle this problem today?", "why": "Describe the current workaround.",
            "options": [], "free_text_only": True}, [], 1, 1, utcnow())
        assert json.loads(question.options)["options"] == []
        startup_review._document(session, data["vid"], "internal_new_user_flow", {"enabled": True})
        session.commit()

    reply = service.post_message(session_id, answer, actor_subject=data["actor"])
    assert reply["job_id"]
    assert service.get_session(session_id)["gstack_answers"][0]["answer"] == answer
    job_id = run_queued("idea.validate")
    assert len(calls) == 2
    saved = service.get_session(session_id)
    assert saved["state"] != "confirmed"
    assert saved["validation_runs"] == []
    message = next(item for item in reversed(list_messages(data["vid"])) if item["kind"] == "idea_validation_ready")
    assert all(text in message["body_md"] for text in (audience, problem, offer, answer))
    assert "confirm" in message["body_md"].lower() and "not proof" in message["body_md"].lower()
    assert "verified paying customers" not in message["body_md"]
    assert "\u2014" not in message["body_md"] and "the founder" not in message["body_md"].lower()
    with pytest.raises(RuntimeError, match="validation_required"):
        service.confirm(session_id, revision_id=revision_id, venture_id=data["vid"],
                        person_id=data["actor"], start_research=True, accept_validation=True)
    with data["factory"]() as session:
        job = session.get(Job, job_id)
        assert job.status == "failed_dead" and job.last_error == "research_invalid_output"
        dossier = session.get(ResearchDossier, execution.identifier(job_id))
        document = json.loads(dossier.summary)
        assert dossier.state == "failed" and document["saved_summary"]["verified"] is False
        assert document["saved_summary"]["provenance"] == "founder_provided"
        operations = document["_runtime"]["operations"]
        assert all(operations[key]["state"] == "completed" and operations[key]["usage"]["settled"] is True
                   and operations[key]["error_code"] == "research_invalid_output" for key in ("review", "review-repair"))
        assert session.get(IdeaRevision, revision_id).structured == structured_before
        jobs_before = session.query(Job).count()
        ledger_before = session.query(CreditLedgerEntry).count()
        runs_before = session.query(ValidationRun).count()
        job.status = "scheduled"
        session.commit()
    with pytest.raises(RuntimeError, match="validation_pending"):
        service.confirm(session_id, revision_id=revision_id, venture_id=data["vid"],
                        person_id=data["actor"], start_research=False, accept_validation=True)
    run_queued("idea.validate")
    assert len(calls) == 2
    assert len([item for item in list_messages(data["vid"]) if item["kind"] == "idea_validation_ready"]) == 1
    with data["factory"]() as session:
        dossier = session.get(ResearchDossier, execution.identifier(job_id))
        document = json.loads(dossier.summary)
        document.pop("saved_summary")
        dossier.summary = json.dumps(document)
        session.commit()
    confirmed = asyncio.run(router.handle_message(data["vid"], "Please confirm my idea. Do not start research.",
                                                  actor_subject=data["actor"]))
    assert confirmed["kind"] != "confirmation" and not any(action.get("confirmation") for action in confirmed["actions"])
    assert service.get_session(session_id)["state"] == "confirmed", confirmed
    with data["factory"]() as session:
        assert session.get(IdeaSession, session_id).confirmed_revision_id == revision_id
        assert json.loads(session.get(ResearchDossier, execution.identifier(job_id)).summary)["saved_summary"]["verified"] is False
        assert session.query(Job).count() == jobs_before
        assert session.query(CreditLedgerEntry).count() == ledger_before
        assert session.query(ValidationRun).count() == runs_before


@pytest.mark.parametrize("audience,problem", [
    ("Freight dispatchers", "Missed delivery appointments"),
    ("Amateur gardeners", "Plants drying out during holidays"),
])
@pytest.mark.parametrize("working", [False, True, "race"])
@pytest.mark.parametrize("has_options", [False, True])
@pytest.mark.parametrize("message", [
    "This is the idea I want to pursue, please confirm it",
    "Yes, this is the idea I want to pursue. Let's go with it.",
])
def test_typed_confirmation_is_done_or_explains_when_to_retry(confirmation_saved, monkeypatch, audience, problem, working, has_options, message):
    import asyncio
    from app.chat import router
    from app.chat.store import append_message
    from app.core.local_mode import is_local
    from app.db.models import CreditLedgerEntry, Job
    from app.db.supymem_models import ClarifierTurn, IdeaRevision, IdeaSession
    from app.idea import service
    from app.ventures import startup_review
    from core.clock import utcnow
    from core.ids import new_id

    data = confirmation_saved
    with data["factory"]() as session:
        idea = IdeaSession(id=new_id("ids"), workspace_id=data["wid"], venture_id=data["vid"],
            case="know", state="reviewing", created_at=utcnow(), updated_at=utcnow())
        session.add(idea)
        session.flush()
        revision = service.append_revision(session, idea, {"customer": audience, "problem": problem,
            "offer": "Reminders", "workflow": "Check upcoming tasks", "desired_outcome": "Avoid missed tasks",
            "first_user_path": "Try a reminder", "pricing_hypothesis": "Monthly subscription"}, source="founder")
        sid, rid, qid = idea.id, revision.id, new_id("ict")
        question = "Who will use your first version?"
        actions = [{"label": audience, "action": "chat.reply", "payload": {"question_id": qid, "text": audience}}] if has_options else []
        session.add(ClarifierTurn(id=qid, session_id=sid, workspace_id=data["wid"], venture_id=data["vid"],
            turn=1, role="agent", kind="question", text=question,
            options=json.dumps({"field": "customer", "options": [audience] if has_options else []}),
            revision_id=rid, created_at=utcnow()))
        if working is True:
            session.add(Job(id=new_id("job"), kind="idea.clarify_turn", workspace_id=data["wid"],
                venture_id=data["vid"], status="running", created_at=utcnow(),
                payload_json=json.dumps({"session_id": sid, "revision_id": rid})))
        startup_review._document(session, data["vid"], "internal_new_user_flow", {"enabled": True})
        session.commit()
        jobs_before = session.query(Job).count()
        ledger_before = session.query(CreditLedgerEntry).count()
        revisions_before = session.query(IdeaRevision).count()
    append_message(data["vid"], role="assistant", kind="idea_clarification_ready", body_md=question,
        actions=actions, citations=[{"question": True}])

    async def forbidden(*_args, **_kwargs):
        pytest.fail("An explicit idea confirmation must not call a model or start unrelated work")

    monkeypatch.setattr(router.general, "handle", forbidden)
    monkeypatch.setattr(router, "_dispatch_intent", forbidden)
    if working == "race":
        original_confirm = service.confirm

        def starts_work_then_confirms(*args, **kwargs):
            with data["factory"]() as session:
                session.add(Job(id=new_id("job"), kind="idea.clarify_turn", workspace_id=data["wid"],
                    venture_id=data["vid"], status="running", created_at=utcnow(),
                    payload_json=json.dumps({"session_id": sid, "revision_id": rid})))
                session.commit()
            return original_confirm(*args, **kwargs)

        monkeypatch.setattr(service, "confirm", starts_work_then_confirms)
        jobs_before += 1
    for _attempt in range(2):
        reply = asyncio.run(router.handle_message(data["vid"], message, actor_subject=data["actor"],
            **({"reply_to_question": True, "question_id": qid} if has_options else {})))
        assert not any(action.get("confirmation") or action.get("action") == "idea.confirm" for action in reply["actions"])
        with data["factory"]() as session:
            saved = session.get(IdeaSession, sid)
            assert session.query(Job).count() == jobs_before
            assert session.query(CreditLedgerEntry).count() == ledger_before
            founder_turns = session.query(ClarifierTurn).filter_by(session_id=sid, role="founder").all()
            assert all(turn.text != message and json.loads(turn.options)["provenance"] == "not_answered" for turn in founder_turns)
            if working:
                assert saved.state == "reviewing" and saved.confirmed_revision_id is None
                assert saved.current_revision_id == rid and session.query(IdeaRevision).count() == revisions_before
                assert "will not confirm it automatically" in reply["body_md"]
                assert "When your summary is ready, tell me to confirm it" in reply["body_md"]
            else:
                assert saved.state == "confirmed" and saved.confirmed_revision_id == saved.current_revision_id
                assert "Your idea is saved" in reply["body_md"]
                fields = json.loads(session.get(IdeaRevision, saved.confirmed_revision_id).structured)
                assert fields["customer"] == audience and fields["problem"] == problem
                if is_local():
                    assert reply["kind"] == "navigation"
                    assert any(action.get("action") == "whats_next" for action in reply["actions"])
                else:
                    assert reply["actions"]


@pytest.mark.parametrize("audience", ["Freight dispatchers", "Amateur gardeners"])
def test_confirm_replay_keeps_the_same_saved_revision_without_starting_work(confirmation_saved, audience):
    from app.db.models import Job
    from app.db.supymem_models import IdeaSession
    from app.idea import service
    from core.clock import utcnow
    from core.ids import new_id

    data = confirmation_saved
    session_id = new_id("ids")
    with data["factory"]() as session:
        idea = IdeaSession(id=session_id, workspace_id=data["wid"], venture_id=data["vid"], case="know",
                           state="reviewing", created_at=utcnow(), updated_at=utcnow())
        session.add(idea)
        session.flush()
        revision_id = service.append_revision(session, idea, {"customer": audience}, source="founder").id
        session.commit()
    service.confirm(session_id, revision_id=revision_id, venture_id=data["vid"],
                    person_id=data["actor"], start_research=False)
    with data["factory"]() as session:
        jobs_before = session.query(Job).count()
        confirmed_at = session.get(IdeaSession, session_id).confirmed_at
    replay = service.confirm(session_id, revision_id=revision_id, venture_id=data["vid"],
                             person_id=data["actor"], start_research=False)
    assert replay["session"]["state"] == "confirmed"
    assert replay["session"]["current_revision"]["id"] == revision_id
    with pytest.raises(RuntimeError, match="idea_review_conflict"):
        service.confirm(session_id, revision_id="stale", venture_id=data["vid"], start_research=False)
    with pytest.raises(KeyError):
        service.confirm(session_id, revision_id=revision_id, venture_id="another_venture", start_research=False)
    with data["factory"]() as session:
        assert session.get(IdeaSession, session_id).confirmed_revision_id == revision_id
        assert session.get(IdeaSession, session_id).confirmed_at == confirmed_at
        assert session.query(Job).count() == jobs_before


@pytest.mark.parametrize("domain", ["dental clinics", "home cooks"])
@pytest.mark.parametrize("state,kind", [("clarifying", "question"), ("reviewing", "question"), ("validating", "question"), ("reviewing", "proposal"), ("confirmed", "proposal")])
def test_saved_journey_is_read_in_chat_without_starting_work(venture, monkeypatch, domain, state, kind):
    from app.chat import idea_proposal
    from app.db.models import Job
    from app.db.supymem_models import IdeaSession
    from app.idea import service
    from app.state import db
    from core.clock import utcnow

    client, vid, workspace_id = venture
    text = f"Founder wants to help {domain}—the founder hasn't clarified the problem."
    report = {"summary": text, "strengths": [text], "biggest_risk": text, "gaps": [text],
              "findings": [{"assessment": text}], "proposed_tests": [{"hypothesis": text,
                  "action": "Ask privately–do not send anything automatically.", "success_measure": "2–3 requests"}]}
    turn = {"id": "question_saved", "role": "agent", "kind": kind,
            "text": f"Who in {domain} needs this?" if kind == "question" else text,
            "options": {"options": [domain, "Not sure"], "field": "customer"}}
    saved = {"id": "idea_saved", "state": state, "idea_summary": text, "turns": [turn],
             "current_revision": {"id": "revision_saved", "summary": text, "structured": {}},
             "validation_runs": [{"state": "complete", "revision_id": "revision_saved", "report": report}] * 2}
    original = json.dumps(saved, sort_keys=True)
    with db()() as session:
        now = utcnow() + timedelta(seconds=1)
        session.add(IdeaSession(id="idea_saved", workspace_id=workspace_id, venture_id=vid,
                               state=state, case="know", current_revision_id="revision_saved",
                               created_at=now, updated_at=now))
        session.commit()
        before = session.query(Job).filter(Job.kind.in_(("idea.validate", "idea.clarify_turn"))).count()
    monkeypatch.setattr(service, "get_session", lambda session_id: saved if session_id == "idea_saved" else None)
    from app.chat import experiments as chat_experiments
    # This fixture stubs the saved session, so the handler's own checks cannot see a confirmed idea.
    monkeypatch.setattr(chat_experiments, "_idea_gate", lambda _venture_id: None)
    direct = idea_proposal.review(vid)
    response = client.post(f"/ventures/{vid}/chat/confirm", json={"action": "idea.review"})
    assert response.status_code == 200, response.text
    reply = response.json()
    assert not any(action.get("href") for action in reply["actions"])
    assert "—" not in reply["body_md"] and "–" not in reply["body_md"]
    if kind == "question":
        assert direct["kind"] == "idea_clarification_ready"
        assert all(action["payload"]["question_id"] == "question_saved" for action in direct["actions"])
        assert domain in reply["body_md"]
    else:
        assert f"You want to help {domain}, you haven't clarified the problem." in reply["body_md"]
        assert reply["body_md"].count("**Test:**") == 1
        assert "Ask privately, do not send anything automatically." in reply["body_md"]
        assert all(label in reply["body_md"] for label in ("What we found", "Your opening", "What I'd do next"))
        assert "Biggest risk" not in reply["body_md"] and "**Gaps:**" not in reply["body_md"]
        if state == "confirmed":
            assert any(action.get("action") == "experiment.recommendation.prepare" for action in reply["actions"])
            assert "Show experiments here" in {action["label"] for action in reply["actions"]}
        else:
            assert reply["actions"] == [{"label": "Accept this review", "action": "idea.confirm",
                "payload": {"session_id": "idea_saved", "revision_id": "revision_saved"}, "style": "primary"}]
            assert "Confirm your idea before planning a market experiment" in reply["body_md"]
        assert not any(action.get("action") == "idea.proposal.approve" for action in reply["actions"])
    assert json.dumps(saved, sort_keys=True) == original
    with db()() as session:
        assert session.query(Job).filter(Job.kind.in_(("idea.validate", "idea.clarify_turn"))).count() == before


@pytest.mark.parametrize("domain", ["dental clinics", "public libraries"])
def test_report_confirmation_uses_the_shown_final_action(venture, monkeypatch, domain):
    from app.chat import router
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.idea import service
    from app.state import db
    from core.clock import utcnow

    client, venture_id, workspace_id = venture
    saved = {"id": "idea_report", "state": "reviewing", "turns": [{"id": "proposal_report",
        "role": "agent", "kind": "proposal", "text": f"Your idea serves {domain}.", "options": {}}],
        "current_revision": {"id": "revision_report", "structured": {"customer": domain}},
        "validation_runs": [{"state": "complete", "revision_id": "revision_report", "report": {
            "summary": f"Your idea serves {domain}.", "proposed_tests": []}}]}
    with db()() as session:
        now = utcnow() + timedelta(seconds=1)
        session.add(IdeaSession(id=saved["id"], workspace_id=workspace_id, venture_id=venture_id,
            state="reviewing", case="know", current_revision_id="revision_report", created_at=now, updated_at=now))
        session.flush()
        session.add(ClarifierTurn(id="proposal_report", session_id=saved["id"], workspace_id=workspace_id,
            venture_id=venture_id, turn=1, role="agent", kind="proposal", text=saved["turns"][0]["text"],
            options="{}", revision_id="revision_report", created_at=now))
        session.commit()
    monkeypatch.setattr(service, "get_session", lambda session_id: saved if session_id == saved["id"] else None)
    calls = []

    async def execute(venture_id, action, **kwargs):
        calls.append((action, kwargs.get("payload")))
        return {"kind": "text", "body_md": "Your idea is confirmed.", "actions": [], "citations": []}

    monkeypatch.setattr(router, "execute_action", execute)
    response = client.post(f"/ventures/{venture_id}/chat", json={"message": "yes correct"})
    assert response.status_code == 200, response.text
    assert calls == [("idea.confirm", {"session_id": "idea_report", "revision_id": "revision_report"})]


@pytest.mark.parametrize("domain", ["dental clinics", "public libraries"])
def test_chat_first_can_answer_a_page_created_question_but_classic_is_unchanged(venture, monkeypatch, domain):
    from app.chat import idea_bridge
    from app.credits import policy
    from app.db.models import Workspace
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.state import db
    from core.clock import utcnow

    _client, vid, workspace_id = venture
    with db()() as session:
        now = utcnow() + timedelta(seconds=1)
        session.add(IdeaSession(id="idea_page", workspace_id=workspace_id, venture_id=vid, state="clarifying",
                               case="know", created_at=now, updated_at=now))
        session.flush()
        session.add(ClarifierTurn(id="question_page", session_id="idea_page", workspace_id=workspace_id, venture_id=vid,
                                 turn=1, role="agent", kind="question", text="Who needs this?",
                                 options=json.dumps({"field": "customer", "options": [domain, "Not sure"]}), created_at=now))
        session.commit()
    for layout in ("chat", "home", "classic"):
        monkeypatch.setattr(policy, "get_policy", lambda *_args: {"founder_ui": layout})
        with db()() as session:
            workspace = session.get(Workspace, workspace_id)
            settings = json.loads(workspace.settings_json or "{}")
            settings.update(chat_entry_defer_clarification=layout != "classic",
                            intake_venture_id=vid, entry_goal="startup")
            workspace.settings_json = json.dumps(settings)
            session.commit()
        assert idea_bridge.pending_question(vid, domain) == (None if layout == "classic" else ("idea_page", "question_page"))
        assert idea_bridge.pending_question(vid, "What is happening?") is None
        assert idea_bridge.pending_question("ven_other", domain, force=True) is None
