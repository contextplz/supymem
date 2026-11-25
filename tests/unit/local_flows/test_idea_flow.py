"""Your local idea journey uses HTTP, durable jobs and the offline/replay model."""

import asyncio
import io
import json
import re
import socket
import zipfile
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from tests.unit.test_local_mode import _owner_client, local_owner_app as local_owner_app


@pytest.fixture(params=[
    pytest.param(SimpleNamespace(
        idea="A tool that helps independent dental clinics reconcile insurance claims",
        audience="Practice managers at independent dental clinics",
        problem="Staff spend hours matching insurance payments to claims",
        domain="claims.test",
    ), id="b2b"),
    pytest.param(SimpleNamespace(
        idea="A kit that helps renters repair bicycle punctures at home",
        audience="City renters who commute by bicycle",
        problem="Commuters miss trips when a puncture needs a shop visit",
        domain="bicycles.test",
    ), id="consumer"),
])
def venture_case(request):
    return request.param


class OwnerFlow:
    def __init__(self, client, case, network):
        self.client = client
        self.case = case
        self.network = network
        self.responses = []
        self.secret = "synthetic-secret-" + uuid4().hex
        self.venture_id = None
        self.session_id = None

    def request(self, method, path, body=None):
        response = self.client.request(method, path, json=body)
        assert self.secret not in response.text, "Your saved key must never be returned."
        self.responses.append(response)
        return response

    def ok(self, method, path, body=None):
        response = self.request(method, path, body)
        assert 200 <= response.status_code < 300, (path, response.status_code, response.text)
        return response.json()

    def path(self, suffix):
        return f"/ventures/{self.venture_id}{suffix}"

    def session_path(self, suffix=""):
        return self.path(f"/idea/sessions/{self.session_id}{suffix}")

    def drain(self):
        from sqlalchemy import or_, select
        from app.db.models import Job
        from app.state import db
        from app.worker.main import run_worker
        from core.clock import utcnow

        async def worker_round():
            await run_worker(once=True, stop=asyncio.Event())

        for _ in range(20):
            with db()() as session:
                pending = session.scalar(select(Job.id).where(
                    Job.status.in_(["scheduled", "retry_wait"]),
                    or_(Job.run_after.is_(None), Job.run_after <= utcnow()),
                    or_(Job.scheduled_for.is_(None), Job.scheduled_for <= utcnow())))
            if pending is None:
                return
            asyncio.run(worker_round())
        pytest.fail("Your local worker must drain this bounded journey without repeating jobs.")

    def jobs(self):
        from sqlalchemy import select
        from app.db.models import Job
        from app.state import db

        with db()() as session:
            return [(job.kind, job.status, job.last_error) for job in session.scalars(
                select(Job).where(Job.venture_id == self.venture_id))]

    def create(self, raw_idea=None):
        venture = self.ok("POST", "/ventures", {
            "raw_idea": raw_idea or self.case.idea, "start": False})
        self.venture_id = venture["id"]
        self.client.headers["X-Workspace-Id"] = venture["workspace_id"]
        return venture

    def open(self, raw_idea=None):
        self.create(raw_idea)
        saved = self.ok("POST", self.path("/idea/sessions"), {})
        self.session_id = saved["id"]
        self.drain()
        return self.saved()

    def saved(self):
        return self.ok("GET", self.session_path())

    def answer(self, text):
        self.ok("POST", self.session_path("/messages"), {"text": text})
        self.drain()
        return self.saved()

    def confirm(self):
        saved = self.saved()
        result = self.ok("POST", self.session_path("/confirm"), {
            "revision_id": saved["current_revision"]["id"], "start_research": False})
        self.drain()
        return result

    def ready(self):
        self.open()
        self.answer(self.case.audience)
        self.answer("Skip the remaining questions")
        self.confirm()

    def model(self):
        return self.ok("PUT", "/local/providers", {
            "provider": "openai", "api_key": self.secret, "model": "gpt-4.1-mini"})

    def mailbox_settings(self):
        return self.ok("PUT", "/local/settings/smtp", {
            "host": "mail.example.test", "port": 587, "user": f"you@{self.case.domain}",
            "password": self.secret, "sender_name": "Your team", "postal_address": "12 Test Lane"})

    def import_contact(self):
        preview = self.ok("POST", self.path("/gtm/contacts/import/preview"), {
            "text": f"name,email,country\nA willing interviewee,interviewee@{self.case.domain},US"})
        committed = self.ok("POST", self.path("/gtm/contacts/import"), {
            "preview_id": preview["preview_id"], "lawful_basis_attested": True, "lawful_basis": "consent"})
        assert committed["contact_ids"], committed
        return committed["contact_ids"][0]

    def chat(self, text):
        result = self.ok("POST", self.path("/chat"), {
            "message": text, "idempotency_key": uuid4().hex})
        self.drain()
        return result

    def safe(self):
        from sqlalchemy import select
        from app.db.models import GtmTouch, HumanTask, Job
        from app.state import db

        assert not any(kind in {"smtp", "imap", "publishing", "forbidden"}
                       for kind, _ in self.network), self.network
        with db()() as session:
            assert not list(session.scalars(select(GtmTouch).where(
                GtmTouch.sent_at.is_not(None)))), "Your idea work must not send messages."
            unasked = list(session.scalars(select(Job.kind).where(Job.kind.in_(
                ["email.send", "gtm.email.send", "pagebuilder.publish", "deploy.run"]))))
            assert not unasked, "Your idea work must not queue sending or publishing."
            assert not session.scalar(select(HumanTask.id).where(HumanTask.category == "idea_review")), \
                "Your idea review must not save optional tasks you have not accepted."


