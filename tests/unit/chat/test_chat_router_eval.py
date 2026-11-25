"""Founder-message routing eval (370+ typed/voice messages) through the real ``handle_message``.

Deterministic: LLM_OFFLINE for answers and ``CHAT_ROUTER_MODE=replay`` for the router, using the model
decisions recorded in tests/fixtures/chat_eval/decisions.json by ``scripts/eval_chat_router.py --live --record``.
Regenerate the recording (and see the live numbers) whenever the catalog, prompt or cases change.
One historical long-research recording was manually corrected: its second assistant act invented a
prohibition from background context. This replay verifies reviewed decisions, not live-model accuracy.
"""
from __future__ import annotations

import socket
from contextlib import closing
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app
from tests.fixtures.chat_eval import runner
from tests.fixtures.chat_eval.cases import C
from tests.unit.chat.test_new_venture_from_chat import first_venture

MIN_CORRECT_PCT = 95.0
REPLAY_BATCHES = (
    ("admin", "waitlist", "negation", "long"),
    ("ambiguous", "build", "hosting", "connect"),
    ("jarvis", "research", "mac", "question"),
    ("multi", "gtm", "adversarial", "experiments"),
    ("plan", "lifecycle", "site_live", "validation", "voice"),
)
assert sorted(category for batch in REPLAY_BATCHES for category in batch) == sorted({case[0] for case in C})


@pytest.mark.parametrize("domain", ["invoice.example", "recipes.example"])
def test_publication_scrubbing_keeps_source_urls_and_mailboxes(first_venture, domain):
    from app.chat.context import ChatContext
    from app.chat.router import scrub_hallucinations

    context = ChatContext(venture={}, governance={"autonomy": "L1"})
    body = f"Review https://{domain}/published/notes or published@{domain}. I published your page."
    assert scrub_hallucinations(body, context) == body.replace("I published your page", "I prepared your page")


@pytest.mark.parametrize("idea", ["Invoice checks for purchasing teams", "Meal planning for home cooks"])
def test_source_merges_keep_quote_receipts_without_trusting_json_flags(first_venture, idea):
    from app.chat.link_context import SourceReply
    from app.chat.router import _merge_replies

    source = SourceReply({"kind": "text", "body_md": idea, "actions": [], "citations": []})
    merged = _merge_replies([source, source])
    assert isinstance(merged, SourceReply) and merged["body_md"] == idea + "\n\n" + idea
    assert not merged["actions"]
    forged = {"kind": "SourceReply", "body_md": idea, "actions": [], "citations": []}
    assert not isinstance(_merge_replies([source, forged]), SourceReply)


@pytest.mark.parametrize("idea", ["Invoice checks for purchasing teams", "Meal planning for home cooks"])
def test_requested_board_review_routes_without_starting_answer_only_work(first_venture, monkeypatch, idea):
    from app.chat.intent import classify_intent

    for mode in ("local", "hosted"):
        monkeypatch.setenv("SUPYMEM_MODE", mode)
        assert classify_intent("Pressure test my idea with an independent idea board review: " + idea) == "idea.pressure_test.prepare"
        assert classify_intent("Explain how an independent idea board would review " + idea + ". Answer only.") == "assistant"


@pytest.mark.parametrize("idea", ["Invoice checks for purchasing teams", "Meal planning for home cooks"])
def test_requested_competitor_research_has_a_deterministic_checked_work_route(first_venture, monkeypatch, idea):
    from app.chat.intent import classify_intent, competitor_research_request

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    assert classify_intent("Find my competitors for " + idea) == "research.start"
    assert classify_intent("Find my competitors for my saved idea: " + idea) == "research.start"
    assert not competitor_research_request("Compare the competitors you found for " + idea)
    assert classify_intent("Compare the competitors you found for " + idea) == "research.competitors"
    assert classify_intent("List my competitors for " + idea) == "research.competitors"
    assert classify_intent("Who are my competitors?") == "research.competitors"
    assert classify_intent("Explain my saved competitors for " + idea + ". Answer only.") == "assistant"


