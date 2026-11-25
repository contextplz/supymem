"""Exploration preserves the same rules for unrelated B2B and consumer markets."""
import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.company_os_harness import configure_local_test_model, local_owner_clients
from tests.unit import test_autonomous_experiments


@pytest.fixture(autouse=True)
def _day_one_plan_card_on(monkeypatch):
    monkeypatch.setenv("SUPYMEM_DAY_ONE_PLAN_CARD", "1")  # production keeps this card off

market = test_autonomous_experiments.market
venture = test_autonomous_experiments.venture


@pytest.mark.parametrize("autopilot,missing", [
    (False, "audience"), (False, "problem"), (False, "both"), (False, "neither"), (True, "both"),
])
@pytest.mark.parametrize("domain,name,audience,problem", [
    ("invoiceharbor.example", "Invoice Harbor", "independent bookkeepers", "matching invoices to payments"),
    ("trailnest.example", "Trail Nest", "weekend hikers", "planning safe day hikes"),
])
def test_minimal_entry_exploration_handler(venture, monkeypatch, caplog, domain, name, audience, problem, autopilot, missing):
    from sqlalchemy import select
    from app.chat.store import list_messages
    from app.core.local_mode import is_local
    from app.credits.policy import set_policy
    from app.db.models import Job, Venture, Workspace
    from app.entry import chat_start
    from app.experiments import exploration
    from app.state import db
    from app.worker.handlers import dispatch, install_default_handlers

    _client, _vid, workspace_id = venture
    set_policy("feature_flags", {"exploration_v1": True, "experiments_autopilot_v1": autopilot}, updated_by="test")
    if is_local():
        from app.core.local_secrets import update_settings

        configure_local_test_model()
        update_settings("capabilities", {"run_for_me_experiments": autopilot}, {})
    with db()() as session:
        actor = session.get(Workspace, workspace_id).owner_id
    idea = f"A tool that helps with {problem}"
    proposal = chat_start.preview(text=idea, actor_id=actor)["proposal"]
    receipt = asyncio.run(chat_start.confirm(proposal["id"], actor, name=name))
    venture_id = receipt["venture_id"]
    workspace_id = receipt["workspace_id"]
    with db()() as session:
        saved = session.get(Venture, venture_id)
        assert saved.name == name and saved.raw_idea == idea
        job = session.scalar(select(Job).where(Job.venture_id == venture_id,
            Job.kind == "experiment.exploration.prepare"))
        assert job is None
    test_autonomous_experiments.confirm_saved_idea(venture_id)
    from app.ventures import startup_review
    from app.ventures.current_idea import intake_pending

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
    with db()() as session:
        job = session.scalar(select(Job).where(Job.venture_id == venture_id,
            Job.kind == "experiment.exploration.prepare"))
        assert job is not None
        payload = json.loads(job.payload_json)

    calls = []
    async def ideas(messages, **kwargs):
        calls.append(kwargs["purpose"])
        drafts = [{"type": "who", "statement": f"You could try group {index} of {audience}.",
            "confidence": 0.2,
            **({"audience": f"Group {index} of {audience}"} if missing not in {"audience", "both"} else {}),
            **({"problem": problem} if missing not in {"problem", "both"} else {})}
            for index in range(3)]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({"beliefs": drafts})))])

    monkeypatch.setattr(exploration.metered, "llm", ideas)
    install_default_handlers()
    asyncio.run(dispatch("experiment.exploration.prepare", payload))
    asyncio.run(dispatch("experiment.exploration.prepare", payload))
    cards = [item for item in list_messages(venture_id) if item["kind"] in {"exploration_options", "experiments"}]
    assert len(cards) == 1
    if missing in {"audience", "both"}:
        assert cards[0]["kind"] == "experiments"
    if cards[0]["kind"] == "exploration_options":
        assert len(cards[0]["options"]) == 3
    else:
        assert "who are you trying to reach" in cards[0]["body_md"]
        assert exploration._load(venture_id)["beliefs"] == []
        from app.entry import service
        from app.gtm import journey

        with service.venture_scope(venture_id, actor_id=actor, workspace_id=workspace_id):
            journey.update_icp(workspace_id, venture_id, audience, problem=problem, start=False)
        if autopilot:
            from app.chat import market_beliefs

            assert calls == []
            prepared = []
            async def plan(_venture_id, payload):
                assert payload == {"day_one": True, "max_emails_per_day": 0}
                prepared.append(_venture_id)
                return {"kind": "market_beliefs", "body_md": "Your market plan is ready to review.",
                    "actions": [{"label": "Review your plan", "confirmation": {"action": "experiment.beliefs.approve"}}],
                    "citations": []}
            monkeypatch.setattr(market_beliefs, "prepare", plan)
            asyncio.run(dispatch("experiment.exploration.prepare", payload))
            asyncio.run(dispatch("experiment.exploration.prepare", payload))
            assert prepared == []
            assert not [item for item in list_messages(venture_id) if item["kind"] == "market_beliefs"]
            assert not [record for record in caplog.records if record.getMessage() == "entry_exploration_failed"]
            return
        assert any(action["action"] == "experiment.exploration.next" for action in cards[0]["actions"])
        asyncio.run(dispatch("experiment.exploration.prepare", payload))
        asyncio.run(dispatch("experiment.exploration.prepare", payload))
        ready = [item for item in list_messages(venture_id) if item["kind"] == "exploration_options"]
        assert len(ready) == 1 and len(ready[0]["options"]) == 3
        assert all(item["audience"] and item["problem"] for item in exploration._load(venture_id)["beliefs"])
        waiting = {key: cards[0][key] for key in ("kind", "body_md", "actions", "citations")}
        async def wait_for_scope(_venture_id, _kind):
            return waiting
        monkeypatch.setattr(exploration, "start_round", wait_for_scope)
        with service.venture_scope(venture_id, actor_id=actor, workspace_id=workspace_id):
            following = asyncio.run(exploration.act(venture_id, "experiment.exploration.next_round",
                {"round_id": ready[0]["round_id"]}))
        assert following == waiting
    assert not [record for record in caplog.records if record.getMessage() == "entry_exploration_failed"]