@pytest.fixture
def owner(local_owner_app, monkeypatch, venture_case):
    import imaplib
    import smtplib
    import urllib.request
    import primp
    from app.config import settings
    from llm_gateway.cassette import DETERMINISTIC_FIXTURES
    from tests.planning_fixtures import proposal

    memo = {"score": 5, "vote": "Back if", "risks": [
        {"area": "demand", "risk": f"You have not verified demand from {venture_case.audience}."},
        {"area": "delivery", "risk": "You have not observed someone completing the proposed workflow."},
        {"area": "distribution", "risk": "You have not tested how you will reach your first customer."}],
        "questions": ["Who will try your offer first?", "What do you need to observe?", "How will you reach them?"],
        "conditions": ["You record one permission-based customer interview."]}
    monkeypatch.setitem(DETERMINISTIC_FIXTURES, "idea.board", {"content": json.dumps(memo),
                                                           "tokens_in": 100, "tokens_out": 100})
    diagnosis = {"bottleneck": f"You have not verified demand from {venture_case.audience}.",
        "rationale": venture_case.problem, "assumptions": ["You can reach your proposed audience."],
        "evidence_gaps": ["You have not recorded customer interviews."],
        "next_actions": [{"kind": "research", "instruction": f"Research recent discussions by {venture_case.audience}.",
                          "reason": "You need to compare your assumption with observed problems."}]}
    monkeypatch.setitem(DETERMINISTIC_FIXTURES, "gtm.god_diagnose", {"content": json.dumps(diagnosis),
        "tokens_in": 100, "tokens_out": 100})
    plan = proposal({"sources": [], "assets": []})
    plan.update(summary=venture_case.idea, goal_first_paid_user="You record one paid pilot or a rejection.",
                assumptions=[f"You can reach {venture_case.audience}."], exclusions=["Sending and publishing are not authorized."])
    for milestone in plan["milestones"]:
        milestone.update(title=f"Your {milestone['kind']} test", description=venture_case.idea,
                         acceptance=["You record an observed result without assuming demand."])
        for task in milestone["tasks"]:
            task.update(title=f"Review your {milestone['kind']} test", description=venture_case.problem,
                        acceptance=["You record the result and choose your next test."])
            landing = milestone["kind"] == "landing"
            task.update(capability_id="firstlook.landing" if landing else "founder.action",
                implementation="new_code" if landing else "founder_action", landing_expectations=None,
                founder_action=None if landing else {"title": "Review your proposed offer",
                    "steps": [f"Ask permission to interview {venture_case.audience}."],
                    "verification": "You record what happened, not an assumed outcome."})
    monkeypatch.setitem(DETERMINISTIC_FIXTURES, "plan.compile", {"content": json.dumps(plan),
        "tokens_in": 900, "tokens_out": 1600})

    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "replay")
    monkeypatch.setenv("EMAIL_PROVIDER", "log")
    monkeypatch.setenv("CHANNELS_DRY_RUN", "false")
    monkeypatch.setenv("GUESSED_EMAILS", "off")
    monkeypatch.setenv("SUPYMEM_SINGLE_PROCESS", "0")
    monkeypatch.setattr(settings, "new_user_flow", "on")
    network = []

    def external(request):
        host = request.url.host or ""
        if host == "supymem.com" or host.endswith(".supymem.com"):
            network.append(("forbidden", host))
            raise AssertionError("Your local app must not contact supymem.com.")
        if host.endswith(("vercel.com", "netlify.com", "amazonaws.com")):
            network.append(("publishing", host))
            return httpx.Response(200, json={"id": "fake-publication"})
        if host not in {venture_case.domain, "cloudflare-dns.com", "api.tavily.com", "api.exa.ai",
                        "html.duckduckgo.com", "duckduckgo.com", "127.0.0.1", "localhost"}:
            network.append(("forbidden", host))
            raise AssertionError("Your flow contacted a service outside its fake transports.")
        network.append(("search" if "search" in request.url.path else "page", host))
        if host == "cloudflare-dns.com":
            return httpx.Response(200, json={"Status": 0, "Answer": [{"type": 15, "data": "10 mx.example.test"}]})
        if host.endswith("duckduckgo.com"):
            return httpx.Response(200, text=f'<div class="body"><a href="https://{venture_case.domain}/discussion">'
                                  f'<h2>{venture_case.problem}</h2>{venture_case.idea}</a></div>')
        if request.url.path.endswith("/search"):
            source = {"title": venture_case.problem,
                      "url": f"https://{venture_case.domain}/discussion",
                      "content": venture_case.idea, "score": 0.9}
            return httpx.Response(200, json={"results": [source]})
        return httpx.Response(200, text=f"<html><body><h1>{venture_case.problem}</h1>"
                              f"<p>{venture_case.audience}</p></body></html>",
                              headers={"content-type": "text/html"})

    real_async = httpx.AsyncClient
    real_sync = httpx.Client

    def async_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(external)
        return real_async(*args, **kwargs)

    def sync_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(external)
        return real_sync(*args, **kwargs)

    def deny_socket(*args, **kwargs):
        network.append(("forbidden", "unfaked transport"))
        raise AssertionError("Your offline flow must not open a network socket.")

    def mailbox(kind):
        def connect(*args, **kwargs):
            network.append((kind, "mailbox"))
            raise AssertionError("Your idea journey must not open your mailbox.")
        return connect

    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    monkeypatch.setattr(httpx, "Client", sync_client)
    monkeypatch.setattr(primp, "Client", lambda **kwargs: SimpleNamespace(
        request=lambda method, url, **options: external(httpx.Request(method, url))))
    monkeypatch.setattr(socket.socket, "connect", deny_socket)
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", int(port or 443)))])
    monkeypatch.setattr(urllib.request, "urlopen", deny_socket)
    monkeypatch.setattr(smtplib, "SMTP", mailbox("smtp"))
    monkeypatch.setattr(smtplib, "SMTP_SSL", mailbox("smtp"))
    monkeypatch.setattr(imaplib, "IMAP4_SSL", mailbox("imap"))
    with _owner_client(local_owner_app, base_url="http://127.0.0.1",
                       client=("127.0.0.1", 50000)) as client:
        yield OwnerFlow(client, venture_case, network)