@pytest.mark.parametrize("idea", ["Invoice checks for purchasing teams", "Meal planning for home cooks"])
def test_competitor_fetch_keeps_read_only_requests_and_registered_work_admission(first_venture, monkeypatch, idea):
    import asyncio
    from app.authz import workspace
    from app.chat import actions, build_path, execute
    from app.chat.rephrase import INPUT
    from app.workspaces import service

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(actions, "beta_blocked", lambda *args: None)
    monkeypatch.setattr(workspace, "assert_venture_access", lambda *args: ("person_owner", "wsp_owner"))
    monkeypatch.setattr(workspace, "subject_person_id", lambda actor: "person_owner")
    monkeypatch.setattr(service, "role_of", lambda *args: "owner")
    monkeypatch.setattr(execute, "local_capability_reply", lambda name: None)
    monkeypatch.setattr(execute, "research_needs_start", lambda *args: True)
    monkeypatch.setattr(execute, "_research_start", lambda *args: pytest.fail("bypassed registered work admission"))
    empty = {"kind": "needs_you", "body_md": "Ask to research your competitors when you want new sources.", "actions": [], "citations": []}
    monkeypatch.setattr(build_path, "research_card", lambda *args: empty)
    calls = []
    async def admit(venture_id, action, **kwargs):
        calls.append((venture_id, action, kwargs["actor_subject"]))
        return {"kind": "text", "body_md": "Your requested research started.", "actions": [], "citations": [], "action_started": True}
    monkeypatch.setattr(execute, "execute_action", admit)
    for message in ["Who are my competitors for " + idea + "? Answer only.",
                    "Compare my competitors for " + idea + ". Do not read or research sources.",
                    "Who are my competitors for " + idea + "?"]:
        token = INPUT.set({"venture_id": first_venture, "original": message})
        try:
            reply = asyncio.run(execute._execute_action(first_venture, "research.competitors", actor_subject="person_owner"))
        finally:
            INPUT.reset(token)
        if message.endswith("?"):
            assert reply.get("action_started") and calls == [(first_venture, "research.start", "person_owner")]
        else:
            assert reply == empty and not calls


@pytest.mark.parametrize("idea", ["Invoice checks for purchasing teams", "Meal planning for home cooks"])
def test_competitor_shortcut_keeps_classified_action_and_source_authority(first_venture, monkeypatch, idea):
    import asyncio
    from app.chat import guard, idea_bridge, intent, other_venture, router
    from app.chat.context import ChatContext

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    calls, requests = [], []
    recognizes_request = intent.competitor_research_request
    async def execute(venture_id, action, **kwargs):
        calls.append(action)
        return {"kind": "text", "body_md": "Your requested research is saved.", "actions": [], "citations": []}
    def requested(text):
        recognized = recognizes_request(text)
        if recognized:
            requests.append(text)
        return recognized
    monkeypatch.setattr(router, "execute_action", execute)
    monkeypatch.setattr(intent, "competitor_research_request", requested)
    monkeypatch.setattr(guard, "guard_reply", lambda *args, **kwargs: None)
    monkeypatch.setattr(other_venture, "other_venture_reply", lambda *args: None)
    monkeypatch.setattr(idea_bridge, "written_request", lambda *args: False)
    context = ChatContext(venture={"id": first_venture})
    message = "Find my competitors for " + idea
    for action, source in [("research.start", None), ("research.competitors", None), ("research.competitors", "message_saved")]:
        asyncio.run(router._prepare_intent(first_venture, context, action, message, None, source_message_id=source))
        assert calls[-1] == action
    assert requests and all(request == message for request in requests)
    assert calls == ["research.start", "research.competitors", "research.competitors"]
    before = list(requests)
    asyncio.run(router._prepare_intent(first_venture, context, "research.competitors", "Do not research my competitors for " + idea, None))
    assert requests == before


