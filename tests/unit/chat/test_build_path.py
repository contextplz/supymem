import pytest

from tests.unit.chat.test_round3_matrix import venture as base_venture


@pytest.fixture(params=[
    pytest.param(("Software for factory maintenance teams reconciling equipment invoices.",
                  "factory maintenance teams"), id="b2b"),
    pytest.param(("An app for amateur birdwatchers predicting migration activity from weather forecasts.",
                  "amateur birdwatchers"), id="consumer"),
])
def first_time_founder(tmp_path, monkeypatch, request):
    from tests.company_os_harness import company_os_client, configure_local_test_model, fake_signed_in_codex, load_company_os_app

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/founder.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    main = load_company_os_app()
    configure_local_test_model()
    from app.coding_agents_consent import set_consent
    from app.core.local_owner import ensure_owner

    # 7f2df3d9c pins the actual executable when saving consent.
    fake_signed_in_codex(tmp_path, monkeypatch)
    set_consent(ensure_owner().id, "codex", True)
    idea, audience = request.param
    with company_os_client(main.create_app()) as client:
        response = client.post("/ventures", json={"raw_idea": idea})
        assert response.status_code == 201, response.text
        yield client, response.json()["id"], idea, audience


def founder_card_click(client, venture_id, card, action, key):
    confirmation = action.get("confirmation")
    response = client.post(f"/ventures/{venture_id}/chat/confirm", json={
        "action": action["action"],
        "payload": confirmation["resulting_state"] if confirmation else action.get("payload", {}),
        "idempotency_key": key,
        **({"message_id": card["id"]} if confirmation else {}),
    })
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def first_confirmed_idea(first_time_founder):
    client, venture_id, _, _ = first_time_founder
    review = send(client, venture_id, "Skip the questions")
    start = next(action for action in review["actions"] if action["action"] == "idea.confirm")
    saved = founder_card_click(client, venture_id, review, start, "start-venture")
    assert "Your idea is saved" in saved["body_md"]
    return client, venture_id


def founder_recommendation(client, venture_id):
    reply = send(client, venture_id, "What should I do next?")
    page = next(action for action in reply["actions"] if action["action"] == "pagebuilder.build")
    return reply, page


def founder_page_jobs(venture_id):
    from app.db.models import Job
    from app.state import db
    from sqlalchemy import select

    with db()() as session:
        return session.scalars(select(Job).where(Job.venture_id == venture_id, Job.kind == "pagebuilder.build")).all()


def assert_founder_page_receipt(venture_id, receipt):
    import json

    job, = founder_page_jobs(venture_id)
    assert json.loads(job.payload_json)["coding_provider"] == "codex"
    assert receipt["kind"] != "confirmation" and not any(action.get("confirmation") for action in receipt["actions"])
    assert any((action.get("action") == "status" and (action.get("payload") or {}).get("job_id") == job.id)
               or ("watch" in action["label"].lower() and job.id in (action.get("href") or ""))
               for action in receipt["actions"]), receipt


def test_first_founder_recommendation_runs_immediately(first_confirmed_idea):
    client, venture_id = first_confirmed_idea
    recommendation, action = founder_recommendation(client, venture_id)
    assert not founder_page_jobs(venture_id)
    receipt = founder_card_click(client, venture_id, recommendation, action, "first-page-click")
    assert_founder_page_receipt(venture_id, receipt)


def test_first_founder_recommendation_replays_the_build(first_confirmed_idea):
    client, venture_id = first_confirmed_idea
    recommendation, action = founder_recommendation(client, venture_id)
    first = founder_card_click(client, venture_id, recommendation, action, "first-page-click")
    repeated = founder_card_click(client, venture_id, recommendation, action, "second-page-click")
    assert repeated["id"] == first["id"] or "already" in repeated["body_md"].lower(), repeated
    assert_founder_page_receipt(venture_id, repeated)


@pytest.mark.parametrize("field,question", [("offer", "What would you like to build?"),
    ("customer", "Who would use this first?")])