def test_your_file_backed_local_app_starts_before_idea_intake(local_owner_app, venture_case):
    """Your laptop's SQLite database must open before you can save an idea."""
    with _owner_client(local_owner_app, base_url="http://127.0.0.1",
                       client=("127.0.0.1", 50000)) as client:
        response = client.post("/ventures", json={"raw_idea": venture_case.idea, "start": False})
        assert response.status_code == 201, response.text


def assert_requested(reply, *, optional_actions=()):
    assert reply.get("kind") not in {"confirmation", "action_failed", "provider_error", "needs_you"}, reply
    assert not any(action.get("confirmation") and action["confirmation"].get("action") not in optional_actions
                   for action in reply.get("actions", [])), reply
    assert not re.search(r"\b\d+(?:[.,]\d+)?\s*credits?\b|\bcredits?\s*[:=]\s*\d",
                         reply.get("body_md", ""), re.I), reply


def assert_next_step(reply, expected):
    text = json.dumps(reply).casefold()
    for word in expected:
        assert word.casefold() in text, reply
    assert not re.search(r"\b\d+\s*credits?\b", text), reply


def test_your_idea_intake_is_saved_without_starting_work(owner):
    """Saving your idea does not silently start research, sending or publishing."""
    from sqlalchemy import select
    from app.db.models import Job
    from app.state import db

    venture = owner.create()
    owner.drain()
    assert venture["raw_idea"] == owner.case.idea
    with db()() as session:
        assert not list(session.scalars(select(Job.kind).where(
            Job.venture_id == owner.venture_id, Job.kind.in_(
                ["idea.validate", "research.deep", "plan.compile"]))))
    owner.safe()


