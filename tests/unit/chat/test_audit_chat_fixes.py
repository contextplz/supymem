"""Regression tests for the 2026-09-30 chat audit (G11 E12 G7 G9 G10 G3 G14 G24 E14 D1 D9 D12 D5 F6 G1 B3 C7)."""

from __future__ import annotations

import json

import pytest
from tests.company_os_harness import company_os_test_client, load_company_os_app
from tests.company_os_harness import configure_local_test_model


@pytest.mark.parametrize("audience", ["freight dispatch teams", "families sharing garden plots"])
def test_local_research_setup_is_a_next_step_without_starting_work(tmp_path, monkeypatch, audience):
    import asyncio
    from contextlib import nullcontext
    import httpx

    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/setup.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    main = load_company_os_app()
    from app.chat import build_path, customers, execute
    from app.core import local_mode
    from app.core.local_secrets import update_settings
    from app.gtm import journey
    from app.research import explore, local_providers
    from app.ventures import build_prerequisites

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("EMAIL_PROVIDER", "configured")
    update_settings("search", {"provider": "tavily"}, {})
    with company_os_test_client(main.create_app()) as client:
        selected = client.post("/local/providers/sample", json={})
        assert selected.status_code == 200, selected.text
    monkeypatch.setattr(local_mode, "is_test_mode", lambda: False)
    monkeypatch.setattr(local_providers, "search_provider", lambda **kwargs: None)
    monkeypatch.setattr(build_path, "research_prerequisites", lambda venture_id: None)
    monkeypatch.setattr(build_prerequisites, "answers_pending", lambda *args: False)
    monkeypatch.setattr(customers, "_view", lambda venture_id: {"state": "idle", "can_prepare": True,
        "discovery_started": False, "icp": {"who": audience}})
    monkeypatch.setattr(customers, "_as", lambda *args: nullcontext("workspace"))
    authorized = []
    monkeypatch.setattr(journey, "authority", lambda *args, **kwargs: authorized.append(args))
    monkeypatch.setattr(journey, "prepare", lambda *args, **kwargs: pytest.fail("Unconfigured search must not start"))
    for reply in (build_path.research_card("venture"),
                  asyncio.run(customers.find_reply("venture", f"Find customers among {audience}")),
                  asyncio.run(customers.execute("venture", "gtm.find", {}, "founder")),
                  execute._research_start("venture", "founder"),
                  customers._search_failure({"state": "failed", "icp": {"who": audience}}, retry_status=True),
                  asyncio.run(explore.explore_web(audience))):
        body = reply.get("body_md") or reply.get("summary") or ""
        assert "Keys and connections" in body
        assert not any(action.get("confirmation") for action in reply.get("actions", []))
    assert not authorized
    monkeypatch.setattr(execute, "_action_capability", lambda *args: None)
    inner = asyncio.run(customers.execute("venture", "gtm.find", {}, "founder"))
    assert inner["action_started"] is False and "Keys and connections" in inner["body_md"]
    assert authorized == [("workspace", "venture")]
    update_settings("search", {"provider": "builtin"}, {})
    monkeypatch.setattr(local_providers, "search_provider", lambda **kwargs: object())
    monkeypatch.setattr(local_providers, "scrape_provider", lambda: None)
    assert "Keys and connections" in local_providers.setup_reply()["body_md"]
    monkeypatch.setattr(local_providers, "search_provider", lambda **kwargs: local_providers.LocalHttpResearchProvider("firecrawl", ""))
    monkeypatch.setattr(local_providers, "_reader_endpoint", lambda: ("http://127.0.0.1:3002", ""))

    class FailedReader:
        async def post(self, url, **kwargs):
            return httpx.Response(200, json={"success": False}, request=httpx.Request("POST", url))

    with pytest.raises(ValueError, match="Keys and connections"):
        asyncio.run(local_providers.LocalHttpResearchProvider("firecrawl", "")._firecrawl_post(FailedReader(), "search", {"query": audience}))
    monkeypatch.setattr(local_providers, "scrape_provider", lambda: object())
    transient = customers._search_failure({}, "public_search_unavailable", retry_status=True)
    assert transient["body_md"].startswith("Your search could not finish. Retry")
    assert "If you use built-in search" in transient["body_md"]
    assert any(action["label"] == "Retry" for action in transient["actions"])


@pytest.mark.parametrize("audience", ["commercial cleaning crews", "parents organizing school carpools"])
def test_saved_venture_reference_does_not_become_a_search_audience(audience):
    load_company_os_app()
    from app.gtm.discovery import request_audience, search_note

    from app.gtm.journey import search_audience

    assert search_audience(audience, "Find potential customers for my saved idea") == audience
    assert request_audience(f"Find {audience} for my current venture") == audience
    assert search_audience(audience, f"Find {audience} for my current venture") == audience
    assert search_note(f"Find {audience} near Manchester", audience)


@pytest.mark.parametrize("mode", ["hosted", "local"])
@pytest.mark.parametrize("topic", ["supplier invoices", "family hiking routes"])
def test_coaching_quote_requires_relevant_evidence_not_just_its_card(monkeypatch, topic, mode):
    monkeypatch.setenv("SUPYMEM_MODE", mode)
    load_company_os_app()
    from app.knowledge import yc_video_advice

    row = {"id": "reference", "topics": topic.split(), "action_discussed": topic,
           "source_video": {"title": "Interview advice", "url": "https://www.youtube.com/watch?v=source"},
           "evidence": {"excerpts": ["Your interview customers join a panel to discuss hiring executives."]}}
    monkeypatch.setattr(yc_video_advice, "search", lambda *args, **kwargs: [row])
    unrelated = yc_video_advice.quote(f"Interview your customers about {topic}", context=topic,
                                     topic=f"Interview your customers about {topic}")
    if mode == "local":
        assert unrelated is None
    else:
        assert unrelated["id"] == "reference"
    row["evidence"]["excerpts"] = [f"Ask your customers about their latest problem with {topic}."]
    assert yc_video_advice.quote(f"Interview your customers about {topic}", context=topic,
                                topic=f"Interview your customers about {topic}")["id"] == "reference"


@pytest.mark.parametrize("mode,audience", [("company", "industrial equipment inspectors"),
                                            ("consumer", "families planning their summer camping holidays")])
def test_experiment_hypotheses_keep_complete_grounding_and_hiring_fits_only_businesses(monkeypatch, mode, audience):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    from types import SimpleNamespace

    load_company_os_app()
    from app.experiments import autonomous

    problem = "tracking every agreed maintenance step without collecting private records or offering professional advice"
    icp = {"who": audience, "problem": problem, "mode": mode, "needs_input": False}
    template = autonomous.RECIPES["postings"]["hypothesis_template"]
    expected = template.format(**autonomous._hypothesis_slots(icp))
    assert autonomous._hypothesis(template, icp) == expected
    snapshot = {"venture_id": "venture", "workspace_id": "workspace", "stage": "idea", "direction": 1,
                "journey": {"icp": icp}, "waitlist": {}, "idea_problem": problem}
    # c2c62e461 takes search and page pricing from the saved local provider.
    specs = autonomous.select_runnable(snapshot, SimpleNamespace(name="firecrawl", search_micro=0, scrape_micro=0))
    # 5a078e63c keeps the complete grounded hypothesis locally.
    scan_specs = [spec for spec in specs if spec.get("scan") in autonomous.RECIPES]
    assert scan_specs
    for spec in scan_specs:
        template = autonomous.RECIPES[spec["scan"]]["hypothesis_template"]
        assert spec["hypothesis"] == autonomous._hypothesis(template, spec["icp"])
    assert any(spec.get("scan") == "postings" for spec in scan_specs) is (mode == "company")


@pytest.mark.parametrize("audience", ["independent theatre managers", "people planning accessible day trips"])
@pytest.mark.parametrize("confirmed", [False, True])
def test_explicit_private_first_page_request_does_not_route_to_a_rewrite(monkeypatch, request, audience, confirmed):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("EMAIL_PROVIDER", "log")
    client, venture_id, workspace_id = request.getfixturevalue("env")
    from app.chat import execute
    from app.chat.intent import _ONE_CLICK_PAGE_RE, classify_intent
    from app.core import local_capabilities
    from app.db.models import Venture
    from app.db.models import Job, SiteVersion
    from app.db.supymem_models import IdeaSession
    from app.idea import service
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id
    from desktop_runtime import agent_status

    monkeypatch.setattr(agent_status, "statuses", lambda: [])

    original_enabled = local_capabilities.enabled
    monkeypatch.setattr(local_capabilities, "enabled",
                        lambda name: True if name == "landing_pages" else original_enabled(name))
    with db()() as session:
        venture = session.get(Venture, venture_id)
        venture.name = f"Planning for {audience}"
        venture.raw_idea = f"A shared planning checklist for {audience}"
        idea = IdeaSession(id=new_id("ids"), workspace_id=workspace_id, venture_id=venture_id,
            case="know", state="confirmed" if confirmed else "clarifying", created_at=utcnow(), updated_at=utcnow())
        session.add(idea)
        session.flush()
        revision = service.append_revision(session, idea, {"customer": audience,
            "problem": "Missed planning steps", "offer": venture.raw_idea}, source="founder")
        idea.confirmed_revision_id = revision.id if confirmed else None
        idea_id = idea.id
        session.commit()
    blocked = execute._refine_site_context(venture_id)
    assert "requirements" in blocked["body_md"] and "charter" not in blocked["body_md"].casefold()
    for message in (f"Build a landing page for {audience}. Keep it private and do not send anything",
                    f"Create my first website for {audience}. Do not publish it"):
        assert classify_intent(message) == "pagebuilder.build"
        assert _ONE_CLICK_PAGE_RE.fullmatch(message)
    reply = _chat(client, venture_id, message)
    assert not reply.get("action_started") and not _cards(reply), reply["body_md"]
    assert "Settings > Keys and connections > Coding tools" in reply["body_md"]
    with db()() as session:
        assert session.query(SiteVersion).filter_by(venture_id=venture_id).count() == 0
        assert session.query(Job).filter_by(venture_id=venture_id, kind="pagebuilder.build").count() == 0
        saved = session.get(IdeaSession, idea_id)
        assert saved.state == ("confirmed" if confirmed else "clarifying")
        assert bool(saved.confirmed_revision_id) is confirmed