def test_first_founder_idea_does_not_repeat_known_questions(first_time_founder, monkeypatch, field, question):
    import json

    from tests.idea_fixtures import completion, install_provider
    from tests.integration.test_entry_chat_api import _preview
    from tests.security.test_durable_journey_boundary import run_queued

    client, venture_id, idea, audience = first_time_founder
    preview = _preview(client, idea, request_id="first-idea")
    assert preview.get("onboarding_field") not in {"offer", "customer"}, preview
    assert preview["proposal"]["route"] == "startup", preview

    def repeat_known_question(_messages, _kwargs):
        result = completion(proposal=False, understanding={"offer": idea, "customer": audience}, text=question,
                            options=["Something else", "I am not sure"])
        content = json.loads(result.choices[0].message.content)
        content["question"]["field"] = field
        result.choices[0].message.content = json.dumps(content)
        return result

    calls = install_provider(monkeypatch, repeat_known_question)
    base = f"/ventures/{venture_id}/idea/sessions"
    response = client.post(base)
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    run_queued("idea.clarify_turn")
    saved = client.get(f"{base}/{session_id}")
    assert saved.status_code == 200, saved.text
    agent_turn = [turn for turn in saved.json()["turns"] if turn["role"] == "agent"][-1]
    assert calls and agent_turn["kind"] in {"proposal", "question"}, saved.json()
    assert (agent_turn.get("options") or {}).get("field") not in {"offer", "customer"}
    assert question not in agent_turn["text"]


def test_first_founder_repeated_recommendation_is_saved_once(first_confirmed_idea):
    client, venture_id = first_confirmed_idea
    first, _ = founder_recommendation(client, venture_id)
    repeated, _ = founder_recommendation(client, venture_id)
    assert repeated["id"] == first["id"]
    history = client.get(f"/ventures/{venture_id}/chat")
    assert history.status_code == 200, history.text
    recommendations = [message["id"] for message in history.json()["items"] if message["kind"] == "next_moves"]
    assert recommendations == [first["id"]]


def test_first_founder_can_type_a_codex_page_request(first_confirmed_idea):
    client, venture_id = first_confirmed_idea
    receipt = send(client, venture_id, "Build my landing page with Codex")
    assert_founder_page_receipt(venture_id, receipt)


def test_first_founder_without_a_tool_gets_the_connect_step(first_confirmed_idea, monkeypatch):
    from desktop_runtime import agent_status

    client, venture_id = first_confirmed_idea
    monkeypatch.setattr(agent_status, "statuses", lambda: [])
    receipt = send(client, venture_id, "Build my landing page with Codex")
    assert "Settings > Keys and connections > Coding tools" in receipt["body_md"]
    assert any("connect" in action["label"].lower() and action.get("href") == "/settings/local#coding-tools"
               for action in receipt["actions"])
    assert not founder_page_jobs(venture_id)


@pytest.mark.parametrize("idea", ["Maintenance scheduling for factories", "Birdwatching journals for families"])
@pytest.mark.parametrize("steps", [[], [("research.prepare", "Research your idea")]])
def test_local_build_recommendation_uses_saved_readiness_without_starting(monkeypatch, idea, steps):
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.chat import build_path
    from app.builder import ctl
    from app.ventures import build_prerequisites

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(build_prerequisites, "remaining", lambda venture_id, brief: steps)
    monkeypatch.setattr(ctl, "local_tool", lambda *args, **kwargs: "codex", raising=False)
    monkeypatch.setattr(ctl, "request_local_build", lambda *args, **kwargs: pytest.fail("Advice cannot start work"), raising=False)
    reply = build_path.local_build_reply("venture", {"idea": {"confirmed": True, "idea_summary": idea}})
    action = reply["actions"][0]
    assert action["action"] == (steps[0][0] if steps else "build.tool.start")
    assert "confirmation" not in action
    assert "credit" not in reply["body_md"].lower()
    assert "No" in reply["body_md"] or "Nothing" in reply["body_md"]
    if not steps:
        assert "codex" in action["payload"]["text"].lower()


@pytest.mark.parametrize("idea", ["Maintenance scheduling for factories", "Birdwatching journals for families"])
def test_local_build_chain_uses_the_guarded_recommendation_not_a_hosted_quote(monkeypatch, idea):
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.chat import build_chain, build_path
    from app.builder import execution

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    calls = []
    monkeypatch.setattr(execution, "estimate", lambda *args, **kwargs: pytest.fail("Your tool subscription does not use hosted quotes"))
    monkeypatch.setattr(build_path, "local_build_reply", lambda *args, **kwargs: calls.append((args, kwargs)) or {"kind": "text"}, raising=False)
    result = build_chain.reply("venture", "", "plan", {}, [{"id": "app"}], idea, "owner", task_id="exact-task")
    assert result == {"kind": "text"} and calls == [(("venture",), {"plan_task_id": "exact-task"})]


@pytest.fixture()
def venture(base_venture):
    from app.core.local_mode import is_local

    if is_local():
        response = base_venture[0].post("/local/providers/sample", json={})
        assert response.status_code == 200, response.text
        from app.db.models import Venture
        from app.state import db

        with db()() as session:
            idea = session.get(Venture, base_venture[1]).raw_idea
        created = base_venture[0].post("/ventures", json={"raw_idea": idea, "start": False})
        assert created.status_code == 201, created.text
        return base_venture[0], created.json()["id"], created.json()["workspace_id"]
    return base_venture


