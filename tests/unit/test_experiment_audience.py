import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from tests.company_os_harness import local_owner_clients

from tests.unit.gtm.test_journey import journey_db as journey_fixture
from tests.unit.gtm.test_known import known_case
from tests.unit.test_autonomous_experiments import market, venture
from tests.unit.test_market_beliefs import set_local_experiments

journey_db = journey_fixture


def test_local_requested_experiments_use_saved_scope_without_confirming_again(market):
    from app.core.local_mode import is_local
    from sqlalchemy import select
    from app.chat import experiments
    from app.db.models import Venture
    from app.db.supymem_models import IdeaSession
    from app.state import db

    _client, venture_id, _workspace_id, view, *_rest = market
    audience, problem = view["icp"]["who"], view["icp"]["problem"]
    with db()() as session:
        session.get(Venture, venture_id).raw_idea = f"Audience: {audience}\nProblem: {problem}"
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id))
        idea_id = idea.id
        idea.state, idea.confirmed_revision_id = "reviewing", None
        session.commit()
    view["icp"].update(who="", confirmed=False, needs_input=True)
    blocked = experiments._idea_gate(venture_id)
    if is_local():
        assert blocked is None
    else:
        assert "confirm your idea" in blocked["body_md"]
    reply = experiments.list_reply(venture_id)
    if is_local():
        assert reply["kind"] == "experiments" and reply["actions"]
        assert audience in reply["body_md"]
    else:
        assert "Nothing was started" in reply["body_md"]
    with db()() as session:
        assert session.get(IdeaSession, idea_id).state == "reviewing"


@pytest.mark.parametrize("customer,previous,problem", [
    ("commercial kitchen supervisors", "equipment repair contractors", "keeping appliance maintenance logs"),
    ("weekend trail walkers", "campsite hosts", "sharing arrival itineraries"),
], ids=["b2b-maintenance", "consumer-outdoors"])
def test_new_plan_checks_recruited_customer_not_downstream_roles(journey_db, monkeypatch, customer, previous, problem):
    from app.chat import experiments
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.experiments import autonomous, ledger
    from app.ventures.current_idea import revision_fields
    from core.clock import utcnow

    data = journey_db
    fields = {"customer": customer, "problem": problem,
        "offer": f"Help {customer} with {problem} so {previous} receive accurate information.",
        "constraints": f"Use consenting participants and anonymous notes. Exclude {previous}. Do not send messages.",
        "_answered_fields": {"customer": previous}}
    with data["factory"]() as session:
        session.add(IdeaSession(id="downstream_session", venture_id=data["vid"], workspace_id=data["wid"],
            case="know", state="confirmed", current_revision_id="downstream_revision",
            confirmed_revision_id="downstream_revision", created_at=utcnow(), updated_at=utcnow()))
        session.flush()
        session.add(IdeaRevision(id="downstream_revision", session_id="downstream_session", venture_id=data["vid"],
            workspace_id=data["wid"], rev=1, source="founder", structured=json.dumps(fields), created_at=utcnow()))
        session.commit()
    snapshot = autonomous._snapshot(data["vid"])
    monkeypatch.setattr(autonomous, "_snapshot", lambda *_: {**snapshot,
        "workspace_id": data["wid"], "venture_id": data["vid"], "journey": {"icp": {"who": customer, "problem": problem}}})
    monkeypatch.setattr(experiments, "_idea_gate", lambda *_: None)
    action = (f"You interview five consenting {customer} about sharing information with {previous}. "
        f"You invite {customer}, not {previous}, to a follow-up. {fields['constraints']}")
    calls = []

    async def plan(*args, **kwargs):
        calls.append(action)
        return {"hypothesis": fields["offer"], "action": action,
            "success_measure": f"Pass if three commit to sharing information with {previous}; fail if none do; otherwise inconclusive."}

    monkeypatch.setattr(experiments, "_llm_json", plan)
    reply = asyncio.run(experiments.execute(data["vid"], "experiment.start", {"text": "Design a private test."}, data["actor"]))
    assert calls == [action] and customer in reply["body_md"] and previous in reply["body_md"]
    saved = ledger.list_for_venture(data["vid"])
    assert len(saved) == 1 and saved[0]["action"] == action
    assert saved[0]["measure"] and "inconclusive" in saved[0]["measure"]
    for action in (
        f"You interview five {previous} about helping {customer}. {fields['constraints']}",
        f"You interview {customer} and {previous} about {problem}. {fields['constraints']}",
        f"You interview {customer} about {problem}, then recruit {previous} for follow-ups. {fields['constraints']}",
    ):
        calls.clear()
        rejected = asyncio.run(experiments.execute(data["vid"], "experiment.start",
            {"text": "Design another private test."}, data["actor"]))
        assert calls == [action, action]
        assert "Nothing was started" in rejected["body_md"] and rejected["actions"]
        assert ledger.list_for_venture(data["vid"]) == saved
    with data["factory"]() as session:
        assert revision_fields(session.get(IdeaRevision, "downstream_revision")) == fields