@pytest.mark.parametrize("audience", ["maintenance managers at regional mills with 200 to 500 employees",
                                       "parents organizing hiking trips with children aged 6 to 12"])
def test_pending_customer_question_accepts_addressed_audience_answers(env, audience):
    import json

    from sqlalchemy import select
    from app.chat.idea_bridge import _plausible_answer, pending_question
    from app.db.supymem_models import ClarifierTurn
    from app.state import db

    question = ClarifierTurn(text="Who needs this first?", options=json.dumps({"field": "customer", "options": []}))
    assert _plausible_answer(f"You can focus on {audience}. They check calendar notes before their weekly planning session.", question,
                             {"offer": "A shared planning checklist", "customer": ""})
    client, venture_id, _ = env
    sid, qid, _ = _saved_clarification(env, chat_entry=True, question="Who needs this first?", field="customer", options=())
    answer = f"You can focus on {audience}. They check calendar notes before their weekly planning session."
    assert pending_question(venture_id, answer) == (sid, qid)
    reply = _chat(client, venture_id, answer)
    with db()() as session:
        assert session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == sid,
            ClarifierTurn.role == "founder", ClarifierTurn.text == answer)) is not None, reply


@pytest.mark.parametrize("audience,document_request", [
    ("warehouse procurement teams", "Before we continue, give me a supplier onboarding checklist for warehouse procurement teams."),
    ("families planning camping trips", "For now, could you make a packing checklist for families planning camping trips?"),
])
def test_setup_document_requests_keep_the_request_out_of_saved_answers(audience, document_request):
    load_company_os_app()
    from app.chat.idea_bridge import split_question_reply, written_request
    from app.db.supymem_models import ClarifierTurn

    question = ClarifierTurn(kind="question", text="What do you want to offer?", options=json.dumps({"field": "offer"}))
    assert written_request(document_request)
    assert split_question_reply(document_request, question=question) == ("", document_request)
    contextual = f"I need a test brief for {audience}. Write it as a document."
    assert written_request(contextual)
    assert split_question_reply(contextual, question=question) == ("", contextual)
    mixed = f"{audience}. {document_request}"
    assert split_question_reply(mixed, options=[audience]) == (audience, document_request)
    assert not written_request(f"{document_request} Then send it to {audience}.")


@pytest.mark.parametrize("idea", ["Invoice matching for manufacturers", "Holiday planning for families"])
def test_context_before_written_request_preserves_no_work_boundary(idea):
    load_company_os_app()
    from app.chat.idea_bridge import written_request

    request = f"I have not interviewed anyone about {idea}. Give me an interview plan. Do not contact anyone."
    assert written_request(request)
    assert not written_request(f"How many signups do we have for {idea}? Draft an email.")
    assert not written_request(f"Research {idea}. Give me an interview plan.")
    assert not written_request(f"Write an app for {idea}.")


@pytest.mark.parametrize("idea", ["Invoice matching for manufacturers", "Holiday planning for families"])
def test_component_edits_keep_compound_speech_act_routing(idea):
    load_company_os_app()
    from app.chat.intent import classify_intent, split_speech_acts
    from app.chat.site_actions import classify_site_intent

    request = f"Rewrite the headline to {idea}"
    assert classify_intent(request) == "change"
    assert classify_site_intent(request) == "site.rewrite"
    assert split_speech_acts("How many signups do we have? " + request)[-1] == ("change", request)


@pytest.mark.parametrize("offer,customer,topic", [
    ("Shared invoice matching board", "Independent factory managers", "invoice"),
    ("Shared holiday planning board", "Families planning their holidays", "holiday"),
])
def test_initial_document_does_not_replace_extracted_prefill(offer, customer, topic):
    load_company_os_app()
    from app.entry.chat_start import _onboarding_progress

    source = "# Product brief\n" + f"Historical {topic} observations.\n" * 5000 + f"\nCustomer: {customer}"
    intake = {"path": "startup", "extracted": {"idea": offer, "audience": customer},
              "prefill_answers": {"offer": offer, "customer": customer}}
    accepted = {}
    _onboarding_progress(intake, source, accepted_answers=accepted)
    assert {**intake["prefill_answers"], **accepted} == intake["prefill_answers"]
    _onboarding_progress(intake, source + "\nOffer: A shared scheduling board", accepted_answers=accepted)
    assert accepted["offer"] == "A shared scheduling board"


@pytest.mark.parametrize("idea", ["Invoice matching for manufacturers", "Holiday planning for families"])
def test_board_review_has_an_exact_pro_route(idea):
    load_company_os_app()
    from llm_gateway import sole
    assert sole._resolved_tier("idea.board", "pro") == "pro"


@pytest.mark.parametrize("idea", ["Invoice matching for manufacturers", "Holiday planning for families"])
@pytest.mark.parametrize("action,target", [
    ("ceo_review", "ceo_review"), ("idea.pressure_test.prepare", "idea.pressure_test"),
])
def test_explicit_review_button_consumes_only_its_requested_card(monkeypatch, idea, action, target):
    import asyncio
    load_company_os_app()
    from app.chat import execute
    from app.chat.confirm import ConfirmationCard
    calls = []

    async def preview(*args, **kwargs):
        return {"kind": "confirmation", "body_md": idea,
                "actions": [ConfirmationCard(target, "cap.write.research", idea).as_action()]}

    async def start(venture_id, reply, actor_subject, **kwargs):
        calls.append(kwargs["actions"])
        return {"kind": "text", "body_md": "Your review is ready. Try the suggested test.", "actions": []}

    monkeypatch.setattr(execute, "_execute_action", preview)
    monkeypatch.setattr(execute, "start_requested_work", start)
    reply = asyncio.run(execute.execute_action("ven_fixture", action))
    assert calls == [{target}] and reply["kind"] != "confirmation"


@pytest.mark.parametrize("idea", ["Invoice matching for manufacturers", "Holiday planning for families"])
@pytest.mark.parametrize("mode", [None, "hosted", "local"])
def test_retry_preparation_starts_requested_work_only_in_local_mode(monkeypatch, idea, mode):
    import asyncio

    load_company_os_app()
    from app.chat import execute

    if mode is None:
        monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    else:
        monkeypatch.setenv("SUPYMEM_MODE", mode)
    prepared = {"kind": "text", "body_md": idea, "actions": [{"action": "job.retry"}]}
    started = {"kind": "text", "body_md": idea, "actions": [], "action_started": True}
    calls = []

    async def preview(*args, **kwargs):
        return prepared

    async def start(venture_id, reply, actor_subject, **kwargs):
        calls.append(kwargs["actions"])
        return started

    monkeypatch.setattr(execute, "_execute_action", preview)
    monkeypatch.setattr(execute, "start_requested_work", start)
    result = asyncio.run(execute.execute_action("ven_fixture", "job.retry"))
    assert calls == ([{"job.retry", "research.start", "plan.generate"}] if mode == "local" else [])
    assert result == (started if mode == "local" else prepared)


@pytest.mark.parametrize("idea", ["Invoice matching for manufacturers", "Holiday planning for families"])
def test_explicit_new_venture_intent_consumes_its_card(monkeypatch, idea):
    import asyncio
    load_company_os_app()
    from app.chat import router
    from app.chat.confirm import ConfirmationCard
    calls = []

    async def prepare(*args, **kwargs):
        return {"kind": "confirmation", "body_md": idea,
                "actions": [ConfirmationCard("venture.start_new", "cap.write.venture", idea).as_action()]}

    async def start(venture_id, reply, actor_subject, **kwargs):
        calls.append(kwargs["actions"])
        return {"kind": "text", "body_md": "Your venture is ready. Open its conversation.", "actions": []}

    monkeypatch.setattr(router, "_prepare_intent", prepare)
    monkeypatch.setattr("app.chat.execute.start_requested_work", start)
    reply = asyncio.run(router._dispatch_intent("ven_fixture", None, "new_venture", "Start a new venture: " + idea, None, None))
    assert calls == [{"venture.start_new"}] and reply["kind"] != "confirmation"