@pytest.mark.parametrize("domain", ["Supplier invoice reconciliation", "Family hiking routes"])
def test_research_recovery_keeps_read_only_review_during_intake(domain):
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.chat.next_moves import intake_actions

    card = {"intake_pending": True, "first_question_pending": True, "actions": []}
    review = {"label": "Review your saved answers", "action": "startup.review", "payload": {"domain": domain}, "style": "primary"}
    work = {"label": "Start research", "action": "research.start", "payload": {"domain": domain}}
    assert intake_actions(card, [review, work]) == [review]


@pytest.mark.parametrize("audience", ["Equipment calibration managers", "Weekend rowing groups"])
def test_research_confirmation_failure_is_not_a_started_action(venture, monkeypatch, audience):
    from app.chat import execute
    from app.chat.store import append_message, list_messages
    from app.research import service
    from app.ventures import build_prerequisites, journey_overrides

    client, venture_id, _ = venture
    monkeypatch.setattr(build_prerequisites, "answers_pending", lambda *args: False)
    monkeypatch.setattr(journey_overrides, "read", lambda *args: {"skipped": []})
    monkeypatch.setattr(execute, "local_capability_reply", lambda *args: None)
    monkeypatch.setattr(execute, "_confirm_idea_for_research", lambda *args:
        {"kind": "text", "body_md": f"Review your saved answers for {audience}, then retry.",
         "actions": [{"action": "startup.review", "label": "Review your answers"}], "citations": []})
    monkeypatch.setattr(service, "start", lambda *args, **kwargs: pytest.fail("Research must not start after a refusal"))
    assert execute._research_start(venture_id)["action_started"] is False
    stale = append_message(venture_id, role="assistant", kind="confirmation", body_md="Your saved research choice.",
        actions=[{"label": "Start research", "confirmation": {"action": "research.start", "resulting_state": {}}}])
    response = client.post(f"/ventures/{venture_id}/chat/confirm",
        json={"action": "research.start", "message_id": stale["id"], "idempotency_key": "refused-research"})
    assert response.status_code == 200, response.text
    assert response.json()["confirmed_action"]["state"] == "not_started"
    saved = next(message for message in list_messages(venture_id) if message["id"] == stale["id"])
    assert saved["actions"][0]["confirmation"]["state"] == "not_started"


@pytest.mark.parametrize("audience", ["Equipment calibration managers", "Weekend rowing groups"])
@pytest.mark.parametrize("outcome,expected", [("refused", False), ("unconfirmed", None), ("unconfirmed_runtime", None), ("queued", True)])
def test_research_dispatch_failure_preserves_queue_uncertainty(venture, monkeypatch, audience, outcome, expected):
    from sqlalchemy.exc import SQLAlchemyError
    from app.autopilot import receipts
    from app.chat import execute
    from app.chat.store import append_message, list_messages
    from app.research import service
    from app.research.errors import ResearchPreflightBlocked
    from app.ventures import build_prerequisites, journey_overrides, state_brief

    client, venture_id, _ = venture
    monkeypatch.setattr(build_prerequisites, "answers_pending", lambda *args: False)
    monkeypatch.setattr(journey_overrides, "read", lambda *args: {"skipped": ["validation"]})
    monkeypatch.setattr(execute, "local_capability_reply", lambda *args: None)
    calls, queued = [], []

    def start(*args, **kwargs):
        calls.append(True)
        if outcome == "unconfirmed":
            raise SQLAlchemyError("Commit acknowledgment is unavailable")
        if outcome == "unconfirmed_runtime":
            raise RuntimeError("Dispatch outcome is unavailable")
        if outcome == "refused":
            raise ResearchPreflightBlocked("research_provider_unavailable")
        return {"job_id": "saved-research"}

    def unavailable(*args):
        raise RuntimeError("Research readback is unavailable")

    monkeypatch.setattr(service, "start", start)
    monkeypatch.setattr(receipts, "queued_job", queued.append)
    monkeypatch.setattr(state_brief, "invalidate_venture_state_brief", unavailable)
    reply = execute._research_start(venture_id)
    assert reply["action_started"] is expected
    assert queued == (["saved-research"] if outcome == "queued" else [])
    assert any(action.get("action") == "status" for action in reply["actions"])
    stale = append_message(venture_id, role="assistant", kind="confirmation", body_md=f"Your saved research choice for {audience}.",
        actions=[{"label": "Start research", "confirmation": {"action": "research.start", "resulting_state": {}}}])
    response = client.post(f"/ventures/{venture_id}/chat/confirm",
        json={"action": "research.start", "message_id": stale["id"], "idempotency_key": "failed-dispatch"})
    assert response.status_code == 200, response.text
    state = "starting" if expected is None else "started" if expected else "not_started"
    assert response.json()["confirmed_action"]["state"] == state
    saved = next(message for message in list_messages(venture_id) if message["id"] == stale["id"])
    assert saved["actions"][0]["confirmation"]["state"] == state
    if expected is None:
        replay = client.post(f"/ventures/{venture_id}/chat/confirm",
            json={"action": "research.start", "message_id": stale["id"], "idempotency_key": "do-not-replay"})
        assert replay.status_code == 200, replay.text
        assert replay.json()["confirmed_action"]["state"] == "starting"
        assert calls == [True, True]