@pytest.mark.parametrize("idea", ["Invoice checks for purchasing teams", "Meal planning for home cooks"])
def test_requested_competitor_research_keeps_the_requested_followup_plan(first_venture, monkeypatch, idea):
    import asyncio
    from app.chat import llm_router, router

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    calls = []
    async def route(*args, **kwargs):
        return llm_router.Decision(acts=[("research.start", "Find my competitors for " + idea), ("plan_generate", "Create my execution plan")])
    async def dispatch(venture_id, context, action, text, speaker, *args, **kwargs):
        calls.append(action)
        return {"kind": "text", "body_md": "Your request is saved.", "actions": [], "citations": []}
    monkeypatch.setattr(llm_router, "route", route)
    monkeypatch.setattr(router, "_dispatch_intent", dispatch)
    asyncio.run(router.handle_message(first_venture, "Find my competitors for " + idea + " and then create my execution plan"))
    assert calls == ["research.start", "plan_generate"]


@pytest.mark.parametrize("domain", ["invoice.example", "recipes.example"])
def test_public_link_answers_read_sources_before_any_model(first_venture, monkeypatch, domain):
    import asyncio
    from app.chat import assistant, link_context, execute

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    calls = []
    monkeypatch.setattr(execute, "local_capability_reply", lambda name: None)
    monkeypatch.setattr(link_context, "read_links", lambda text, **kwargs: calls.append(text) or [("https://" + domain, "A public source describes its audience.")])
    monkeypatch.setattr(assistant, "get_venture_state_brief", lambda *args, **kwargs: pytest.fail("read fell into generic answer"))
    reply = asyncio.run(assistant.answer_question("ven_source", "Read https://" + domain + " and summarize its public facts."))
    assert len(calls) == 1
    assert "A public source describes its audience." in reply["body_md"]
    assert reply["citations"] == [{"label": domain, "href": "https://" + domain}]
    assert not reply["actions"]
    import secrets
    credential = "sk-" + secrets.token_hex(32)
    monkeypatch.setattr(link_context, "read_links", lambda *args, **kwargs: [("https://" + domain, credential)])
    rejected = asyncio.run(assistant.answer_question("ven_source", "Read https://" + domain))
    assert "without keys or passwords" in rejected["body_md"]
    if credential in str(rejected):
        pytest.fail("Your page reader returned a credential.")


@pytest.mark.parametrize("domain", ["invoice.example", "recipes.example"])
def test_link_reads_stop_at_research_setup_without_legacy_fallback(first_venture, monkeypatch, domain):
    import asyncio
    from app.chat import assistant, link_context, execute

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    blocked = {"kind": "text", "body_md": "In Settings > Your capabilities, switch on Research.", "actions": [], "citations": []}
    monkeypatch.setattr(execute, "local_capability_reply", lambda name: blocked if name == "research" else None)
    monkeypatch.setattr(link_context, "read_links", lambda *args, **kwargs: pytest.fail("read despite Research setup"))
    assert asyncio.run(assistant.answer_question("ven_source", "Read https://" + domain)) == blocked


@pytest.mark.parametrize("domain", ["invoice.example", "recipes.example"])
def test_link_reads_honor_your_prohibition_before_transport(first_venture, monkeypatch, domain):
    import asyncio
    from app.chat import assistant, link_context

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(link_context, "read_links", lambda *args, **kwargs: pytest.fail("a prohibited page was read"))
    reply = asyncio.run(assistant.answer_question("ven_source", "Do not open https://" + domain + ". Explain how I can review it instead."))
    assert "no link was opened" in reply["body_md"] and "Paste the passage" in reply["body_md"]
    assert not reply["actions"] and not reply["citations"]


