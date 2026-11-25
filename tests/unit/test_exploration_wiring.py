"""Service journeys use real queued runners with deterministic provider fixtures."""
import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.company_os_harness import local_chat as local_chat

from tests.unit.test_exploration import exploration_market, market, seed, venture
from tests.unit.test_autonomous_experiments import confirm_saved_idea, credit_meter


@pytest.fixture(autouse=True)
def _day_one_plan_card_on(monkeypatch, local_chat):
    monkeypatch.setenv("JOB_WORKSPACE_INFLIGHT_CAP", "9")
    monkeypatch.setenv("SUPYMEM_DAY_ONE_PLAN_CARD", "1")  # production keeps this card off


@pytest.fixture(autouse=True)
def charlie_ideas(exploration_market, monkeypatch):
    from app.experiments import exploration
    from llm_gateway.routing import resolve_tier
    import re

    view = exploration_market[3]
    calls = []
    async def ideas(messages, **kwargs):
        assert resolve_tier(kwargs["purpose"]) == "flash"
        calls.append(messages)
        count = int(re.search(r"Suggest exactly (\d+)", messages[0]["content"])[1])
        drafts = [{"type": "who", "statement": f"You could serve group {len(calls)}-{index} of {view['icp']['who']}.",
            "audience": f"Group {len(calls)}-{index} of {view['icp']['who']}",
            "problem": view["icp"]["problem"], "confidence": 0.2} for index in range(count)]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"beliefs": drafts})))])
    monkeypatch.setattr(exploration.metered, "llm", ideas)