@pytest.mark.parametrize("command", [
    "Find prospective warehouse customers for my saved idea",
    "Locate interested gardening users for our current project",
])
def test_search_for_saved_scope_uses_customer_search_without_losing_prohibitions(command):
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.chat.intent import classify_intent, requested_customer_search

    assert requested_customer_search(command)
    assert classify_intent(command) == "gtm_review"
    assert not requested_customer_search("Do not " + command)
    assert not requested_customer_search("Locate my stored contacts for our current project")


def test_research_next_step_has_no_usage_cost_clause(monkeypatch):
    from types import SimpleNamespace
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.chat import build_path
    from app.estimate import meters
    from app.research.prior_art import runner
    for domain in ["Inventory reconciliation for distributors", "Trip planning for families"]:
        for enabled in [True, False]:
            monkeypatch.setattr(runner, "enabled", lambda: enabled)
            monkeypatch.setattr(meters, "research_estimate", lambda *args, **kwargs: [SimpleNamespace(micro=2_000_000)])
            text = build_path.reply("ven_test", [("research.prepare", f"Research demand for {domain}")])["body_md"]
            line = text.split("\n\n")[1]
            assert line.count("(") == line.count(")") == 0
            assert "credit" not in text.lower()
            assert "Nothing was started or published." in text


def seed(vid, wid, offer="Track bakery orders for customers", audience="Bakery customers", problem="Customers cannot see their order status"):
    import json
    from app.db.models import Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.state import db
    from app.ventures import startup_review
    from core.clock import utcnow

    now = utcnow()
    with db()() as session:
        row = session.get(Venture, vid)
        row.name = "QueueBuddy"
        row.raw_idea = offer
        startup_review._document(session, vid, "internal_new_user_flow", {"enabled": True})
        session.add(IdeaSession(id="idea_build_path", workspace_id=wid, venture_id=vid, case="know", state="confirmed",
            current_revision_id="revision_build_path", confirmed_revision_id="revision_build_path", created_at=now, updated_at=now))
        session.flush()
        session.add(IdeaRevision(id="revision_build_path", session_id="idea_build_path", workspace_id=wid, venture_id=vid,
            rev=1, source="founder", summary=row.raw_idea, structured=json.dumps({"offer": row.raw_idea,
                "customer": audience, "problem": problem, "progress": "No app yet"}), created_at=now))
        session.commit()


def primary(reply):
    actions = [action for action in reply["actions"] if action.get("style") == "primary"]
    assert len(actions) == 1, reply
    assert not actions[0].get("href"), reply
    assert "dossier" not in reply["body_md"].lower()
    assert "—" not in reply["body_md"]
    return actions[0]


def send(client, vid, message):
    response = client.post(f"/ventures/{vid}/chat", json={"message": message})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("offer,audience,problem", [
    ("Reconcile supplier invoices", "Procurement teams", "Invoice totals disagree"),
    ("Organize weekend hikes", "Local walking groups", "Shared routes are hard to choose"),
])
@pytest.mark.parametrize("requested", [True, False])
def test_skip_questions_keeps_requested_research_and_no_contact_constraint(venture, monkeypatch, offer, audience, problem, requested):
    from app.chat import build_path
    from app.db.supymem_models import IdeaSession
    from app.state import db

    client, vid, wid = venture
    seed(vid, wid, offer, audience, problem)
    with db()() as session:
        idea = session.get(IdeaSession, "idea_build_path")
        idea.state, idea.confirmed_revision_id = "clarifying", None
        session.commit()
    calls = []
    original = build_path.research_prerequisites

    def prerequisites(venture_id):
        calls.append(venture_id)
        return original(venture_id)

    monkeypatch.setattr(build_path, "research_prerequisites", prerequisites)
    follow_up = f"Research demand for {offer}. Do not contact anyone." if requested else "Do not research or contact anyone."
    reply = send(client, vid, "Skip questions. " + follow_up)
    assert bool(calls) == requested, reply
    if requested:
        assert "research" in reply["body_md"].lower(), reply
        assert reply["actions"], reply["body_md"]
        assert primary(reply)["action"] in {"idea.review", "startup.review", "research.start", "status"}
    assert all(action.get("action") not in {"gtm.send", "outreach.send"} for action in reply["actions"])
    with db()() as session:
        idea = session.get(IdeaSession, "idea_build_path")
        assert idea.confirmed_revision_id is None