@pytest.mark.parametrize("idea", ["Invoice matching for manufacturers", "Holiday planning for families"])
def test_referral_help_uses_existing_signed_panel(env, idea):
    client, venture_id, _ = env
    from app.db.models import Venture
    from app.state import db
    with db()() as session:
        session.get(Venture, venture_id).raw_idea = idea
        session.commit()
    reply = _chat(client, venture_id, "How do I refer a friend?")
    from app.core.local_mode import is_local

    if is_local():
        assert "installer you used" in reply["body_md"]
        assert "not your app data folder or keys" in reply["body_md"]
        assert "Settings > Keys and connections" in reply["body_md"]
        assert "Your workspace stays private" in reply["body_md"]
        assert not any(action.get("action") == "referrals.show" for action in reply["actions"])
    else:
        assert any(action.get("action") == "referrals.show" for action in reply["actions"]), (reply["body_md"], reply["actions"])
        assert "Copy" in reply["body_md"]
    assert "credit" not in reply["body_md"].lower()


@pytest.mark.parametrize("label", ["Review invoice drafts", "Find people planning family walks"])
def test_unstarted_work_names_the_actual_next_step_and_button(monkeypatch, label):
    load_company_os_app()
    from app.chat import claims
    from app.chat.handlers import general

    actions = [{"label": label, "action": "gtm.status", "payload": {"show_saved": True}}]
    monkeypatch.setattr(general, "_next_step_actions", lambda _: actions)
    reply = claims.honest_reply("ven_fixture", "I have sent your email.")
    assert label in reply["body_md"] and "button below" in reply["body_md"] and reply["actions"] == actions
    assert "You can start the next step here" not in reply["body_md"]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/audit.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FIRSTLOOK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "mem.sqlite3"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("FIRSTLOOK_COPY_MODE", "fallback")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("CODING_AGENT", "inhouse_stub")
    main = load_company_os_app()
    configure_local_test_model()
    from app import state as state_mod
    from app.memory import reset_memory_for_tests

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    reset_memory_for_tests(tmp_path / "mem.sqlite3")
    with company_os_test_client(main.create_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        from app.core.local_mode import is_local
        if is_local():
            from core.local_security import _proxy_secret
            client.headers["X-Local-Proxy-Secret"] = _proxy_secret().decode("ascii")
        made = client.post("/ventures", json={"raw_idea": "a lobby lock waitlist for apartment buildings"})
        assert made.status_code == 201, made.text
        yield client, made.json()["id"], made.json()["workspace_id"]


def _chat(client, vid, text):
    r = client.post(f"/ventures/{vid}/chat", json={"message": text})
    assert r.status_code == 200, r.text
    return r.json()


def _confirm(client, vid, card):
    c = card["confirmation"]
    r = client.post(f"/ventures/{vid}/chat/confirm",
                    json={"action": c["action"], "payload": c.get("resulting_state") or {}})
    assert r.status_code == 200, r.text
    return r.json()


def _cards(msg):
    return [a for a in msg.get("actions") or [] if a.get("confirmation")]


def _intent(text):
    from app.chat.intent import classify_intent

    return classify_intent(text)


# ---------------------------------------------------------------- classification

CASES = [
    ("how do I pause?", "pause_help"),
    ("should I pause the venture?", "assistant"),
    ("pause the venture", "pause"),
    ("stop the venture", "pause"),
    ("freeze the company", "pause"),
    ("stop the mac task", "cancel"),
    ("cancel the build", "cancel"),
    ("draft a linkedin post about my launch", "content.draft"),
    ("write a tweet announcing the waitlist", "content.draft"),
    ("make content for linkedin", "content.draft"),
    ("generate content topics", "content.draft"),
    ("write 5 blog post ideas", "content.draft"),
    ("disconnect linkedin", "connections.disconnect"),
    ("share on reddit", "content.draft"),
    ("I posted it on reddit", "content.draft"),
    ("diagnose my go to market", "gtm_review"),
    ("run a diagnosis", "gtm_review"),
    ("run a gtm diagnosis for my gym app", "gtm_review"),
    ("close the waitlist", "waitlist.close"),
    ("turn off the waitlist", "waitlist.close"),
    ("disable signups", "waitlist.close"),
    ("how do I close the waitlist?", "assistant"),
    ("from now on call yourself Jarvis", "manager.rename"),
    ("what is your name?", "assistant"),
]


def test_classification():
    load_company_os_app()
    from app.chat.intent import classify_intent

    wrong = {text: got for text, expected in CASES if (got := classify_intent(text)) != expected}
    assert not wrong


def test_reading_the_saved_diagnosis_and_publishing_still_route_as_before():
    load_company_os_app()
    assert _intent("gtm diagnosis") == "gtm.diagnosis"
    assert _intent("post to linkedin: hello") == "social_post"
    assert _intent("open the waitlist") == "waitlist.enable"


# ---------------------------------------------------------------- G7: drafting is not a web search

def test_drafting_request_never_runs_a_web_search(env, monkeypatch):
    client, vid, _ = env
    from app.research import explore

    async def boom(*a, **k):
        raise AssertionError("web search must not run")

    monkeypatch.setattr(explore, "explore_web", boom)
    for text in ("draft a linkedin post about my launch", "share on reddit", "disconnect linkedin"):
        msg = _chat(client, vid, text)
        if text == "disconnect linkedin":
            assert not _cards(msg) and "LinkedIn is not connected" in msg["body_md"], text
        else:
            assert not _cards(msg), text  # a post is written here, never searched for


# ---------------------------------------------------------------- E12: never publish the instruction

def test_post_request_takes_text_not_the_command():
    load_company_os_app()
    from app.chat.posts import post_request

    assert post_request("post this on linkedin") == ("", ["linkedin"])
    assert post_request("tweet this") == ("", ["twitter"])
    assert post_request("post to linkedin: We open early access today") == ("We open early access today", ["linkedin"])
    assert post_request('post on linkedin "Early access opens today"') == ("Early access opens today", ["linkedin"])


def _enable_posting(monkeypatch):
    from app.core.local_mode import is_local

    monkeypatch.setenv("SUPYMEM_SOCIAL_POST_ENABLED", "1")
    if is_local():
        from app.core.local_secrets import update_settings

        update_settings("composio", {"auth_configs": {"linkedin": "offline-linkedin-auth"}}, {"api_key": "offline-project-key"})
        update_settings("capabilities", {"connected_apps": True}, {})


def test_post_without_text_asks_for_it_and_shows_no_card(env, monkeypatch):
    from app.core.local_mode import is_local

    client, vid, _ = env
    _enable_posting(monkeypatch)
    msg = _chat(client, vid, "post this on linkedin")
    if is_local():
        from app.core.local_capabilities import blocked_message

        # 77be6a460 excludes social posting even when an app is connected.
        assert blocked_message("social_posting") in msg["body_md"] and not msg["actions"]
    else:
        assert not _cards(msg) and "exact text" in msg["body_md"] and "nothing was posted" in msg["body_md"]


def test_posting_default_off_offers_no_card_or_external_send(env, monkeypatch):
    client, vid, _ = env
    from app.actuation import execute as actuation

    async def forbidden(*args, **kwargs):
        raise AssertionError("disabled posting must not reach external actuation")

    monkeypatch.delenv("SUPYMEM_SOCIAL_POST_ENABLED", raising=False)
    monkeypatch.setattr(actuation, "execute", forbidden)
    msg = _chat(client, vid, "post this on linkedin")
    assert not _cards(msg)
    from app.core.local_mode import is_local

    if is_local():
        from app.core.local_capabilities import blocked_message

        assert blocked_message("social_posting") in msg["body_md"] and not msg["actions"]
    else:
        from app.chat.actions import SOCIAL_POST_COMING_SOON

        assert SOCIAL_POST_COMING_SOON["body_md"] in msg["body_md"] and not msg["actions"]


@pytest.mark.asyncio
async def test_forged_social_confirm_cannot_post_the_instruction(env, monkeypatch):
    _, vid, _ = env
    _enable_posting(monkeypatch)
    from app.actuation import execute as actuation, social
    from app.chat.execute import execute_action
    from app.core.local_mode import is_local

    async def boom(*a, **k):
        raise AssertionError("must not post")

    monkeypatch.setattr(actuation, "execute", boom)
    monkeypatch.setattr(social, "founder_social_connected", lambda v: True)
    out = await execute_action(vid, "social_post", payload={"query": "post this on linkedin"})
    if is_local():
        from app.core.local_capabilities import blocked_message

        assert out["body_md"] == blocked_message("social_posting") and not out["actions"]
    else:
        assert "nothing was posted" in out["body_md"]


# ---------------------------------------------------------------- G11: approvals are scoped and named

@pytest.fixture()
def approver(env, monkeypatch):
    from app.approvals import service

    monkeypatch.setattr(service, "_can_approve", lambda *a, **k: True)
    return env


def _approval(wid, vid, summary, venture=None):
    from app.approvals.service import request_approval

    return request_approval(workspace_id=wid, kind="proposal", subject_ref=f"prop_{summary[:4]}", summary=summary,
                            requested_by="person_1", detail={"venture_id": venture or vid})


def test_approve_shows_what_and_ignores_other_ventures(approver):
    client, vid, wid = approver
    _approval(wid, vid, "Send the pilot outreach batch")
    _approval(wid, vid, "Other venture pricing change", venture="ven_someone_else")
    msg = _chat(client, vid, "approve the pending item")
    assert not _cards(msg)
    assert "Other venture" not in msg["body_md"]
    done = msg
    assert "Recorded **approved**" in done["body_md"] and "Send the pilot outreach batch" in done["body_md"]
    assert "apr_" not in done["body_md"]
    from app.approvals.service import list_approvals

    assert [a.summary for a in list_approvals(wid, status="pending")] == ["Other venture pricing change"]


def test_ambiguous_approve_asks_which_and_decides_only_the_chosen(approver):
    client, vid, wid = approver
    first = _approval(wid, vid, "First item to decide")
    _approval(wid, vid, "Second item to decide")
    msg = _chat(client, vid, "approve the pending item")
    cards = _cards(msg)
    assert len(cards) == 2 and "Which" in msg["body_md"]
    chosen = next(c for c in cards if c["confirmation"]["resulting_state"]["approval_id"] == first.id)
    _confirm(client, vid, chosen)
    from app.approvals.service import list_approvals

    assert [a.summary for a in list_approvals(wid, status="pending")] == ["Second item to decide"]


def test_unqualified_confirm_with_two_pending_decides_nothing(approver):
    client, vid, wid = approver
    _approval(wid, vid, "One")
    _approval(wid, vid, "Two")
    msg = _chat(client, vid, "approve the pending item")
    c = msg["actions"][0]["confirmation"]
    done = client.post(f"/ventures/{vid}/chat/confirm", json={"action": "approve", "payload": {"query": "approve the pending item"}}).json()
    assert "did not pick one" in done["body_md"] and c
    from app.approvals.service import list_approvals

    assert len(list_approvals(wid, status="pending")) == 2


def test_approve_with_nothing_pending_says_so(env):
    client, vid, _ = env
    msg = _chat(client, vid, "approve the pending item")
    assert not _cards(msg) and "No pending approvals" in msg["body_md"]


# ---------------------------------------------------------------- G10: cancel stops builds, is honest otherwise

def test_requested_build_cancel_uses_the_scoped_authenticated_actor(env, monkeypatch):
    client, vid, _ = env
    from app.builder import execution as builds
    from app.chat import stop_work

    monkeypatch.setattr(stop_work, "active_build", lambda v: ("run_1", "app_1"))
    cancelled = []
    monkeypatch.setattr(builds, "cancel", lambda run_id, actor: cancelled.append((run_id, actor)) or {"state": "cancelling"})
    monkeypatch.setattr(stop_work, "_run_scope", lambda run_id: (vid, "app_1"))
    from app.authz.workspace import LOCAL_PERSON_ID

    client.headers["X-Subject"] = "local"
    requested = _chat(client, vid, "cancel the build")
    assert not _cards(requested) and "stopping" in requested["body_md"]
    assert cancelled == [("run_1", LOCAL_PERSON_ID)]
    cancelled.clear()
    out = stop_work.run_build_cancel(vid, {"run_id": "run_1"}, "person_1")
    assert cancelled == [("run_1", "person_1")] and "stopping" in out["body_md"]
    monkeypatch.setattr(stop_work, "_run_scope", lambda run_id: ("ven_other", "app_1"))
    with pytest.raises(KeyError):
        stop_work.run_build_cancel(vid, {"run_id": "run_1"}, "person_1")


def test_cancel_with_nothing_running_and_unstoppable_work_is_honest(env, monkeypatch):
    client, vid, _ = env
    from app.chat import stop_work
    from app.core.local_mode import is_local

    monkeypatch.setattr(stop_work, "_active_mac_href", lambda v: None)
    monkeypatch.setattr(stop_work, "_research_running", lambda v: False)
    assert "No build is running" in _chat(client, vid, "cancel the build")["body_md"]
    assert "No Mac task is open" in _chat(client, vid, "stop the mac task")["body_md"]
    assert "No research is running" in _chat(client, vid, "cancel the research")["body_md"]
    monkeypatch.setattr(stop_work, "_active_mac_href", lambda v: f"/v/{vid}/device-work-orders/dwo_1")
    monkeypatch.setattr(stop_work, "_research_running", lambda v: True)
    mac = _chat(client, vid, "cancel the mac task")
    assert "can't cancel a Mac task from chat" in mac["body_md"] and mac["actions"][0]["href"].endswith("dwo_1")
    research = _chat(client, vid, "cancel the research")
    assert research["body_md"].split("-->\n", 1)[-1] == (
        "Stopping research now. It stops before its next step; anything already in progress "
        "finishes, and the sources found so far stay saved.")
    # ed24fe27b preserves links to the local Research screen.
    assert research["actions"] == ([{"label": "Open research", "href": f"/v/{vid}/research", "style": "primary"}]
                                   if is_local() else [])


def test_cancel_research_requests_cancellation_only_for_venture_active_research(env):
    client, vid, wid = env
    from app.core.local_mode import is_local
    from app.db.models import Job
    from app.state import db
    from core.clock import utcnow

    jobs = [("job_research_leased", "research.deep", vid, "leased"),
            ("job_research_running", "research.deep", vid, "running"),
            ("job_research_pending", "research.deep", vid, "pending"),
            ("job_research_finished", "research.deep", vid, "succeeded"),
            ("job_research_other", "research.deep", "ven_other", "leased"),
            ("job_plan_leased", "plan.compile", vid, "leased")]
    with db()() as session:
        for job_id, kind, venture_id, status in jobs:
            session.add(Job(id=job_id, kind=kind, venture_id=venture_id, workspace_id=wid,
                            status=status, payload_json="{}", created_at=utcnow()))
        session.commit()

    reply = _chat(client, vid, "cancel the research")
    assert "Stopping research now." in reply["body_md"]
    assert reply["actions"] == ([{"label": "Open research", "href": f"/v/{vid}/research", "style": "primary"}]
                                if is_local() else [])
    with db()() as session:
        for job_id, kind, venture_id, status in jobs:
            saved = session.get(Job, job_id)
            expected = kind == "research.deep" and venture_id == vid and status != "succeeded"
            assert saved.cancel_requested is expected
            assert saved.status == ("cancelled" if expected and status == "pending" else status)


# ---------------------------------------------------------------- G3 / G4 / D9: the managed waitlist

def test_close_waitlist_when_none_exists_claims_nothing(env):
    client, vid, _ = env
    msg = _chat(client, vid, "close the waitlist")
    assert all(c["confirmation"]["action"] != "waitlist.close" for c in _cards(msg))
    assert "not on" in msg["body_md"] and "has been closed" not in msg["body_md"]


def test_requested_waitlist_close_leaves_reopening_unstarted(env):
    client, vid, wid = env
    from app.conversion import waitlist

    waitlist.configure(vid, wid, enabled=True, actor="person_1")
    done = _chat(client, vid, "close the waitlist")
    assert all(card["confirmation"]["action"] != "waitlist.close" for card in _cards(done))
    assert "waitlist is closed" in done["body_md"] and waitlist.status(vid, wid)["enabled"] is False
    again = _chat(client, vid, "turn off the waitlist")
    assert all(c["confirmation"]["action"] != "waitlist.close" for c in _cards(again)) and "not on" in again["body_md"]


def test_who_signed_up_reads_the_managed_waitlist(env):
    client, vid, wid = env
    from app.conversion import waitlist
    from app.db.supymem_models import VentureWaitlistEntry
    from app.state import db
    from core.clock import utcnow

    waitlist.configure(vid, wid, enabled=True, actor="person_1")
    with db()() as s:
        s.add(VentureWaitlistEntry(id="vwe_1", venture_id=vid, workspace_id=wid, email="ada@example.com",
                                   consent_version=waitlist.VERSION, consent_text="c", is_test=False, created_at=utcnow()))
        s.add(VentureWaitlistEntry(id="vwe_2", venture_id=vid, workspace_id=wid, email="probe@qa.invalid",
                                   consent_version=waitlist.VERSION, consent_text="c", is_test=True, created_at=utcnow()))
        s.commit()
    from app.chat.execute import _leads

    body = _leads(vid)["body_md"]
    assert "1 waitlist signup" in body and "ada@example.com" in body and "qa.invalid" not in body


# ---------------------------------------------------------------- D1 / D12 / D5: a published site is described as published

def test_preview_and_revenue_answers_know_the_site_is_live(env, monkeypatch):
    client, vid, wid = env
    from app.planning import publication_evidence

    monkeypatch.setattr(publication_evidence, "public_site_evidence",
                        lambda *a, **k: {"source": "deployment", "id": "dep_1", "url": "https://lobby.example/", "observed_at": None})
    msg = _chat(client, vid, "show me the landing page")
    assert "live at https://lobby.example/" in msg["body_md"] and "not public" not in msg["body_md"]
    rev = _chat(client, vid, "how much revenue do we have?")["body_md"]
    assert "local preview" not in rev and ".;" not in rev
    assert "No verified live customer payment is recorded here" in rev and "net earnings are unconfirmed" in rev


def test_revenue_answer_while_unpublished_still_says_local_preview(env):
    client, vid, _ = env
    rev = _chat(client, vid, "how many visitors do we have?")["body_md"]
    assert "local preview" in rev and ".;" not in rev


def test_deployment_milestones_post_once_with_plain_copy(env):
    client, vid, _ = env
    from app.chat.milestone_hooks import deployment_failed, deployment_live

    deployment_live(vid, "dep_1", "https://lobby.example/")
    deployment_live(vid, "dep_1", "https://lobby.example/")
    deployment_failed(vid, "dep_2", "hosting_public_copy_unreviewed")
    from app.chat.store import list_messages

    bodies = [m["body_md"] for m in list_messages(vid)]
    assert sum("is live at https://lobby.example/" in b for b in bodies) == 1
    failed = next(b for b in bodies if "Publishing did not finish" in b)
    assert "hosting_" not in failed
    from app.core.local_mode import is_local

    if is_local():
        assert "I couldn't confirm that your page was published" in failed
        assert "Open Website" in failed and "before trying again" in failed
    else:
        assert "reviewed" in failed


def test_site_replies_never_print_codes():
    load_company_os_app()
    from app.chat.site_actions import plain_hosting, plain_waitlist

    for code in ("hosting_public_copy_unreviewed", "hosting_provider_unavailable", "hosting_something_new"):
        assert "hosting_" not in plain_hosting(code)
    for code in ("waitlist_probe_recent", "waitlist_something_new"):
        assert "waitlist_" not in plain_waitlist(code)


# ---------------------------------------------------------------- E14 / F6

@pytest.mark.parametrize("idea", ["Stock checks for warehouse teams", "Watering reminders for home gardeners"])
def test_status_replies_use_plain_words(env, monkeypatch, idea):
    client, vid, _ = env
    from app.chat import execute

    monkeypatch.setattr(execute, "_brief_or_empty", lambda v, force=False: {
        "idea": idea,
        "connections": {"gmail": {"connected": False}, "other": [{"provider": "composio_linkedin", "status": "ACTIVE"}]},
        "content_drafts": [{"platform": "linkedin", "state": "review"}, {"state": None}]})
    monkeypatch.setattr("app.connections.hub.hub_status", lambda workspace_id: {"order": ["gmail", "linkedin"], "connections": {
        "gmail": {"connected": False}, "linkedin": {"connected": True, "ready": True}}})
    conns = execute._connections_status(vid)["body_md"]
    assert "**LinkedIn**: Connected" in conns and "=" not in conns
    drafts = execute._content_drafts(vid)["body_md"]
    assert "Draft for LinkedIn, waiting for your review" in drafts and "?" not in drafts and "(" not in drafts


@pytest.mark.asyncio
async def test_recording_a_not_run_result_with_no_experiment_does_not_crash(env, monkeypatch):
    _, vid, _ = env
    from app.chat import experiments

    async def not_run(venture_id, text, hypothesis, action, success_measure):
        return {"ran": False}

    monkeypatch.setattr(experiments, "extract", not_run)
    out = await experiments.record_prepare(vid, "nobody replied yet, we haven't run the second test")
    assert "has not been run yet" in out["body_md"]


# ---------------------------------------------------------------- G1: rename is real; no false claims

def test_requested_rename_persists_everywhere_without_another_confirmation(env):
    client, vid, _ = env
    from app.org.manager import manager_name

    assert manager_name(vid) == "Charlie"
    done = _chat(client, vid, "from now on call yourself Jarvis")
    assert not _cards(done)
    assert manager_name(vid) == "Jarvis" and "I'm Jarvis now" in done["body_md"]
    assert "Jarvis" in _chat(client, vid, "what is your name?")["body_md"]


def test_a_reply_cannot_claim_a_persona_or_setting_change():
    load_company_os_app()
    from app.chat import claims

    assert claims.claims_action("Understood. I will respond as Jarvis from now on.")
    assert claims.claims_action("The managed waitlist has been closed.")
    reply = claims.honest_reply("ven_x", "Understood. I will respond as Jarvis from now on.")
    assert "not changed anything" in reply["body_md"]
    assert not claims.claims_action("Do you want me to rename myself?")


# ---------------------------------------------------------------- B3: nonsense is not an answer

def test_noise_detector():
    load_company_os_app()
    from app.chat.guard import looks_like_noise

    assert looks_like_noise("asdf qwerty lol") and looks_like_noise("asdfgh")
    assert not looks_like_noise("landlords with small buildings")
    assert not looks_like_noise("idk")
    assert not looks_like_noise("we use qwerty keyboards in our product for small clinics")


def _saved_clarification(env, *, chat_entry, question="What do landlords use today?", field="current_solution", options=("Spreadsheet",)):
    """A real owned session/question and saved typed chat card, without a provider call."""
    from sqlalchemy import func, select

    from app.chat.store import append_message
    from app.db.models import Workspace
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    client, vid, wid = env
    opened = client.post(f"/ventures/{vid}/idea/sessions")
    assert opened.status_code == 201, opened.text
    sid, qid = opened.json()["id"], new_id("ict")
    with db()() as session:
        workspace = session.get(Workspace, wid)
        workspace.settings_json = json.dumps({**json.loads(workspace.settings_json or "{}"),
            "chat_entry_defer_clarification": chat_entry, "entry_goal": "startup"})
        idea = session.get(IdeaSession, sid)
        assert (idea.venture_id, idea.workspace_id) == (vid, wid)
        turn = (session.scalar(select(func.max(ClarifierTurn.turn)).where(ClarifierTurn.session_id == sid)) or 0) + 1
        session.add(ClarifierTurn(id=qid, session_id=sid, workspace_id=wid, venture_id=vid, turn=turn,
            role="agent", kind="question", text=question,
            options=json.dumps({"field": field, "options": list(options)}), created_at=utcnow()))
        session.commit()
    chips = [{"label": "Spreadsheet", "action": "chat.reply", "text": "Spreadsheet",
              "payload": {"question_id": qid}, "style": "secondary"}]
    append_message(vid, role="assistant", body_md=question, actions=chips, kind="idea_clarification_ready")
    return sid, qid, chips


@pytest.mark.parametrize("idea,audience,answer", [
    ("A service that reconciles supplier invoices", "factory finance teams",
     "Not sure, maybe factory finance teams could try it first because they handle disputed invoices?"),
    ("An app that helps families plan accessible holidays", "parents planning holidays",
     "I don't know, maybe parents planning holidays could try it first because they need accessible activities?"),
])
def test_tentative_question_mark_reply_is_saved_as_the_pending_answer(env, monkeypatch, idea, audience, answer):
    from sqlalchemy import select
    from app.chat.idea_bridge import pending_question
    from app.db.models import Venture
    from app.db.supymem_models import ClarifierTurn
    from app.state import db

    client, vid, _ = env
    with db()() as session:
        session.get(Venture, vid).raw_idea = idea
        session.commit()
    sid, qid, _ = _saved_clarification(env, chat_entry=True,
        question=f"Who among {audience} would try this first and why?", field="first_user_path")
    for request in ("Who should I ask first?", "Which audience should I choose?", "Can you help me decide?",
                    "Could you suggest a first customer?", "Should I ask someone else?", "Tell me what to do?",
                    f"Can {audience} benefit from this?", "Could this help someone else?", "Please, can you advise me?",
                    "Do you have another suggestion?", "Write a launch plan", "Start a new venture: a gardening service"):
        assert pending_question(vid, request) is None
    for tentative in (f"Maybe {audience}?", f"Could be {audience}?"):
        assert pending_question(vid, tentative) == (sid, qid)
    assert pending_question(vid, answer) == (sid, qid)
    monkeypatch.setenv("CHAT_ROUTER_MODE", "off")
    reply = _chat(client, vid, answer)
    assert reply["kind"] == "idea_clarification_pending"
    with db()() as session:
        saved = session.scalars(select(ClarifierTurn).where(ClarifierTurn.session_id == sid,
            ClarifierTurn.role == "founder", ClarifierTurn.text == answer)).all()
        assert len(saved) == 1 and saved[0].kind == "answer"
        question = session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == sid,
            ClarifierTurn.role == "agent", ClarifierTurn.turn < saved[0].turn)
            .order_by(ClarifierTurn.turn.desc()).limit(1))
        assert question.id == qid
    assert pending_question(vid, answer) is None