async def run_parallel(venture_id, workspace_id, provider, monkeypatch, *, successful=True, expected_jobs=9):
    from app.chat.conversations import bind_job_conversation
    from app.core.jobs import complete_job, lease
    from app.db.supymem_models import Experiment
    from app.db.session import serialize_writes
    from app.experiments import autonomous
    from app.state import db
    from app.worker import handlers

    handlers.install_default_handlers()
    with db()() as session:
        jobs = lease(session, "exploration-test", kinds=[autonomous.KIND], n=expected_jobs)
        session.commit()
    assert len(jobs) == expected_jobs
    entered = 0
    ready = asyncio.Event()
    search = provider.search

    async def overlap(query, **kwargs):
        nonlocal entered
        entered += 1
        if entered >= len(jobs):
            ready.set()
        await asyncio.wait_for(ready.wait(), 10)
        return await search(query, **kwargs)

    monkeypatch.setattr(provider, "search", overlap)

    async def dispatch(job):
        payload = {**job.payload, "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts}
        with bind_job_conversation(payload, venture_id=venture_id, workspace_id=workspace_id):
            await handlers.dispatch(job.kind, payload)
        with db()() as session:
            serialize_writes(session, key="market-experiments:" + venture_id)
            complete_job(session, job.id, worker_id=job.worker_id, attempt=job.attempts)
            session.commit()

    await asyncio.gather(*(dispatch(job) for job in jobs))
    with db()() as session:
        rows = list(session.scalars(select(Experiment).where(Experiment.venture_id == venture_id,
            Experiment.id.in_([job.payload["experiment_id"] for job in jobs]))))
        assert len(rows) == expected_jobs and all(row.started_at and row.ended_at for row in rows)
        assert max(row.started_at for row in rows) < min(row.ended_at for row in rows)
        assert all(json.loads(row.context).get("result" if successful else "error") for row in rows)
    return jobs


def test_j1_idea_card_parallel_comparison_selection_and_next_step(exploration_market, credit_meter, monkeypatch):
    from app.chat import next_moves, router
    from app.chat.execute import execute_action
    from app.chat.store import append_message, list_messages
    from app.core.local_mode import is_local
    from app.experiments import activity, exploration

    _client, vid, wid, view, _landing, provider, calls, *_rest = exploration_market
    seed(exploration_market)

    async def journey():
        message = f"My idea is {view['icp']['problem']} for {view['icp']['who']}"
        if is_local():
            message = f"Explore three options for {view['icp']['problem']} for {view['icp']['who']}"
        card = await router.handle_message(vid, message)
        assert card["kind"] == "exploration_options" and len(card["options"]) == 3
        assert card["round_id"] != card["id"]
        assert not calls
        def refused(_session, _scope):
            raise PermissionError("The output authority expired")
        with pytest.raises(PermissionError):
            append_message(vid, role="assistant", kind=card["kind"], body_md=card["body_md"],
                actions=card["actions"], citations=[{"exploration_options": {"round_id": card["round_id"]}}],
                output_authority=refused)
        started = await execute_action(vid, "experiment.exploration.start", payload={"round_id": card["round_id"]})
        assert started["action_started"] is True
        expected_jobs = 3 if is_local() else 9
        assert len(started["run_ids"]) == expected_jobs
        repeated = await execute_action(vid, "experiment.exploration.start", payload={"round_id": card["round_id"]})
        assert repeated["run_ids"] == started["run_ids"]
        await run_parallel(vid, wid, provider, monkeypatch, expected_jobs=expected_jobs)
        completed_runs = set(started["run_ids"])
        if is_local():
            while len(completed_runs) < 9:
                resumed = await execute_action(vid, "experiment.exploration.start", payload={"round_id": card["round_id"]})
                assert resumed["action_started"] is True
                assert 0 < len(resumed["run_ids"]) <= 3
                assert not completed_runs.intersection(resumed["run_ids"])
                await run_parallel(vid, wid, provider, monkeypatch, expected_jobs=len(resumed["run_ids"]))
                completed_runs.update(resumed["run_ids"])
        assert len(completed_runs) == 9
        events = activity.list_activity(vid)
        assert all(event["evidence_level"] == "desk_research" for event in events if event["action"] == "compare")
        assert {option["id"] for option in card["options"]} <= {event["option_id"] for event in events}
        assert exploration.get_round(vid, card["round_id"])["execution_mode"] == "parallel"
        saved = [message for message in list_messages(vid) if message["kind"] == "exploration_options"]
        assert len(saved) == 1 and saved[0]["id"] == card["id"]
        assert saved[0]["comparison"]["citations"]
        assert "searches" in saved[0]["comparison"]["summary"] and "sources" in saved[0]["comparison"]["summary"]
        assert not [message for message in list_messages(vid) if message["kind"] in {"experiment_result", "experiment_running"}]
        selected = await execute_action(vid, "experiment.exploration.select",
            payload={"round_id": card["round_id"], "option_id": card["options"][0]["id"]})
        assert selected["selected_option_id"] == card["options"][0]["id"]
        state = next_moves._snapshot(vid)
        assert state["exploration"]["option_id"] == selected["selected_option_id"]
        following = await execute_action(vid, "experiment.exploration.continue",
            payload={"round_id": card["round_id"], "option_id": selected["selected_option_id"]})
        if is_local():
            from app.db.supymem_models import Experiment
            from app.experiments import autonomous
            from app.state import db

            assert following["action_started"] is False and not following["run_ids"]
            with db()() as session:
                pending = [experiment for experiment in session.scalars(select(Experiment).where(
                    Experiment.venture_id == vid, Experiment.workspace_id == wid,
                    Experiment.state == "proposed"))
                    if json.loads(experiment.context or "{}").get("exploration_round_id") == card["round_id"]]
                assert pending
                for experiment in pending:
                    spec = json.loads(experiment.context)["spec"]
                    assert autonomous._run_blocked_reason(session, spec) == (
                        "You tested this hypothesis in the last 14 days. Wait or change the inputs before repeating it.")
        else:
            assert following["action_started"] is True and following["run_ids"]
            assert following["run_ids"][0] not in completed_runs

    asyncio.run(journey())
    assert view["allow_sending"] is False and view["auto_approve_first_emails"] is False


@pytest.mark.parametrize("enabled", [False, True], ids=["flag-off", "flag-on"])
def test_confirmed_startup_intake_posts_one_exploration_card(startup_intake, monkeypatch, enabled):
    from app.chat.store import list_messages
    from app.core.jobs import enqueue
    from app.core.local_mode import is_local
    from app.db.models import Job, Venture
    from app.db.supymem_models import EntryChatProposal
    from app.entry import chat_start
    from app.experiments import exploration, ledger
    from app.state import db
    from app.worker.handlers import dispatch, install_default_handlers
    from llm_gateway.routing import resolve_tier

    client, proposal_id, actor, idea, view = startup_intake
    monkeypatch.setattr(exploration, "get_policy", lambda _key: {"exploration_v1": enabled})
    expected_enabled = enabled or is_local()
    calls = []
    directions = []
    def queued(session, kind, payload, **kwargs):
        if kind == "experiment.exploration.prepare":
            with db()() as committed:
                proposal = committed.get(EntryChatProposal, proposal_id)
                assert proposal.state == "committed"
                assert proposal.target_venture_id == kwargs["venture_id"]
                assert committed.get(Venture, proposal.target_venture_id).raw_idea == idea
                directions.append(ledger.current_direction(committed, proposal.target_venture_id))
                assert directions[-1]
        return enqueue(session, kind, payload, **kwargs)
    monkeypatch.setattr("app.core.jobs.enqueue", queued)
    async def ideas(messages, **kwargs):
        calls.append(kwargs["purpose"])
        assert resolve_tier(kwargs["purpose"]) == "flash"
        with db()() as session:
            assert session.get(EntryChatProposal, proposal_id).state == "committed"
            assert ledger.current_direction(session, kwargs["venture_id"]) == directions[0]
        import re
        types = ["who"] * int(re.search(r"Suggest exactly (\d+)", messages[0]["content"])[1])
        drafts = [{"type": kind, "statement": f"You could try {kind} idea {len(calls)}-{index} for {view['icp']['who']}.",
            "audience": f"Group {index} of {view['icp']['who']}", "problem": view["icp"]["problem"], "confidence": 0.2}
            for index, kind in enumerate(types)]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"beliefs": drafts})))])
    monkeypatch.setattr(exploration.metered, "llm", ideas)
    response = client.post("/entry/chat/confirm", json={"proposal_id": proposal_id})
    assert response.status_code == 200, response.text
    receipt = response.json()
    vid = receipt["venture_id"]
    assert receipt["status"] == "committed" and receipt["created_new"] is True
    assert calls == []
    assert not [item for item in list_messages(vid) if item["kind"] == "exploration_options"]
    assert len([item for item in list_messages(vid) if item["kind"] == "entry_pending"]) == 1
    with db()() as session:
        assert session.scalar(select(Job).where(Job.venture_id == vid,
            Job.kind == "experiment.exploration.prepare")) is None
    complete_startup_intake(vid)
    with db()() as session:
        jobs = list(session.scalars(select(Job).where(Job.venture_id == vid, Job.kind == "experiment.exploration.prepare")))
        assert len(jobs) == int(expected_enabled)
        assert all(job.status == "scheduled" and job.attempts == 0 and job.max_attempts == 5 for job in jobs)
        payloads = [json.loads(job.payload_json) for job in jobs]
    if expected_enabled:
        install_default_handlers()
        async def run():
            from app.chat.conversations import bind_job_conversation
            with chat_start.service.venture_scope(vid, actor_id=actor, workspace_id=receipt["workspace_id"]):
                with bind_job_conversation(payloads[0], venture_id=vid, workspace_id=receipt["workspace_id"]):
                    await dispatch("experiment.exploration.prepare", payloads[0])
                    await dispatch("experiment.exploration.prepare", payloads[0])
        asyncio.run(run())
        messages = [item for item in list_messages(vid) if item["kind"] == "exploration_options"]
        assert len(messages) == 1 and len(messages[0]["options"]) == 3
        assert calls == ["experiment.exploration"]
        assert messages[0]["actions"][0]["action"] == "experiment.exploration.start"
    else:
        assert directions == [] and calls == []
    assert resolve_tier("experiment.beliefs") == "flash"
    assert resolve_tier("experiment.reviews") == "flash"
    assert asyncio.run(chat_start.confirm(proposal_id, actor)) == receipt
    with db()() as session:
        assert json.loads(session.get(EntryChatProposal, proposal_id).receipt_json) == receipt
        assert len(list(session.scalars(select(Job).where(Job.venture_id == vid,
            Job.kind == "experiment.exploration.prepare")))) == int(expected_enabled)
    assert len([item for item in list_messages(vid) if item["kind"] == "exploration_options"]) == int(expected_enabled)