@pytest.mark.parametrize("customer,reworded,previous,problem", [
    ("independent textile mills", "independent textile mill owners", "fashion retailers", "scheduling dye batches"),
    ("community choirs", "community choir singers", "festival attendees", "finding rehearsal spaces"),
], ids=["b2b-textiles", "consumer-music"])
def test_reworded_customer_is_not_superseded_but_corrected_customer_stays_replaced(
    journey_db, monkeypatch, customer, reworded, previous, problem,
):
    from app.chat import experiments
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.experiments import autonomous, ledger
    from core.clock import utcnow

    data = journey_db
    with data["factory"]() as session:
        for identifier, revision_id, fields, rev in (
            ("old_customer", "old_customer_revision", {"customer": previous}, 1),
            ("current_customer", "current_customer_revision", {"customer": customer, "problem": problem,
                "_answered_fields": {"customer": reworded},
                "_prefill_facts": {"customer": {"value": reworded}},
                "_corrects_revision_id": "old_customer_revision"}, 2),
        ):
            session.add(IdeaSession(id=identifier, venture_id=data["vid"], workspace_id=data["wid"],
                case="know", state="confirmed", current_revision_id=revision_id,
                confirmed_revision_id=revision_id, created_at=utcnow(), updated_at=utcnow()))
            session.flush()
            session.add(IdeaRevision(id=revision_id, session_id=identifier, venture_id=data["vid"],
                workspace_id=data["wid"], rev=rev, source="founder", structured=json.dumps(fields), created_at=utcnow()))
            session.flush()
        session.add(IdeaRevision(id="reworded_revision", session_id="current_customer", venture_id=data["vid"],
            workspace_id=data["wid"], rev=1, source="founder", structured=json.dumps({"customer": reworded}), created_at=utcnow()))
        session.commit()
    snapshot = {"workspace_id": data["wid"], "venture_id": data["vid"], "stage": "ideating", "direction": "current_customer_revision",
        "waitlist": {}, "app_id": None,
        "journey": {"icp": {"who": reworded, "problem": problem, "needs_input": False}}}
    monkeypatch.setattr(autonomous, "_snapshot", lambda *_: deepcopy(snapshot))
    monkeypatch.setattr(experiments, "_idea_gate", lambda *_: None)
    target = reworded
    calls = []

    async def plan(*args, **kwargs):
        calls.append(target)
        return {"hypothesis": f"You can help {target} with {problem}.",
            "action": f"You interview five consenting {target} and keep anonymous notes.",
            "success_measure": "Pass if three agree to try it. Fail if all decline; otherwise inconclusive."}

    monkeypatch.setattr(experiments, "_llm_json", plan)
    current = experiments._experiment_snapshot(data["vid"])
    assert current["superseded_customers"] == [previous]
    assert current["journey"]["icp"]["who"] == customer
    reply = asyncio.run(experiments.execute(data["vid"], "experiment.start", {"text": "Design a private test."}, data["actor"]))
    assert calls == [reworded] and reworded in reply["body_md"]
    saved = ledger.list_for_venture(data["vid"])
    assert len(saved) == 1 and reworded in saved[0]["hypothesis"]
    target = previous
    rejected = asyncio.run(experiments.execute(data["vid"], "experiment.start", {"text": "Design another private test."}, data["actor"]))
    assert calls == [reworded, previous, previous]
    assert "Nothing was started" in rejected["body_md"] and rejected["actions"]
    assert ledger.list_for_venture(data["vid"]) == saved