@pytest.mark.parametrize("idea,audience,command,guard_copy", [
    ("A service that schedules repair appointments", "independent repair shop owners",
     "send 40 emails to independent repair shop owners in Raleigh today", "won't blast"),
    ("A photo-sharing app for hiking clubs", "hiking club members",
     "text 40 users of hiking clubs in Porto today", "don't automate"),
])
def test_filler_prefixed_requests_never_answer_a_pending_gstack_question(env, monkeypatch, idea, audience, command, guard_copy):
    from sqlalchemy import select
    from app.chat.idea_bridge import pending_question
    from app.db.models import Job, Venture
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.gstack.office_hours import PHASE
    from app.state import db

    client, vid, _ = env
    with db()() as session:
        session.get(Venture, vid).raw_idea = idea
        session.commit()
    sid, qid, _ = _saved_clarification(env, chat_entry=True,
        question=f"What motivates {audience} to try this first?", field="first_user_path")
    with db()() as session:
        question = session.get(ClarifierTurn, qid)
        question.options = json.dumps({**json.loads(question.options), "phase": PHASE,
            "gstack_id": "Q3", "gstack_name": "Customer motivations", "queue": []})
        session.get(IdeaSession, sid).state = "validating"
        session.commit()
        jobs_before = session.query(Job).filter(Job.kind.in_(("idea.clarify_turn", "idea.validate"))).count()
    requests = (command, f"write a launch plan for {audience} in the city today",
        f"search for alternatives used by {audience} in the city today",
        f"find ten potential customers among {audience} in the city today",
        f"build an invitation page for {audience} in the city today",
        f"post an invitation for {audience} in the city today",
        f"email one invitation to {audience} in the city today",
        f"mass-message 40 people among {audience} in the city today",
        f"I would like to email one invitation to {audience}",
        f"I need you to post an invitation for {audience}",
        f"I need you to build an invitation page for {audience}",
        f"I want you to find ten contacts among {audience}",
        f"go ahead and search for alternatives used by {audience}",
        "go ahead and tell me what to do next",
        "can you suggest a first customer?", "tell me what to do next")
    for filler in ("just", "ok", "okay", "so", "please", "hey", "um", "uh", "hmm", "well", "alright",
                   "yes", "like", "and", "ah", "erm", "ok, so um, please,"):
        for request in requests:
            assert pending_question(vid, f"{filler} {request}") is None
    for answer in (f"Um, maybe {audience} would try it first because they want less paperwork?",
                   f"Well, let's start with {audience} because they want fewer missed appointments?",
                   f"They send 40 emails to {audience} after every meeting."):
        assert pending_question(vid, answer) == (sid, qid)
    assert pending_question(vid, "Spreadsheet", force=True) == (sid, qid)
    monkeypatch.setenv("CHAT_ROUTER_MODE", "off")
    text = f"um, so please {command}"
    reply = _chat(client, vid, text)
    assert guard_copy in reply["body_md"].lower()
    with db()() as session:
        assert session.scalar(select(ClarifierTurn.id).where(ClarifierTurn.session_id == sid,
            ClarifierTurn.role == "founder", ClarifierTurn.text == text)) is None
        latest = session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == sid)
            .order_by(ClarifierTurn.turn.desc()).limit(1))
        assert latest.id == qid
        assert session.query(Job).filter(Job.kind.in_(("idea.clarify_turn", "idea.validate"))).count() == jobs_before