@pytest.fixture
def startup_intake(exploration_market, monkeypatch):
    from app.chat import assistant
    from app.core.local_mode import is_local
    from app.db.models import Workspace
    from app.entry import chat_start
    from app.state import db

    async def narrate(*_args, **_kwargs):
        return "You can choose your next experiment in Experiments when you are ready."

    monkeypatch.setattr(assistant, "_complete", narrate)
    if is_local():
        from app.core.local_secrets import update_settings

        update_settings("capabilities", {"run_for_me_experiments": False}, {})
    client, _vid, wid, view, *_rest = exploration_market
    with db()() as session:
        actor = session.get(Workspace, wid).owner_id
    idea = f"A startup tool for {view['icp']['who']} to help with {view['icp']['problem']}."
    proposal = chat_start.preview(text=idea, actor_id=actor)["proposal"]
    assert proposal["route"] == "startup" and proposal["state"] == "pending"
    assert proposal["target_venture_id"] is None
    return client, proposal["id"], actor, idea, view


def complete_startup_intake(venture_id):
    from app.db.models import Venture
    from app.entry import chat_start
    from app.state import db
    from app.ventures import startup_review
    from app.ventures.current_idea import intake_pending

    assert intake_pending(venture_id)
    confirm_saved_idea(venture_id)
    with db()() as session:
        review_enabled = startup_review.enabled(session, session.get(Venture, venture_id).id)
    if review_enabled:
        while True:
            review = startup_review.get(venture_id)
            if not review["question"]:
                break
            startup_review.skip_question(venture_id, review["revision"], review["question"]["field"])
        startup_review._mutate(venture_id, review["revision"], "priorities", value="Learn about my market")
    assert not intake_pending(venture_id)
    chat_start.enqueue_ready_exploration(venture_id)


def assert_exploration_failure(venture_id, messages, caplog, *, queued):
    from app.chat.store import list_messages
    from app.core.local_mode import is_local

    posted = list_messages(venture_id)
    if queued:
        failures = [message for message in posted if message["kind"] == "experiment_failed"]
        assert len(failures) == 1 and posted[:-1] == messages and posted[-1] == failures[0]
        body = failures[0]["body_md"].lower()
        assert "your idea is saved" in body and "check your saved progress before trying again" in body
        assert all(word not in body for word in ("credit", "model", "connection", "private", "nothing was", "nothing launched"))
        retry = [action for action in failures[0]["actions"] if action["label"] == "Retry"]
        assert len(retry) == 1 and retry[0]["action"] == "status" and retry[0]["payload"] == {}
        assert not any(action.get("action") == "experiment.exploration.start" for action in failures[0]["actions"])
    else:
        assert posted == messages
    action = "status" if queued else "experiment.exploration.prepare"
    failed = [record for record in caplog.records if record.getMessage() == "founder_action_failed"
        and record.venture_id == venture_id and record.action == action]
    assert failed and all(record.error_type == "RuntimeError" for record in failed)
    for record in failed:
        diagnostic = f"RuntimeError at {record.error_location} code=chat_action_failed"
        if is_local():
            assert record.exc_info is None and record.exc_text == f"Exception: {diagnostic}"
        else:
            exc_type, safe_error, traceback = record.exc_info
            assert exc_type is Exception and traceback is None and str(safe_error) == diagnostic
    assert "PRIVATE" not in caplog.text and "test_secret" not in caplog.text
    assert not any(record.getMessage() == "entry_exploration_failed" for record in caplog.records)