def test_your_answer_advances_without_repeating_a_resolved_question(owner):
    """Your offer and audience answers are saved and never asked for again."""
    saved = owner.open()
    questions = [turn for turn in saved["turns"] if turn["role"] == "agent" and turn["kind"] == "question"]
    assert questions, (saved, owner.jobs())
    saved = owner.answer(owner.case.audience)
    saved = owner.answer(owner.case.problem)
    assert sum(turn["text"] == owner.case.audience for turn in saved["turns"]) == 1, (saved["turns"], owner.jobs())
    assert sum(turn["text"] == owner.case.problem for turn in saved["turns"]) == 1, (saved["turns"], owner.jobs())
    later = [turn for turn in saved["turns"] if turn["role"] == "agent" and turn["kind"] == "question"]
    fields = [turn["options"].get("field") for turn in later]
    assert fields.count("offer") == 0 and fields.count("customer") <= 1, fields
    question_text = [turn["text"].strip().casefold() for turn in later]
    assert len(question_text) == len(set(question_text)), question_text
    for text in (owner.case.audience, owner.case.problem):
        answer = next(turn for turn in saved["turns"] if turn["text"] == text)
        metadata = answer["options"].get("original_metadata", answer["options"])
        assert metadata["founder_text"] == text and metadata["question_id"]
    owner.safe()


@pytest.mark.parametrize("classification", ["unclear", "off-topic"])
def test_your_unusable_answer_keeps_original_text_and_metadata_not_evidence(owner, monkeypatch, classification):
    from sqlalchemy import select
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.gstack import office_hours
    from app.state import db
    from app.ventures import founder_facts
    from core.clock import utcnow

    owner.open()
    text = "I'm not sure" if classification == "unclear" else owner.case.problem
    metadata = {"provenance": "founder_stated", "founder_text": text, "suggested_text": "", "source": owner.case.domain}
    monkeypatch.setattr(office_hours, "is_off_topic", lambda *args: classification == "off-topic")
    monkeypatch.setattr(founder_facts, "schedule", lambda *args, **kwargs: pytest.fail("Your unusable answer is not evidence."))
    with db()() as session:
        row = session.get(IdeaSession, owner.session_id)
        question = session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == row.id,
            ClarifierTurn.role == "agent", ClarifierTurn.kind == "question").order_by(ClarifierTurn.turn.desc()).limit(1))
        options = json.loads(question.options)
        assert options["phase"] == office_hours.PHASE
        office_hours.record_answer(session, row, question, options, text, utcnow(), metadata=metadata)
        answer = session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == row.id,
            ClarifierTurn.role == "founder").order_by(ClarifierTurn.turn.desc()).limit(1))
        assert answer.text == text
        saved_metadata = json.loads(answer.options)
        assert saved_metadata["original_metadata"] == {**metadata, "question_id": question.id}
        assert saved_metadata["classification"] == classification and not saved_metadata["founder_text"]
        assert "question_id" not in metadata and "classification" not in metadata
        projected = next(item for item in office_hours.answers_for(session, row.id)
                         if item["id"] == options["gstack_id"])
        assert projected["assumed"] and projected["answer"].startswith("Assumed: ")
        assert not projected["founder_text"] and projected["provenance"] == "suggested_choice"
        assert text not in projected["answer"]
        session.commit()
    owner.answer(text)
    owner.safe()