@pytest.mark.parametrize("card", [
    {"kind": "market_beliefs", "body_md": "Your plan is ready to review.",
     "actions": [{"confirmation": {"action": "experiment.beliefs.approve"}}], "citations": []},
    {"kind": "experiments", "body_md": "Who are you trying to reach?", "actions": [], "citations": []},
    {"kind": "text", "body_md": "You still need to describe the problem.", "missing_field": "problem"},
    {"body_md": "You have no experiments ready yet."},
    {},
    {"body_md": "   "},
    None,
], ids=["approval", "question", "missing-info", "plain-text", "empty", "blank", "none"])
def test_day_one_posts_informative_cards_once_and_skips_empty(venture, monkeypatch, caplog, card):
    from app.core.local_mode import is_local
    from app.chat import market_beliefs
    from app.chat.store import list_messages
    from app.db.models import Person, Venture, Workspace
    from app.entry import chat_start
    from app.experiments import belief_autopilot
    from app.state import db

    caplog.set_level("INFO", logger=chat_start.__name__)
    monkeypatch.setattr(belief_autopilot, "enabled", lambda: True)
    calls = []
    async def prepare(vid, payload):
        assert payload == {"day_one": True, "max_emails_per_day": 0}
        calls.append(vid)
        return deepcopy(card)
    monkeypatch.setattr(market_beliefs, "prepare", prepare)
    for index, idea in enumerate(("Track machine maintenance for factory supervisors", "Book nature walks for weekend travellers")):
        actor, workspace_id, venture_id = f"usr_day_one_{index}", f"wsp_day_one_{index}", f"ven_day_one_{index}"
        now = chat_start.utcnow()
        with db()() as session:
            session.add(Person(id=actor, display_name=actor, created_at=now))
            session.add(Workspace(id=workspace_id, kind="company", owner_id=actor, name=actor,
                entry_mode="idea", created_at=now, updated_at=now))
            session.add(Venture(id=venture_id, workspace_id=workspace_id, name=actor,
                raw_idea=idea, stage="idea", created_at=now, updated_at=now))
            session.commit()
        test_autonomous_experiments.confirm_saved_idea(venture_id)
        if is_local():
            from app.ventures import startup_review

            current = startup_review.get(venture_id)
            while current["question"]:
                current = startup_review.skip_question(venture_id, current["revision"], current["question"]["field"])
            startup_review._mutate(venture_id, current["revision"], "priorities", value="Learn about my market")
        before = len(list_messages(venture_id))
        payload = {"venture_id": venture_id, "workspace_id": workspace_id, "actor_id": actor, "proposal_id": f"day-one-{index}"}
        asyncio.run(chat_start.run_exploration(payload))
        asyncio.run(chat_start.run_exploration(payload))
        messages = list_messages(venture_id)
        informative = bool(card and (card.get("body_md") or "").strip())
        assert len(messages) == before + int(informative)
        if informative:
            assert messages[-1]["body_md"].endswith("\n" + card["body_md"])
            assert "<!--employee:Charlie:" in messages[-1]["body_md"]
            assert messages[-1]["kind"] == card.get("kind", "text")
    assert len(calls) == (2 if card and (card.get("body_md") or "").strip() else 4)
    assert not [record for record in caplog.records if record.getMessage() == "entry_exploration_failed"]
    if not card or not (card.get("body_md") or "").strip():
        assert len([record for record in caplog.records if record.getMessage() == "entry_exploration_empty"]) == 4