@pytest.mark.parametrize("idea", ["Invoice checks for purchasing teams", "Meal planning for home cooks"])
def test_reviewed_test_keeps_selection_and_source_authority(first_venture, monkeypatch, idea):
    import asyncio
    from fastapi import HTTPException
    from app.chat import experiments, router
    from app.chat.context import ChatContext
    from app.tasks import chat as task_chat

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    text = "Run the test from the review for " + idea
    calls = []
    monkeypatch.setattr(experiments, "scan_request", lambda request: "reviews")
    monkeypatch.setattr(experiments, "scan_prepare", lambda *args: pytest.fail("Your selected test became a generic review scan"))
    monkeypatch.setattr(experiments, "named_prepare", lambda *args: pytest.fail("Your selected test became a generic experiment"))

    async def dispatch(venture_id, intent, request):
        calls.append((venture_id, intent, request))
        return {"kind": "text", "body_md": "Your selected test is prepared.", "actions": [], "citations": []}

    monkeypatch.setattr(experiments, "dispatch", dispatch)
    context = ChatContext(venture={"id": first_venture})
    reply = asyncio.run(router._prepare_intent(first_venture, context, "experiment_from_review", text, None))
    assert reply["body_md"] == "Your selected test is prepared."
    assert calls == [(first_venture, "experiment_from_review", text)]

    def denied(*args, **kwargs):
        raise HTTPException(404, "Your saved source is unavailable.")

    monkeypatch.setattr(task_chat, "reply_authority", denied)
    with pytest.raises(HTTPException) as blocked:
        asyncio.run(router._prepare_intent(first_venture, context, "experiment_from_review", text, None,
                                          source_message_id="unverified-review"))
    assert blocked.value.status_code == 404
    assert calls == [(first_venture, "experiment_from_review", text)]


@pytest.mark.parametrize("idea", ["Invoice checks for purchasing teams", "Meal planning for home cooks"])
def test_requested_scan_dispatches_its_existing_checked_action_once(first_venture, monkeypatch, idea):
    import asyncio
    from app.chat import router, execute
    from app.chat.context import ChatContext

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    calls = []
    prepared = {"kind": "experiment_autonomous_offer", "body_md": "Your scan is ready.",
                "actions": [{"action": "experiment.run", "payload": {"experiment_id": "exp_requested"}}], "citations": []}
    async def prepare(*args, **kwargs):
        return prepared
    async def start(venture_id, reply, actor, *, actions):
        calls.append((venture_id, actor, actions))
        return {"kind": "text", "body_md": "Your requested scan started.", "actions": [], "citations": [], "action_started": True}
    monkeypatch.setattr(router, "_prepare_intent", prepare)
    monkeypatch.setattr(execute, "local_capability_reply", lambda name: None)
    monkeypatch.setattr(execute, "start_requested_work", start)
    context = ChatContext(venture={"id": "ven_scan"})
    reply = asyncio.run(router._dispatch_intent("ven_scan", context, "change", "Run a demand scan for " + idea, None, "person_owner"))
    assert reply["action_started"] and calls == [("ven_scan", "person_owner", {"experiment.run"})]


@pytest.mark.parametrize("idea", ["Invoice checks for purchasing teams", "Meal planning for home cooks"])
def test_customer_search_uses_research_switch_recovery(first_venture, monkeypatch, idea):
    from app.chat.execute import local_capability_reply
    from app.core import local_capabilities
    from app.research import local_providers

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_capabilities, "enabled", lambda name: name != "research")
    monkeypatch.setattr(local_capabilities, "capability", lambda name: {"state": "off" if name == "research" else "ready"})
    monkeypatch.setattr(local_capabilities, "blocked_message", lambda name: "In Settings > Features, switch on Research." if name == "research" else "You can find customers.")
    monkeypatch.setattr(local_providers, "search_ready", lambda: False)
    reply = local_capability_reply("find_customers")
    assert reply["body_md"] == "In Settings > Features, switch on Research.", idea
    assert reply["actions"][0]["href"] == "/settings/local/capabilities"


