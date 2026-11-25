"""Market experiments execute for unrelated audiences without assigning founder homework."""
import asyncio
import json
from copy import deepcopy
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest


@pytest.fixture
def venture(tmp_path, monkeypatch, request):
    from tests.company_os_harness import company_os_test_client, load_company_os_app

    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/chat.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "memory.sqlite3"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    main = load_company_os_app()

    with company_os_test_client(main.create_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        created = client.post("/ventures", json={"raw_idea": getattr(request, "param", "A shared calendar for neighborhood volunteers")})
        assert created.status_code == 201, created.text
        yield client, created.json()["id"], created.json()["workspace_id"]


@pytest.mark.parametrize("audience", ["warehouse operators", "home gardeners"])
def test_prohibited_scan_cannot_be_prepared_or_silently_started(venture, monkeypatch, audience):
    from app.chat import execute, experiments, router
    from app.chat.llm_router import _prohibited_act

    text = f"Research competitors for {audience}. Do not contact anyone, run an experiment, or build anything."
    assert _prohibited_act("experiment_start", text)
    assert _prohibited_act("experiment_start", text.replace("Do not", "Don’t"))
    assert experiments.scan_request(f"Research competitors for {audience}. No experiments.") is None
    assert not _prohibited_act("experiment_start", f"Run an experiment for {audience}. Do not contact experiment participants.")
    assert experiments.scan_request(text) is None
    calls = []

    async def prepare(*args, **kwargs):
        return {"actions": [{"confirmation": {"action": "experiment.run", "resulting_state": {}}}]}

    async def start(*args, **kwargs):
        calls.append(kwargs)
        return {}

    monkeypatch.setattr(router, "_prepare_intent", prepare)
    monkeypatch.setattr(execute, "start_requested_work", start)
    asyncio.run(router._dispatch_intent("venture", None, "research", text, None, "founder"))
    assert not any("experiment.run" in call["actions"] for call in calls)
    refused = asyncio.run(router._dispatch_intent("venture", None, "experiment_start", text, None, "founder"))
    assert refused["kind"] == "refusal"
    assert not any(action.get("action") or action.get("confirmation") for action in refused["actions"])


def test_long_audience_and_problem_keep_runnable_recipes(market):
    from app.core.local_mode import is_local
    from app.experiments import autonomous

    _client, venture_id, _workspace_id, view, *_rest = market
    view["icp"]["who"] += " coordinating schedules and records across several locations with limited support"
    view["icp"]["problem"] += " while reconciling incomplete records and coordinating time sensitive changes across locations"
    save_market_context(venture_id, view)
    specs = autonomous.available(venture_id)
    demand = next(spec for spec in specs if spec["scan"] == "demand")
    assert demand["icp"]["who"] == view["icp"]["who"]
    assert demand["icp"]["problem"] == view["icp"]["problem"]
    if is_local():
        assert len(demand["hypothesis"]) > 160
        assert demand["icp"]["who"].casefold() in demand["hypothesis"].casefold()
        assert demand["icp"]["problem"].casefold() in demand["hypothesis"].casefold()
    assert any(spec["scan"] == "postings" for spec in specs) == (not is_local() or view["icp"]["mode"] != "consumer")


@pytest.mark.parametrize("audience,problem", [("laboratory coordinators", "missed equipment reservations"),
                                             ("families sharing gardens", "missed watering turns")])
@pytest.mark.parametrize("status", ["missing", "confirm", "conflict"])
@pytest.mark.usefixtures("venture")
def test_research_context_does_not_restore_a_problem_rejected_by_known_checks(audience, problem, status):
    from app.experiments import autonomous

    icp = {"who": audience, "problem": "", "needs_input": True,
           "known_checks": {"problem": {"status": status, "value": None}}}
    restored = autonomous._research_icp(icp, idea_problem=problem, idea_offer=problem)
    assert restored["who"] == audience
    assert restored["problem"] == "" and restored["needs_input"]


@pytest.mark.parametrize("audience,idea", [
    ("warehouse coordinators", "A private delivery scheduling calendar for warehouse coordinators"),
    ("adult hikers", "A private walking route diary for adult hikers"),
])
@pytest.mark.parametrize("status,source,local_mode", [
    ("confirm", "Your idea description", True),
    ("missing", None, True),
    ("conflict", "Your idea description", True),
    ("confirm", "Your selected document", True),
    ("confirm", "Your idea description", False),
])
@pytest.mark.usefixtures("venture")
def test_local_public_scan_can_check_an_idea_topic_without_confirming_pain(monkeypatch, audience, idea, status, source, local_mode):
    from app.experiments import autonomous

    monkeypatch.setenv("SUPYMEM_MODE", "local" if local_mode else "hosted")
    monkeypatch.setattr(autonomous, "autopilot_enabled", lambda: True)
    monkeypatch.setattr(autonomous, "RECIPES", {kind: autonomous.RECIPES[kind] for kind in ("demand", "competitors")})
    snapshot = {"venture_id": "scan_venture", "workspace_id": "scan_workspace", "direction": None,
        "stage": "idea", "idea_text": idea, "idea_problem": idea, "waitlist": {}, "app_id": None,
        "journey": {"icp": {"who": audience, "problem": "", "needs_input": True,
            "known_checks": {"problem": {"status": status, "value": idea, "source": source}}}}}
    original = deepcopy(snapshot)
    provider = SimpleNamespace(name="duckduckgo", search_micro=0, scrape_micro=0)
    specs = autonomous.select_runnable(snapshot, provider)
    scan = next((spec for spec in specs if spec.get("scan") == "demand"), None)
    allowed = local_mode and status == "confirm" and source == "Your idea description"
    assert bool(scan) is allowed
    assert snapshot == original
    if allowed:
        assert scan["icp"]["problem"] == idea and scan["icp"]["research_topic_only"]
        assert scan["icp"]["known_checks"]["problem"]["status"] == "confirm"
        result = autonomous._scan_result(scan, [{"sources": [], "retrieved_sources": 0}], 0, 0)
        assert "not a confirmed customer problem" in result["insight"]
        assert result["outcome"] == "inconclusive" and result["sources"] == []


def confirm_saved_idea(venture_id):
    from sqlalchemy import select
    from app.db.models import Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.state import db
    from core.clock import utcnow

    with db()() as session:
        venture = session.get(Venture, venture_id)
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id,
            IdeaSession.workspace_id == venture.workspace_id, IdeaSession.state != "abandoned"))
        if idea is None:
            idea = IdeaSession(id="confirmed_" + venture_id, workspace_id=venture.workspace_id,
                venture_id=venture_id, case="know", state="confirmed", created_at=utcnow(), updated_at=utcnow())
            session.add(idea)
            session.flush()
        if not idea.current_revision_id:
            revision = IdeaRevision(id="confirmed_revision_" + venture_id, session_id=idea.id,
                venture_id=venture_id, workspace_id=venture.workspace_id, rev=1, source="founder",
                summary=venture.raw_idea, structured="{}", created_at=utcnow())
            session.add(revision)
            idea.current_revision_id = revision.id
        idea.state = "confirmed"
        idea.confirmed_revision_id = idea.current_revision_id
        session.commit()


def save_market_context(venture_id, view):
    from app.db.models import Venture
    from app.state import db
    from app.ventures import founder_facts

    with db()() as session:
        founder_facts.save(session, session.get(Venture, venture_id), {
            field: {"value": value, "quote": value, "source": "review", "confidence": 1.0} if value else None
            for field, key in (("audience", "who"), ("problem", "problem"))
            for value in (view["icp"].get(key),)})
        session.commit()


@pytest.fixture(params=[("company", "warehouse teams", "tracking inventory shortages", "Stock Ledger"),
                        ("consumer", "home gardeners", "tracking watering schedules", "Plant Journal")],
                ids=["b2b-inventory", "consumer-gardening"])