@pytest.fixture
def exploration_market(market, monkeypatch):
    from app.experiments import exploration, ledger

    monkeypatch.setattr(exploration, "get_policy", lambda _key: {"exploration_v1": True})
    monkeypatch.setattr(ledger, "ranking_inputs", lambda _vid: {"journey": deepcopy(market[3])})
    return market


def seed(market, kind="segment", count=6):
    from app.experiments import beliefs, exploration
    from app.state import db

    _client, vid, _wid, view, *_rest = market
    with db()() as session:
        saved = beliefs.row(session, vid, create=True)
        current = json.loads(saved.context)
        current["beliefs"] = [{"id": f"belief_{index}", "type": exploration.BELIEF_TYPES[kind],
            "statement": f"You could try option {index} for {view['icp']['who']} to solve {view['icp']['problem']}.",
            "audience": f"Group {index} of {view['icp']['who']}", "problem": view["icp"]["problem"],
            "confidence": 0.2, "status": "to_test", "evidence": []} for index in range(count)]
        saved.context = json.dumps(current)
        session.commit()
    return current


def record(market, round_, index, *, state="concluded", measured=None, extra=None, scan="demand", runner="scan"):
    from app.db.supymem_models import Experiment
    from app.experiments import ledger
    from app.state import db

    _client, vid, wid, _view, *_rest = market
    ident = f"test_{round_['number']}_{index}"
    spec = {"id": ident, "direction": round_["direction"], "runner": runner, "scan": scan,
            "hypothesis": round_["options"][index]["statement"], "method": scan, "metric": "signal", "min_sample": 5,
            "success_threshold": {"op": ">=", "value": 3, "min_events": 5},
            "failure_threshold": {"op": "<", "value": 1, "min_events": 5},
            "belief_ids": [round_["options"][index]["belief_id"]]}
    context = {"runner": "system", "spec": spec, **(extra or {})}
    if measured is not None:
        context["result"] = {"measured": measured, "sources": [{"url": "https://source.example/discussion"}]}
    with db()() as session:
        row = session.get(Experiment, ident)
        if row is None:
            row = Experiment(id=ident, venture_id=vid, workspace_id=wid, kind=ledger.KIND,
                             hypothesis=spec["hypothesis"], state=state, context=json.dumps(context))
            session.add(row)
        else:
            row.state, row.context = state, json.dumps(context)
        session.commit()
    return spec


@pytest.mark.parametrize("kind", ["segment", "value_proposition", "product_approach", "market_gap", "demand_test"])
def test_round_has_three_distinct_same_kind_ideas_and_read_api_is_internal_level_free(exploration_market, kind):
    from app.experiments import beliefs, exploration

    client, vid, *_rest = exploration_market
    seed(exploration_market, kind)
    assert exploration.list_rounds(vid) == {"state": "empty", "status": "Idea", "rounds": []}
    round_ = asyncio.run(exploration.start_round(vid, kind))
    assert len(round_["options"]) == len({item["belief_id"] for item in round_["options"]}) == 3
    assert {item["evidence_level"] for item in round_["options"]} == {"hypothesis"}
    assert asyncio.run(exploration.start_round(vid, kind))["id"] == round_["id"]
    assert {item["type"] for item in beliefs.load(vid)["beliefs"]} == {exploration.BELIEF_TYPES[kind]}
    response = client.get(f"/ventures/{vid}/experiments/rounds")
    assert response.status_code == 200
    assert response.json()["rounds"][0]["status"] == "Idea"
    assert "evidence_level" not in response.text and "hypothesis" not in response.text
    assert all(item["status"] == "Idea" for item in response.json()["rounds"][0]["options"])