@pytest.mark.parametrize("idea", ["Invoice checks for purchasing teams", "Meal planning for home cooks"])
def test_research_skip_keeps_the_requested_followup_plan(first_venture, monkeypatch, idea):
    import asyncio
    from app.chat import router
    from app.chat.context import ChatContext
    from app.ventures import journey_overrides

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    calls = []
    async def execute(venture_id, action, **kwargs):
        calls.append((action, None))
        return {"kind": "text", "body_md": "Your requested step completed.", "actions": [], "citations": []}
    async def dispatch(venture_id, context, intent, text, *args, **kwargs):
        calls.append((intent, text))
        return {"kind": "text", "body_md": "Your requested step completed.", "actions": [], "citations": []}
    monkeypatch.setattr(router, "execute_action", execute)
    monkeypatch.setattr(router, "_dispatch_intent", dispatch)
    monkeypatch.setattr(journey_overrides, "prepare", lambda *args: {"actions": [{"confirmation": {"resulting_state": {"step": "research"}}}]})
    message = "Skip research and create a provisional execution plan for " + idea
    tail = "create a provisional execution plan for " + idea
    context = ChatContext(venture={"id": first_venture})
    asyncio.run(router._prepare_intent(first_venture, context, "journey.skip_step", message, None))
    assert [intent for intent, _ in calls] == ["journey.skip_step", "plan_generate"]
    assert calls[1][1] == tail
    calls.clear()
    asyncio.run(router._prepare_intent(first_venture, context, "journey.skip_step", message + " if demand is proven", None))
    assert [intent for intent, _ in calls] == ["journey.skip_step"]


@pytest.mark.parametrize("idea", ["Invoice checks for purchasing teams", "Meal planning for home cooks"])
def test_requested_scan_checks_research_before_preparing_work(first_venture, monkeypatch, idea):
    import asyncio
    from app.chat import router, execute
    from app.chat.context import ChatContext

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    blocked = {"kind": "text", "body_md": "In Settings > Features, switch on Research.", "actions": [], "citations": []}
    async def forbidden(*args, **kwargs):
        pytest.fail("prepared a scan with Research off")
    monkeypatch.setattr(execute, "local_capability_reply", lambda name: blocked if name == "research" else None)
    monkeypatch.setattr(router, "_prepare_intent", forbidden)
    context = ChatContext(venture={"id": first_venture})
    reply = asyncio.run(router._dispatch_intent(first_venture, context, "experiment_start", "Run a demand scan for " + idea, None))
    assert reply == blocked


@pytest.mark.parametrize("idea", ["Equipment inspections for warehouse operators. Operators schedule safety checks per facility.",
                                  "Weekend walks for families. Parents find accessible trails and plan their trips."])
def test_requested_venture_creation_is_not_spend_but_extra_work_still_is(first_venture, idea):
    import asyncio
    from sqlalchemy import select
    from app.chat.router import handle_message
    from app.db.models import Job
    from app.state import db

    record = []
    undo = runner.install_probe(record)
    try:
        case = asyncio.run(runner.run_case(handle_message, first_venture, "Start a new venture: " + idea, record))
    finally:
        undo()
    case["expected"] = ["new_venture"]
    with db()() as session:
        jobs = list(session.scalars(select(Job.kind)))
    assert case["delta"] == [1] + [0] * 6 + [1], (case["delta"], jobs)
    assert not runner._spent(case)
    for index in range(7):
        delta = list(case["delta"])
        delta[index] += 1
        assert runner._spent({**case, "delta": delta})
    assert runner._spent({**case, "expected": ["ANSWER"]})
    assert runner._spent({**case, "seen": ["assistant"]})
    assert runner._spent({**case, "delta": [0] * 7 + [2]})
    assert runner._spent({**case, "new_jobs": [{**case["new_jobs"][0], "kind": "gtm.journey.prepare"}]})
    assert runner._spent({**case, "new_jobs": [{**case["new_jobs"][0], "venture_id": first_venture}]})
    assert runner._spent({**case, "new_jobs": [{**case["new_jobs"][0], "workspace_id": "foreign-workspace"}]})