@pytest.mark.parametrize("field", ["text", "chip"])
def test_your_key_is_not_saved_as_an_unclear_answer(owner, field):
    from sqlalchemy import select
    from app.db.supymem_models import ClarifierTurn
    from app.state import db

    owner.open()
    credential = "sk-proj-" + owner.secret
    response = owner.request("POST", owner.session_path("/messages"), {"text": "", field: credential})
    assert response.status_code == 422, response.text
    assert_next_step(response.json(), ["Keys and connections", "key field", "answer"])
    with db()() as session:
        turns = list(session.scalars(select(ClarifierTurn).where(ClarifierTurn.session_id == owner.session_id)))
        assert all(credential not in turn.text and credential not in (turn.options or "") for turn in turns)
    owner.safe()


@pytest.mark.parametrize("tail,requested", [
    ("create a provisional execution plan from my saved idea", True),
    ("then generate my execution plan", True),
    ("create my plan without sending or publishing anything", True),
    ('create my plan for a "small delivery pilot"', True),
    ("create my plan if I approve later", False),
    ("do not create my plan", False),
    ("tell me how to create my plan", False),
])
def test_only_your_explicit_plan_request_continues_after_a_skip(venture_case, tail, requested):
    from app.ventures.journey_overrides import parse, plan_after_skip

    text = f"Skip research and {tail} for {venture_case.audience}"
    assert parse(text)[0] == "journey.skip_step"
    follow_up = plan_after_skip(text)
    assert bool(follow_up) is requested
    if requested:
        assert follow_up == f"{tail.removeprefix('then ')} for {venture_case.audience}"
    assert plan_after_skip(f"Restore research and {tail}") is None


@pytest.mark.parametrize("pages_on", [True, False])
def test_your_plan_setup_names_every_missing_saved_field_and_screen(owner, pages_on):
    from app.chat.plan_chain import _blocked
    from app.credits.errors import CreditsUnavailable
    from app.validation.board import _quote

    venture = owner.create()
    owner.ok("PUT", "/local/capabilities/landing_pages", {"on": pages_on})
    setup = next(item for item in owner.ok("GET", "/local/capabilities")["capabilities"]
                 if item["name"] == "content")
    reply = _blocked(owner.venture_id, "plan_model_required")
    assert_next_step(reply, ["Keys and connections", "Your AI provider", *setup["missing"]])
    assert reply["actions"][0]["href"] == "/settings/local"
    with pytest.raises(CreditsUnavailable) as caught:
        _quote(venture["workspace_id"], {})
    for field in setup["missing"]:
        assert field in str(caught.value)
    review = owner.ok("POST", owner.path("/chat/confirm"), {"action": "idea.pressure_test.prepare", "payload": {}})
    assert_next_step(review, ["Keys and connections", "Your AI provider", *setup["missing"]])
    assert any(action.get("href") == "/settings/local" for action in review["actions"])
    owner.safe()


def test_your_idea_api_keeps_session_scope_after_service_extraction(owner):
    owner.open()
    first_venture, session_id = owner.venture_id, owner.session_id
    sessions = owner.ok("GET", owner.path("/idea/sessions"))["items"]
    assert [item["id"] for item in sessions] == [session_id]
    assert sessions[0]["state"] and sessions[0]["case"] and sessions[0]["project_intent"]
    owner.create()
    assert owner.ok("GET", owner.path("/idea/sessions")) == {"items": []}
    response = owner.request("POST", owner.path(f"/idea/sessions/{session_id}/confirm"), {"start_research": False})
    assert response.status_code == 404, response.text
    saved = owner.ok("GET", f"/ventures/{first_venture}/idea/sessions/{session_id}")
    assert saved["state"] != "confirmed"
    owner.safe()