def market(venture, monkeypatch, request):
    from app.conversion import waitlist
    from app.core.local_mode import is_local
    from app.db.models import Venture
    from app.db.supymem_models import FounderApp, VentureWaitlist
    from app.experiments import autonomous
    from app.gtm import journey
    from app.signals.research import ScrapedText, SearchResults
    from app.state import db
    from core.clock import utcnow

    client, venture_id, workspace_id = venture
    if is_local():
        from app.core.providers_local import save_settings
        from core.local_security import console_origin

        client.headers["origin"] = console_origin()
        save_settings(provider="openai_compatible", base_url="http://127.0.0.1:1234/v1",
            models={"chat": "local-test-model"})
    mode, audience, problem, alternative = request.param
    view = {"icp": {"who": audience, "problem": problem, "mode": mode, "needs_input": False,
                    "competitors": [{"name": alternative}]},
            "allow_sending": False, "auto_approve_first_emails": False, "experiments": []}
    landing = {"enabled": False, "submission_verified": False, "published_site": None}
    app_id = "app_market_" + mode
    now = utcnow()
    with db()() as session:
        row = session.get(Venture, venture_id)
        row.name, row.raw_idea = alternative, problem
        session.add(FounderApp(id=app_id, venture_id=venture_id, workspace_id=workspace_id,
            slug="market-" + mode, name=alternative, kind="web", created_at=now, updated_at=now))
        session.add(VentureWaitlist(venture_id=venture_id, workspace_id=workspace_id,
            enabled=True, consent_version=waitlist.VERSION, enabled_by="local", updated_at=now))
        session.commit()
    confirm_saved_idea(venture_id)
    from app.ventures.current_idea import current_revision
    from tests.company_os_harness import append_idea_revision

    with db()() as session:
        revision = current_revision(session.get(Venture, venture_id), session)
        append_idea_revision(session, revision.id,
            structured=json.dumps({**json.loads(revision.structured), "_questions_skipped": True}))
        session.commit()
    save_market_context(venture_id, view)
    monkeypatch.setattr(journey, "status", lambda *args, **kwargs: deepcopy(view))
    monkeypatch.setattr(waitlist, "status", lambda *args, **kwargs: deepcopy(landing))
    hits = [{"url": f"https://www.reddit.com/r/discussion/comments/abc{index}/problem/",
             "title": f"{audience}: {problem}—discussion", "author": f"user{index}",
             "snippet": f"I struggle with {problem} using {alternative} and waste hours every week.",
             "published_at": (now - timedelta(days=2)).isoformat()} for index in range(3)]
    hits += [{**hits[0], "url": "https://www.reddit.com/r/discussion/comments/undated/problem/", "published_at": None},
             {**hits[0], "url": "https://www.reddit.com/r/discussion/comments/older/problem/",
              "published_at": (now - timedelta(days=100)).isoformat()},
             {**hits[0], "url": "https://www.reddit.com/r/discussion/comments/secondhand/problem/",
              "snippet": f"My colleague struggles with {problem}."},
             {"url": "https://alternative.example/", "title": alternative,
              "snippet": f"We provide software for {problem} for {audience}."}]
    calls = []

    class Provider:
        name = "firecrawl"
        search_micro = 0
        scrape_micro = 0
        factory = staticmethod(autonomous._provider)
        fail_on_search = None

        async def search(self, query, **kwargs):
            calls.append(("search", query, kwargs))
            if self.fail_on_search == sum(call[0] == "search" for call in calls):
                import httpx

                request = httpx.Request("POST", "https://search.example/search")
                raise httpx.HTTPStatusError("Rejected", request=request, response=httpx.Response(404, request=request))
            return SearchResults(deepcopy(hits), provider_rows=len(hits), rejected_shape=0,
                rejected_missing_url=0, usage={"firecrawl_self_hosted": True})

        async def scrape(self, url):
            calls.append(("scrape", url))
            hit = next(item for item in hits if item["url"] == url)
            return ScrapedText(hit["snippet"], metadata={"published_at": hit.get("published_at"), "author": hit.get("author")},
                usage={"firecrawl_self_hosted": True})

    provider = Provider()
    monkeypatch.setattr(autonomous, "_provider", lambda: provider)
    return client, venture_id, workspace_id, view, landing, provider, calls, hits, app_id


@pytest.fixture
def credit_meter(market, monkeypatch):
    from app.credits import metered
    from app.credits.gate import Meter
    from app.credits.reserve import Hold

    meters = []

    @asynccontextmanager
    async def charge(**kwargs):
        meter = Meter(hold=Hold("hold_market", "acc_market", kwargs["estimate_micro"]), passthrough=False,
            purpose=kwargs["purpose"], estimate_micro=kwargs["estimate_micro"],
            **{key: kwargs.get(key) for key in ("workspace_id", "venture_id", "account_id", "model", "provider", "request_id")})
        meter.protect_outcome = lambda: None
        meters.append(meter)
        yield meter
        if meter.recorded:
            meter.settle_result = SimpleNamespace(actual_micro=meter.actual_micro)

    monkeypatch.setattr(metered, "charge", charge)
    return meters