@pytest.mark.parametrize("offer,audience,problem", [
    ("Inventory alerts", "Warehouse operators", "Stockouts go unnoticed"),
    ("Plan neighborhood walks", "Local families", "Routes are difficult to coordinate"),
])
def test_production_build_research_next_step_scenario(venture, offer, audience, problem):
    client, vid, wid = venture
    seed(vid, wid, offer, audience, problem)
    reply = build_guidance(client, vid)
    assert primary(reply)["action"] == "startup.review"
    assert "credit" not in reply["body_md"] and "build plan" in reply["body_md"]
    response = client.post(f"/ventures/{vid}/chat/confirm", json={"action": "research"})
    assert response.status_code == 200, response.text
    assert primary(response.json())["action"] == "startup.review"
    reply = send(client, vid, "ok start the research")
    card = primary(reply)
    assert card["action"] == "startup.review"
    assert not card.get("confirmation")
    response = client.post(f"/ventures/{vid}/chat/confirm", json={"action": "research.start"})
    assert response.status_code == 200, response.text
    assert primary(response.json())["action"] == "startup.review"
    next_step = send(client, vid, "What should I do next?")
    assert_find_customers_next_step(next_step)
    response = client.post(f"/ventures/{vid}/chat/confirm", json={"action": "startup.review"})
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "startup_review"
    from app.db.models import Job
    from app.state import db
    with db()() as session:
        assert not session.query(Job).filter_by(venture_id=vid, kind="research.deep").count()


def build_guidance(client, venture_id):
    # d7bfd069f distinguishes a read-only prerequisite review from a request to build.
    return founder_card_click(client, venture_id, {}, {"action": "build.prepare"}, "build-guidance")


def assert_find_customers_next_step(reply):
    from app.core.local_mode import is_local

    assert reply["kind"] == "next_moves"
    if is_local():
        assert [action["action"] for action in reply["actions"]] == ["gtm.find"]
    else:
        assert len(reply["actions"]) >= 2


def finish_answers(vid):
    import asyncio
    from app.ventures import startup_review

    for _ in range(20):
        review = startup_review.get(vid)
        if not review["question"]:
            break
        question = review["question"]
        startup_review.save_answer(vid, review["revision"], question["field"], question.get("prefill") or "No customers or revenue yet")
    assert not review["question"]
    return asyncio.run(startup_review.save_priorities(vid, review["revision"], "Research competitors", None))


@pytest.mark.parametrize("offer,audience,problem", [
    ("Track equipment certifications", "Factory safety managers", "Renewals get missed"),
    ("Share family recipes", "Home cooks", "Recipes get lost between generations"),
])
def test_build_card_review_click_and_completed_review_continue_without_paid_work(venture, monkeypatch, offer, audience, problem):
    from app.chat import build_path
    from app.db.models import Job
    from app.research import service
    from app.state import db
    from app.ventures import startup_review

    client, vid, wid = venture
    seed(vid, wid, offer, audience, problem)
    monkeypatch.setattr(service, "start", lambda *args, **kwargs: pytest.fail("started paid research"))
    card = build_guidance(client, vid)
    action = primary(card)
    assert action["action"] == "startup.review" and not action.get("disabled_reason")
    response = client.post(f"/ventures/{vid}/chat/confirm", json={"action": action["action"]})
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "startup_review" and not response.json()["actions"]
    assert startup_review.get(vid)["complete"] is False
    proactive = send(client, vid, "What should I do next?")
    assert_find_customers_next_step(proactive)
    assert finish_answers(vid)["complete"]
    for enabled in (True, False):
        if not enabled:
            with db()() as session:
                startup_review._document(session, vid, "internal_new_user_flow", {"enabled": False})
                session.commit()
        response = client.post(f"/ventures/{vid}/chat/confirm", json={"action": action["action"]})
        assert response.status_code == 200, response.text
        continued = response.json()
        assert "no saved answers or priorities left to review" in continued["body_md"]
        assert primary(continued)["action"] == "research.prepare"
        assert "credit" not in continued["body_md"]
    with db()() as session:
        assert not session.query(Job).filter_by(venture_id=vid, kind="research.deep").count()


