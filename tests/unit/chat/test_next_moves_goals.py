import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from tests.company_os_harness import load_company_os_app, local_chat as local_chat

from tests.unit.chat.test_next_moves import DOMAINS, _saved_state
from tests.unit.tasks.test_chat import chat_db as chat_db, call as chat_call, source as chat_source


@pytest.mark.parametrize("idea", DOMAINS)
def test_intake_prefills_keep_their_text_validation(idea):
    load_company_os_app()
    from app.chat.next_moves import intake_actions

    card = {"intake_pending": True, "first_question_pending": True, "actions": []}
    valid = [{"action": "chat.prefill", "text": idea},
             {"action": "chat.prefill", "payload": {"text": idea}}]
    invalid = [{"action": "chat.prefill", "text": text} for text in ("", "   ", 5)]
    invalid.append({"action": "chat.prefill", "payload": {"text": ""}, "text": idea})
    assert intake_actions(card, [*valid, *invalid, {"action": "research.start"}]) == valid


@pytest.mark.parametrize("idea", DOMAINS)
def test_local_empty_next_moves_return_the_exact_setup_step(monkeypatch, idea):
    load_company_os_app()
    from app.chat import actions, customers, next_moves
    from app.ventures import stage

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    state = {**_saved_state(idea), "idea": {"confirmed": True}, "questions_pending": False}
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    monkeypatch.setattr(stage, "get_stage", lambda _: {"stage": "idea", "label": "Shaping your idea"})
    monkeypatch.setattr(customers, "saved_search_request", lambda *args: "")
    monkeypatch.setattr(next_moves, "_blocked_actions", lambda _: {"ceo_review", "research.prepare"})
    monkeypatch.setattr(next_moves, "_fresh_recommendation", lambda _: None)
    monkeypatch.setattr(next_moves, "_customer_calls_offer", lambda *args: None)
    monkeypatch.setattr(next_moves, "experiments_autopilot_enabled", lambda: False)
    step = "Open Settings > Keys and connections > Your AI provider and save your model key."
    monkeypatch.setattr(actions, "disabled_reason", lambda _: step)
    reply = next_moves.compose("venture")
    assert reply["body_md"] == step
    assert reply["actions"] == [{"label": "Keys and connections", "href": "/settings/local", "style": "primary"}]


@pytest.mark.parametrize("idea", ["Inventory alerts for warehouse operators", "Watering reminders for home gardeners"])
def test_local_experiment_suggestions_use_saved_switch_not_policy(chat_db, monkeypatch, idea):
    from app.chat import next_moves
    from app.core.local_secrets import update_settings
    from app.credits import policy
    from app.db.models import Venture
    from tests.company_os_harness import configure_local_test_model

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    configure_local_test_model()
    with chat_db["factory"]() as session:
        session.get(Venture, "ven_chat").raw_idea = idea
        session.commit()
    monkeypatch.setattr(policy, "get_policy", lambda *_args, **_kwargs:
        pytest.fail("Your saved local switch must control experiment suggestions"))
    for enabled in (False, True, False):
        update_settings("capabilities", {"run_for_me_experiments": enabled}, {})
        assert next_moves.experiments_autopilot_enabled() is enabled


@pytest.mark.parametrize("idea", ["Inventory alerts for warehouse operators", "Watering reminders for home gardeners"])
def test_customer_search_next_moves_exclude_running_and_just_started_actions(chat_db, monkeypatch, idea):
    from app.chat import next_moves
    from app.chat.store import append_message
    from app.core.local_mode import is_local
    from app.core.local_secrets import update_settings
    from app.credits import policy
    from app.db.models import ChatMessage, GtmContact, GtmEmailEvent, GtmTouch, VentureDocument
    from app.db.supymem_models import BuildRun, Experiment, IdeaSession
    from app.tenancy import bind
    from app.ventures import stage

    data = chat_db
    from tests.company_os_harness import configure_local_test_model

    configure_local_test_model()
    for model in (GtmContact, GtmEmailEvent, GtmTouch, Experiment, VentureDocument, IdeaSession, BuildRun):
        model.__table__.create(data["factory"].kw["bind"], checkfirst=True)
    state = _saved_state(idea)
    state["icp"].update(confirmed=True)
    if is_local():
        update_settings("capabilities", {"run_for_me_experiments": True, "background_work": True}, {})
    else:
        policy.set_policy("feature_flags", {"experiments_autopilot_v1": True, "goals_v1": True}, "test")
    monkeypatch.setattr(next_moves, "db", lambda: data["factory"])
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    monkeypatch.setattr(next_moves, "_fresh_recommendation", lambda _: None)
    monkeypatch.setattr(stage, "get_stage", lambda _: {"stage": "idea", "label": "Shaping your idea"})
    with bind(workspace_id="wsp_chat", venture_id="ven_chat", actor="usr_chat"), data["conversations"].bind_conversation("ven_chat", "cnv_private"):
        offered = next_moves.compose("ven_chat")
        assert any(action["action"] == "chat.reply" for action in offered["actions"])
        for search_state in ("queued", "running"):
            state["search_state"], state["search_job"] = search_state, "customer-search"
            reply = append_message("ven_chat", role="assistant", body_md="Your customer search has started.")
            with data["factory"]() as session:
                saved = session.get(ChatMessage, reply["id"])
                card = next((item["next_moves"] for item in json.loads(saved.citations_json or "[]") if "next_moves" in item), None)
            if is_local():
                assert card is None and "next_moves" not in reply
                card = next_moves.compose("ven_chat")
            assert card["actions"]
            assert not any(action["action"] in {"chat.reply", "gtm.find"} or action["label"] == "Run it for me"
                           for action in card["actions"])
            assert any(action["label"] == "Design an experiment" and action["action"] == "experiment.start"
                       for action in card["actions"])