@pytest.mark.parametrize("text,is_answer", [
    ("Write me a detailed 10-point launch plan", False),
    ("Please, make a launch checklist for the next month", False),
    ("Could you draft outreach emails to apartment owners", False),
    ("I want to build a website for the venture", False),
    ("Plan our launch for next week", False),
    ("Research competitors and pricing for this business", False),
    ("Find ten potential customers in my city", False),
    ("My favourite dinner is pasta with tomato sauce", False),
    ("The weather is sunny today", False),
    ("Spreadsheet", True),
    ("Paper lists", True),
    ("I don't know", True),
    ("Landlords use spreadsheets to track tenants and repair requests", True),
    ("They use paper notebooks and call tenants one at a time", True),
    ("We currently track tenant requests in Excel and send reminders by email", True),
])
def test_only_plausible_messages_answer_the_saved_question(env, monkeypatch, text, is_answer):
    from sqlalchemy import select

    client, vid, _ = env
    from app.chat.idea_bridge import pending_question
    from app.db.supymem_models import ClarifierTurn
    from app.state import db

    sid, qid, _ = _saved_clarification(env, chat_entry=True, options=("Spreadsheet", "Paper lists"))
    assert pending_question(vid, text) == ((sid, qid) if is_answer else None)
    if not is_answer:
        monkeypatch.setenv("CHAT_ROUTER_MODE", "off")
        reply = _chat(client, vid, text)
        assert reply["kind"] != "idea_clarification_pending"
        assert pending_question(vid, "Spreadsheet") == (sid, qid)
        with db()() as session:
            assert session.scalar(select(ClarifierTurn.id).where(
                ClarifierTurn.session_id == sid, ClarifierTurn.role == "founder", ClarifierTurn.text == text)) is None