@pytest.mark.parametrize("offer,audience,problem", [
    ("Reconcile supplier invoices", "Procurement teams", "Invoice totals disagree with purchase orders"),
    ("Organize weekend hikes", "Local walking groups", "Finding a suitable shared route takes too long"),
])
def test_completed_or_disabled_review_disagreement_advances_without_recursion(venture, monkeypatch, offer, audience, problem):
    from app.chat import build_path, execute
    from app.db.models import Venture
    from app.state import db
    from app.ventures import build_prerequisites, startup_review, state_brief

    client, vid, wid = venture
    seed(vid, wid, offer, audience, problem)
    assert finish_answers(vid)["complete"]
    with db()() as session:
        completed = startup_review.initialize(session, session.get(Venture, vid))
    monkeypatch.setattr(startup_review, "review", lambda *args: {**completed, "complete": False})
    monkeypatch.setattr(startup_review, "initialize", lambda *args: completed)
    assert startup_review.get(vid)["complete"]
    with db()() as session:
        assert not startup_review.review(session, session.get(Venture, vid))["complete"]
    brief = {"idea": {"confirmed": True}, "research": {}, "plan": {}}
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *args, **kwargs: brief)
    for enabled in (True, False):
        with db()() as session:
            startup_review._document(session, vid, "internal_new_user_flow", {"enabled": enabled})
            session.commit()
            assert not build_prerequisites.answers_pending(session, session.get(Venture, vid))
        assert [step[0] for step in build_prerequisites.remaining(vid)] == ["research.prepare", "plan.generate"]
        with monkeypatch.context() as stale:
            stale.setattr(build_prerequisites, "remaining", lambda *args: [
                ("startup.review", "Confirm your saved answers and priorities"),
                ("research.prepare", "Research competitors and demand"),
            ])
            if not enabled:
                stale.setattr(startup_review, "enabled", lambda *_args: False)
                stale.setattr(startup_review, "get", lambda *args: pytest.fail("opened disabled review"))
            response = client.post(f"/ventures/{vid}/chat/confirm", json={"action": "startup.review"})
            assert response.status_code == 200, response.text
            continued = response.json()
            assert "no saved answers or priorities left to review" in continued["body_md"]
            assert primary(continued)["action"] == "research.prepare"
            assert "credit" not in continued["body_md"].lower()
            moves = execute._whats_next(vid)
            assert moves["kind"] == "next_moves"
            assert any(action["action"] == "research.prepare" for action in moves["actions"])
            assert "credit" not in moves["body_md"].lower()