def _replay_cases(cases):
    """Keep each replay's data outside pytest's shared temp retention."""
    with TemporaryDirectory(prefix="router-eval-") as directory, pytest.MonkeyPatch.context() as mp:
        tmp = Path(directory) / "data"
        mp.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp}/e.db")
        mp.setenv("COMPANY_OS_DATA_DIR", str(tmp))
        mp.setenv("SUPYMEM_LOCAL_PROXY_SECRET_FILE", str(tmp / "owner-session.secret"))
        mp.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp / "mem.sqlite3"))
        mp.setenv("COMPANY_OS_SECRETS_ENV", str(tmp / "import.env"))
        mp.setenv("LLM_OFFLINE", "1")
        mp.setenv("SUPYMEM_BETA_SCOPE", "0")  # module fixture runs before the autouse default; eval exercises the full registry
        mp.setenv("ONBOARDING_SYNC", "0")
        mp.setenv("CHAT_ROUTER_MODE", "replay")
        mp.setenv("CHAT_ROUTER_DECISIONS", str(runner.DECISIONS))
        mp.setenv("CHAT_ROUTER_RECORD", "0")
        def no_network(*args, **kwargs):
            raise OSError("Offline replay does not open network connections")
        mp.setattr(socket.socket, "connect", no_network)
        mp.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(0, 0, 0, "", ("93.184.216.34", 0))])
        main = load_company_os_app()
        from app.state import MEM

        with closing(MEM), TestClient(
            main.create_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 50000),
        ) as client:
            from app.core.local_mode import is_local
            if is_local():
                from core.local_security import _proxy_secret
                client.headers["X-Local-Proxy-Secret"] = _proxy_secret().decode("ascii")
            made = client.post("/ventures", json={"raw_idea": "LabHire helps QA engineers find matching roles", "name": "LabHire"})
            assert made.status_code == 201, made.text
            from app.chat import llm_router
            from app.chat.router import handle_message

            llm_router._REPLAY = None
            runner.install_replay_web(mp)  # No live web/network data in recorded-model CI.
            if is_local():
                from app.core import local_owner
                from app.tenancy import bind

                aliases = {"research": "research|research.start",
                           "start_product_build": "start_product_build|pagebuilder.build|build.tool.start"}
                cases = [(category, text, ["|".join(aliases.get(option, option) for option in expected.split("|"))
                    for expected in expectations]) for category, text, expectations in cases]
                owner = local_owner.ensure_owner()
                token = local_owner._OWNER.set(owner)
                try:
                    venture = made.json()
                    with bind(workspace_id=venture["workspace_id"], venture_id=venture["id"], actor=owner.id):
                        out = runner.run_all(partial(handle_message, actor_subject=owner.id), venture["id"], cases)
                finally:
                    local_owner._OWNER.reset(token)
            else:
                out = runner.run_all(handle_message, made.json()["id"], cases)
    summary = runner.score(out)
    return summary, out


def _replay_category(category):
    return _replay_cases([case for case in C if case[0] == category])


def _routing_failures(rows):
    return [f"[{row['cat']}] {row['text'][:70]!r} exp={row['expected']} seen={row['seen']} cards={row['cards']}"
            for row in rows if not row["ok"]]


@pytest.mark.parametrize("topic", ["warehouse inventory audits", "family hiking plans"])
def test_replay_keeps_sqlite_when_pytest_temp_is_recreated(monkeypatch, tmp_path_factory, tmp_path, topic):
    from shutil import rmtree

    replay_temp = tmp_path / "pytest-managed"
    replay_temp.mkdir()
    monkeypatch.setattr(tmp_path_factory, "mktemp", lambda *args, **kwargs: replay_temp)
    run_all = runner.run_all
    question = f"What should I consider for {topic}? Answer only."

    def after_cleanup(handle_message, venture_id, cases):
        rmtree(replay_temp)
        replay_temp.mkdir()
        return run_all(handle_message, venture_id, cases)

    monkeypatch.setattr(runner, "run_all", after_cleanup)
    summary, _ = _replay_cases([("question", question, ["ANSWER"])])
    assert summary["correct"] == summary["n"] == 1