def test_your_office_hours_api_keeps_not_ready_contract_without_new_jobs(owner, monkeypatch):
    from app.api import gstack

    owner.create()
    assert owner.ok("GET", owner.path("/gstack/plan"))["plan"] is None
    assert owner.ok("GET", owner.path("/product/brief"))["brief"] is None
    for suffix, error in [("/gstack/plan", "plan_not_ready"), ("/product/brief", "brief_not_ready")]:
        response = owner.request("POST", owner.path(suffix), {})
        assert response.status_code == 404 and response.json()["detail"] == error, response.text
    assert not owner.jobs()
    def unexpected_error(venture_id):
        raise KeyError(owner.case.domain)

    monkeypatch.setattr(gstack, "plan_for", unexpected_error)
    with pytest.raises(KeyError, match=owner.case.domain):
        gstack._plan_for(owner.venture_id)
    owner.safe()


def test_you_can_skip_review_and_confirm_the_summary_without_research(owner):
    """Skipping leaves assumptions visible; confirming saves your summary and starts no research."""
    owner.open()
    owner.answer(owner.case.audience)
    saved = owner.answer("Skip the remaining questions")
    assert saved["state"] == "reviewing", saved
    assert saved["current_revision"]["structured"].get("_questions_skipped"), saved
    owner.confirm()
    assert owner.saved()["state"] == "confirmed"
    dossier = owner.ok("GET", owner.path("/research/dossier"))
    assert not dossier["available"], dossier
    owner.safe()


def test_your_requested_board_review_runs_without_a_second_confirmation(owner):
    """Your requested independent review is useful judgment, not claimed market evidence."""
    owner.model()
    owner.ready()
    reply = owner.chat("Pressure test my idea with an independent idea board review")
    assert_requested(reply, optional_actions={"idea.pressure_test.accept"})
    body = reply.get("body_md", "").casefold()
    assert "risk" in body and ("not evidence" in body or "not market evidence" in body), reply
    owner.safe()


def test_your_requested_gtm_diagnosis_is_saved_with_a_next_move(owner):
    """Your GTM diagnosis gives you a next move without claiming customer validation."""
    owner.model()
    owner.ready()
    reply = owner.chat("Diagnose my go-to-market and tell me the next move")
    assert_requested(reply)
    runs = owner.ok("GET", "/api/v1" + owner.path("/gtm/god/runs"))
    assert runs["runs"], "Your requested diagnosis must be saved."
    completed = runs["runs"][0]
    assert completed["state"] == "completed" and completed["result"], (completed, owner.jobs())
    assert any(word in json.dumps(completed["result"]).casefold() for word in ["next", "ask", "interview"]), completed
    owner.safe()


@pytest.mark.parametrize("research", [
    pytest.param(False, id="without-research"),
    pytest.param(True, id="with-research"),
])
def test_your_requested_plan_is_saved_from_the_context_you_choose(owner, research):
    """You can request an evidence-backed plan or explicitly choose a provisional plan."""
    owner.model()
    owner.ready()
    owner.ok("PUT", owner.path("/resources"), {"assets": [], "complete": True})
    if research:
        reply = owner.chat("Research my market and competitors for my saved idea")
        assert_requested(reply)
        dossier = owner.ok("GET", owner.path("/research/dossier"))
        assert dossier["available"] and dossier["sources"], dossier
        request = "Create my execution plan using the research you saved"
    else:
        request = "Skip research and create a provisional execution plan from my saved idea"
    reply = owner.chat(request)
    assert_requested(reply)
    plans = owner.ok("GET", owner.path("/plans"))["items"]
    assert plans, ("Your requested execution plan must be saved.", reply, owner.jobs())
    plan = owner.ok("GET", owner.path(f"/plans/{plans[0]['id']}"))
    assert plan["state"] in {"founder_review", "draft"}, plan
    assert plan.get("milestones"), (plan.get("generation_state"), owner.jobs())
    if not research:
        assert plan["provisional"] is True, plan
        assert not owner.network, "Your explicit research skip must not search or read pages."
    owner.safe()


@pytest.mark.parametrize("feature,message", [
    ("research", "Research my market and competitors for my saved idea"),
    ("find_customers", "Find potential customers for my saved idea"),
])
def test_a_switched_off_feature_names_the_switch_you_can_turn_on(owner, feature, message):
    """A switched-off research action tells you the exact app switch, without doing it."""
    owner.model()
    owner.ready()
    switch = owner.ok("PUT", f"/local/capabilities/{feature}", {"on": False})
    assert switch["state"] == "off", switch
    reply = owner.chat(message)
    assert_next_step(reply, [switch["next_step"]])
    assert not owner.network
    owner.safe()