@pytest.mark.parametrize("customer,previous,problem", [
    ("independent ceramic kiln operators", "pottery wholesalers", "scheduling shared firing slots"),
    ("balcony birdwatchers", "urban nature bloggers", "identifying visiting birds"),
], ids=["b2b-ceramics", "consumer-birdwatching"])
@pytest.mark.parametrize("snapshot_current", [False, True], ids=["stale-snapshot", "current-snapshot"])
@pytest.mark.parametrize("invalid_retry", [False, True], ids=["repaired-plan", "invalid-retry"])
def test_experiment_plan_uses_current_confirmed_customer(journey_db, monkeypatch, customer, previous, problem, snapshot_current, invalid_retry):
    from app.chat import experiments
    from app.db.models import Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.experiments import autonomous, ledger
    from app.ventures.current_idea import revision_fields
    from core.clock import utcnow

    data = journey_db
    limits = f"Use consenting participants and anonymous notes. Exclude {previous}. Do not send messages or collect private records."
    fields = {"customer": customer, "problem": problem, "constraints": [limits],
        "_answered_fields": {"customer": previous}, "summary": f"A service for {previous}",
        "validation_tests": [{"hypothesis": f"You can recruit {previous}"}]}
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = f"A service for {previous}"
        session.add(IdeaSession(id="corrected_session", venture_id=data["vid"], workspace_id=data["wid"],
            case="know", state="confirmed", current_revision_id="corrected_revision",
            confirmed_revision_id="corrected_revision", created_at=utcnow(), updated_at=utcnow()))
        session.flush()
        session.add(IdeaRevision(id="corrected_revision", session_id="corrected_session", venture_id=data["vid"],
            workspace_id=data["wid"], rev=1, source="founder", structured=json.dumps(fields), created_at=utcnow()))
        session.commit()
    saved = {"workspace_id": data["wid"], "venture_id": data["vid"], "stage": "ideating", "direction": "",
        "journey": {"icp": {"who": customer if snapshot_current else previous, "problem": problem, "needs_input": False},
            "intake": {"customer": previous}, "drafts": [{"customer": previous}]},
        "idea_text": f"A service for {previous}", "idea_problem": problem,
        "waitlist": {"enabled": False}, "app_id": None}
    monkeypatch.setattr(autonomous, "_snapshot", lambda *_args: deepcopy(saved))
    monkeypatch.setattr(experiments, "_idea_gate", lambda *_args: None)
    calls = []

    async def plan(system, user, *_args, **_kwargs):
        calls.append((system, user))
        if len(calls) == 1:
            context = json.loads(user.split("Saved context:\n", 1)[1])
            assert context["customer"] == customer and context["problem"] == problem
            assert set(context) == {"customer", "problem", "constraints"}
            assert limits in json.dumps(context)
        target = previous if len(calls) == 1 else customer
        return {"hypothesis": f"You can recruit {target} to test demand for help with {problem}.",
            "action": f"You prepare five interviews with {target}. {limits}" if not invalid_retry or len(calls) == 1 else limits * 100,
            "success_measure": "You pass with three dated follow-ups, fail with none, otherwise inconclusive."}

    monkeypatch.setattr(experiments, "_llm_json", plan)
    snapshot = experiments._experiment_snapshot(data["vid"])
    assert snapshot["journey"]["icp"]["who"] == customer
    reply = asyncio.run(experiments.execute(data["vid"], "experiment.start", {"text": "Design an experiment."}, data["actor"]))
    assert len(calls) == 2
    rows = ledger.list_for_venture(data["vid"])
    if invalid_retry:
        assert not rows and "keeps your limits" in reply["body_md"] and "Nothing was started" in reply["body_md"]
        assert reply["actions"][0]["payload"]["text"] == "Design an experiment."
    else:
        assert customer in reply["body_md"] and previous not in reply["body_md"].replace(limits, "")
        assert problem in reply["body_md"] and "I need your customer" not in reply["body_md"]
        assert limits in reply["body_md"]
        assert len(rows) == 1 and customer in rows[0]["hypothesis"] and previous not in rows[0]["hypothesis"]
        assert previous not in rows[0]["action"].replace(limits, "")
    assert saved["journey"]["icp"]["who"] == (customer if snapshot_current else previous)
    with data["factory"]() as session:
        assert revision_fields(session.get(IdeaRevision, "corrected_revision")) == fields