def test_unsupported_options_start_a_fresh_round_without_reusing_ideas(exploration_market):
    from app.experiments import beliefs, exploration

    _client, vid, *_rest = exploration_market
    seed(exploration_market)
    first = asyncio.run(exploration.start_round(vid))
    for index in range(3):
        record(exploration_market, first, index, measured={"events": 5, "signal": 0})
    result = asyncio.run(exploration.refresh(vid))
    assert len(result["rounds"]) == 2
    previous, following = result["rounds"]
    assert previous["state"] == "did_not_work" and previous["selected_option_id"] is None
    assert len(following["options"]) == 3 and following["state"] == "pending"
    assert {item["belief_id"] for item in previous["options"]}.isdisjoint(item["belief_id"] for item in following["options"])
    assert len(asyncio.run(exploration.refresh(vid))["rounds"]) == 2
    assert all(len(item["evidence"]) == 1 for item in beliefs.load(vid)["beliefs"][:3])


def test_recorded_actions_drive_transitions_without_claiming_drafts_were_sent(exploration_market):
    from app.experiments import beliefs, exploration

    _client, vid, *_rest = exploration_market
    seed(exploration_market)
    round_ = asyncio.run(exploration.start_round(vid))
    record(exploration_market, round_, 0, state="running")
    record(exploration_market, round_, 1, state="running", extra={"prepared": True, "assignments": {"contact": "arm"}}, runner="outreach_split")
    record(exploration_market, round_, 2, state="running", extra={"error": "Your research provider did not finish."})
    result = asyncio.run(exploration.refresh(vid))["rounds"][0]
    assert [item["state"] for item in result["options"]] == ["running", "blocked_by_permission", "failed"]
    assert result["state"] == "running" and result["selected_option_id"] is None
    internal = beliefs.load(vid)["rounds"][0]["options"][1]
    assert internal["evidence_level"] == "drafted_outreach" and not internal["finished"]
    record(exploration_market, round_, 0, state="running", measured={"events": 2, "signal": 3})
    result = asyncio.run(exploration.refresh(vid))
    assert len(result["rounds"]) == 1 and result["rounds"][0]["options"][0]["state"] == "too_early"
    assert result["rounds"][0]["options"][0]["status"] == "Too early to tell"


def test_selection_reuses_thresholds_prefers_market_results_and_carries_only_selected_scope(exploration_market):
    from app.experiments import autonomous, beliefs, exploration

    _client, vid, _wid, view, *_rest = exploration_market
    seed(exploration_market)
    round_ = asyncio.run(exploration.start_round(vid))
    scan = record(exploration_market, round_, 0, measured={"events": 100, "signal": 4})
    real = record(exploration_market, round_, 1, measured={"events": 5, "signal": 3}, scan="page_test", runner="observe")
    record(exploration_market, round_, 2, measured={"events": 5, "signal": 0})
    assert autonomous.outcome(scan, {"events": 100, "signal": 4}) == "supported"
    assert autonomous.outcome(real, {"events": 5, "signal": 3}) == "supported"
    result = asyncio.run(exploration.refresh(vid))["rounds"][0]
    assert result["selected_option_id"] == round_["options"][1]["id"] and result["status"] == "Worked"
    state = {"icp": deepcopy(view["icp"]), "allow_sending": False}
    continued = exploration.continue_selected(vid, state)
    assert continued["exploration"]["option_id"] == result["selected_option_id"]
    assert continued["icp"]["who"] == "Group 1 of " + view["icp"]["who"]
    assert state["icp"] == view["icp"] and continued["allow_sending"] is False
    assert beliefs.load(vid)["rounds"][0]["options"][1]["evidence_level"] == "conclusion"
    from app.state import db

    with db()() as session:
        saved = beliefs.row(session, vid)
        current = json.loads(saved.context)
        current["beliefs"].append({"id": "next_belief", "parent_id": round_["options"][1]["belief_id"]})
        current["plan"] = {"tests": [{"belief_id": "next_belief", "experiment_id": "next_test", "state": "queued"}]}
        saved.context = json.dumps(current)
        session.commit()
    assert exploration.continue_selected(vid, state)["experiment"] == {"id": "next_test", "state": "proposed"}