def run_jobs():
    from app.chat.conversations import bind_job_conversation
    from app.core.jobs import complete_job, lease
    from app.experiments.autonomous import KIND
    from app.state import db
    from app.worker import handlers

    handlers.install_default_handlers()
    assert KIND in handlers.HANDLERS
    with db()() as session:
        jobs = lease(session, "experiment-test-worker", kinds=[KIND], n=1)
        session.commit()
    assert len(jobs) == 1
    job = jobs[0]
    payload = {**job.payload, "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts}
    with bind_job_conversation(payload, venture_id=job.venture_id, workspace_id=job.workspace_id):
        asyncio.run(handlers.dispatch(job.kind, payload))
        asyncio.run(handlers.dispatch(job.kind, payload))
    with db()() as session:
        complete_job(session, job.id, worker_id=job.worker_id, attempt=job.attempts)
        session.commit()
    return job


def test_runnable_selection_respects_stage_and_real_capabilities(market, monkeypatch):
    from app.core.local_mode import is_local
    from app.chat import experiments
    from app.experiments import autonomous, ledger

    _client, venture_id, _workspace_id, view, landing, provider, calls, _hits, _app_id = market
    snapshot = autonomous._snapshot(venture_id)
    early = autonomous.select_runnable({**snapshot, "stage": "ideating"}, provider)
    expected_recipes = {kind for kind, recipe in autonomous.RECIPES.items()
        if not recipe.get("connector") and (is_local() or not recipe.get("approach"))} - {"segments"}
    if is_local():
        expected_recipes = expected_recipes - {"adjacent_swap"} | {"outreach_split"}
        if view["icp"]["mode"] == "consumer":
            expected_recipes.discard("postings")
    assert {item["scan"] for item in early} == expected_recipes
    assert all(item["hypothesis"] and item["method"] and item["ceiling_micro"] >= (0 if is_local() else 1) for item in early)
    demand = next(item for item in early if item["scan"] == "demand")
    assert len(demand["queries"]) == 3 and all("job posting" not in query and not query.endswith("hiring") for query in demand["queries"])
    assert not calls
    assert autonomous.select_runnable({**snapshot, "stage": "archived"}, provider) == []
    assert autonomous.select_runnable({**snapshot, "journey": {"icp": {"needs_input": True}}}, provider) == []
    without_search = autonomous.select_runnable(snapshot, None)
    if is_local():
        assert all(item["runner"] != "scan" for item in without_search)
    else:
        assert without_search == []
    view.update(allow_sending=True, auto_approve_first_emails=True,
        experiments=[{"title": "Problem angle", "sent": 10, "replies": 2, "reply_rate": .2},
                     {"title": "Outcome angle", "sent": 10, "replies": 4, "reply_rate": .4}])
    landing.update(enabled=True, submission_verified=True, published_site={"url": "https://landing.example/"})
    live = autonomous._snapshot(venture_id)
    assert {item["scan"] for item in autonomous.select_runnable({**live, "stage": "operating"}, provider)} == expected_recipes | {"outreach", "landing"}
    assert {item["scan"] for item in autonomous.select_runnable({**live, "app_id": None}, None)} == {"outreach"} | {
        item["scan"] for item in without_search}
    assert "landing" not in {item["scan"] for item in autonomous.select_runnable({**live, "waitlist": {**landing, "submission_verified": False}}, provider)}
    ledger.create_from_tests(venture_id, "validation", [{"hypothesis": "An optional interview could help", "action": "Interview people"}])
    listed = experiments.list_reply(venture_id)
    assert listed["kind"] == "experiments"
    assert listed["body_md"].index("Your experiments:") < listed["body_md"].index("Demand signal scan")
    assert "Did you run one?" not in listed["body_md"] and "—" not in listed["body_md"]
    assert len(listed["actions"]) >= experiments.SHOWN_EXPERIMENTS + 3
    assert "More you can run: " in listed["body_md"]
    assert any(item["action"] == "experiment.request.prepare" for item in listed["actions"])
    assert all(item["action"] in {"experiment.prepare", "experiment.run", "experiment.recommendation.prepare",
                                  "experiment.page.guide", "experiment.channel.prepare", "experiment.request.prepare",
                                  "experiment.start", "experiment.audience.change"} for item in listed["actions"])
    assert len(ledger.list_for_venture(venture_id)) == 1
    system = autonomous.available(venture_id)[0]
    assert ledger.get(system["id"], venture_id) is None
    with pytest.raises(KeyError):
        ledger.start(system["id"], venture_id)
    with pytest.raises(KeyError):
        ledger.record_result(system["id"], venture_id, True, "An invented result", "supported", "Unverified")
    assert len(ledger.create_from_tests(venture_id, "validation", [{"hypothesis": system["hypothesis"]}])) == 1
    for name in ("FIRECRAWL_SELF_HOSTED_URL", "FIRECRAWL_SELF_HOSTED_TOKEN", "FIRECRAWL_API_KEY", "EXA_API_KEY", "TAVILY_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    if is_local():
        from app.core.local_secrets import update_settings
        assert provider.factory() is not None
        update_settings("capabilities", {"run_for_me_experiments": False}, {})
        assert autonomous.select_runnable(snapshot, provider)
    else:
        assert provider.factory() is None


def test_experiment_list_orders_by_live_risk_and_cost_with_reason(market, monkeypatch):
    from app.chat import experiments
    from app.credits.units import credits_to_micro
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous, ledger
    from app.state import db
    from core.clock import utcnow

    _client, venture_id, workspace_id, view, _landing, _provider, calls, _hits, _app_id = market
    direction = autonomous._snapshot(venture_id)["direction"]
    base = {"venture_id": venture_id, "workspace_id": workspace_id, "direction": direction, "icp": view["icp"],
            "state": "proposed", "method": "Read recorded event counts.", "question": "Q3", "days": 14, "info": 1}
    candidates = [{**base, "id": "exp_costly", "title": "Costly test", "hypothesis": "A payment commitment is possible", "ceiling_micro": credits_to_micro(100)},
                  {**base, "id": "exp_cheap", "title": "Lean test", "hypothesis": "Customers will make a payment commitment", "ceiling_micro": 0}]
    for candidate in candidates:
        candidate["fingerprint"] = autonomous._fingerprint(candidate)
    with db()() as session:
        session.add(Experiment(id="exp_supported", venture_id=venture_id, workspace_id=workspace_id,
            kind=ledger.KIND, state="concluded", hypothesis=f"{view['icp']['who']} report a concrete problem",
            outcome="supported", ended_at=utcnow(), context=json.dumps({"runner": "system", "spec": {"question": "Q1", "direction": direction, "scan": "demand"}})))
        session.commit()
    monkeypatch.setattr(autonomous, "available", lambda _venture_id: candidates)
    reply = asyncio.run(experiments.dispatch(venture_id, "experiment_list", "show my experiments"))
    assert "Your experiments:\nBiggest open risk:" in reply["body_md"]
    assert reply["body_md"].index("Lean test") < reply["body_md"].index("Costly test")
    assert [action["payload"]["experiment_id"] for action in reply["actions"]
            if action["action"] == "experiment.prepare"] == ["exp_cheap", "exp_costly"]
    assert not calls
    with db()() as session:
        for index in range(3):
            session.add(Experiment(id=f"exp_running_{index}", venture_id=venture_id, workspace_id=workspace_id,
                kind=ledger.KIND, state="running", hypothesis=f"Another question {index}"))
        session.commit()
    blocked = experiments.list_reply(venture_id)
    assert all(action["action"] != "experiment.prepare" for action in blocked["actions"]) and "3 experiments running" in blocked["body_md"]


@pytest.mark.hosted_only
def test_typed_experiment_requests_start_available_titles_without_provider_calls(market, monkeypatch):
    from app.chat import assistant, experiments
    from app.chat.context import assemble
    from app.chat.intent import classify_intent
    from app.chat.router import _dispatch_intent
    from app.experiments import autonomous, ledger

    async def answer_question(*_args, **_kwargs):
        return "You can choose a test in Experiments when you want to run it."

    monkeypatch.setattr(assistant, "_complete", answer_question)
    client, venture_id, workspace_id, _view, _landing, _provider, calls, _hits, _app_id = market
    from app.db.models import Job, Workspace
    from app.state import db
    with db()() as session:
        actor = session.get(Workspace, workspace_id).owner_id
    available = autonomous.available
    specs = available(venture_id)
    for spec in specs:
        blocked = autonomous.autopilot_enabled() and autonomous.run_blocked_reason(venture_id, spec["id"])
        response = client.post(f"/ventures/{venture_id}/chat", json={"message": f"run the {spec['title'].lower()}"})
        assert response.status_code == 200, response.text
        if blocked:
            assert response.json()["kind"] == "experiment_blocked"
            assert blocked in response.json()["body_md"]
            assert not any(action.get("confirmation") for action in response.json()["actions"])
            continue
        assert response.json()["kind"] == "experiment_running"
        assert not any(action.get("confirmation") for action in response.json()["actions"])
        assert "credit" not in response.json()["body_md"].lower()
    with db()() as session:
        job_ids = list(session.query(Job.id))
    titles = dict(zip((item["id"] for item in specs), ("Supplier renewal pulse", "Recipe popularity check")))
    monkeypatch.setattr(autonomous, "TITLES", {**autonomous.TITLES,
                                             **{f"fixture_{index}": title for index, title in enumerate(titles.values())}})

    def renamed(venture_id):
        return [{**item, "title": titles[item["id"]]} for item in available(venture_id) if item["id"] in titles]

    monkeypatch.setattr(autonomous, "available", renamed)
    for spec in renamed(venture_id):
        title = spec["title"]
        expected = client.post(f"/ventures/{venture_id}/chat/confirm", json={
            "action": "experiment.prepare", "payload": {"experiment_id": spec["id"]}})
        assert expected.status_code == 200, expected.text
        assert not any(action.get("confirmation") for action in expected.json()["actions"])
        for message in (f"run the {title}", f"start {title}", f"do {title}",
                        f"please run {title.split()[0].lower()} check"):
            response = client.post(f"/ventures/{venture_id}/chat", json={"message": message})
            assert response.status_code == 200, response.text
            reply = response.json()
            assert reply["kind"] == "experiment_running", (message, reply)
            assert not any(action.get("confirmation") for action in reply["actions"])
            assert "credit" not in reply["body_md"].lower()
        for message in (f"what is the {title}?", f"should I run {title}?",
                        f"do you run {title}", f"do not run {title}"):
            response = client.post(f"/ventures/{venture_id}/chat", json={"message": message})
            assert response.status_code == 200, response.text
            assert not any(action.get("confirmation", {}).get("action") == "experiment.run"
                           for action in response.json().get("actions", [])), (message, response.json())
        task_routed = asyncio.run(_dispatch_intent(venture_id, assemble(venture_id), "change", f"do {title}",
                                                  None, actor, source_message_id="not-a-task"))
        assert task_routed["kind"] == "experiment_running" and not task_routed["actions"]
    with monkeypatch.context() as patch:
        def unexpected_available(_venture_id):
            pytest.fail("Unrelated or incomplete requests must not read experiment availability")

        patch.setattr(autonomous, "available", unexpected_available)
        for message in ("do my landing page", "run my landing page", "start the customer search", "run research",
                        "build/run my landing page", "run outreach", "do email",
                        *(f"run {title.split()[0]}" for title in titles.values())):
            assert experiments.named_prepare(venture_id, message) is None, message
    for message in ("run an experiment", "run my experiments",
                    "run " + " ".join(title.split()[0] for title in titles.values()) + " check"):
        response = client.post(f"/ventures/{venture_id}/chat", json={"message": message})
        assert response.status_code == 200, response.text
        assert response.json()["kind"] == "experiments", response.json()
    assert classify_intent("start the next experiment") == "experiment_start"
    assert classify_intent("I ran the test") == "experiment_record"
    assert all(item["state"] == "running" for item in renamed(venture_id))
    with db()() as session:
        assert list(session.query(Job.id)) == job_ids
    assert not calls and not ledger.list_for_venture(venture_id)


@pytest.mark.hosted_only
def test_named_experiment_preparation_does_not_block_chat_event_loop(market, monkeypatch):
    import threading
    from app.chat.context import assemble
    from app.chat.router import _prepare_intent
    from app.experiments import autonomous

    _client, venture_id, _workspace_id, _view, _landing, _provider, calls, _hits, _app_id = market
    specs = autonomous.available(venture_id)
    context = assemble(venture_id)

    async def drive():
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        entered = asyncio.Event()
        release = threading.Event()
        call_threads = []

        def slow_available(_venture_id):
            call_threads.append(threading.get_ident())
            if len(call_threads) == 1:
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(5), "Chat event loop did not release the blocked availability lookup"
            return deepcopy(specs)

        monkeypatch.setattr(autonomous, "available", slow_available)
        pending = asyncio.create_task(_prepare_intent(venture_id, context, "research",
            f"run {specs[0]['title'].lower()}", None, source_message_id="not-a-task"))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert not pending.done()
            assert call_threads[0] != loop_thread
        finally:
            release.set()
            reply = await asyncio.wait_for(pending, 10)
        assert reply["actions"][0]["confirmation"]["resulting_state"]["experiment_id"] == specs[0]["id"]
        assert len(call_threads) == 2 and all(thread != loop_thread for thread in call_threads)

    asyncio.run(drive())
    assert not calls


@pytest.mark.hosted_only
def test_requested_paid_experiment_starts_without_visible_confirmation(market, credit_meter):
    from app.db.models import Job
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous
    from app.state import db

    client, venture_id, _workspace_id, _view, _landing, _provider, calls, *_rest = market
    spec = next(item for item in autonomous.available(venture_id) if item["scan"] == "demand")
    response = client.post(f"/ventures/{venture_id}/chat/confirm", json={
        "action": "experiment.prepare", "payload": {"experiment_id": spec["id"]}})
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "experiment_running"
    assert not any(action.get("confirmation") for action in response.json()["actions"])
    assert not calls and not credit_meter
    with db()() as session:
        assert session.query(Job).filter(Job.kind == autonomous.KIND).count() == 1
        experiment = session.get(Experiment, spec["id"])
        assert json.loads(experiment.context)["spec"]["ceiling_micro"] == spec["ceiling_micro"]
    run_jobs()
    assert calls and credit_meter


@pytest.mark.hosted_only
def test_channel_bakeoff_explains_missing_setup_without_starting_work(market):
    from app.db.models import Job
    from app.experiments import channel_tests
    from app.state import db

    client, venture_id, _workspace_id, _view, landing, _provider, calls, *_rest = market
    with db()() as session:
        jobs_before = set(session.query(Job.id).all())
    response = client.post(f"/ventures/{venture_id}/chat/confirm", json={
        "action": "experiment.channel.prepare", "payload": {"kind": "channels"}})
    assert response.status_code == 200, response.text
    reply = response.json()
    assert reply["kind"] == "needs_you"
    assert "landing page builder is not available" in reply["body_md"] and "test your idea without a page" in reply["body_md"]
    assert reply["actions"][0]["action"] == "chat.prefill"
    assert reply["actions"][0]["payload"]["text"] == "Record my customer interview: "
    assert channel_tests.prepare(venture_id, {"kind": "channels"})["action_started"] is False
    assert not calls
    with db()() as session:
        assert set(session.query(Job.id).all()) == jobs_before
    with pytest.raises(ValueError, match="channel_test_input_invalid"):
        channel_tests.prepare(venture_id, {"kind": "channels", "untrusted": True})
    landing.update(enabled=True, published_site={"url": "https://published.example/landing"})
    prepared = channel_tests.prepare(venture_id, {"kind": "channels"})
    assert prepared["kind"] == "confirmation"
    assert prepared["actions"][0]["confirmation"]["action"] == "experiment.channel.configure"


@pytest.mark.hosted_only
def test_run_confirmation_exposes_and_validates_server_credit_ceiling(market, credit_meter, monkeypatch):
    from app.analytics.feature_catalog import ACTION_SLUG
    from app.experiments import autonomous

    client, venture_id, _workspace_id, _view, _landing, provider, calls, _hits, _app_id = market
    listed = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": "experiment.list"}).json()
    demand = next(item for item in autonomous.available(venture_id) if item["scan"] == "demand")
    button = {"action": "experiment.prepare", "payload": {"experiment_id": demand["id"]}}
    assert any(item["action"] == "experiment.prepare" and item["payload"]["experiment_id"] == demand["id"]
               for item in listed["actions"])
    card = autonomous.prepare(venture_id, demand["id"])["actions"][0]["confirmation"]
    assert card["action"] == "experiment.run"
    state = card["resulting_state"]
    assert state["ceiling_micro"] == 95000  # includes the one widened pass of a weak first result
    assert 'credit' not in card['interpretation'].lower()
    assert "credit" not in " ".join(card["consequences"]).lower()
    assert ACTION_SLUG["experiment.run"] == ACTION_SLUG["experiment.prepare"] == "experiments"
    with pytest.raises(ValueError, match="experiment_confirmation_changed"):
        asyncio.run(autonomous.run(venture_id, {**state, "ceiling_micro": 0}))
    with pytest.raises(ValueError, match="experiment_confirmation_changed"):
        asyncio.run(autonomous.run(venture_id, {**state, "fingerprint": "untrusted"}))
    assert not calls
    assert all(item["state"] == "proposed" for item in autonomous.available(venture_id))
    from app.credits import metered

    original_search, original_scrape = provider.search, provider.scrape

    async def repriced_search(query, **kwargs):
        result = await original_search(query, **kwargs)
        result.usage["firecrawl_self_hosted"] = False
        monkeypatch.setattr(metered, "price_micro", lambda *args, **kwargs: 1_000_000)
        return result

    async def hosted_scrape(url):
        result = await original_scrape(url)
        result.usage["firecrawl_self_hosted"] = False
        return result

    monkeypatch.setattr(provider, "search", repriced_search)
    monkeypatch.setattr(provider, "scrape", hosted_scrape)
    ready = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": button["action"], "payload": button["payload"]})
    assert ready.status_code == 200 and ready.json()["kind"] == "experiment_running", ready.text
    assert not calls and not credit_meter
    assert not any(action.get("confirmation") for action in ready.json()["actions"])
    run_jobs()
    assert credit_meter[-1].estimate_micro == state["ceiling_micro"] and credit_meter[-1].actual_micro == 60000  # the widened pass didn't run


@pytest.mark.parametrize("scan", ["demand", "competitors", "outreach", "landing"])
@pytest.mark.hosted_only
def test_experiment_runs_and_posts_grounded_insight_with_sources(market, credit_meter, scan, monkeypatch):
    from app.chat.store import list_messages
    from app.db.supymem_models import AppEvent, Experiment, VentureWaitlistEntry
    from app.experiments import autonomous
    from app.state import db
    from core.clock import utcnow
    from core.copy import no_dashes

    client, venture_id, workspace_id, view, landing, provider, calls, hits, app_id = market
    meters = credit_meter
    view.update(allow_sending=True, auto_approve_first_emails=True,
        experiments=[{"title": "Problem angle", "sent": 10, "replies": 2, "reply_rate": .2},
                     {"title": "Outcome angle", "sent": 20, "replies": 8, "reply_rate": .4}])
    landing.update(enabled=True, submission_verified=True, published_site={"url": "https://landing.example/"})
    now = utcnow()
    with db()() as session:
        for name, path, when in [("landing_view", "/", now), ("page_view", "/", now),
                                  ("page_view", "/app", now), ("page_view", None, now), ("page_view", 7, now),
                                  ("landing_view", "/", now - timedelta(days=8))]:
            session.add(AppEvent(app_id=app_id, workspace_id=workspace_id, name=name, props=json.dumps({"path": path}), ts=when))
        for index, is_test, when in [(0, False, now), (1, True, now), (2, False, now - timedelta(days=8))]:
            session.add(VentureWaitlistEntry(id=f"entry_{index}", venture_id=venture_id, workspace_id=workspace_id,
                email=f"person{index}@example.org", consent_version="early-access-v1", consent_text="I consent", is_test=is_test, created_at=when))
        session.commit()
    spec = next(item for item in autonomous.available(venture_id) if item["scan"] == scan)
    state = autonomous.prepare(venture_id, spec["id"])["actions"][0]["confirmation"]["resulting_state"]
    quote = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": "experiment.prepare", "payload": {"experiment_id": spec["id"]}})
    assert quote.status_code == 200, quote.text
    assert quote.json()["kind"] == "experiment_running" and not calls and not meters
    assert "credit" not in quote.json()["body_md"].lower()
    assert not any(action.get("confirmation") for action in quote.json()["actions"])
    run_jobs()
    saved = [item for item in list_messages(venture_id) if item["kind"] == "experiment_result"]
    result = {key: saved[0][key] for key in ("kind", "body_md", "actions", "citations")}
    assert len(saved) == 1 and saved[0]["citations"] == result["citations"]
    assert result["body_md"] == saved[0]["body_md"] and result["citations"]
    assert all(source["href"] == source["url"] and source["label"] == no_dashes(source["title"]) for source in result["citations"])
    assert "—" not in result["body_md"] and all("—" not in source["label"] for source in result["citations"])
    assert any(item["kind"] == "experiment_running" for item in list_messages(venture_id))
    with db()() as session:
        row = session.get(Experiment, spec["id"])
        assert row.state == "concluded" and json.loads(row.context)["result"]["insight"] in result["body_md"]
    if scan in {"demand", "competitors"}:
        assert "found 3 first-person complaints" in result["body_md"]
        quoted = [source for source in result["citations"] if source.get("quote")]
        assert len(quoted) == 3 and {source["url"] for source in quoted} == {hit["url"] for hit in hits[:3]}
        assert all(source["quote"] in hits[index]["snippet"] for index, source in enumerate(quoted))
        assert len([call for call in calls if call[0] == "search"]) == 3
        assert len([call for call in calls if call[0] == "scrape"]) == 6
        assert meters[0].estimate_micro == spec["ceiling_micro"] and meters[0].actual_micro == 9
        if scan == "competitors":
            assert "Alternatives to investigate" in result["body_md"] and "https://alternative.example/" in result["body_md"]
    elif scan == "outreach":
        assert "2/10 human replies (20.0%)" in result["body_md"] and "8/20 human replies (40.0%)" in result["body_md"]
        assert "not a randomized winner" in result["body_md"] and not calls and not meters
    else:
        assert "2 recorded visits and 1 consented waitlist signups" in result["body_md"]
        assert "50.0%" in result["body_md"] and not calls and not meters
    count = len(calls)
    cached = asyncio.run(autonomous.run(venture_id, state))
    assert cached["body_md"] in result["body_md"] and cached["citations"] == result["citations"]
    assert len(calls) == count
    if scan == "demand":
        from app.credits import metered
        from app.credits.errors import InsufficientCredits

        other = next(item for item in autonomous.available(venture_id) if item["scan"] == "competitors")
        state = autonomous.prepare(venture_id, other["id"])["actions"][0]["confirmation"]["resulting_state"]

        @asynccontextmanager
        async def denied(**kwargs):
            raise InsufficientCredits("acc_market", 0, kwargs["estimate_micro"])
            yield

        with monkeypatch.context() as patch:
            patch.setattr(metered, "charge", denied)
            assert asyncio.run(autonomous.run(venture_id, state))["kind"] == "experiment_running"
            run_jobs()
        assert len(calls) == count
        assert next(item for item in autonomous.available(venture_id) if item["id"] == other["id"])["retryable"]
        provider.fail_on_search = 5
        assert asyncio.run(autonomous.run(venture_id, state))["kind"] == "experiment_running"
        run_jobs()
        assert meters[-1].actual_micro == 8 and meters[-1].settle_result is not None
        count = len(calls)
        cached = asyncio.run(autonomous.run(venture_id, state))
        assert cached["kind"] == "experiment_result" and "source check failed" not in cached["body_md"]
        assert len(calls) == count
        assert sum(item["kind"] == "experiment_result" for item in list_messages(venture_id)) == 2


def test_identical_experiment_messages_are_deduped_only_in_the_same_conversation(market, monkeypatch):
    from app.chat import experiments
    from app.chat.conversations import bind_conversation
    from app.chat.store import append_message, list_messages
    from app.db.models import ChatConversation
    from app.state import db
    from core.clock import utcnow

    _client, venture_id, workspace_id, view, _landing, _provider, _calls, _hits, _app_id = market
    reply = experiments.list_reply(venture_id)
    first = append_message(venture_id, role="assistant", **reply, idempotency_key="first")
    append_message(venture_id, role="user", body_md="Show the same experiments again")
    second = append_message(venture_id, role="assistant", **reply, idempotency_key="second")
    assert first["id"] == second["id"]
    assert sum(item["kind"] == "experiments" for item in list_messages(venture_id)) == 1
    view["icp"]["offer"] = "A newly saved offer"
    refreshed = experiments.list_reply(venture_id)
    assert refreshed["body_md"] == reply["body_md"] and refreshed["actions"] != reply["actions"]
    updated = append_message(venture_id, role="assistant", **refreshed)
    assert updated["id"] == first["id"] and updated["actions"] == refreshed["actions"]
    append_message(venture_id, role="assistant", body_md="You have a new insight.")
    assert append_message(venture_id, role="assistant", **reply)["id"] != first["id"]
    now = utcnow()
    monkeypatch.setattr("app.chat.conversations.actor_id", lambda: "person_market")
    with db()() as session:
        session.add(ChatConversation(id="cnv_other", venture_id=venture_id, workspace_id=workspace_id,
            creator_person_id="person_market", visibility="private", title="Other conversation", creation_key="other",
            created_at=now, updated_at=now))
        session.commit()
    with bind_conversation(venture_id, "cnv_other"):
        other = append_message(venture_id, role="assistant", **reply)
        assert other["id"] != first["id"] and other["conversation_id"] == "cnv_other"
        assert append_message(venture_id, role="assistant", **reply)["id"] == other["id"]


@pytest.mark.hosted_only
def test_saved_system_runs_survive_rollover_and_missing_prerequisites(market, monkeypatch):
    from app.chat.experiments import list_reply
    from app.db.models import Venture
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous
    from app.state import db
    from core.clock import utcnow

    _client, venture_id, _workspace_id, view, _landing, provider, calls, *_rest = market
    original = autonomous.available(venture_id)
    finished, retry = (next(item for item in original if item["scan"] == scan) for scan in ("demand", "competitors"))
    with db()() as session:
        row = session.get(Experiment, finished["id"])
        row.state = "concluded"
        row.context = json.dumps({**json.loads(row.context), "claimed": True,
            "result": {"insight": f"Saved observations for {view['icp']['who']}", "sources": []}})
        row = session.get(Experiment, retry["id"])
        row.state = "running"
        row.context = json.dumps({**json.loads(row.context), "claimed": True, "error": "No sources retrieved", "retryable": True})
        session.commit()
    saved_card = autonomous.prepare(venture_id, retry["id"])["actions"][0]["confirmation"]
    later = utcnow() + timedelta(days=2)
    monkeypatch.setattr(autonomous, "utcnow", lambda: later)
    rolled = autonomous.available(venture_id)
    assert len({item["id"] for item in rolled}) == len(rolled)
    assert {finished["id"], retry["id"]} <= {item["id"] for item in rolled}
    assert any(item["scan"] == "competitors" and item["id"] != retry["id"] for item in rolled)
    monkeypatch.setattr(autonomous, "_provider", lambda: None)
    view["icp"]["who"] = ""
    save_market_context(venture_id, view)
    restored = autonomous.available(venture_id)
    assert {item["id"] for item in restored} == {finished["id"], retry["id"]}
    reply = list_reply(venture_id)
    assert f"Saved observations for {finished['icp']['who']}" not in reply["body_md"] and finished["title"] in reply["body_md"]
    assert any(action.get("payload", {}).get("experiment_id") == finished["id"] for action in reply["actions"])
    assert any(action.get("payload", {}).get("experiment_id") == retry["id"] for action in reply["actions"])
    assert autonomous.prepare(venture_id, retry["id"])["actions"][0]["confirmation"] == saved_card
    monkeypatch.setattr(autonomous, "_provider", lambda: provider)
    with db()() as session:
        session.get(Venture, venture_id).stage = "paused"
        session.commit()
    assert autonomous.available(venture_id) == [] and not calls


@pytest.mark.hosted_only
def test_unconfigured_research_offers_a_runnable_manual_experiment(market, monkeypatch):
    from app.experiments import autonomous, ledger

    client, venture_id, _workspace_id, view, _landing, _provider, calls, *_rest = market
    monkeypatch.setattr(autonomous, "_provider", lambda: None)
    response = client.post(f"/ventures/{venture_id}/chat", json={"message": "show my experiments"})
    assert response.status_code == 200, response.text
    reply = response.json()
    assert "research service is temporarily unavailable" in reply["body_md"]
    assert "server configuration" in reply["body_md"] and "connected search provider" not in reply["body_md"]
    manual = next(action for action in reply["actions"] if action.get("confirmation"))
    payload = manual["confirmation"]["resulting_state"]
    assert view["icp"]["who"] in payload["hypothesis"] and view["icp"]["problem"] in payload["hypothesis"]
    started = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": "experiment.run_review",
        "payload": payload, "message_id": reply["id"]})
    assert started.status_code == 200, started.text
    row = ledger.list_for_venture(venture_id)[0]
    assert row["state"] == "proposed" and "Prepared your experiment" in started.json()["body_md"] and not calls