def test_experiment_audience_preflight_reuses_or_confirms_scoped_facts(known_case, monkeypatch):
    from app.chat import experiments
    from app.connections import context_docs
    from app.credits.policy import set_policy
    from app.db.models import Venture
    from app.experiments import autonomous
    from app.gtm import journey
    from app.state import MEM
    from app.ventures import founder_facts, startup_review

    data = known_case
    from app.core.local_mode import is_local
    if is_local():
        from app.core.local_secrets import update_settings
        update_settings("composio", {"auth_configs": {"googledocs": "auth_fixture"}}, {})
    assert "Nothing was changed" in experiments.audience_question(data["vid"], field="credentials")["body_md"]
    monkeypatch.setattr(journey, "status", lambda *_args, **_kwargs: {"icp": {"who": "", "problem": data["problem"], "confirmed": False}})
    snapshot = autonomous._snapshot(data["vid"])
    assert snapshot["journey"]["icp"]["who"] == data["audience"]
    reply = experiments.audience_question(data["vid"])
    assert data["audience"] in reply["body_md"] and "First, who" not in reply["body_md"]
    assert not MEM.get("gtm_audience_asked:" + data["vid"])
    with data["factory"]() as session:
        startup_review._document(session, data["vid"], startup_review.INTERNAL_KIND,
            {"founder_facts": {"audience": founder_facts._fact(data["audience"], "website", .8)}})
        session.commit()
    reply = experiments.audience_question(data["vid"])
    assert "Is this correct?" in reply["body_md"] and reply["actions"][0]["label"] == "Yes"
    assert MEM["gtm_audience_asked:" + data["vid"]]["known_check"]["status"] == "confirm"
    monkeypatch.setattr(context_docs, "research_context", lambda *_args: {"connected_context": {"documents": [
        {"title": "Your selected plan", "excerpt": "Audience: Museum visitors"}]}})
    reply = experiments.audience_question(data["vid"])
    assert "conflicting values" in reply["body_md"] and len(reply["actions"]) == 2
    assert "Your selected plan" in reply["body_md"] and data["audience"] in reply["body_md"]
    set_policy("feature_flags", {"known_check_v1": False, "experiments_autopilot_v1": True}, "test")
    assert ("conflicting values" if is_local() else "First, who") in experiments.audience_question(data["vid"])["body_md"]


@pytest.fixture(params=[
    ("dental practices in Finland", "Dental practices struggle to fill cancelled appointments."),
    ("weekend hikers in Chile", "Weekend hikers struggle to plan safe routes."),
], ids=["b2b-healthcare", "consumer-outdoors"])
def audience_case(journey_db, monkeypatch, request):
    from app.core.local_mode import is_local
    from app.credits.policy import set_policy
    from app.db.models import Venture
    from app.experiments import autonomous
    from app.gtm import journey
    from app.state import MEM

    if is_local():
        from app.core.providers_local import save_settings
        save_settings(provider="openai_compatible", base_url="http://127.0.0.1:1234/v1",
            models={"chat": "local-test-model"})

    data = {**journey_db, "audience": request.param[0], "problem": request.param[1]}
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.name, venture.raw_idea = "Audience regression", "An idea with a customer problem to explore."
        session.commit()
    real_status = journey.status
    view = {"icp": {"who": "", "problem": data["problem"], "confirmed": False,
                    "needs_input": True, "mode": "company", "competitors": []}}
    monkeypatch.setattr(journey, "status", lambda *args, **kwargs: deepcopy(view))
    monkeypatch.setattr(journey, "_icp_seed", lambda vid: {"idea": {"problem": data["problem"]}})
    monkeypatch.setattr(autonomous, "_provider", lambda: SimpleNamespace(name="firecrawl", search_micro=0, scrape_micro=0))
    monkeypatch.setattr("app.credits.metered.price_micro", lambda name: 1)
    set_policy("feature_flags", {"experiments_autopilot_v1": True}, "test")
    MEM.pop("gtm_audience_asked:" + data["vid"], None)
    return {**data, "view": view, "real_status": real_status}