def day_one_portfolio(startup_intake, monkeypatch, *, response_ready=True):
    from app.db.models import Venture
    from app.experiments import autonomous, belief_planner, ledger
    from app.state import db
    from tests.unit.test_market_beliefs import model_stub, spec

    view = startup_intake[-1]
    calls = model_stub(monkeypatch, view)
    monkeypatch.setattr(ledger, "ranking_inputs", lambda _vid: {})

    def available(vid, _beliefs):
        with db()() as session:
            saved = session.get(Venture, vid)
            direction, wid = ledger.current_direction(session, vid), saved.workspace_id
        research = spec(vid, wid, direction, "who", 0, cost=120000)
        response = {**spec(vid, wid, direction, "message", 1, cost=150000),
            "scan": "page_test", "page_kind": "headlines", "state": "running", "approval": "each_publish"}
        challenge = {**spec(vid, wid, direction, "pain", 2, cost=180000), "scan": "redteam"}
        for candidate, kind in ((research, "who"), (response, "message"), (challenge, "pain")):
            candidate["belief_ids"] = [belief["id"] for belief in _beliefs if belief["type"] == kind]
        candidates = [research, response, challenge] if response_ready else [research, challenge]
        for candidate in candidates:
            candidate["icp"] = deepcopy(view["icp"])
            candidate["hypothesis"] = f"You can help {view['icp']['who']}\u2014with {view['icp']['problem']}."
            candidate["method"] = candidate["method"].replace(" and ", "\u2014then ", 1)
            candidate["fingerprint"] = autonomous._fingerprint(candidate)
        return candidates

    monkeypatch.setattr(belief_planner, "available", available)
    return calls


@pytest.mark.parametrize("autopilot,exploration_enabled,response_ready", [
    (None, False, False), (False, True, False), ("true", True, False),
    (True, False, True), (True, True, True), (True, False, False)],
    ids=["default-off", "off-legacy", "literal-true-only", "autopilot-only", "both-flags", "response-needs-setup"])
@pytest.mark.hosted_only
def test_confirmed_startup_posts_one_first_experiment_plan(
        startup_intake, monkeypatch, autopilot, exploration_enabled, response_ready):
    from app.chat.store import list_messages
    from app.credits.policy import set_policy
    from app.db.models import Job
    from app.db.supymem_models import EntryChatProposal
    from app.entry import chat_start
    from app.experiments import belief_autopilot, beliefs, exploration
    from app.state import db
    from app.worker.handlers import dispatch, install_default_handlers

    client, proposal_id, actor, _idea, view = startup_intake
    flags = {} if autopilot is None else {"experiments_autopilot_v1": autopilot}
    set_policy("feature_flags", flags, updated_by="test")
    monkeypatch.setattr(exploration, "get_policy", lambda _key: {"exploration_v1": exploration_enabled})
    calls = day_one_portfolio(startup_intake, monkeypatch, response_ready=response_ready) if autopilot is True else []
    response = client.post("/entry/chat/confirm", json={"proposal_id": proposal_id})
    assert response.status_code == 200, response.text
    receipt = response.json()
    vid = receipt["venture_id"]
    assert receipt["status"] == "committed" and calls == []
    assert not [message for message in list_messages(vid) if message["kind"] in {"market_beliefs", "exploration_options"}]
    with db()() as session:
        assert session.get(EntryChatProposal, proposal_id).state == "committed"
        assert session.scalar(select(Job).where(Job.venture_id == vid,
            Job.kind == "experiment.exploration.prepare")) is None
    complete_startup_intake(vid)
    with db()() as session:
        jobs = list(session.scalars(select(Job).where(Job.venture_id == vid, Job.kind == "experiment.exploration.prepare")))
        assert len(jobs) == int(autopilot is True or exploration_enabled)
        payload = json.loads(jobs[0].payload_json) if jobs else None
    if payload:
        install_default_handlers()
        asyncio.run(dispatch("experiment.exploration.prepare", payload))
        before = list_messages(vid)
        asyncio.run(dispatch("experiment.exploration.prepare", payload))
        assert list_messages(vid) == before
    messages = list_messages(vid)
    plans = [message for message in messages if message["kind"] == "market_beliefs"]
    legacy = [message for message in messages if message["kind"] == "exploration_options"]
    if autopilot is True:
        assert len(plans) == 1 and legacy == [] and len(calls) == 1
        plan_message = plans[0]
        body = plan_message["body_md"]
        assert "Here is your first experiment plan" in body
        assert "1. Research:" in body and "Challenge:" in body
        assert ("2. Market response:" in body) == response_ready
        assert ("waits for an approved way to measure real responses" in body) != response_ready
        assert body.count("Why:") == (3 if response_ready else 2)
        assert body.count("Time:") == body.count("Cost:") == body.count("Contacts people: no.")
        assert "Contacts people: yes" not in body
        assert view["icp"]["who"] in body and view["icp"]["problem"] in body
        assert "$0.12" in body and "research runs automatically" in body and "0 per day" in body
        assert "\u2014" not in body
        confirmations = [action for action in plan_message["actions"] if action.get("confirmation")]
        assert len(confirmations) == 1
        confirmation = confirmations[0]["confirmation"]
        assert confirmations[0]["label"] == "Approve weekly experiment budget"
        assert confirmation["action"] == "experiment.beliefs.approve"
        assert "renews every 7 days" in confirmation["interpretation"]
        assert all("\u2014" not in text for text in confirmation["consequences"])
        assert any("disclosure, opt-out and your sender address" in text for text in confirmation["consequences"])
        current = beliefs.load(vid)["plan"]
        assert current["state"] == "draft" and current["renewal"] == "weekly_until_revoked"
        assert current["max_emails_per_day"] == 0 and len(current["tests"]) == (3 if response_ready else 2)
        assert confirmation["resulting_state"] == {"plan_id": current["id"], "fingerprint": current["fingerprint"]}
        assert not view["allow_sending"] and not view["auto_approve_first_emails"]
        with db()() as session:
            assert not list(session.scalars(select(Job).where(Job.venture_id == vid, Job.kind == "experiment.run")))
        belief_autopilot.approve(vid, confirmation["resulting_state"])
        assert beliefs.load(vid)["plan"]["state"] == "active"
        asyncio.run(dispatch("experiment.exploration.prepare", payload))
        assert list_messages(vid) == messages
    else:
        assert plans == [] and len(legacy) == int(exploration_enabled)
        assert beliefs.load(vid).get("plan") is None
    assert asyncio.run(chat_start.confirm(proposal_id, actor)) == receipt
    with db()() as session:
        assert len(list(session.scalars(select(Job).where(Job.venture_id == vid,
            Job.kind == "experiment.exploration.prepare")))) == int(autopilot is True or exploration_enabled)