@pytest.fixture
def local_market(market, monkeypatch):
    import sys
    from types import ModuleType

    from app.connections import local_config
    from app.core.providers_local import save_settings
    from app.credits import metered
    from app.experiments import autonomous
    from app.research import local_providers

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    save_settings(provider="openai_compatible", base_url="http://127.0.0.1:1234/v1",
        models={"chat": "local-test-model"})
    switches = {"research": True, "run_for_me_experiments": True, "connected_apps": True, "send_email": True}
    capabilities = ModuleType("app.core.local_capabilities")
    capabilities.enabled = lambda name: switches.get(name, False)
    capabilities.blocked_message = lambda name: f"Open Features and turn on {name}, then retry your test."
    monkeypatch.setitem(sys.modules, capabilities.__name__, capabilities)
    monkeypatch.setattr(local_config, "enabled", capabilities.enabled)
    provider = market[5]
    async def local_search(query, *, limit):
        result = await metered.call(lambda: provider.search(query, limit=limit), purpose="research.search",
            provider="duckduckgo", estimate_micro=0, workspace_id=market[2], venture_id=market[1])
        result.usage = {}
        return result

    async def local_read(url):
        result = await metered.call(lambda: provider.scrape(url), purpose="research.fetch",
            provider="duckduckgo", estimate_micro=0, workspace_id=market[2], venture_id=market[1])
        result.usage = {}
        return result

    monkeypatch.setattr(local_providers, "local_search", local_search, raising=False)
    monkeypatch.setattr(local_providers, "local_read", local_read, raising=False)
    monkeypatch.setattr(local_providers, "search_ready", lambda: True, raising=False)
    monkeypatch.setattr(autonomous, "_provider", provider.factory)
    monkeypatch.setattr(autonomous, "get_policy", lambda _key: {})
    return market, switches