@pytest.mark.parametrize("source", ["founder", "answered", "prefill", "raw", "raw_current", "confirmed", "confirmed_copy", "changed_copy", "website", "website_confirmed", "website_metadata", "generated", "cleared"])
def test_experiment_audience_falls_back_only_to_your_idea(audience_case, source, monkeypatch):
    from app.chat import experiments
    from app.core.local_mode import is_local
    from app.credits.policy import set_policy
    from app.db.models import Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.experiments import autonomous, beliefs
    from app.ventures import founder_facts
    from core.clock import utcnow

    data = audience_case
    fields = {"customer": data["audience"], "problem": data["problem"]}
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        if source in {"raw", "raw_current"}:
            venture.raw_idea = f"Audience: {data['audience']}\nProblem: {data['problem']}"
        if source == "answered":
            fields["_answered_fields"] = {"customer": data["audience"]}
        if source == "prefill":
            fields["_prefill_facts"] = {"customer": {"value": data["audience"], "source": "founder message", "confidence": .95}}
        if source == "website_metadata":
            fields["_website_answers"] = {"customer": {"answer": data["audience"], "source": "From your website", "status": "answered"}}
        if source in {"founder", "website", "website_confirmed", "cleared"}:
            founder_facts.save(session, venture, {"audience": {
                "value": data["audience"], "quote": data["audience"],
                "source": "website" if source.startswith("website") else "clarification", "confidence": .95}})
        if source == "cleared":
            founder_facts.save(session, venture, {"audience": None})
        confirmed = source in {"confirmed", "website_confirmed", "website_metadata", "cleared"}
        copied = source in {"confirmed_copy", "changed_copy"}
        if source != "raw":
            session.add(IdeaSession(id="audience_session", venture_id=data["vid"], workspace_id=data["wid"],
                case="know", state="confirmed" if confirmed else "reviewing",
                current_revision_id="audience_revision", confirmed_revision_id="prior_audience_revision" if copied else "audience_revision" if confirmed else None,
                created_at=utcnow(), updated_at=utcnow()))
            session.flush()
            if copied:
                prior_fields = {**fields, "customer": "Previous " + data["audience"]} if source == "changed_copy" else fields
                session.add(IdeaRevision(id="prior_audience_revision", session_id="audience_session", venture_id=data["vid"],
                    workspace_id=data["wid"], rev=1, source="agent", structured=json.dumps(prior_fields), created_at=utcnow()))
            session.add(IdeaRevision(id="audience_revision", session_id="audience_session", venture_id=data["vid"],
                workspace_id=data["wid"], rev=2 if copied else 1, source="agent", structured=json.dumps(fields), created_at=utcnow()))
        session.commit()
    if source.startswith("website"):
        data["view"]["icp"]["who"] = data["audience"]
    snapshot = autonomous._snapshot(data["vid"])
    trusted = source in {"founder", "confirmed", "confirmed_copy", "answered", "prefill", "raw", "raw_current"}
    assert snapshot["journey"]["icp"]["who"] == (data["audience"] if trusted else "")
    specs = autonomous.available(data["vid"])
    assert bool(specs) == trusted
    if trusted:
        assert all(spec["icp"]["who"] == data["audience"] for spec in specs)
        assert all("audience_basis" not in spec["icp"] for spec in specs)
        assert all(not spec["icp"]["confirmed"] for spec in specs)
        reply = experiments.list_reply(data["vid"])
        question = experiments.audience_question(data["vid"], field="problem")
        if is_local():
            assert data["problem"] in question["body_md"]
            assert any(action.get("action") == "experiment.audience.change" and action.get("payload", {}).get("field") == "problem"
                for action in question["actions"])
        for answer in (reply,) if is_local() else (reply, question):
            if not is_local() and source in {"founder", "answered", "prefill", "raw_current", "confirmed_copy"}:
                assert "confirm your idea" in answer["body_md"] and "Nothing was started" in answer["body_md"]
            else:
                assert f"I used {data['audience']} from your saved idea" in answer["body_md"]
                assert any(action.get("action") == "experiment.audience.change" for action in answer["actions"])
        monkeypatch.setattr("app.lead_library.service.match", lambda *args, **kwargs: {"total": 0, "filters": {}, "items": []})
        monkeypatch.setattr("app.autopilot.llm.brief_text", lambda vid: data["problem"])
        assert beliefs.context(data["vid"])[1]["icp"]["who"] == data["audience"]
    if source != "raw":
        with data["factory"]() as session:
            assert json.loads(session.get(IdeaRevision, "audience_revision").structured) == fields
    reviewed_audience = "Local " + data["audience"]
    data["view"]["icp"].update(who=reviewed_audience, confirmed=True, needs_input=False)
    if is_local():
        from app.db.models import Job

        with data["factory"]() as session:
            founder_facts.save(session, session.get(Venture, data["vid"]), {"audience":
                founder_facts._fact(reviewed_audience, "review", 1.0)})
            session.commit()
    assert autonomous._snapshot(data["vid"])["journey"]["icp"]["who"] == reviewed_audience
    data["view"]["icp"].update(who="", confirmed=False, needs_input=True)
    set_policy("feature_flags", {"experiments_autopilot_v1": False}, "test")
    if is_local():
        set_local_experiments(False)
        assert autonomous._snapshot(data["vid"])["journey"]["icp"]["who"] == reviewed_audience
        with data["factory"]() as session:
            assert not session.query(Job).filter_by(venture_id=data["vid"]).count()
        return
    assert autonomous._snapshot(data["vid"])["journey"]["icp"]["who"] == ""
    assert "audience_basis" not in autonomous._snapshot(data["vid"])
    assert "experiment.audience.change" not in {action.get("action") for action in experiments.list_reply(data["vid"])["actions"]}