@pytest.mark.parametrize("has_tasks", [False, True])
@pytest.mark.parametrize("idea", ["Inventory alerts for warehouse operators", "Walking groups for neighborhood families"])
def test_next_tasks_reply_is_plain_and_links_to_the_working_page(env, monkeypatch, has_tasks, idea):
    client, _, _ = env
    vid = client.post("/ventures", json={"raw_idea": idea, "start": False}).json()["id"]
    from app.chat import execute
    from app.planning import store
    from app.db.models import Job
    from app.state import db
    from sqlalchemy import select

    monkeypatch.setattr(store, "list_open_tasks", lambda venture_id: [{"title": "Interview landlords"}] if has_tasks else [])
    with db()() as session:
        before = set(session.scalars(select(Job.id).where(Job.venture_id == vid)))
    reply = execute._whats_next(vid)
    with db()() as session:
        assert set(session.scalars(select(Job.id).where(Job.venture_id == vid))) == before
    assert "slate" not in reply["body_md"].lower() and "desk" not in reply["body_md"].lower()
    # Chat-first: Tasks and Plan pages are hidden, so the founder gets an in-chat action, never a link there.
    from app.chat.navigation_map import in_chat
    buttons = in_chat(reply["actions"])
    assert all("href" not in button for button in buttons)
    assert all(button["action"] in {"status", "startup.review"}
               or (button["action"] in {"idea.review", "ceo_review", "research.prepare", "whats_next"}
                   and button["payload"] == {})
               or (button["action"] == "chat.reply" and (button.get("payload") in (
                   {"text": "Skip the questions"}, {"text": "I'd like to share my open questions."})
                   or (isinstance((button.get("payload") or {}).get("question_id"), str)
                       and button["payload"]["question_id"].strip())))
               or (button["action"] == "chat.prefill" and isinstance((button.get("payload") or {}).get("text"), str)
                   and (button["payload"]["text"].strip()))
               or (button["action"] in {"gtm.find", "pagebuilder.build"} and button["payload"].get("prepare") is True)
               for button in buttons), buttons
    assert reply["kind"] == "next_moves"
    from app.core.local_mode import is_local

    if is_local():
        assert reply["state_hash"]
        assert all(button["state_hash"] == reply["state_hash"] for button in buttons)


@pytest.mark.parametrize("idea", ["Inventory alerts for warehouse operators", "Walking groups for neighborhood families"])
def test_next_step_keeps_saved_question_optional_without_tasks(env, monkeypatch, idea):
    from app.chat import execute
    from app.planning import store
    from app.ventures import startup_review

    client, _, _ = env
    created = client.post("/ventures", json={"raw_idea": idea, "start": False}).json()
    vid = created["id"]
    _saved_clarification((client, vid, created["workspace_id"]), chat_entry=True,
                         question="What do your first users use now?")
    monkeypatch.setattr(store, "list_open_tasks", lambda venture_id: [])
    monkeypatch.setattr(startup_review, "enabled", lambda *_args: False)
    reply = execute._whats_next(vid)
    assert not any(action.get("href") for action in reply["actions"])
    # The question is already on screen, so no chip repeats it.
    assert not any(action["action"] == "idea.review" for action in reply["actions"])
    assert "Use the question or review already in your chat" in reply["body_md"]


def test_task_edit_replies_translate_display_text_not_stored_values(env, monkeypatch):
    client, vid, _ = env
    from app.chat import brief, execute
    from app.will import override

    original = override.parse_override
    parsed_values = []

    def parsed(*args, **kwargs):
        result = original(*args, **kwargs)
        parsed_values.append(result)
        return result

    monkeypatch.setattr(override, "parse_override", parsed)
    offered = _chat(client, vid, "change the slate")
    assert not _cards(offered)
    assert "slate" not in offered["body_md"].lower()
    reply = execute._slate_override(vid, {"query": "change the slate"})
    assert "slate" not in reply["body_md"].lower()
    assert reply["actions"][0]["href"] == f"/v/{vid}/tasks"
    assert parsed_values and "slate" in parsed_values[-1].interpretation
    assert "slate" not in brief.render_brief({"name": "Example"}).lower()


@pytest.mark.parametrize("question", ["What do warehouse operators use today?", "What do parents use to plan dinners?"])
def test_nonsense_answer_parks_the_question_without_saving_junk(env, monkeypatch, question):
    client, vid, _ = env
    from app.chat.idea_bridge import pending_question
    from app.idea import service

    sid, qid, _ = _saved_clarification(env, chat_entry=True, question=question)
    assert pending_question(vid, "asdf qwerty lol") is None
    monkeypatch.setattr(service, "post_message", lambda *a, **k: (_ for _ in ()).throw(AssertionError("saved junk")))
    msg = _chat(client, vid, "asdf qwerty lol")
    assert "didn't catch that" in msg["body_md"] and question not in msg["body_md"]
    assert [action["action"] for action in msg["actions"]] == ["idea.review"]
    assert pending_question(vid, "Spreadsheet") == (sid, qid)