@pytest.mark.parametrize("recipe", ["demand", "competitors"])
def test_local_recipes_run_from_chat_without_confirmation(local_market, credit_meter, recipe):
    from app.chat import experiments
    from app.experiments import autonomous

    case, switches = local_market
    spec = next(item for item in autonomous.available(case[1]) if item["scan"] == recipe)
    reply = asyncio.run(experiments.execute(case[1], "experiment.prepare", {"experiment_id": spec["id"]}, None))
    assert reply["kind"] == "experiment_running"
    assert not any(action.get("confirmation") for action in reply["actions"])
    run_jobs()
    saved = next(item for item in autonomous.available(case[1]) if item["id"] == spec["id"])
    assert saved["state"] == "concluded", saved.get("error")
    assert saved["result"]["sources"]
    assert all(source["url"] and source["title"] for source in saved["result"]["sources"])
    assert all(source["quote"] for source in saved["result"]["by_variant"][0]["sources"])
    assert "not proof" in saved["result"]["insight"]
    assert any(call[0] == "search" for call in case[6])
    assert any(call[0] == "scrape" for call in case[6])
    assert credit_meter and all(meter.estimate_micro == 0 and meter.actual_micro == 0 for meter in credit_meter)
    switches["run_for_me_experiments"] = False
    before = list(case[6])
    cached = asyncio.run(experiments.execute(case[1], "experiment.prepare", {"experiment_id": spec["id"]}, None))
    assert cached["kind"] == "experiment_result" and cached["citations"]
    assert case[6] == before