@pytest.mark.parametrize("provider,screen,field", [
    ("tavily", "Your search and page reader", "API key"),
    ("exa", "Your search and page reader", "API key"),
    ("firecrawl", "Your search and page reader", "Firecrawl URL"),
    ("openai", "Your AI provider", None),
])
def test_missing_search_setup_names_the_field_and_keys_screen(owner, provider, screen, field):
    """An optional search upgrade missing its key or URL names exactly what you must save."""
    owner.ready()
    owner.ok("PUT", "/local/settings/search", {"provider": provider})
    capability = owner.ok("GET", "/local/capabilities")["capabilities"]
    research = next(item for item in capability if item["name"] == "research")
    assert research["state"] == "needs_setup", research
    reply = owner.chat("Research my market and competitors for my saved idea")
    required = [field] if field else research["missing"]
    assert required, research
    assert_next_step(reply, ["Keys and connections", screen, *required])
    assert not owner.network
    owner.safe()


@pytest.mark.parametrize("message", [
    pytest.param("Pressure test my idea with an independent idea board review", id="board"),
    pytest.param("Diagnose my go-to-market and tell me the next move", id="diagnosis"),
    pytest.param("Create my execution plan from my saved idea", id="plan"),
])
def test_missing_model_setup_names_your_provider_key_and_keys_screen(owner, message):
    """A missing model key tells you where to save it, not to ask an administrator."""
    owner.ready()
    capabilities = owner.ok("GET", "/local/capabilities")["capabilities"]
    model = next(item for item in capabilities if item["name"] == "content")
    assert model["state"] == "needs_setup" and model["missing"], model
    reply = owner.chat(message)
    assert_next_step(reply, ["Keys and connections", *model["missing"]])
    owner.safe()


def test_missing_review_model_names_keys_screen_without_automatic_retries(owner, monkeypatch):
    from app.core import providers_local
    from app.db.models import Job
    from app.db.supymem_models import ValidationRun
    from app.state import db
    from sqlalchemy import select

    owner.open()
    before = owner.saved()
    providers_local.clear_settings()
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    with pytest.raises(providers_local.LocalProviderError) as missing:
        providers_local.load_local_config()
    requested = owner.ok("POST", owner.session_path("/validate"), {"methods": ["gstack_office_hours"]})
    owner.drain()
    with db()() as session:
        job = session.get(Job, requested["job_id"])
        assert job.status in {"failed", "failed_dead", "poison"}, (job.status, job.last_error)
        assert job.last_error == "validation_model_required"
        assert not session.scalar(select(ValidationRun.id).where(ValidationRun.venture_id == owner.venture_id,
            ValidationRun.state == "complete"))
    messages = owner.ok("GET", owner.path("/chat"))["items"]
    failed = next(message for message in reversed(messages) if message["kind"] == "idea_validation_failed")
    assert str(missing.value) in failed["body_md"] and "Keys and connections" in failed["body_md"]
    assert any(action.get("href") == "/settings/local" for action in failed["actions"])
    assert any(action.get("action") == "job.retry" and action["payload"]["kind"] == "idea.validate"
        for action in failed["actions"])
    assert owner.saved()["current_revision"]["id"] == before["current_revision"]["id"]
    owner.safe()


def test_builtin_research_needs_no_search_key_or_admin_permission(owner):
    """Your default search and page reader are available without an optional vendor key."""
    owner.create()
    saved = owner.ok("GET", "/local/settings/search")
    assert saved["values"].get("provider", "builtin") == "builtin", saved
    capabilities = owner.ok("GET", "/local/capabilities")["capabilities"]
    research = next(item for item in capabilities if item["name"] == "research")
    assert research["state"] == "ready" and research["missing"] == [], research
    owner.safe()