@pytest.mark.parametrize("origin", ["list", "market_plan", "market_plan_failure", "change", "problem"])
def test_experiment_audience_reply_saves_and_resumes_the_request(audience_case, origin, monkeypatch):
    from app.chat import customers, experiments, market_beliefs
    from app.chat.store import append_message
    from app.core.local_mode import is_local
    from app.db.models import Venture
    from app.db.supymem_models import Experiment, IdeaRevision, IdeaSession
    from app.experiments import autonomous, beliefs
    from app.gtm import journey
    from app.state import MEM
    from app.ventures import founder_facts
    from core.clock import utcnow

    data = audience_case
    saved_audience = data["audience"] if is_local() and origin == "problem" else None

    def save_audience(value):
        with data["factory"]() as session:
            founder_facts.save(session, session.get(Venture, data["vid"]), {"audience":
                founder_facts._fact(value, "review", 1.0) if value else None})
            session.commit()

    if origin.startswith("market_plan"):
        set_local_experiments(True)
        with data["factory"]() as session:
            session.add(IdeaSession(id="market_audience_session", venture_id=data["vid"], workspace_id=data["wid"],
                case="know", state="confirmed", current_revision_id="market_audience_revision", confirmed_revision_id="market_audience_revision",
                created_at=utcnow(), updated_at=utcnow()))
            session.flush()
            session.add(IdeaRevision(id="market_audience_revision", session_id="market_audience_session", venture_id=data["vid"],
                workspace_id=data["wid"], rev=1, source="founder", summary="Your idea with a problem to explore.",
                structured=json.dumps({"problem": data["problem"], "_questions_skipped": True}), created_at=utcnow()))
            session.commit()
    payload = {"day_one": True, "max_emails_per_day": 0, "weekly_budget_micro": 1000}
    rejected = asyncio.run(experiments.execute(data["vid"], "experiment.audience.change", {"action": "gtm.find"}, data["actor"]))
    assert "Nothing was changed" in rejected["body_md"]
    assert MEM.get(customers._ASKED.format(data["vid"])) is None
    if origin == "problem":
        if saved_audience:
            save_audience(saved_audience)
            with data["factory"]() as session:
                session.get(Venture, data["vid"]).raw_idea = ""
                session.commit()
        data["view"]["icp"].update(who=data["audience"], problem="")
        monkeypatch.setattr(autonomous, "available", lambda vid: [])
        original_snapshot = autonomous._snapshot
        monkeypatch.setattr(autonomous, "_snapshot", lambda vid: {**original_snapshot(vid), "idea_problem": ""})
        reply = experiments.list_reply(data["vid"])
    elif origin.startswith("market_plan"):
        async def forbidden_generate(vid):
            raise AssertionError("You must answer the audience question before model generation")
        monkeypatch.setattr(beliefs, "generate", forbidden_generate)
        reply = asyncio.run(market_beliefs.prepare(data["vid"], payload))
    elif origin == "change":
        reply = asyncio.run(experiments.execute(data["vid"], "experiment.audience.change", {}, data["actor"]))
    else:
        reply = experiments.list_reply(data["vid"])
    asked_key = customers._ASKED.format(data["vid"])
    asked = MEM[asked_key]
    assert asked["experiment_request"]["action"] == ("experiment.beliefs.prepare" if origin.startswith("market_plan") else "experiment.list")
    assert "plan your experiments" in reply["body_md"]
    append_message(data["vid"], role="assistant", body_md=reply["body_md"], kind=reply["kind"], actions=reply["actions"])
    answer = data["problem"] if origin == "problem" else data["audience"]
    command = customers.command(data["vid"], answer)
    assert command and command[0] == "audience"
    requested = []

    async def plan(vid, options=None):
        requested.append((vid, options))
        if origin == "market_plan_failure":
            raise RuntimeError("The experiment planner is unavailable")
        return {"kind": "market_beliefs", "body_md": "Your experiment plan is ready to review.", "actions": [], "citations": []}

    def no_lead_finding(*args, **kwargs):
        raise AssertionError("An experiment answer must not start lead-finding")

    monkeypatch.setattr(market_beliefs, "prepare", plan)
    monkeypatch.setattr(customers, "render", no_lead_finding)
    monkeypatch.setattr(journey, "prepare", no_lead_finding)
    invalid = asyncio.run(customers.run_command(data["vid"], *command, "I don't know", data["actor"]))
    assert "I could not use that answer" in invalid["body_md"]
    assert MEM[asked_key]["experiment_request"]["action"] == asked["experiment_request"]["action"]
    asked["experiment_request"]["conversation_id"] = "a_different_conversation"
    MEM[asked_key] = asked
    rejected = asyncio.run(customers.run_command(data["vid"], *command, answer, data["actor"]))
    assert "Nothing was changed" in rejected["body_md"]
    with data["factory"]() as session:
        assert (founder_facts.read(session, session.get(Venture, data["vid"]))["audience"]["value"] or None) == saved_audience
    asked["experiment_request"]["conversation_id"] = None
    MEM[asked_key] = asked
    if origin == "problem":
        if saved_audience:
            save_audience(None)
        data["view"]["icp"]["who"] = ""
        reasked = asyncio.run(customers.run_command(data["vid"], *command, answer, data["actor"]))
        assert "who are you trying to reach" in reasked["body_md"]
        assert data["real_status"](data["wid"], data["vid"], authorize=False)["icp"]["who"] == ""
        data["view"]["icp"]["who"] = data["audience"]
        if saved_audience:
            save_audience(saved_audience)
        MEM[asked_key] = asked
    original_update = journey.update_icp

    def save(wid, vid, who, **options):
        assert options["start"] is False
        original_update(wid, vid, who, **options)
        result = data["real_status"](wid, vid, authorize=False)
        data["view"]["icp"] = result["icp"]
        return result

    monkeypatch.setattr(journey, "update_icp", save)
    continued = asyncio.run(customers.run_command(data["vid"], *command, answer, data["actor"]))
    if origin == "market_plan_failure":
        assert "Your answer was saved" in continued["body_md"]
        assert continued["actions"][0]["action"] == "experiment.beliefs.prepare"
        assert continued["actions"][0]["payload"] == payload
    else:
        assert "Saved your" in continued["body_md"]
        assert continued["kind"] == ("market_beliefs" if origin == "market_plan" else "experiments")
    assert "lead library" not in continued["body_md"].lower()
    assert MEM.get(asked_key) is None
    if origin.startswith("market_plan"):
        assert requested == [(data["vid"], payload)]
    else:
        assert requested == []
    with data["factory"]() as session:
        assert data["real_status"](data["wid"], data["vid"], authorize=False)["icp"]["who"] == data["audience"]
        assert all(experiment.state == "proposed" for experiment in session.query(Experiment).all())