@pytest.mark.parametrize("offer,audience,problem", [
    ("Reconcile warehouse inventory", "Distribution operations teams", "Counts disagree across locations"),
    ("Plan accessible family trips", "Parents travelling with children", "Finding suitable activities takes too long"),
])
def test_research_offers_list_every_prerequisite_before_quoting_or_starting(venture, monkeypatch, offer, audience, problem):
    from app.chat import build_path, execute, failed_jobs, journey
    from app.chat.store import append_message, list_messages
    from app.db.models import Job
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.research import service
    from app.state import db
    from core.clock import utcnow

    client, vid, wid = venture
    seed(vid, wid, offer, audience, problem)
    with db()() as session:
        idea = session.get(IdeaSession, "idea_build_path")
        idea.state, idea.confirmed_revision_id = "reviewing", None
        session.add(ClarifierTurn(id="open_question", session_id=idea.id, workspace_id=wid, venture_id=vid,
            revision_id=idea.current_revision_id, turn=1, role="agent", kind="question",
            text=f"Who first needs {offer.lower()}?", options="{}", created_at=utcnow()))
        session.commit()
        before = session.query(Job).filter_by(venture_id=vid, kind="research.deep").count()
    monkeypatch.setattr(service, "start", lambda *args, **kwargs: pytest.fail("started before prerequisites"))
    monkeypatch.setattr(failed_jobs, "latest_failed", lambda *args: {"kind": "research.deep", "code": "research_provider_failed"})

    replies = [build_path.research_card(vid), execute._research_start(vid),
        failed_jobs.prepare(vid, "research.deep"),
        journey.guided_answer(vid, {"next_step": {"chat_action": "research.start"}}, {"state": "idle"}),
        append_message(vid, role="assistant", body_md=f"Want me to research {offer.lower()}?",
            actions=[{"label": "Research", "action": "research", "style": "primary"}])]
    for action in ("research", "research.prepare"):
        response = client.post(f"/ventures/{vid}/chat/confirm", json={"action": action})
        assert response.status_code == 200, response.text
        replies.append(response.json())
    replies.append(send(client, vid, "ok now start the research"))
    for reply in replies:
        assert primary(reply)["action"] == "startup.review"
        assert reply["kind"] == "needs_you"
        assert "3 quick steps" in reply["body_md"]
        assert "saved answers and priorities" in reply["body_md"]
        assert "open question" in reply["body_md"] and "confirm your idea summary" in reply["body_md"]
        assert not any(action.get("confirmation") for action in reply["actions"])

    stale = append_message(vid, role="assistant", kind="confirmation", body_md="Your saved research quote.",
        actions=[{"label": "Do it", "confirmation": {"action": "research.start", "resulting_state": {}}}])
    for nonce in ("first-refusal", "retry-refusal"):
        response = client.post(f"/ventures/{vid}/chat/confirm", json={"action": "research.start", "message_id": stale["id"],
            "idempotency_key": nonce})
        assert response.status_code == 200, response.text
        assert response.json()["confirmed_action"]["state"] == "not_started"
        saved = next(message for message in list_messages(vid) if message["id"] == stale["id"])
        assert saved["actions"][0]["confirmation"]["state"] == "not_started"
    assert finish_answers(vid)["complete"]
    blocked = build_path.research_card(vid)
    assert primary(blocked)["action"] == "idea.review"
    assert "2 quick steps" in blocked["body_md"] and "open question" in blocked["body_md"]
    with db()() as session:
        session.add(ClarifierTurn(id="answered_question", session_id="idea_build_path", workspace_id=wid, venture_id=vid,
            revision_id="revision_build_path", turn=2, role="founder", kind="answer", text=audience, created_at=utcnow()))
        session.commit()
    blocked = build_path.research_card(vid)
    assert "One quick step" in blocked["body_md"] and "open question" not in blocked["body_md"]
    with db()() as session:
        idea = session.get(IdeaSession, "idea_build_path")
        idea.state, idea.confirmed_revision_id = "confirmed", idea.current_revision_id
        session.commit()
        assert session.query(Job).filter_by(venture_id=vid, kind="research.deep").count() == before
    ready = build_path.research_card(vid)
    card = primary(ready)["confirmation"]
    assert card["action"] == "research.start" and "credit" not in card["interpretation"]
    assert "Required idea and answers reviews" not in " ".join(card["consequences"])
    def fail_start(*args, **kwargs):
        raise RuntimeError("research_provider_unavailable")
    monkeypatch.setattr(service, "start", fail_start)
    fresh = append_message(vid, role="assistant", body_md=ready["body_md"], kind=ready["kind"], actions=ready["actions"])
    response = client.post(f"/ventures/{vid}/chat/confirm", json={"action": "research.start", "message_id": fresh["id"],
        "idempotency_key": "provider-refusal"})
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "text" and response.json()["confirmed_action"]["state"] == "starting", response.json()
    monkeypatch.setattr(service, "start", lambda *args, **kwargs: {"job_id": "research_admitted"})
    result = execute._research_start(vid)
    assert result["action_started"] and result["run_id"] == "research_admitted"


@pytest.mark.parametrize("offer,audience,problem", [
    ("Inventory alerts", "Warehouse operators", "Stockouts go unnoticed"),
    ("Plan neighborhood walks", "Local families", "Routes are difficult to coordinate"),
])
def test_shared_prerequisites_follow_answers_and_current_research(venture, offer, audience, problem):
    import json
    from app.db.models import Venture
    from app.db.supymem_models import ResearchDossier
    from app.state import db
    from app.ventures import build_prerequisites, startup_review
    from core.clock import utcnow

    client, vid, wid = venture
    seed(vid, wid, offer, audience, problem)
    assert build_prerequisites.remaining(vid)[0][0] == "startup.review"
    with db()() as session:
        with pytest.raises(ValueError):
            startup_review.require_complete(session, session.get(Venture, vid))
    assert finish_answers(vid)["complete"]
    assert build_prerequisites.remaining(vid)[0][0] == "research.prepare"
    with db()() as session:
        startup_review.require_complete(session, session.get(Venture, vid))
        session.add(ResearchDossier(id="research_build_path", workspace_id=wid, venture_id=vid,
            revision_id="revision_build_path", kind="deep", state="complete", summary=json.dumps({"review_kind": "model_assessment"}),
            created_at=utcnow(), completed_at=utcnow()))
        session.commit()
    assert build_prerequisites.remaining(vid) == [("plan.generate", "Make a build plan")]
    assert primary(build_guidance(client, vid))["action"] == "plan.generate"
    assert send(client, vid, "What should I do next?")["kind"] == "next_moves"