@pytest.mark.parametrize("on", [False, True])
def test_local_scan_gates_ignore_hosted_policy(local_market, on):
    from app.experiments import autonomous, belief_autopilot, tools

    case, switches = local_market
    snapshot = autonomous._snapshot(case[1])
    switches["run_for_me_experiments"] = on
    assert autonomous.autopilot_enabled() is True
    assert belief_autopilot.enabled() is on
    scans = [item for item in autonomous.select_runnable(snapshot, autonomous._provider()) if item.get("runner") == "scan"]
    assert scans
    if on:
        assert ("postings" in {item["scan"] for item in scans}) == (case[3]["icp"]["mode"] != "consumer")
        assert {"paying_for_bad_fix", "graveyard", "search_demand", "distribution", "audience_owners", "anti_pitch", "unit_economics"} <= {item["scan"] for item in scans}
    switches["connected_apps"] = False
    assert not tools.enabled()
    switches["connected_apps"] = True
    assert tools.enabled() is on


def test_local_prepared_plan_keeps_its_selected_interview_without_starting_scans(local_market):
    from sqlalchemy import select

    from app.chat import experiments
    from app.db.models import Job
    from app.experiments import autonomous
    from app.state import db

    case, _switches = local_market
    icp = case[3]["icp"]
    reply = experiments.review_test_run(case[1], {"run": True,
        "hypothesis": f"{icp['who']} struggle with {icp['problem']}.",
        "action": "Interview five people about their recent problems.", "success_measure": "Record five recent incidents."})
    action, = reply["actions"]
    assert action["action"] == "experiment.recommendation.prepare" and action["style"] == "primary"
    from app.experiments import ledger

    selected = ledger.get(action["payload"]["experiment_id"], case[1])
    assert selected["state"] == "proposed" and selected["action"] == "Interview five people about their recent problems."
    assert not any(action.get("confirmation") for action in reply["actions"])
    assert not any(action.get("action") == "experiment.prepare" for action in reply["actions"])
    assert "Nothing was sent, published or built" in reply["body_md"]
    with db()() as session:
        assert not session.scalar(select(Job).where(Job.kind == autonomous.KIND))


def test_local_switch_off_keeps_an_explicitly_requested_scan(local_market, credit_meter):
    from app.chat import experiments
    from app.experiments import autonomous

    case, switches = local_market
    spec = next(item for item in autonomous.available(case[1]) if item["scan"] == "demand")
    asyncio.run(experiments.execute(case[1], "experiment.prepare", {"experiment_id": spec["id"]}, None))
    switches["run_for_me_experiments"] = False
    run_jobs()
    saved = next(item for item in autonomous.available(case[1]) if item["id"] == spec["id"])
    assert saved["state"] == "concluded", saved.get("error")
    assert saved["result"]["sources"] and case[6] and credit_meter


def test_local_scan_keeps_read_sources_when_no_complaints_qualify(local_market, credit_meter, monkeypatch):
    from app.chat import experiments
    from app.chat.store import list_messages
    from app.experiments import autonomous

    case, _switches = local_market
    monkeypatch.setattr(autonomous, "_complaint", lambda *_args: None)
    spec = next(item for item in autonomous.available(case[1]) if item["scan"] == "demand")
    asyncio.run(experiments.execute(case[1], "experiment.prepare", {"experiment_id": spec["id"]}, None))
    run_jobs()
    saved = next(item for item in autonomous.available(case[1]) if item["id"] == spec["id"])
    result = saved["result"]
    assert saved["state"] == "concluded" and result["outcome"] == "inconclusive"
    assert result["complaints"] == result["measured"]["events"] == 0
    assert not result["sources"]
    assert result["inspected_sources"] and all(source["url"] and source["title"] for source in result["inspected_sources"])
    terminal = next(message for message in list_messages(case[1])
        if message["kind"] == "experiment_result")
    assert terminal["citations"] and "no first-person complaints" in terminal["body_md"]
    assert "not customer evidence" in terminal["body_md"]