@pytest.mark.parametrize("categories", REPLAY_BATCHES, ids="-".join)
def test_replay_batch_preserves_routing_accuracy_and_safety(categories):
    rows = []
    for category in categories:
        summary, out = _replay_category(category)
        assert summary["correct"] / summary["n"] >= 0.85, _routing_failures(out)
        rows.extend(out)
    # A requested rename now really runs, so its "Done" is true.
    bad = [(r["text"][:70], r["body"][:100]) for r in rows if (r["false_claim"] and "Done. I'm" not in r["body"]) or r["wrong_spend"] or r["wrong_card"]]
    assert not bad, bad
    assert round(100 * sum(row["ok"] for row in rows) / len(rows), 1) >= MIN_CORRECT_PCT, _routing_failures(rows)


def test_long_autonomy_request_only_offers_reviewed_mode_enable():
    _, out = _replay_category("long")
    from app.core.local_mode import is_local

    row = next(r for r in out if r["text"].startswith("Long story short the product is working"))
    assert row["seen"] == ["god_on"]
    if is_local():
        assert row["cards"] == []
        assert "Settings" in row["body"]
    else:
        assert row["cards"] == ["autopilot.god_on"]
    assert not any(row["delta"])


def test_generic_research_mislabel_keeps_review_card_not_specialist_search():
    _, out = _replay_category("research")
    from app.core.local_mode import is_local

    row = next(r for r in out if r['text'] == 'can you research the competitors for me')
    assert row['seen'] == ['research.start' if is_local() else 'research']
    # The eval venture's idea is still in website recon, so research lists its steps first instead of quoting.
    assert row['cards'] == [] and row['kind'] == 'needs_you' and not any(row['delta'])
    assert 'before research' in row['body'] and 'Nothing was started' in row['body']


@pytest.mark.parametrize("query", ["Public safety pages for warehouse managers", "Public trail planning pages for families"])
def test_replay_search_boundary_exercises_renderer_without_network(monkeypatch, query):
    import asyncio
    load_company_os_app()
    from app.research import explore
    runner.install_replay_web(monkeypatch)
    value = asyncio.run(explore.explore_web(query))
    assert value['provider'] == 'synthetic_eval_source'
    assert value['hits'][0]['url'] == 'https://example.com/eval-source'
    assert value['pages'][0]['excerpt'] == 'Synthetic recorded source text; not a live-source finding.'
    assert value['platforms'] == []


def test_replay_search_registration_does_not_admit_unknown_transports(monkeypatch):
    import asyncio
    load_company_os_app()
    from app.credits import metered
    from app.credits.errors import CreditsUnavailable
    from app.research import explore
    runner.install_replay_web(monkeypatch)
    calls = []
    class UnknownProvider:
        name = "unknown_provider"
        async def search(self, query):
            calls.append(query)
            return []
    for guarded_search in (metered.search, explore.metered_search):
        with pytest.raises(CreditsUnavailable, match="search provider pricing"):
            asyncio.run(guarded_search(UnknownProvider(), "public fixture"))
    assert calls == []


def test_replay_score_still_rejects_unsupported_effect_claim():
    load_company_os_app()
    result = {'body': 'I have published the website', 'expected': ['ANSWER'], 'seen': ['assistant'],
              'cards': [], 'delta': [0] * 7, 'kind': 'text', 'latency': 0, 'cat': 'adversarial', 'text': 'synthetic'}
    assert runner.score([result])['false_claims'] == 1


@pytest.mark.parametrize("draft_request", ["draft shop appointment emails", "draft emails for family music lessons"])
def test_requested_private_draft_only_allows_its_internal_task(draft_request):
    result = {"body": "Here are the drafts for your review. Nothing was sent.", "expected": ["outreach_draft"],
              "seen": ["outreach_draft"], "cards": [], "delta": [0, 1, 0, 0, 0, 0, 0],
              "kind": "text", "latency": 0, "cat": "gtm", "text": draft_request}
    assert runner.score([result])["wrong_spend"] == 0
    for index in (0, 2, 3, 4, 5, 6):
        delta = result["delta"].copy()
        delta[index] = 1
        assert runner.score([{**result, "delta": delta}])["wrong_spend"] == 1
    assert runner.score([{**result, "body": "I have sent the emails"}])["false_claims"] == 1