@pytest.mark.parametrize("events", [0, 2, None, True, "5", float("nan")])
def test_small_or_invalid_samples_never_select_and_always_offer_another_round(exploration_market, events):
    from app.experiments import exploration

    _client, vid, *_rest = exploration_market
    seed(exploration_market)
    round_ = asyncio.run(exploration.start_round(vid))
    for index in range(3):
        record(exploration_market, round_, index, measured={"events": events, "signal": 100})
    result = asyncio.run(exploration.refresh(vid))
    assert result["rounds"][0]["selected_option_id"] is None
    assert result["rounds"][0]["status"] == "Too early to tell" and len(result["rounds"]) == 2
    assert all(item["status"] == "Too early to tell" for item in result["rounds"][0]["options"])


def test_missing_candidates_use_metered_ideas_only_and_preserve_the_existing_plan(exploration_market, monkeypatch):
    import re
    from app.experiments import beliefs, exploration, ledger
    from app.state import db

    _client, vid, wid, view, *_rest = exploration_market
    seed(exploration_market, count=1)
    with db()() as session:
        direction = ledger.current_direction(session, vid)
        saved = beliefs.row(session, vid)
        current = json.loads(saved.context)
        current["plan"] = {"id": "existing_approval", "state": "draft"}
        saved.context = json.dumps(current)
        session.commit()
    monkeypatch.setattr(beliefs, "context", lambda _vid: ({"direction": direction}, {"icp": view["icp"]}))
    calls = []

    async def ideas(messages, **kwargs):
        calls.append((messages, kwargs))
        count = int(re.search(r"Suggest exactly (\d+)", messages[0]["content"]).group(1))
        drafts = [{"type": "who", "statement": f"You could try another group {len(calls)}-{index} of {view['icp']['who']}.",
                   "confidence": 0.3, "audience": f"Other group {len(calls)}-{index} of {view['icp']['who']}",
                   "problem": view["icp"]["problem"]} for index in range(count)]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"beliefs": drafts})))])

    monkeypatch.setattr(exploration.metered, "llm", ideas)
    round_ = asyncio.run(exploration.start_round(vid))
    assert len(round_["options"]) == 3 and len(calls) == 1
    assert calls[0][1]["workspace_id"] == wid and calls[0][1]["purpose"] == "experiment.exploration"
    current = beliefs.load(vid)
    assert current["plan"] == {"id": "existing_approval", "state": "draft"}
    assert all(item["evidence"] == [] for item in current["beliefs"])
    for index in range(3):
        record(exploration_market, round_, index, measured={"events": 5, "signal": 0})

    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("Your idea generator is unavailable.")

    monkeypatch.setattr(exploration.metered, "llm", unavailable)
    with pytest.raises(RuntimeError):
        asyncio.run(exploration.refresh(vid))
    assert len(beliefs.load(vid)["rounds"]) == 1
    monkeypatch.setattr(exploration.metered, "llm", ideas)
    assert len(asyncio.run(exploration.refresh(vid))["rounds"]) == 2
    assert len(beliefs.load(vid)["rounds"][1]["options"]) == 3


def test_unrun_guesses_and_foreign_direction_results_cannot_be_selected(exploration_market):
    from app.db.supymem_models import Experiment
    from app.experiments import exploration
    from app.state import db

    _client, vid, *_rest = exploration_market
    seed(exploration_market)
    round_ = asyncio.run(exploration.start_round(vid))
    record(exploration_market, round_, 0, state="proposed", measured={"events": 20, "signal": 10})
    record(exploration_market, round_, 1, measured={"events": 20, "signal": 10})
    with db()() as session:
        row = session.get(Experiment, "test_1_1")
        context = json.loads(row.context)
        context["spec"]["direction"] = "another_direction"
        row.context = json.dumps(context)
        session.commit()
    result = asyncio.run(exploration.refresh(vid))
    assert len(result["rounds"]) == 1 and result["rounds"][0]["selected_option_id"] is None
    assert {item["state"] for item in result["rounds"][0]["options"]} == {"pending"}