@pytest.mark.parametrize("idea", DOMAINS)
def test_customer_call_goal_offer_is_eligible_nonrepeating_and_uses_existing_reviewed_goal(chat_db, monkeypatch, idea):
    from app.chat import next_moves
    from app.credits import policy
    from app.db.models import ChatMessage, GtmContact, GtmEmailEvent, GtmTouch, Job, Venture, VentureDocument
    from app.db.supymem_models import BuildRun, Experiment, IdeaSession
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.experiments import ledger
    from app.tenancy import bind
    from app.ventures import stage

    data = chat_db
    from tests.company_os_harness import configure_local_test_model

    configure_local_test_model()
    factory = data["factory"]
    for model in (GtmContact, GtmEmailEvent, GtmTouch, Experiment, VentureDocument, IdeaSession, BuildRun):
        model.__table__.create(factory.kw["bind"], checkfirst=True)
    with factory() as session:
        session.get(Venture, "ven_chat").raw_idea = idea
        session.commit()
    state = _saved_state(idea)
    monkeypatch.setattr(next_moves, "db", lambda: factory)
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    monkeypatch.setattr(stage, "get_stage", lambda _: {"stage": "idea", "label": "Shaping your idea"})
    original_policy = policy.get_policy
    flags = {}
    from app.core.local_mode import is_local
    from app.core.local_secrets import update_settings

    def set_flag(name, value):
        flags[name] = value
        if is_local():
            capability_name = {"goals_v1": "background_work", "experiments_autopilot_v1": "run_for_me_experiments"}[name]
            update_settings("capabilities", {capability_name: value}, {})

    set_flag("goals_v1", False)
    set_flag("experiments_autopilot_v1", False)
    monkeypatch.setattr(policy, "get_policy", lambda key, default=None:
        flags if key == "feature_flags" else original_policy(key, default))
    label = "Book 3 customer calls this week"

    def offers(card):
        return [action for action in card["actions"] if action["label"] == label]

    def compose():
        with bind(workspace_id="wsp_chat", venture_id="ven_chat", actor="usr_chat"), data["conversations"].bind_conversation("ven_chat", "cnv_private"):
            return next_moves.compose("ven_chat")

    state["leads"] = ["contact"]
    baseline = compose()
    set_flag("goals_v1", True)
    for disabled in (False, 1, "true"):
        set_flag("experiments_autopilot_v1", disabled)
        assert compose() == baseline
    set_flag("experiments_autopilot_v1", True)
    set_flag("goals_v1", False)
    experiment_baseline = compose()
    for disabled in (False, 1, "true"):
        set_flag("goals_v1", disabled)
        assert compose() == experiment_baseline
    set_flag("goals_v1", True)
    offered = compose()
    assert len(offers(offered)) == 1
    action = offers(offered)[0]
    assert action["action"] == "chat.reply" and action["payload"] == {"text": label}
    assert offered["state_hash"] != baseline["state_hash"]
    assert compose() == offered
    assert "\u2014" not in offered["body_md"]
    for leads, reachable, confirmed, audience, expected in (
            ([], [], False, idea, False), ([], [], True, "", False),
            ([], [], True, idea, True), ([], ["reachable"], False, idea, True)):
        state.update(leads=leads, reachable=reachable)
        state["icp"].update(confirmed=confirmed, who=audience)
        assert bool(offers(compose())) is expected
    state.update(leads=["contact"], reachable=[])

    now = data["clock"][0]
    with factory() as session:
        session.add(ChatMessage(id="previous_offer", workspace_id="wsp_chat", venture_id="ven_chat",
            conversation_id=None, role="assistant", kind="next_moves", body_md=label,
            actions_json=json.dumps([action]), created_at=now))
        session.commit()
    assert len(offers(compose())) == 1
    with factory() as session:
        previous = session.get(ChatMessage, "previous_offer")
        previous.conversation_id = "cnv_private"
        session.commit()
    assert not offers(compose())
    with factory() as session:
        previous = session.get(ChatMessage, "previous_offer")
        previous.kind, previous.actions_json = "text", "[]"
        previous.citations_json = json.dumps([{"next_moves": offered}])
        session.commit()
    assert not offers(compose())
    with factory() as session:
        session.add(ChatMessage(id="following_card", workspace_id="wsp_chat", venture_id="ven_chat",
            conversation_id="cnv_private", role="assistant", kind="next_moves", body_md=baseline["body_md"],
            actions_json=json.dumps(baseline["actions"]), created_at=now + timedelta(seconds=1)))
        session.commit()
    assert len(offers(compose())) == 1

    with factory() as session:
        session.add(Task(id="existing_goal", workspace_id="wsp_chat", venture_id="ven_chat", owner_person_id="usr_chat",
            title="Your saved goal", goal="Your saved goal", state="working", scope_json=json.dumps({"_goals_v1": True}),
            deadline_at=now + timedelta(days=1), created_at=now, updated_at=now))
        session.commit()
    for running in ("submitted", "working", "input_required", "auth_required", "approval_required", "paused"):
        with factory() as session:
            session.get(Task, "existing_goal").state = running
            session.commit()
        assert not offers(compose())
    with factory() as session:
        task = session.get(Task, "existing_goal")
        task.scope_json = json.dumps({"steps": [{"subgoal": "Prepare your saved work"}]})
        session.commit()
    assert not offers(compose())
    with factory() as session:
        task = session.get(Task, "existing_goal")
        task.scope_json, task.state = "{}", "working"
        session.commit()
    assert len(offers(compose())) == 1
    with factory() as session:
        session.get(Task, "existing_goal").state = "completed"
        session.add(GtmContact(id="contact", workspace_id="wsp_chat", source="founder_import", stage_changed_at=now, created_at=now))
        session.add(GtmTouch(id="booking", contact_id="contact", workspace_id="wsp_chat", venture_id="ven_chat",
            direction="in", simulated=1, body_text="I booked a call.", created_at=now))
        session.commit()
    assert len(offers(compose())) == 1
    with factory() as session:
        session.get(GtmTouch, "booking").simulated = 0
        session.commit()
    assert not offers(compose())
    for changes in ({"body_text": "I would like to learn more."}, {"body_text": "I booked a call.", "venture_id": "another_venture"},
                    {"venture_id": "ven_chat", "workspace_id": "another_workspace"},
                    {"workspace_id": "wsp_chat", "created_at": now + timedelta(days=1)}):
        with factory() as session:
            for key, value in changes.items():
                setattr(session.get(GtmTouch, "booking"), key, value)
            session.commit()
        assert len(offers(compose())) == 1
    with factory() as session:
        reply = session.get(GtmTouch, "booking")
        reply.created_at, reply.classification, reply.body_text = now, "automated", "PARTSTAT=ACCEPTED"
        session.add(GtmEmailEvent(id="booking_headers", workspace_id="wsp_chat", event_type="booking_reply_headers",
            metadata_json=json.dumps({"reply_id": "booking", "subject": "Accepted: Customer call",
                "reply_headers": {"Content-Type": "text/calendar; method=REPLY"}}), created_at=now))
        session.commit()
    assert not offers(compose())
    with factory() as session:
        session.delete(session.get(GtmEmailEvent, "booking_headers"))
        reply = session.get(GtmTouch, "booking")
        reply.classification, reply.body_text = None, "I would like to learn more."
        session.commit()
    assert len(offers(compose())) == 1
    with factory() as session:
        session.add(Experiment(id="marked_booking", workspace_id="wsp_chat", venture_id="ven_chat", kind=ledger.KIND,
            context=json.dumps({"booking_marks": {"contact": True}})))
        session.commit()
    assert not offers(compose())
    with factory() as session:
        session.get(Experiment, "marked_booking").context = json.dumps({"booking_marks": {"contact": False}})
        reply = session.get(GtmTouch, "booking")
        reply.body_text, reply.honesty_json = "I booked a call.", json.dumps({"experiment_id": "marked_booking"})
        assert session.scalar(select(func.count()).select_from(TaskRun)) == 0
        assert session.scalar(select(func.count()).select_from(Job)) == 0
        session.commit()
    assert len(offers(compose())) == 1
    assert not data["calls"]

    from app.core.local_mode import is_local
    if is_local():
        from app.core.providers_local import save_settings
        save_settings(provider="openai_compatible", base_url="http://127.0.0.1:11434/v1", models={"chat": "offline-fixture"})
    chat_source(data, action["payload"]["text"])
    reply = chat_call(data, action["payload"]["text"])
    with factory() as session:
        task = session.scalar(select(Task).where(Task.goal == label))
        assert task.state == "working"
        run = session.scalar(select(TaskRun).where(TaskRun.task_id == task.id))
        steps = session.scalars(select(TaskStep).where(TaskStep.run_id == run.id).order_by(TaskStep.position)).all()
        assert [step.skill for step in steps] == ["tasks.booking_link", "gtm.find", "outreach.draft", "tasks.customer_calls"]
        assert not any(step.job_id for step in steps)
        assert session.scalar(select(func.count()).select_from(GtmTouch).where(GtmTouch.direction == "out")) == 0
    assert len(data["calls"]) == 1
    assert ("stop task" if is_local() else "Nothing will be sent") in reply["body_md"]
    assert not offers(compose())