@pytest.mark.parametrize("question", ["What do warehouse operators use today?", "What do parents use to plan dinners?"])
def test_nonsense_answer_parks_even_when_question_is_not_pending_in_idea_bridge(env, question):
    client, vid, _ = env
    from app.chat.idea_bridge import pending_question
    from app.chat.store import list_messages

    _saved_clarification(env, chat_entry=False, question=question)
    assert pending_question(vid, "asdf qwerty lol") is None
    msg = _chat(client, vid, "asdf qwerty lol")
    assert "didn't catch that" in msg["body_md"] and question not in msg["body_md"]
    assert [action["action"] for action in msg["actions"]] == ["idea.review"]
    saved = list_messages(vid, limit=5)
    assert [m["role"] for m in saved[-2:]] == ["user", "assistant"] and saved[-2]["body_md"] == "asdf qwerty lol"


# ---------------------------------------------------------------- experiment results: exact founder phrasings

CALLS = "I called 5 roasters. 2 said they run out of beans every few months and would pay $29/month for a warning. 3 said a spreadsheet is fine."
RESULT = "The test result: 2 of 5 roasters said they would pay $29/month, 3 said a spreadsheet is fine."


def test_result_reports_and_log_followups_are_experiment_records():
    load_company_os_app()
    from app.chat.intent import classify_intent

    for text in (CALLS, RESULT, "yes, log the results", "log them", "Yes, log them.", "go ahead and log it"):
        assert classify_intent(text) == "experiment_record", text
    for text in ("should I log the results?", "what did we learn?", "how many roasters should I call?"):
        assert classify_intent(text) != "experiment_record", text


def _open_experiment(vid, hypothesis):
    from app.experiments import ledger

    ledger.create_from_tests(vid, "chat", [{"hypothesis": hypothesis}])


@pytest.fixture()
def extracted(monkeypatch):
    from app.chat import experiments

    async def fake(venture_id, text, hypothesis, action, success_measure):
        return {"ran": True, "outcome": "inconclusive", "learning": text}

    monkeypatch.setattr(experiments, "extract", fake)


@pytest.mark.parametrize("audience,amount", [("independent repair shops", "$29/month"), ("weekend hikers", "$11/month")])
@pytest.mark.parametrize("template", [CALLS, RESULT])
def test_reported_calls_keep_the_founders_dollar_amount_without_a_card(env, extracted, audience, amount, template):
    from app.experiments import ledger

    client, vid, _ = env
    _open_experiment(vid, f"{audience} would pay {amount} for timely reminders")
    text = template.replace("roasters", audience).replace("$29/month", amount)
    msg = _chat(client, vid, text)
    assert not _cards(msg) and "Add evidence" in msg["body_md"]
    assert amount in msg["body_md"] and "[amount unavailable]" not in msg["body_md"]
    (row,) = ledger.list_for_venture(vid)
    assert row["state"] == "running" and row["outcome"] is None and row["needs_evidence"]
    assert row["result_text"] == text
    assert amount in row["learning"]


@pytest.mark.parametrize("audience,amount", [("independent repair shops", "$29/month"), ("weekend hikers", "$11/month")])
@pytest.mark.parametrize("followup", ["yes, log the results", "log them"])
def test_log_followup_records_the_previous_message_not_nothing(env, extracted, audience, amount, followup):
    client, vid, _ = env
    _open_experiment(vid, f"{audience} would pay {amount} for timely reminders")
    from app.chat.store import append_message

    result = RESULT.replace("roasters", audience).replace("$29/month", amount)
    append_message(vid, role="user", body_md=result, kind="text")
    append_message(vid, role="assistant", body_md="Do you want to log the call outcomes?", kind="text")
    msg = _chat(client, vid, followup)
    assert not _cards(msg) and "Add evidence" in msg["body_md"]
    assert "Tell me what you did" not in msg["body_md"] and "don't have results" not in msg["body_md"]
    from app.experiments import ledger

    (row,) = ledger.list_for_venture(vid)
    assert row["state"] == "running" and row["outcome"] is None and row["needs_evidence"]
    assert row["result_text"] == result
    assert amount in row["learning"]


def test_log_followup_with_nothing_to_log_asks_for_the_result(env, extracted):
    from app.experiments import ledger

    client, vid, _ = env
    before = ledger.list_for_venture(vid)
    msg = _chat(client, vid, "log them")
    assert not _cards(msg) and "Open Experiments, choose Record result" in msg["body_md"]
    assert "who you asked and what they said" in msg["body_md"]
    assert ledger.list_for_venture(vid) == before


def test_founder_amounts_survive_the_l1_scrub_but_invented_ones_do_not():
    load_company_os_app()
    from types import SimpleNamespace

    from app.chat.router import scrub_hallucinations

    ctx = SimpleNamespace(governance={"autonomy": "L1"}, website={})
    out = scrub_hallucinations("They would pay $29/month, or maybe $99.", ctx, known="2 of 5 would pay $29/month")
    assert "$29/month" in out and "$99" not in out and "[amount unavailable]" in out


@pytest.mark.parametrize("audience,amount", [("industrial equipment buyers", "$460"), ("families learning pottery", "$38")])
def test_proposed_product_prices_survive_without_allowing_claimed_payments(audience, amount):
    load_company_os_app()
    from types import SimpleNamespace
    from app.chat.router import scrub_hallucinations
    from app.chat.store import _clean

    ctx = SimpleNamespace(governance={"autonomy": "L1"}, website={})
    for label in ("**Proposed price:", "**Proposed paid pilot:** Charge **"):
        proposal = f"{label} {amount} upfront** for a one-week pilot for {audience}."
        body = proposal + " They already paid $913.\nNo payment is recorded. Ask a buyer to pay by Friday."
        result = scrub_hallucinations(body, ctx)
        assert proposal in result and "$913" not in result
        assert "Ask a buyer to pay by Friday." in result
        visible = _clean(result + "\nThis uses 17 credits.", assistant=True)
        assert amount in visible and "17 credits" not in visible
    assert "$913" not in scrub_hallucinations("Your platform usage costs $913. Open Settings.", ctx)
    malformed = "Proposed price:" + " " * 4096 + "Ask a buyer to choose a price."
    assert scrub_hallucinations(malformed, ctx) == malformed


@pytest.mark.parametrize("audience,typed,rendered", [
    ("independent hospital purchasing teams", "two thousand four hundred dollars", "$2,400"),
    ("parents planning allergy-safe dinners", "twenty-seven dollars", "$27"),
    ("independent hospital purchasing teams", "$2,450.50", "$2450.50"),
    ("parents planning allergy-safe dinners", "three hundred and forty dollars", "$340"),
    ("parents planning allergy-safe dinners", "$49", "$49.00"),
])
def test_saved_founder_pilot_price_keeps_typed_form_without_allowing_invented_amounts(env, audience, typed, rendered):
    from app.chat.context import ChatContext
    from app.chat.router import _known_amounts, scrub_hallucinations
    from app.db.models import Venture
    from app.db.session import serialize_writes
    from app.state import db
    from app.ventures import founder_facts as facts

    _, venture_id, _ = env
    statement = f"For a one-month pilot for {audience}, you proposed {typed}, but this price is untested."
    with db()() as session:
        serialize_writes(session, key=f"startup_review:{venture_id}")
        venture = session.get(Venture, venture_id)
        foreign = Venture(id=venture_id + "_other", workspace_id=venture.workspace_id, name=venture.name,
            raw_idea=venture.raw_idea, stage=venture.stage, created_at=venture.created_at, updated_at=venture.updated_at)
        session.add(foreign)
        session.flush()
        facts.save(session, foreign, {"pricing": facts._fact("$999", "clarification")})
        facts.save(session, venture, {"pricing": facts._fact(statement, "clarification"),
            "progress": facts._fact("$777", "website")})
        session.commit()
    known = _known_amounts(venture_id, "Give me a short summary.")
    ctx = ChatContext(venture={"id": venture_id})
    summary = f"Your proposed pilot price is {rendered} for one month. It is untested. Ask a buyer about it next."
    assert scrub_hallucinations(summary, ctx, known) == summary.replace(rendered, typed)
    for amount in ("$999", "$777"):
        unsupported = scrub_hallucinations(f"Your pilot could cost {amount}. Ask a buyer about it next.", ctx, known)
        assert amount not in unsupported
    for unrelated in ("We have twenty-seven users.", "It takes twenty-seven days.", "It costs twenty-seven euros."):
        assert "$27" not in scrub_hallucinations("Your pilot costs $27. Specify your price next.", ctx, unrelated)
    for unrelated in ("It costs nineteen twenty dollars.", "It costs one million twenty dollars.", "It costs minus twenty dollars."):
        assert "$20" not in scrub_hallucinations("Your pilot costs $20. Specify your price next.", ctx, unrelated)
    assert "$20" not in scrub_hallucinations("Your pilot costs $20. Specify your price next.", ctx,
        "It costs twenty dollars and fifty cents.")