def test_market_failure_outweighs_supported_desk_research_for_the_same_option(exploration_market):
    from app.db.supymem_models import Experiment
    from app.experiments import beliefs, exploration, ledger
    from app.state import db

    _client, vid, wid, *_rest = exploration_market
    seed(exploration_market)
    round_ = asyncio.run(exploration.start_round(vid))
    for index in range(3):
        record(exploration_market, round_, index, measured={"events": 100, "signal": 4 if index == 0 else 0})
    with db()() as session:
        source = session.get(Experiment, "test_1_0")
        context = json.loads(source.context)
        context["spec"].update(id="market_failure", runner="observe", scan="page_test")
        context["result"]["measured"] = {"events": 5, "signal": 0}
        session.add(Experiment(id="market_failure", venture_id=vid, workspace_id=wid, kind=ledger.KIND,
            hypothesis=source.hypothesis, state="concluded", context=json.dumps(context)))
        session.commit()
    result = asyncio.run(exploration.refresh(vid))
    assert result["rounds"][0]["selected_option_id"] is None and len(result["rounds"]) == 2
    current = beliefs.load(vid)
    assert current["beliefs"][0]["status"] == "disproven" and len(current["beliefs"][0]["evidence"]) == 2


def test_failed_followup_does_not_erase_existing_support_or_claim_market_contact(exploration_market):
    from app.db.supymem_models import Experiment
    from app.experiments import beliefs, exploration, ledger
    from app.state import db

    _client, vid, wid, *_rest = exploration_market
    seed(exploration_market)
    round_ = asyncio.run(exploration.start_round(vid))
    for index in range(3):
        record(exploration_market, round_, index, measured={"events": 5, "signal": 4 if index == 0 else 0})
    with db()() as session:
        source = session.get(Experiment, "test_1_0")
        spec = json.loads(source.context)["spec"]
        spec.update(id="failed_followup", runner="outreach_split")
        session.add(Experiment(id="failed_followup", venture_id=vid, workspace_id=wid, kind=ledger.KIND,
            hypothesis=source.hypothesis, state="running", context=json.dumps({"runner": "system", "spec": spec,
                "prepared": True, "error": "Your follow-up could not finish safely."})))
        session.commit()
    result = asyncio.run(exploration.refresh(vid))["rounds"][0]
    assert result["selected_option_id"] == round_["options"][0]["id"]
    assert beliefs.load(vid)["rounds"][0]["options"][0]["evidence_level"] == "desk_research"
    assert result["status"] == result["options"][0]["status"] == "Too early to tell"


def test_explicit_round_resolution_preserves_live_tests_and_retries(exploration_market):
    from app.experiments import beliefs, exploration

    _client, vid, _wid, view, *_rest = exploration_market
    options = [{"statement": f"You could try group {index} of {view['icp']['who']} for {view['icp']['problem']}.",
                "audience": view["icp"]["who"]} for index in range(6)]
    with pytest.raises(ValueError):
        exploration.create_round(vid, kind="segment", options=[{**item, "evidence_level": "market_response"}
                                                               for item in options[:3]])
    with pytest.raises(ValueError):
        exploration.create_round(vid, kind="segment", options=[options[0]] * 3)
    first = exploration.create_round(vid, kind="segment", options=options[:3])
    record(exploration_market, first, 0, state="running", measured={"events": 2, "signal": 4})
    live = exploration.resolve_round(vid, first["id"], next_options=options[3:])
    assert live["ended_at"] is None and "next_round" not in live
    for index in range(3):
        record(exploration_market, first, index, measured={"events": 5, "signal": 0})
    resolved = exploration.resolve_round(vid, first["id"], next_options=options[3:])
    retry = exploration.resolve_round(vid, first["id"], next_options=options[3:])
    assert retry["next_round"]["id"] == resolved["next_round"]["id"]
    assert len(beliefs.load(vid)["rounds"]) == 2
    assert exploration.get_round(vid, first["id"])["options"] == resolved["options"]