def test_keyless_scan_reserves_its_page_budget_for_readable_public_sources(local_market, credit_meter, monkeypatch):
    from urllib.parse import urlsplit
    from app.chat import experiments
    from app.experiments import autonomous
    from app.research.deep import _NO_SCRAPE_HOSTS
    from app.signals.local_reader import UnbilledReadError

    case, _switches = local_market
    provider = case[5]
    original = provider.scrape
    attempted = []

    async def read(url):
        attempted.append(url)
        host = urlsplit(url).hostname or ""
        if any(host == blocked or host.endswith("." + blocked) for blocked in _NO_SCRAPE_HOSTS):
            raise UnbilledReadError("Your page does not allow this read. Choose another public URL.")
        return await original(url)

    monkeypatch.setattr(provider, "scrape", read)
    monkeypatch.setattr(autonomous, "_complaint", lambda *_args: None)
    spec = next(item for item in autonomous.available(case[1]) if item["scan"] == "demand")
    asyncio.run(experiments.execute(case[1], "experiment.prepare", {"experiment_id": spec["id"]}, None))
    run_jobs()
    result = next(item for item in autonomous.available(case[1]) if item["id"] == spec["id"])["result"]
    assert attempted and all(not any((urlsplit(url).hostname or "") == blocked
        or (urlsplit(url).hostname or "").endswith("." + blocked) for blocked in _NO_SCRAPE_HOSTS) for url in attempted)
    assert len(attempted) <= spec["pages"]
    assert result["inspected_sources"] and not result["sources"] and result["outcome"] == "inconclusive"


@pytest.mark.parametrize("all_failed", [False, True])
def test_local_scan_records_failed_reads_without_failing_readable_sources(local_market, monkeypatch, caplog, all_failed):
    import httpx

    from app.experiments import autonomous
    from app.signals.local_reader import UnbilledReadError

    case, _switches = local_market
    provider = case[5]
    original = provider.scrape
    unread = "https://www.reddit.com/r/discussion/comments/abc0/problem/"

    async def read(url):
        if all_failed or url == unread:
            request = httpx.Request("GET", url)
            raise UnbilledReadError("Your page could not be read.") from httpx.HTTPStatusError(
                "Forbidden", request=request, response=httpx.Response(403, request=request))
        return await original(url)

    monkeypatch.setattr(provider, "scrape", read)
    spec = next(item for item in autonomous.available(case[1]) if item["scan"] == "demand")
    result = asyncio.run(autonomous._scan(spec, provider))
    assert not any(record.getMessage() == "founder_action_failed" and getattr(record, "action", "").endswith(".fetch")
        for record in caplog.records)
    assert any(failure["url"] == unread and failure["reason"] == "blocked access" for failure in result["source_failures"])
    if all_failed:
        assert "choose Built-in" in result["error"] and "retry your scan" in result["error"]
    else:
        assert "error" not in result and result["inspected_sources"]


def test_local_scan_quotes_and_metering_use_saved_model(local_market, monkeypatch):
    from app.core.providers_local import save_settings
    from app.credits import estimate, metered
    from app.experiments import autonomous

    case, _switches = local_market
    model = "DeepSeek-V4-Flash"
    save_settings(provider="azure_openai", base_url="https://provider.example.invalid/v1",
        api_key="synthetic-local-test-key", models={"chat": model})
    expected = estimate.estimate_llm_micro(model, prompt_tokens=6000, max_tokens=768)
    estimates = []
    original = estimate.estimate_llm_micro

    def track(model, **tokens):
        price = original(model, **tokens)
        estimates.append((model, price))
        return price

    monkeypatch.setattr(estimate, "estimate_llm_micro", track)
    specs = autonomous.select_runnable(autonomous._snapshot(case[1]), case[5])
    assert any(spec["extractor"] in {"review_theme", "status_quo"} for spec in specs if spec["runner"] == "scan")
    assert estimates and all(item == (model, expected) for item in estimates)

    calls = []

    async def capture(_transport, **details):
        calls.append(details["model"])

    monkeypatch.setattr(metered, "call", capture)
    asyncio.run(metered.llm([{"role": "user", "content": specs[0]["icp"]["who"]}],
        purpose="experiment.reviews", workspace_id=case[2], venture_id=case[1], tier="flash"))
    assert calls == [model]


def test_local_blocked_reader_names_its_feature_setting(local_market, credit_meter, monkeypatch):
    from app.chat import experiments
    from app.experiments import autonomous
    from app.research import local_providers

    case, switches = local_market
    spec = next(item for item in autonomous.available(case[1]) if item["scan"] == "demand")

    async def blocked(query, *, limit):
        raise ValueError("Research is off")

    monkeypatch.setattr(local_providers, "local_search", blocked)
    asyncio.run(experiments.execute(case[1], "experiment.prepare", {"experiment_id": spec["id"]}, None))
    switches["research"] = False
    run_jobs()
    saved = next(item for item in autonomous.available(case[1]) if item["id"] == spec["id"])
    assert "Open Features" in saved["error"] and "research" in saved["error"]
    assert "server configuration" not in saved["error"] and not credit_meter


def test_local_switch_off_stops_belief_selection(local_market, monkeypatch):
    from app.experiments import belief_autopilot

    case, switches = local_market
    switches["run_for_me_experiments"] = False
    selected = []
    monkeypatch.setattr(belief_autopilot.beliefs, "load", lambda _venture: selected.append(True) or {})
    asyncio.run(belief_autopilot.tick(case[1]))
    assert not selected and not case[6]


def test_local_tool_design_points_to_connection_setup(local_market):
    from app.chat import experiments

    case, switches = local_market
    switches["connected_apps"] = False
    reply = experiments._runner_prepare(case[1], {"required_tool": "typeform", "hypothesis": "Read my form responses."})
    assert reply["kind"] == "experiment_blocked" and "Open Features" in reply["body_md"]
    assert "connected_apps" in reply["body_md"]
    linked = asyncio.run(experiments.execute(case[1], "experiment.survey.link", {"form_id": "one"}, None))
    assert linked["kind"] == "experiment_blocked" and not case[6]