@pytest.mark.parametrize("failure", ["enqueue", "prepare", "informative-card", "append"])
def test_first_experiment_plan_failure_keeps_onboarding_committed(startup_intake, monkeypatch, caplog, failure):
    from app.chat import market_beliefs
    from app.chat.store import list_messages
    from app.core import jobs
    from app.core.local_mode import is_local
    from app.credits.policy import set_policy
    from app.db.models import Job
    from app.db.supymem_models import EntryChatProposal
    from app.entry import chat_start
    from app.experiments import exploration
    from app.state import db
    from app.worker.handlers import dispatch, install_default_handlers

    client, proposal_id, actor, _idea, _view = startup_intake
    set_policy("feature_flags", {"experiments_autopilot_v1": True}, updated_by="test")
    if is_local():
        from app.core.local_secrets import update_settings

        update_settings("capabilities", {"run_for_me_experiments": True}, {})
    monkeypatch.setattr(exploration, "get_policy", lambda _key: {})
    day_one_portfolio(startup_intake, monkeypatch)
    enqueue, append_message = jobs.enqueue, chat_start.append_message
    queue_error = RuntimeError("plan queue unavailable PRIVATE api_key=test_secret")
    prepare_error = RuntimeError("plan unavailable PRIVATE model/credit/connection hints api_key=test_secret")
    append_error = RuntimeError("plan message unavailable PRIVATE api_key=test_secret")

    def failed_enqueue(session, kind, payload, **kwargs):
        if kind == "experiment.exploration.prepare":
            raise queue_error
        return enqueue(session, kind, payload, **kwargs)

    async def failed_prepare(*_args, **_kwargs):
        if failure == "informative-card":
            return {"kind": "text", "body_md": "Nothing was launched.", "actions": [], "citations": []}
        raise prepare_error

    def failed_append(*args, **kwargs):
        if kwargs.get("kind") == "market_beliefs":
            raise append_error
        return append_message(*args, **kwargs)

    if failure == "enqueue":
        monkeypatch.setattr(jobs, "enqueue", failed_enqueue)
    elif failure in {"prepare", "informative-card"}:
        monkeypatch.setattr(market_beliefs, "prepare", failed_prepare)
    else:
        monkeypatch.setattr(chat_start, "append_message", failed_append)
    response = client.post("/entry/chat/confirm", json={"proposal_id": proposal_id})
    assert response.status_code == 200, response.text
    receipt = response.json()
    vid = receipt["venture_id"]
    messages = list_messages(vid)
    assert receipt["status"] == "committed"
    assert len([message for message in messages if message["kind"] == "entry_pending"]) == 1
    with db()() as session:
        assert session.scalar(select(Job).where(Job.venture_id == vid,
            Job.kind == "experiment.exploration.prepare")) is None
    complete_startup_intake(vid)
    with db()() as session:
        job = session.scalar(select(Job).where(Job.venture_id == vid, Job.kind == "experiment.exploration.prepare"))
        assert bool(job) == (failure != "enqueue")
        payload = json.loads(job.payload_json) if job else None
    if payload:
        install_default_handlers()
        if failure == "informative-card":
            asyncio.run(dispatch("experiment.exploration.prepare", payload))
            posted = list_messages(vid)
            assert len(posted) == len(messages) + 1
            assert posted[-1]["kind"] == "text" and "<!--employee:Charlie:" in posted[-1]["body_md"]
            assert posted[-1]["body_md"].endswith("\nNothing was launched.")
            asyncio.run(dispatch("experiment.exploration.prepare", payload))
            assert list_messages(vid) == posted
        else:
            with pytest.raises(RuntimeError, match="plan"):
                asyncio.run(dispatch("experiment.exploration.prepare", payload))
    if failure != "informative-card":
        assert_exploration_failure(vid, messages, caplog, queued=payload is not None)
    assert asyncio.run(chat_start.confirm(proposal_id, actor)) == receipt
    with db()() as session:
        saved = session.get(EntryChatProposal, proposal_id)
        assert saved.state == "committed" and json.loads(saved.receipt_json) == receipt
    if failure == "informative-card":
        assert not [record for record in caplog.records if record.getMessage() == "founder_action_failed"
            and record.venture_id == vid and record.action in {"status", "experiment.exploration.prepare"}]