def test_paused_sending_does_not_erase_an_already_recorded_market_result(exploration_market):
    from app.experiments import beliefs, exploration

    _client, vid, _wid, view, *_rest = exploration_market
    seed(exploration_market)
    round_ = asyncio.run(exploration.start_round(vid))
    record(exploration_market, round_, 0, state="running", runner="outreach_split",
           measured={"events": 5, "signal": 3, "contacted": 5, "booked": 1}, extra={"prepared": True})
    for index in (1, 2):
        record(exploration_market, round_, index, measured={"events": 5, "signal": 0})
    result = asyncio.run(exploration.refresh(vid))["rounds"][0]
    assert result["selected_option_id"] == round_["options"][0]["id"] and result["state"] == "worked"
    assert beliefs.load(vid)["rounds"][0]["options"][0]["evidence_level"] == "conclusion"
    assert view["allow_sending"] is False and view["auto_approve_first_emails"] is False


def test_generated_duplicates_or_wrong_kinds_never_create_a_round(exploration_market, monkeypatch):
    from app.experiments import beliefs, exploration, ledger
    from app.state import db

    _client, vid, _wid, view, *_rest = exploration_market
    original = seed(exploration_market, count=1)
    with db()() as session:
        direction = ledger.current_direction(session, vid)
    monkeypatch.setattr(beliefs, "context", lambda _vid: ({"direction": direction}, {"icp": view["icp"]}))
    for kind in ("who", "message"):
        async def invalid(*_args, **_kwargs):
            draft = {"type": kind, "statement": original["beliefs"][0]["statement"], "confidence": 0.5,
                     "audience": view["icp"]["who"], "problem": view["icp"]["problem"]}
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps({"beliefs": [draft, draft]})))])

        monkeypatch.setattr(exploration.metered, "llm", invalid)
        with pytest.raises(ValueError):
            asyncio.run(exploration.start_round(vid))
        assert beliefs.load(vid) == original


def test_empty_market_uses_the_existing_complete_belief_generator(exploration_market, monkeypatch):
    from app.experiments import beliefs, exploration
    from app.state import db

    _client, vid, _wid, view, *_rest = exploration_market
    calls = []

    async def complete_beliefs(venture_id):
        calls.append(venture_id)
        seed(exploration_market, count=3)
        with db()() as session:
            saved = beliefs.row(session, venture_id)
            current = json.loads(saved.context)
            current["beliefs"].extend({"id": "other_" + kind, "type": kind, "confidence": 0.2,
                "status": "to_test", "evidence": [],
                "statement": f"You could try a {kind} idea for {view['icp']['who']}."}
                for kind in beliefs.TYPES if kind != "who")
            saved.context = json.dumps(current)
            session.commit()
        return current

    monkeypatch.setattr(beliefs, "generate", complete_beliefs)
    result = asyncio.run(exploration.start_round(vid))
    assert calls == [vid] and len(result["options"]) == 3
    assert {item["type"] for item in beliefs.load(vid)["beliefs"]} == set(beliefs.TYPES)


def test_local_owner_switch_and_hosted_defaults_preserve_scope(exploration_market, monkeypatch):
    from app.experiments import exploration
    from app.core.local_mode import is_local
    from app.tenancy import bind

    client, vid, wid, *_rest = exploration_market
    seed(exploration_market)
    asyncio.run(exploration.start_round(vid))
    for workspace_id, venture_id in (("foreign_workspace", vid), (wid, "foreign_venture")):
        with bind(workspace_id=workspace_id, venture_id=venture_id):
            with pytest.raises(KeyError):
                exploration.list_rounds(vid)
    monkeypatch.setattr(exploration, "get_policy", lambda _key: None)
    if is_local():
        from app.core.local_secrets import update_settings

        assert exploration.enabled() is True
        assert client.get(f"/ventures/{vid}/experiments/rounds").status_code == 200
        update_settings("capabilities", {"research": False}, {})
    assert exploration.enabled() is False
    assert client.get(f"/ventures/{vid}/experiments/rounds").status_code == 404
    with pytest.raises(KeyError):
        asyncio.run(exploration.start_round(vid))
    original = {"icp": {}}
    assert exploration.continue_selected(vid, original) is original