def test_local_approved_plan_prepares_outreach_without_another_card(local_market, monkeypatch):
    from app.experiments import autonomous, belief_autopilot, service

    case, switches = local_market
    switches["send_email"] = False
    spec = next(item for item in autonomous.available(case[1]) if item.get("catalog_key") == "E19")
    spec["checkpoint"] = {"after_sample": 10, "wait_days": 2, "max_response_rate": .05}
    context = {"beliefs": [], "plan": {"id": "approved", "state": "active", "tests": [{"state": "queued", "experiment_id": spec["id"]}]}}
    requested = []

    async def narrow(*_args):
        return None

    async def run(_venture, payload):
        requested.append(payload)

    monkeypatch.setattr(service, "run_evaluate", lambda *_args: None)
    monkeypatch.setattr(belief_autopilot.beliefs, "load", lambda *_args: context)
    monkeypatch.setattr(belief_autopilot, "_read_results", lambda *_args: context)
    monkeypatch.setattr(belief_autopilot, "_narrow", narrow)
    monkeypatch.setattr(belief_autopilot.belief_planner, "available", lambda *_args: [spec])
    monkeypatch.setattr(belief_autopilot.ledger, "ranking_inputs", lambda *_args: {"experiments": []})
    monkeypatch.setattr(belief_autopilot.ledger, "quality_enabled", lambda: True)
    monkeypatch.setattr(belief_autopilot, "_plan_followups", lambda *_args: context)
    monkeypatch.setattr(belief_autopilot.engine, "blocked_reason", lambda *_args: None)
    monkeypatch.setattr(belief_autopilot, "_claim", lambda *_args: True)
    monkeypatch.setattr(belief_autopilot, "_launched", lambda *_args: None)
    monkeypatch.setattr(belief_autopilot, "daily_note", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(autonomous, "run", run)
    asyncio.run(belief_autopilot.tick(case[1]))
    assert len(requested) == 1 and requested[0]["experiment_id"] == spec["id"]
    assert requested[0]["variants"] == spec["variants"] and not case[6]


def test_local_missing_capabilities_use_saved_setup(local_market, monkeypatch):
    import sys

    from app.core import local_secrets
    from app.experiments import autonomous

    monkeypatch.setitem(sys.modules, "app.core.local_capabilities", None)
    monkeypatch.setattr(local_secrets, "read_settings", lambda _section: {"values": {}})
    monkeypatch.setattr(local_secrets, "get_secret", lambda *_args: None)
    assert autonomous._local_enabled("run_for_me_experiments")
    assert not autonomous._local_enabled("connected_apps")
    assert not autonomous._local_enabled("send_email")
    assert "Your outgoing mail" in autonomous._local_blocked_message("send_email")


@pytest.mark.parametrize("on", [False, True])
def test_local_dependency_gates_use_owner_switches_and_keep_quality_checks(local_market, monkeypatch, on):
    from app.connections import context_docs
    from app.experiments import belief_autopilot, belief_planner, ledger

    _case, switches = local_market
    switches["run_for_me_experiments"] = on

    def hosted_policy(_key):
        pytest.fail("Local experiment gates must not read hosted policy")

    for module in (belief_autopilot, belief_planner, context_docs, ledger):
        monkeypatch.setattr(module, "get_policy", hosted_policy, raising=False)
    assert ledger.autopilot_enabled() is True
    assert belief_autopilot.enabled() is on
    assert belief_planner.enabled() is True
    assert ledger.quality_enabled() is True
    assert context_docs.enabled() is True
    switches["connected_apps"] = False
    assert not context_docs.enabled()


@pytest.mark.parametrize("provider_name", ["duckduckgo", "tavily"])
def test_local_message_comparison_binds_the_selected_research_provider(local_market, provider_name):
    from app.experiments import autonomous, methods

    case, _switches = local_market
    spec = next(item for item in autonomous.available(case[1]) if item.get("catalog_key") == "E19")
    original = deepcopy(spec)
    options = [{"id": f"option_{index}", "belief_id": f"belief_{index}",
        "statement": f"{spec['icp']['who']}: {spec['icp']['problem']} {index}"} for index in range(2)]
    provider = SimpleNamespace(name=provider_name, search_micro=0 if provider_name == "duckduckgo" else 3,
        scrape_micro=0 if provider_name == "duckduckgo" else 5)
    result = methods.bind_propositions(spec, options, provider, followup_days=(3,))
    assert result["provider"] == provider_name
    assert result["search_micro"] == provider.search_micro
    assert result["scrape_micro"] == provider.scrape_micro
    assert result["search_ceiling_micro"] == 4 * provider.search_micro + 24 * provider.scrape_micro
    assert result["ceiling_micro"] == result["search_ceiling_micro"] + spec["min_sample"] * 2 * spec["email_micro"]
    assert "research" in result["requires"] and "firecrawl" not in result["requires"]
    assert all(arm["params"]["icp"] == spec["icp"] and arm["params"]["queries"] for arm in result["variants"])
    assert spec == original


@pytest.mark.parametrize("explicit_scope", [False, True])
def test_local_search_meter_inherits_scope_without_overwriting_explicit_scope(local_market, monkeypatch, explicit_scope):
    from app.credits import metered
    from app.research import local_providers
    from app.tenancy import bind

    case, _switches = local_market
    calls = []

    async def search(query):
        return [{"title": query, "url": "https://public.example/evidence"}]

    async def meter(operation, **kwargs):
        calls.append(kwargs)
        return await operation()

    monkeypatch.setattr(metered, "call", meter)
    provider = SimpleNamespace(name="duckduckgo", search=search)
    overrides = {"workspace_id": "explicit_workspace", "venture_id": "explicit_venture"} if explicit_scope else {}
    with bind(workspace_id=case[2], venture_id=case[1]):
        result = asyncio.run(local_providers.search(provider, case[3]["icp"]["problem"], **overrides))
    assert result and len(calls) == 1
    assert calls[0]["workspace_id"] == overrides.get("workspace_id", case[2])
    assert calls[0]["venture_id"] == overrides.get("venture_id", case[1])


@pytest.mark.parametrize("send", [False, True])
def test_local_outreach_comparisons_prepare_without_send_setup(local_market, monkeypatch, send):
    from app.autopilot import outreach
    from app.chat import experiments
    from app.experiments import autonomous, methods, outreach_tests
    from app.gtm import journey

    case, switches = local_market
    switches["send_email"] = send
    icp = case[3]["icp"]
    icp.update(cities=["Portland", "Denver"], company_sizes=["small teams", "large teams"],
        segments=["regular users", "occasional users"], excluded_segments=["occasional users"])
    specs = autonomous.available(case[1])
    comparisons = [item for item in specs if item.get("runner") == "outreach_split"]
    assert {"E19", "E08", "E09", "E39", "E21"} <= {item["catalog_key"] for item in comparisons}
    booking = next(item for item in comparisons if item["catalog_key"] == "E26")
    setup = asyncio.run(experiments.execute(case[1], "experiment.prepare", {"experiment_id": booking["id"]}, None))
    assert setup["kind"] == "question" and "Your outgoing mail" in setup["body_md"]
    assert not any(action.get("confirmation") for action in setup["actions"])
    spec = next(item for item in comparisons if item["catalog_key"] == "E19")
    made = []
    monkeypatch.setattr(outreach_tests, "_assign", lambda *_args: {"one": "arm_0", "two": "arm_1"})
    monkeypatch.setattr(methods, "prepare_messages", lambda *_args: None)
    monkeypatch.setattr(journey, "_draft_ready_admitted", lambda *_args, **kwargs: made.append(kwargs) or 2)
    monkeypatch.setattr(outreach, "drafts", lambda *_args: [])
    reply = asyncio.run(experiments.execute(case[1], "experiment.prepare", {"experiment_id": spec["id"]}, None))
    assert reply["kind"] == "experiment_running", reply
    run_jobs()
    assert len(made) == 1 and made[0]["review_only"] is True
    if not send:
        assert "Open Features" in outreach_tests.send_block(case[1], {})
    else:
        monkeypatch.setattr(outreach, "send_slots_used_today", lambda _venture: 0)
        draft = {"experiment_id": spec["id"], "variant": "arm_0", "contact_id": "one"}
        assert outreach_tests.send_block(case[1], draft) is None
        assert draft["experiment_send_micro"] == 0
        monkeypatch.setattr(outreach, "send_slots_used_today", lambda _venture: 100)
        assert outreach_tests.send_block(case[1], {}) == "experiment_daily_cap"


def test_local_outreach_reads_only_published_mailboxes_for_each_audience(local_market, credit_meter, monkeypatch):
    from app.experiments import autonomous, outreach_tests
    from app.gtm import enrich, journey
    from app.research import local_providers, source_quality
    from app.signals.research import ScrapedText, SearchResults

    case, _switches = local_market
    icp = case[3]["icp"]
    checked = []
    monkeypatch.setattr(enrich, "classify", lambda *_args: "person" if icp["mode"] == "consumer" else "company")
    monkeypatch.setattr(journey, "_geography_mismatch", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(journey, "_same_offer", lambda *_args: False)
    monkeypatch.setattr(source_quality, "relevant_search_lead", lambda *_args: checked.append("consumer") or True)
    monkeypatch.setattr(source_quality, "relevant_company_lead", lambda *_args: checked.append("company") or True)
    pages = {"https://public.example/team": f"{icp['who']} handle {icp['problem']}. Contact us at hello@public.example.",
        "https://silent.example/team": f"{icp['who']} handle {icp['problem']}. There is no public mailbox."}

    async def search(query, *, limit):
        return SearchResults([{"url": url, "title": icp["who"], "snippet": ""} for url in pages],
            provider_rows=len(pages), rejected_shape=0, rejected_missing_url=0)

    async def read(url):
        return ScrapedText(pages[url], metadata={}, usage={})

    monkeypatch.setattr(local_providers, "local_search", search)
    monkeypatch.setattr(local_providers, "local_read", read)
    spec = {"venture_id": case[1], "workspace_id": case[2], "icp": icp,
        "variants": [{"key": "arm_0", "params": {"who": icp["who"], "icp": icp, "queries": [icp["problem"]]}}],
        "runner_params": {"pages_per_arm": 2}, "search_micro": 0, "scrape_micro": 0}
    result = asyncio.run(outreach_tests._search(spec, autonomous._provider()))
    rows = result["buckets"]["arm_0"]
    assert checked and set(checked) == {icp["mode"]}
    assert len(rows) == 1 and rows[0]["email"] == "hello@public.example"
    assert rows[0]["source_url"] == "https://public.example/team"