@pytest.mark.parametrize("confirmed,research_ready,plan,detail,expected", [
    (False, False, {}, {}, ["idea.review", "research.prepare", "plan.generate"]),
    (True, False, {}, {}, ["research.prepare", "plan.generate"]),
    (True, True, {}, {}, ["plan.generate"]),
    (True, True, {"present": True, "id": "plan_current", "state": "founder_review"},
     {"method": "metered_foundry_plan"}, ["plan.approve"]),
    (True, True, {"present": True, "id": "plan_current", "state": "approved"},
     {"method": "metered_foundry_plan", "stale_dependencies": True}, ["plan.generate"]),
    (True, True, {"present": True, "id": "plan_current", "state": "approved"},
     {"method": "metered_foundry_plan"}, []),
])
def test_shared_order_and_plan_provenance(venture, monkeypatch, confirmed, research_ready, plan, detail, expected):
    from app.planning import compiler
    from app.ventures import build_prerequisites

    monkeypatch.setattr(build_prerequisites, "answers_pending", lambda *args: False)
    monkeypatch.setattr(compiler, "get_plan", lambda *args: detail)
    steps = build_prerequisites.remaining(venture[1], {"idea": {"confirmed": confirmed},
        "research": {"plan_ready": research_ready}, "plan": plan})
    assert [action for action, _ in steps] == expected


@pytest.mark.parametrize("idea", ["Inventory alerts for warehouse operators", "Walking groups for neighborhood families"])
def test_ready_build_without_legacy_tasks_does_not_offer_another_plan(venture, monkeypatch, idea):
    from app.chat import execute, router
    from app.planning import store
    from app.ventures import build_prerequisites, state_brief
    from app.will import slate
    from app.chat import next_moves
    from tests.unit.chat.test_next_moves import _saved_state

    monkeypatch.setattr(next_moves, "_snapshot", lambda _: _saved_state(idea, later=True))
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *args, **kwargs: {"plan": {"present": True}})
    monkeypatch.setattr(build_prerequisites, "remaining", lambda *args: [])
    monkeypatch.setattr(store, "list_open_tasks", lambda *args: [])
    monkeypatch.setattr(slate, "latest_slate", lambda *args: None)
    monkeypatch.setattr(router, "_product_build_reply", lambda *args: {"actions": [{"action": "build.prepare"}]})
    reply = execute._whats_next(venture[1])
    assert reply["kind"] == "next_moves"
    assert not any(action["action"] == "plan.generate" for action in reply["actions"])


@pytest.mark.parametrize("idea", ["Inventory alerts for warehouse operators", "Walking groups for neighborhood families"])
def test_managed_tasks_do_not_bypass_missing_research(venture, monkeypatch, idea):
    from app.builder import execution
    from app.chat import execute
    from app.planning import store
    from app.ventures import state_brief

    seed(venture[1], venture[2], offer=idea, audience=idea, problem=idea)
    assert finish_answers(venture[1])["complete"]
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *args, **kwargs: {
        "idea": {"confirmed": True, "idea_summary": idea}, "research": {"plan_ready": False}})
    monkeypatch.setattr(store, "list_open_tasks", lambda *args: [{"title": "Build the app", "source": "approved_plan:old"}])
    monkeypatch.setattr(execution, "estimate", lambda *args, **kwargs: pytest.fail("quoted before research"))
    reply = execute._whats_next(venture[1])
    assert reply["kind"] == "next_moves"
    assert any(action["action"] == "research.prepare" for action in reply["actions"])
    assert not any(action["action"] == "build.start" for action in reply["actions"])


@pytest.mark.parametrize("personal", [False, True])
def test_paused_prerequisites_and_next_step_require_resume_first(venture, monkeypatch, personal):
    from app.chat import execute
    from app.planning import store
    from app.ventures import build_prerequisites, state_brief

    brief = {"idea": {"paused": True, "confirmed": False,
                      "raw_idea": "Neighborhood walking groups" if personal else "Inventory alerts for warehouses",
                      "project_intent": "personal" if personal else "startup"}}
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *args, **kwargs: brief)
    monkeypatch.setattr(build_prerequisites, "answers_pending", lambda *args: pytest.fail("reviewed answers before resume"))
    monkeypatch.setattr(store, "list_open_tasks", lambda *args: pytest.fail("offered work before resume"))
    assert build_prerequisites.remaining(venture[1]) == [("resume", "Resume")]
    reply = execute._whats_next(venture[1])
    assert primary(reply)["action"] == "resume"
    assert "paused" in reply["body_md"] and "Nothing new will start" in reply["body_md"]