@pytest.mark.parametrize("direction_changes", [0, 1, 2, 3], ids=["stable", "one-change", "two-changes", "retry-backoff"])
def test_intake_exploration_retries_direction_races_without_stale_options_or_duplicate_cards(
        startup_intake, monkeypatch, caplog, direction_changes):
    from app.chat.conversations import bind_job_conversation
    from app.chat.store import list_messages
    from app.core.jobs import RetryableError, complete_job, fail_job, lease
    from app.db.models import Job, Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.experiments import beliefs, exploration, ledger
    from app.state import db
    from app.ventures.current_idea import current_idea
    from app.worker.handlers import dispatch, install_default_handlers
    from core.clock import as_utc, utcnow
    from core.ids import new_id

    client, proposal_id, _actor, _idea, view = startup_intake
    original_icp = deepcopy(view["icp"])
    other_icp = ({"who": "amateur astronomers", "problem": "planning stargazing trips", "mode": "consumer"}
                 if original_icp["mode"] == "company" else
                 {"who": "dental clinic managers", "problem": "reducing missed appointments", "mode": "company"})
    response = client.post("/entry/chat/confirm", json={"proposal_id": proposal_id})
    assert response.status_code == 200, response.text
    vid = response.json()["venture_id"]
    with db()() as session:
        assert session.scalar(select(Job).where(Job.venture_id == vid,
            Job.kind == "experiment.exploration.prepare")) is None
    complete_startup_intake(vid)
    with db()() as session:
        claimed = lease(session, "direction-race-test", kinds=["experiment.exploration.prepare"])
        session.commit()
    assert len(claimed) == 1 and claimed[0].venture_id == vid
    prepared_directions = []
    request_ids = []

    def context(venture_id):
        with db()() as session:
            venture = session.get(Venture, venture_id)
            return {"direction": ledger.current_direction(session, venture_id)}, {
                "idea_and_research": current_idea(venture, session=session, for_display=True), "icp": deepcopy(view["icp"])}

    async def generate(messages, **kwargs):
        inputs = json.loads(messages[1]["content"])
        audience, problem = view["icp"]["who"], view["icp"]["problem"]
        assert audience in inputs["idea_and_research"] and audience in inputs["founder_request"]
        request_ids.append(kwargs["request_id"])
        with db()() as session:
            prepared_directions.append(ledger.current_direction(session, vid))
            if len(prepared_directions) <= direction_changes:
                view["icp"].update(other_icp if len(prepared_directions) % 2 else original_icp)
                idea_session = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == vid)
                    .order_by(IdeaSession.created_at.desc()).limit(1))
                previous = session.get(IdeaRevision, idea_session.current_revision_id)
                revision = IdeaRevision(id=new_id("rev"), session_id=idea_session.id,
                    workspace_id=idea_session.workspace_id, venture_id=vid, rev=previous.rev + 1, source="charlie",
                    summary=f"A startup tool for {view['icp']['who']} to help with {view['icp']['problem']}.",
                    structured="{}", created_at=utcnow())
                session.add(revision)
                idea_session.current_revision_id = revision.id
                session.commit()
        drafts = [{"type": "who", "statement": f"You could serve group {index} of {audience} to help with {problem}.",
            "audience": f"Group {index} of {audience}", "problem": problem, "confidence": 0.2} for index in range(3)]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"beliefs": drafts})))])

    monkeypatch.setattr(beliefs, "context", context)
    monkeypatch.setattr(exploration.metered, "llm", generate)
    install_default_handlers()

    def run(job):
        with bind_job_conversation(job.payload, venture_id=vid, workspace_id=job.workspace_id):
            asyncio.run(dispatch(job.kind, job.payload))

    job = claimed[0]
    if direction_changes == 3:
        with pytest.raises(RetryableError, match="exploration_direction_changed") as raised:
            run(job)
        assert len(prepared_directions) == 3 and not beliefs.load(vid).get("rounds")
        assert not [item for item in list_messages(vid) if item["kind"] == "exploration_options"]
        complete_startup_intake(vid)
        with db()() as session:
            failed_at = utcnow()
            assert fail_job(session, job.id, raised.value, now=failed_at,
                            worker_id=job.worker_id, attempt=job.attempts) == "retry_wait"
            session.commit()
            saved_job = session.get(Job, job.id)
            assert saved_job.last_error == "exploration_direction_changed"
            assert as_utc(saved_job.run_after) > as_utc(failed_at)
            claimed = lease(session, "direction-race-test", kinds=[job.kind], now=saved_job.run_after)
            session.commit()
        assert len(claimed) == 1 and claimed[0].attempts == 2
        job = claimed[0]
        assert any(record.message == "exploration_direction_changed" for record in caplog.records)
    run(job)
    run(job)
    with db()() as session:
        direction = ledger.current_direction(session, vid)
        complete_job(session, job.id, worker_id=job.worker_id, attempt=job.attempts)
        session.commit()
        assert session.get(Job, job.id).status == "succeeded"
    rounds = beliefs.load(vid)["rounds"]
    cards = [item for item in list_messages(vid) if item["kind"] == "exploration_options"]
    assert len(rounds) == len(cards) == 1 and len(cards[0]["options"]) == 3
    assert rounds[0]["direction"] == prepared_directions[-1] == direction
    assert all(view["icp"]["who"] in item["statement"] and view["icp"]["problem"] in item["statement"]
               for item in rounds[0]["options"])
    assert len(prepared_directions) == direction_changes + 1
    assert len(set(request_ids)) == len(request_ids)