def test_your_saved_keys_are_redacted_from_settings_and_idea_work(owner, caplog):
    """Your model, search and mailbox secrets remain private across this owner journey."""
    owner.model()
    owner.ok("PUT", "/local/settings/search", {"provider": "tavily", "api_key": owner.secret})
    owner.mailbox_settings()
    owner.ready()
    for path in ["/local/providers", "/local/llm-settings", "/local/settings/search",
                 "/local/settings/smtp", "/local/capabilities",
                 owner.path("/journey"), owner.session_path()]:
        owner.ok("GET", path)
    exported = owner.request("GET", "/local/settings/export")
    assert exported.status_code == 200, exported.text
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert all(owner.secret.encode() not in archive.read(name) for name in archive.namelist())
    assert owner.secret not in caplog.text
    owner.safe()


def test_your_interview_draft_keeps_disclosure_opt_out_and_sender_address(owner):
    """Your requested interview draft is unsent, with source disclosure and opt-out intact."""
    owner.model()
    owner.mailbox_settings()
    owner.ready()
    contact_id = owner.import_contact()
    result = owner.ok("POST", owner.path(f"/leads/contacts/{contact_id}/draft"), {})
    assert_requested(result["receipt"])
    leads = result["items"]
    lead = next(item for item in leads if item["id"] == contact_id)
    draft = lead.get("draft")
    assert draft and not draft.get("sent_at"), lead
    body = draft.get("body", "")
    assert "I have your address from my own contacts." in body or "Your email address is listed publicly on " in body, draft
    assert re.search(r"opt.out|unsubscribe|\bSTOP\b|no thanks", body, re.I), draft
    assert "12 Test Lane" in body, draft
    owner.safe()


def test_your_suppressed_contact_is_not_redrafted_when_sending_is_enabled(owner):
    """Your Do not contact choice stays suppressed even after you enable sending and request a draft."""
    owner.model()
    owner.mailbox_settings()
    owner.ready()
    contact_id = owner.import_contact()
    owner.ok("PATCH", f"/api/v1/ai-gtm/contacts/{contact_id}", {"do_not_contact": True})
    owner.ok("PUT", "/local/capabilities/send_email", {"on": True})
    result = owner.request("POST", owner.path(f"/leads/contacts/{contact_id}/draft"), {})
    assert result.status_code in {200, 409, 422}, result.text
    assert any(word in result.text.casefold() for word in ["opted out", "opt-out", "suppressed", "stop", "do not contact"]), result.text
    leads = owner.ok("GET", owner.path("/gtm/contacts"))["items"]
    assert not any(item.get("draft") for item in leads if item["id"] == contact_id), leads
    owner.safe()


def test_your_send_cap_cannot_be_bypassed_by_a_plan_or_api_request(owner):
    """Your local owner can enable sending but cannot exceed a batch's bounded send cap."""
    owner.model()
    owner.mailbox_settings()
    owner.create()
    owner.ok("PUT", "/local/capabilities/send_email", {"on": True})
    response = owner.request("POST", "/api/v1" + owner.path("/gtm/journey/send"), {
        "batch_id": "unrequested-batch", "draft_ids": ["unsaved-draft"],
        "approval_digest": "a" * 64, "cap": 6})
    assert response.status_code == 422, response.text
    owner.drain()
    owner.safe()


def test_your_board_review_button_runs_without_another_confirmation(owner):
    """Choosing Review executes the independent review, not another confirmation card."""
    owner.model()
    owner.ready()
    reply = owner.ok("POST", owner.path("/chat/confirm"), {
        "action": "idea.pressure_test.prepare", "payload": {}})
    owner.drain()
    assert_requested(reply, optional_actions={"idea.pressure_test.accept"})
    body = reply.get("body_md", "").casefold()
    assert "risk" in body and "not evidence" in body, reply
    owner.safe()


def test_your_requested_idea_and_plan_work_never_sends_or_publishes(owner):
    """Enabling sending and publishing does not authorize side effects of idea or plan work."""
    owner.model()
    owner.mailbox_settings()
    owner.ready()
    owner.ok("PUT", "/local/settings/publishing", {
        "provider": "vercel", "project": "your-test-site", "token": owner.secret})
    owner.ok("PUT", "/local/capabilities/send_email", {"on": True})
    owner.ok("PUT", "/local/capabilities/publish_pages", {"on": True})
    owner.chat("Create my execution plan from my saved idea. Do not send or publish anything")
    owner.chat("Pressure test my idea with an independent idea board review")
    owner.safe()