def test_l1_scrub_rewrites_sent_claims_but_keeps_nothing_is_sent():
    load_company_os_app()
    from types import SimpleNamespace

    from app.chat.router import scrub_hallucinations

    ctx = SimpleNamespace(governance={"autonomy": "L1"}, website={})
    out = scrub_hallucinations("I sent the emails. They appear here for your review, and nothing is sent. It was not sent.", ctx)
    assert out == "I prepared the emails. They appear here for your review, and nothing is sent. It was not sent."


@pytest.mark.parametrize("domain", ["supplier inventory audits", "balcony gardening lessons"])
def test_publication_negation_covers_its_clause_without_exempting_positive_claims(domain):
    load_company_os_app()
    from types import SimpleNamespace

    from app.chat.router import scrub_hallucinations

    context = SimpleNamespace(governance={"autonomy": "L1"}, website={})
    for negation in ("no evidence", "nothing indicating", "not a single indication", "never any indication"):
        body = f"You have {negation} that your detailed announcement about {domain} was published or sent. Review your draft next."
        assert scrub_hallucinations(body, context) == body
    for boundary in (". ", "; ", "\n", ", but ", ", however "):
        body = f"You have no draft about {domain}{boundary}Your announcement was published or sent. Review it next."
        expected = body.replace("published or sent", "prepared or prepared")
        assert scrub_hallucinations(body, context) == expected
    body = f"Your announcement about {domain} was published or sent. Review it next."
    assert scrub_hallucinations(body, SimpleNamespace(governance={"autonomy": "L1"}, website={"published": True})) == body


def test_second_venture_with_same_derived_name_gets_a_suffix(env):
    client, vid, wid = env
    first = client.get(f"/ventures/{vid}").json()["name"]
    second = client.post("/ventures", json={"raw_idea": "a lobby lock waitlist for apartment buildings"})
    third = client.post("/ventures", json={"raw_idea": "a lobby lock waitlist for apartment buildings"})
    assert second.status_code == 201 and second.json()["name"] == f"{first} 2"
    assert third.status_code == 201 and third.json()["name"] == f"{first} 3"
    assert len({vid, second.json()["id"], third.json()["id"]}) == 3


def test_dashboard_run_status_is_paused_after_pause(env):
    client, vid, wid = env
    assert client.post(f"/ventures/{vid}/pause", json={"reason": "t"}).status_code == 200
    snap = client.get(f"/ventures/{vid}/dashboard").json()
    assert snap["run_status"]["state"] == "paused"


@pytest.mark.asyncio
@pytest.mark.parametrize("audience", ["warehouse purchasing teams", "families cooking together"])
async def test_social_post_gives_a_manual_publishing_next_step_by_default(env, monkeypatch, audience):
    client, vid, _ = env
    from app.chat.execute import execute_action

    monkeypatch.delenv("SUPYMEM_SOCIAL_POST_ENABLED", raising=False)
    text = f"post to linkedin: Update for {audience}"
    msg = _chat(client, vid, text)
    assert not _cards(msg)
    out = await execute_action(vid, "social_post", payload={"query": text, "content": audience, "platforms": ["linkedin"]})
    from app.core.local_mode import is_local

    if is_local():
        from app.core.local_capabilities import blocked_message

        assert blocked_message("social_posting") in msg["body_md"] and not msg["actions"]
        assert blocked_message("social_posting") in out["body_md"] and not out["actions"]
    else:
        from app.chat.actions import SOCIAL_POST_COMING_SOON

        assert SOCIAL_POST_COMING_SOON["body_md"] in msg["body_md"]
        assert out == SOCIAL_POST_COMING_SOON


def test_channel_answers_stay_answers_while_bulk_sends_are_commands():
    from app.chat.idea_bridge import _outreach_command

    for answer in ("Email mostly, sometimes calls", "WhatsApp groups with other bakers", "Text messages to the PM at the end of the day", "Post in local Facebook groups"):
        assert not _outreach_command(answer)
    for command in ("send 500 emails to nurses in Chicago today", "email 200 dentists in Ohio", "reach out to every gym owner in Denver"):
        assert _outreach_command(command)


@pytest.mark.parametrize("audience", ["commercial kitchen inspectors", "parents choosing swimming lessons"])
def test_written_material_does_not_capture_other_chat_actions(audience):
    load_company_os_app()
    from app.chat.idea_bridge import written_request

    assert written_request(f"Write a welcome note for {audience}.")
    assert written_request(f'Write a welcome note for {audience} including the example "draft Morgan note".')
    assert written_request(f"Prepare a handbook for {audience}. Return it as a document.")
    for request in (f"Draft four outreach emails for {audience}.",
                    f"Draft a LinkedIn post for {audience}.",
                    f"Draft Morgan note for {audience}.",
                    f"How many leads do we have for {audience}? Draft an email.",
                    "Can you write code?"):
        assert not written_request(request), request


@pytest.mark.parametrize("audience", ["commercial kitchen inspectors", "parents choosing swimming lessons"])
def test_pricing_copy_is_not_a_payment_but_positive_payments_stay_blocked(audience):
    load_company_os_app()
    from app.chat.guard import outreach_task_reply

    assert outreach_task_reply("", f"Draft pricing copy for {audience}: charge $19 per month") is None
    for command in ("pay the supplier", "charge the card", "refund the registration fee"):
        assert outreach_task_reply("", f"Include the refund policy for {audience}, {command}")["kind"] == "refusal"


@pytest.mark.parametrize("offer", ["Inspection records for commercial kitchens", "Swimming lessons for families"])
def test_guidance_without_a_saved_question_keeps_the_explanation_action(env, offer):
    from app.chat.journey import guided_answer

    client, _, _ = env
    created = client.post("/ventures", json={"raw_idea": offer}).json()
    venture_id, workspace_id = created["id"], created["workspace_id"]
    reply = guided_answer(venture_id, {"workspace_id": workspace_id, "idea": {"name": offer, "confirmed": False}},
                          {"state": "queued", "activity": "Your idea is waiting."})
    assert [action.get("action") for action in reply["actions"]] == ["blocker.explain"]
    assert "next" in reply["body_md"].lower()


@pytest.mark.parametrize("host,offer", [("inspections.example", "commercial kitchen records"),
                                       ("swimming.example", "neighborhood swimming lessons")])
def test_explicit_page_lookup_reads_only_that_page_without_search(monkeypatch, host, offer):
    import asyncio
    from types import SimpleNamespace

    load_company_os_app()
    from app.research import explore
    from app.tools import reader

    url = f"https://{host}/guide"
    reads = []

    def fetch(page_url):
        reads.append(page_url)
        return SimpleNamespace(text=offer, title=offer, final_url=page_url)

    monkeypatch.setattr(reader, "read_page", fetch)
    monkeypatch.setattr(explore, "_provider_from_env", lambda: pytest.fail("A named page must not start a search"))
    from app.research import local_providers

    async def local_fetch(page_url):
        return reader.read_page(page_url).text

    monkeypatch.setattr(local_providers, "fetch_page", local_fetch)
    monkeypatch.setattr(local_providers, "scrape_provider", lambda: object())
    monkeypatch.setattr(local_providers, "search_provider", lambda **kwargs: pytest.fail("A named page must not start a search"))
    result = asyncio.run(explore.explore_web(f"Can you read {url}?"))
    assert reads == [url]
    assert result["mode"] == "ok" and result["pages"][0]["excerpt"] == offer

    def unavailable(*_args):
        raise ValueError("page_unavailable")

    monkeypatch.setattr(reader, "read_page", unavailable)
    result = asyncio.run(explore.explore_web(f"Can you read {url}?"))
    assert result["mode"] == "error" and result["hits"] == result["pages"] == []


@pytest.mark.parametrize("offer", ["Commercial kitchen inspection records", "Family swimming lessons"])
def test_working_memory_reset_preserves_live_aliases(tmp_path, offer):
    load_company_os_app()
    from app import state

    captured = state.MEM
    state.reset_mem_for_tests(tmp_path / "reset-memory.db")
    assert state.MEM is captured
    captured["offer"] = offer
    assert state.MEM["offer"] == offer


@pytest.mark.parametrize("offer", ["Inspection records for commercial kitchens", "Swimming lessons for families"])
def test_authenticated_actions_pass_the_resolved_local_actor(env, monkeypatch, offer):
    import asyncio

    from app.authz.workspace import subject_person_id
    from app.chat import customers
    from app.chat.execute import execute_action
    from fastapi import HTTPException

    client, _, _ = env
    venture_id = client.post("/ventures", json={"raw_idea": offer}).json()["id"]
    actors = []

    async def execute(_venture_id, action, payload, actor):
        actors.append(actor)
        return {"kind": "text", "body_md": "Review your saved work next.", "actions": [], "citations": []}

    monkeypatch.setattr(customers, "execute", execute)
    asyncio.run(execute_action(venture_id, "gtm.status", payload={"show_saved": True}))
    assert actors == [subject_person_id(None)]
    with pytest.raises(HTTPException):
        asyncio.run(execute_action(venture_id, "gtm.status", payload={"show_saved": True}, actor_subject="usr_action_outsider"))
    assert actors == [subject_person_id(None)]