@pytest.mark.parametrize("failure", ["enqueue", "exploration", "append"])
def test_exploration_failure_does_not_change_intake_confirmation(startup_intake, monkeypatch, caplog, failure):
    from app.chat.store import list_messages
    from app.core import jobs
    from app.db.models import Job
    from app.db.supymem_models import EntryChatProposal
    from app.entry import chat_start
    from app.experiments import exploration
    from app.state import db
    from app.worker.handlers import dispatch, install_default_handlers

    client, proposal_id, actor, _idea, _view = startup_intake
    enqueue = jobs.enqueue
    append_message = chat_start.append_message
    exploration_error = RuntimeError("exploration unavailable PRIVATE model/credit/connection hints api_key=test_secret")
    queue_error = RuntimeError("exploration queue unavailable PRIVATE api_key=test_secret")
    append_error = RuntimeError("exploration message unavailable PRIVATE api_key=test_secret")
    async def unavailable(*_args, **_kwargs):
        raise exploration_error
    def failed_enqueue(session, kind, payload, **kwargs):
        if kind == "experiment.exploration.prepare":
            raise queue_error
        return enqueue(session, kind, payload, **kwargs)
    def failed_append(*args, **kwargs):
        if kwargs.get("kind") == "exploration_options":
            raise append_error
        return append_message(*args, **kwargs)
    if failure == "enqueue":
        monkeypatch.setattr(jobs, "enqueue", failed_enqueue)
    elif failure == "exploration":
        monkeypatch.setattr(exploration, "act", unavailable)
    else:
        monkeypatch.setattr(chat_start, "append_message", failed_append)
    response = client.post("/entry/chat/confirm", json={"proposal_id": proposal_id})
    assert response.status_code == 200, response.text
    receipt = response.json()
    vid = receipt["venture_id"]
    messages = list_messages(vid)
    normal = [item for item in messages if item["kind"] == "entry_pending"]
    assert receipt["status"] == "committed" and len(normal) == 1
    assert receipt["message"] in normal[0]["body_md"]
    with db()() as session:
        assert session.scalar(select(Job).where(Job.venture_id == vid,
            Job.kind == "experiment.exploration.prepare")) is None
    complete_startup_intake(vid)
    with db()() as session:
        job = session.scalar(select(Job).where(Job.venture_id == vid, Job.kind == "experiment.exploration.prepare"))
        assert bool(job) == (failure != "enqueue")
        payload = json.loads(job.payload_json) if job else None
    if payload:
        install_default_handlers()
        with pytest.raises(RuntimeError, match="exploration"):
            asyncio.run(dispatch("experiment.exploration.prepare", payload))
    assert_exploration_failure(vid, messages, caplog, queued=payload is not None)
    assert asyncio.run(chat_start.confirm(proposal_id, actor)) == receipt
    with db()() as session:
        assert json.loads(session.get(EntryChatProposal, proposal_id).receipt_json) == receipt


@pytest.mark.hosted_only
def test_j3_too_small_next_step_starts_recorded_work(exploration_market, credit_meter, monkeypatch):
    from app.chat.execute import execute_action
    from app.chat.store import append_message
    from app.experiments import activity, exploration

    _client, vid, wid, _view, _landing, provider, *_rest = exploration_market
    seed(exploration_market)

    async def journey():
        pending = await execute_action(vid, "experiment.exploration.next")
        append_message(vid, role="assistant", body_md=pending["body_md"], kind=pending["kind"],
            citations=pending["citations"], actions=pending["actions"])
        started = await execute_action(vid, "experiment.exploration.start", payload={"round_id": pending["round_id"]})
        await run_parallel(vid, wid, provider, monkeypatch)
        compared = exploration.get_round(vid, pending["round_id"])
        assert compared["comparison"]["too_small"] is True and compared["selected_option_id"] is None
        assert activity.list_activity(vid)
        action = compared["actions"][0]
        following = await execute_action(vid, action["action"], payload=action["payload"])
        assert following.get("action_started") is True, following
        assert len(following["run_ids"]) == 9
        assert not set(started["run_ids"]) & set(following["run_ids"])

    asyncio.run(journey())


@pytest.mark.hosted_only
def test_flag_off_preserves_founder_routing_and_blocks_actions(exploration_market, monkeypatch):
    from app.chat import router
    from app.chat.execute import execute_action
    from app.chat.store import list_messages
    from app.experiments import exploration

    _client, vid, _wid, view, *_rest = exploration_market
    original = deepcopy(view)
    monkeypatch.setattr(exploration, "get_policy", lambda _key: {})
    called = []
    async def existing(*args, **kwargs):
        called.append(args)
        return {"kind": "text", "body_md": "Your saved next step.", "actions": [], "citations": []}
    monkeypatch.setattr(router, "_dispatch_intent", existing)
    result = asyncio.run(router.handle_message(vid, "Explore three options for my idea"))
    assert result["kind"] != "exploration_options" and called
    for action in ("start", "retry", "continue", "select", "next", "next_round"):
        with pytest.raises(KeyError):
            asyncio.run(execute_action(vid, "experiment.exploration." + action))
    assert not [message for message in list_messages(vid) if message["kind"] == "exploration_options"]
    assert view == original


def test_missing_search_connection_never_claims_to_start_tests(exploration_market, monkeypatch):
    from app.chat.execute import execute_action
    from app.db.models import Job
    from app.experiments import autonomous, exploration
    from app.state import db

    _client, vid, *_rest = exploration_market
    seed(exploration_market)
    monkeypatch.setattr(autonomous, "_provider", lambda: None)
    pending = asyncio.run(execute_action(vid, "experiment.exploration.next"))
    blocked = asyncio.run(execute_action(vid, "experiment.exploration.start", payload={"round_id": pending["round_id"]}))
    assert blocked["action_started"] is False and not blocked["run_ids"]
    assert blocked["state"] == "blocked_by_permission"
    assert blocked["actions"][0]["action"] == "connections.status"
    assert "search connection" in blocked["body_md"]
    with db()() as session:
        assert session.scalar(select(Job.id).where(Job.venture_id == vid, Job.kind == autonomous.KIND)) is None
    assert exploration.get_round(vid, pending["round_id"])["state"] == "blocked_by_permission"


@pytest.mark.hosted_only
def test_retry_starts_real_new_attempts_without_old_failures_masking_progress(exploration_market, credit_meter, monkeypatch):
    from app.chat.execute import execute_action
    from app.experiments import exploration

    _client, vid, wid, _view, _landing, provider, *_rest = exploration_market
    seed(exploration_market)
    original_search = provider.search
    async def failed_search(*_args, **_kwargs):
        raise RuntimeError("The configured provider rejected this test request")
    monkeypatch.setattr(provider, "search", failed_search)

    async def journey():
        pending = await execute_action(vid, "experiment.exploration.next")
        started = await execute_action(vid, "experiment.exploration.start", payload={"round_id": pending["round_id"]})
        await run_parallel(vid, wid, provider, monkeypatch, successful=False)
        assert exploration.get_round(vid, pending["round_id"])["state"] == "failed"
        monkeypatch.setattr(provider, "search", original_search)
        retried = await execute_action(vid, "experiment.exploration.retry", payload={"round_id": pending["round_id"]})
        assert retried["action_started"] is True and len(retried["run_ids"]) == 9
        assert not set(started["run_ids"]) & set(retried["run_ids"])
        assert all(option["state"] == "running" for option in retried["options"])
        await run_parallel(vid, wid, provider, monkeypatch)
        assert exploration.get_round(vid, pending["round_id"])["state"] != "failed"

    asyncio.run(journey())


def test_exploration_request_is_not_consumed_as_a_pending_answer(exploration_market, monkeypatch):
    from app.chat import idea_bridge, router

    _client, vid, *_rest = exploration_market
    seed(exploration_market)
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Exploring options must not answer an unrelated saved question")
    monkeypatch.setattr(idea_bridge, "pending_question", forbidden)
    card = asyncio.run(router.handle_message(vid, "Explore three options for my idea"))
    assert card["kind"] == "exploration_options" and len(card["options"]) == 3


def test_exploration_after_an_answer_keeps_the_pending_question(exploration_market, monkeypatch):
    from app.chat import idea_bridge, rephrase, router

    seen = []
    question = object()

    def pending(*_args, **_kwargs):
        seen.append("pending")
        return question

    async def handle(*_args, **_kwargs):
        seen.append(rephrase.INPUT.get()["questionnaire_waiting"])
        return {"kind": "text", "body_md": "Review your saved question in Ideas.", "actions": [], "citations": []}

    monkeypatch.setattr(idea_bridge, "pending_question", pending)
    monkeypatch.setattr(router, "_handle_message", handle)
    monkeypatch.setattr(router, "_continue_clarifying", lambda _venture, reply: reply)
    asyncio.run(router.handle_message(exploration_market[1], "Yes; explore three options for my idea"))
    assert seen == ["pending", question]


def test_failed_next_ideas_do_not_erase_results_or_fail_completed_research(exploration_market, credit_meter, monkeypatch):
    from app.chat.execute import execute_action
    from app.chat.store import append_message, list_messages
    from app.core.local_mode import is_local
    from app.experiments import exploration

    _client, vid, wid, _view, _landing, provider, *_rest = exploration_market
    seed(exploration_market, count=3)
    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("The ideas request was rejected")
    monkeypatch.setattr(exploration.metered, "llm", unavailable)

    async def journey():
        card = await execute_action(vid, "experiment.exploration.next")
        saved = append_message(vid, role="assistant", body_md=card["body_md"], kind=card["kind"],
            actions=card["actions"], citations=card["citations"])
        started = await execute_action(vid, "experiment.exploration.start", payload={"round_id": card["round_id"]})
        await run_parallel(vid, wid, provider, monkeypatch, expected_jobs=3 if is_local() else 9)
        completed_runs = set(started["run_ids"])
        if is_local():
            while len(completed_runs) < 9:
                resumed = await execute_action(vid, "experiment.exploration.start", payload={"round_id": card["round_id"]})
                assert resumed["action_started"] is True and 0 < len(resumed["run_ids"]) <= 3
                assert not completed_runs.intersection(resumed["run_ids"])
                await run_parallel(vid, wid, provider, monkeypatch, expected_jobs=len(resumed["run_ids"]))
                completed_runs.update(resumed["run_ids"])
        assert len(completed_runs) == 9
        messages = [item for item in list_messages(vid) if item["kind"] == "exploration_options"]
        assert len(messages) == 1 and messages[0]["id"] == saved["id"]
        assert messages[0]["comparison"]["citations"]
        assert exploration.get_round(vid, card["round_id"])["ended_at"]

    asyncio.run(journey())
