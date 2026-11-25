"""Suggested buttons must reach a dispatcher, not just the action registry."""
import ast
import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

from tests.company_os_harness import local_chat as local_chat
from tests.unit.gtm.test_journey import journey_db as journey_db
from tests.unit.chat.test_next_moves import DOMAINS, _saved_state


def _literal_kinds(expression):
    if isinstance(expression, ast.IfExp):
        return _literal_kinds(expression.body) | _literal_kinds(expression.orelse)
    return {expression.value} if isinstance(expression, ast.Constant) and isinstance(expression.value, str) else set()


def _emitted_kinds(source):
    tree = ast.parse(Path(source).read_text())
    permission_reads = {node.value for node in ast.walk(tree)
                        if isinstance(node, ast.Attribute) and node.attr == "required_permission"}
    kinds = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict) and any(isinstance(key, ast.Constant) and key.value in {"label", "payload"}
                                             for key in node.keys):
            kinds.update(value.value for key, value in zip(node.keys, node.values)
                         if isinstance(key, ast.Constant) and key.value == "action"
                         and isinstance(value, ast.Constant) and isinstance(value.value, str))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "offer":
            kinds.update(_literal_kinds(node.args[1]))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"get_action", "_card"} and node not in permission_reads:
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                kinds.add(node.args[0].value)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_step":
            if len(node.args) > 6:
                kinds.update(_literal_kinds(node.args[6]))
    return kinds - {""}


@pytest.mark.parametrize("role,place,count,mode", [
    ("weekend climbing instructors", "Denver", 7, "consumer"),
    ("home bakers", "Leeds", 12, "consumer"),
    ("warehouse purchasing managers", "Oslo", 9, "company"),
])
def test_zero_result_chips_dispatch_visible_constrained_followups(journey_db, monkeypatch, caplog, role, place, count, mode):
    from datetime import timedelta
    import json
    from sqlalchemy import select
    from core.clock import utcnow
    from app.chat import customers, execute, proactive
    from app.chat.actions import disabled_reason
    from app.api.chat_api import ConfirmBody, _execute_confirmed_action
    from app.db.models import GtmContact, Job, VentureDocument
    from app.research import explore
    from app.core.local_mode import is_local
    from app.research import local_providers

    data = journey_db
    request = f"find me {count} {role} in {place}"
    view = {"state": "ready", "job_id": "job_empty", "discovery_started": True,
            "discovery_note": request, "reachable_leads": [],
            "icp": {"who": role, "location": place, "mode": mode, "chat_confirmed": True}}
    with data["factory"]() as session:
        session.add(VentureDocument(id="journey_empty", venture_id=data["vid"], kind="gtm_journey",
                    title="Customer search", body_md="", provenance_json=json.dumps(view),
                    created_at=utcnow(), updated_at=utcnow()))
        session.commit()
        before = (list(session.scalars(select(Job.id))), list(session.scalars(select(GtmContact.id))))
        event, = proactive._gtm_events(session, data["vid"], utcnow())

    def forbidden(*args, **kwargs):
        pytest.fail("Empty search recovery must not wait for the full status pipeline or unrelated library queries")

    searches = []

    async def search(provider, query, **kwargs):
        assert provider.name == "firecrawl" and kwargs["purpose"] == "research.explore.search"
        searches.append(query)
        url = "https://www.linkedin.com/in/public-profile" if "site:linkedin.com/in/" in query else "https://forum.example/community"
        return [{"url": url, "title": f"Public source for {role}", "snippet": f"{role} in {place}"}]

    async def no_answer(*args, **kwargs):
        return None

    monkeypatch.setattr(customers, "_view", forbidden)
    monkeypatch.setattr(customers, "_library_section", forbidden)
    provider_module = local_providers if is_local() else explore
    provider_name = "search_provider" if is_local() else "_provider_from_env"
    search_name = "search" if is_local() else "metered_search"
    monkeypatch.setattr(provider_module, provider_name, lambda: type("Provider", (), {"name": "firecrawl"})())
    monkeypatch.setattr(provider_module, search_name, search)
    monkeypatch.setattr(explore, "_platform_lookups", forbidden)
    monkeypatch.setattr(explore, "_extract_pages", forbidden)
    monkeypatch.setattr(execute, "_official_named_topics", forbidden)
    monkeypatch.setattr(execute, "_answer_from_search", no_answer)

    assert customers._saved_empty_reply(data["vid"])

    def dispatch(action, payload=None):
        return asyncio.run(_execute_confirmed_action(data["vid"], ConfirmBody(action=action, payload=payload or {}),
                           data["wid"], data["actor"], None, None))

    legacy = dispatch("gtm.status")
    assert legacy["gtm_search_progress"]["state"] == "ready"
    monkeypatch.setattr(customers, "_library_section", lambda *args, **kwargs: ("", []))
    rendered = customers.render(data["vid"], view)
    parts = proactive._brief_parts(data["vid"], utcnow() - timedelta(hours=1), utcnow())
    assert parts["actions"] == rendered["actions"]
    assert all(value in proactive.fallback_text(parts) for value in (role, place, str(count)))
    for reply in (legacy, rendered, {"body_md": event["body"], "actions": event["actions"]}):
        assert role in reply["body_md"] and place in reply["body_md"] and str(count) in reply["body_md"]
        assert "contact" in reply["body_md"].lower() and "public" in reply["body_md"].lower()
        assert "\u2014" not in str(reply)
        assert 2 <= len(reply["actions"]) <= 3
        for action in reply["actions"]:
            assert disabled_reason(action["action"]) is None
            result = dispatch(action["action"], action.get("payload"))
            assert result["id"]
            assert result["body_md"].strip()
            if is_local():
                assert action["payload"]["search_context"] not in result["body_md"]
            else:
                assert all(value in result["body_md"] for value in (role, place, str(count)))
            assert "cannot use this action" not in result["body_md"]
    assert len(searches) == 6
    assert all(all(value in query for value in (role, place, str(count))) for query in searches)
    monkeypatch.setattr(provider_module, provider_name, lambda: type("Provider", (), {"name": "duckduckgo"})())
    unavailable = dispatch(rendered["actions"][0]["action"], rendered["actions"][0]["payload"])
    assert "unavailable" in unavailable["body_md"] and len(searches) == 6
    if is_local():
        assert rendered["actions"][0]["payload"]["search_context"] not in unavailable["body_md"]
    else:
        assert all(value in unavailable["body_md"] for value in (role, place, str(count)))
    count_only = customers.empty_search_reply(view | {"discovery_note": f"find me {count}"})
    assert all(all(value in action["payload"]["search_context"] for value in (role, place, str(count)))
               for action in count_only["actions"])
    with data["factory"]() as session:
        assert before == (list(session.scalars(select(Job.id))), list(session.scalars(select(GtmContact.id))))
        session.add(VentureDocument(id="saved_draft", venture_id=data["vid"], kind="outreach_draft",
                    title="Saved draft", body_md="", provenance_json="{}", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    assert customers._saved_empty_reply(data["vid"]) is None
    assert not any("unhandled_chat_action" in record.message for record in caplog.records)


@pytest.mark.parametrize("audience,place", [
    ("industrial equipment buyers", "Rotterdam"),
    ("amateur astronomy enthusiasts", "Dunedin"),
])
def test_saved_empty_recovery_keeps_current_conversation_constraints(journey_db, monkeypatch, audience, place):
    import json
    from core.clock import utcnow
    from app.chat import conversations, customers
    from app.chat.store import append_message
    from app.db.models import VentureDocument

    data = journey_db
    request = f"Find me 6 {audience} in {place}, only public groups with recent activity"
    view = {"state": "ready", "job_id": "empty_job", "run_id": "empty_run",
            "discovery_note": f"Find {audience}", "icp": {"who": audience}}
    with data["factory"]() as session:
        session.add(VentureDocument(id="empty_journey", venture_id=data["vid"], kind="gtm_journey",
            title="Customer search", body_md="", provenance_json=json.dumps(view),
            created_at=utcnow(), updated_at=utcnow()))
        session.commit()

    def forbidden(*args, **kwargs):
        pytest.fail("Fast recovery must not run the status pipeline or library matching")

    monkeypatch.setattr(customers, "_view", forbidden)
    monkeypatch.setattr(customers, "_library_section", forbidden)
    own = conversations.create(data["vid"], idempotency_key="recovery-own")
    other = conversations.create(data["vid"], idempotency_key="recovery-other")
    with conversations.bind_conversation(data["vid"], own["id"]):
        append_message(data["vid"], role="user", body_md=request)
    with conversations.bind_conversation(data["vid"], other["id"]):
        append_message(data["vid"], role="user", body_md="Find me 99 unrelated contacts elsewhere")
    with conversations.bind_conversation(data["vid"], own["id"]):
        reply = asyncio.run(customers.execute(data["vid"], "gtm.status", {}, data["actor"]))

    assert request in reply["body_md"] and "unrelated contacts" not in str(reply)
    assert [action["action"] for action in reply["actions"]] == ["openworker_search", "openworker_search", "contacts.preview"]
    assert all(request in action["payload"]["search_context"] for action in reply["actions"])
    assert reply["citations"] == [{"gtm_search_progress": {"run_id": "empty_run", "job_id": "empty_job", "state": "ready"}}]


@pytest.mark.parametrize("old_audience,old_location,audience,location", [
    ("School catering coordinators", "Toronto", "Birdwatching beginners", "Kyoto"),
    ("Birdwatching beginners", "Kyoto", "School catering coordinators", "Toronto"),
])
def test_recovery_does_not_claim_updated_constraints_were_searched(journey_db, old_audience, old_location, audience, location):
    import json
    from app.chat import conversations
    from app.chat.store import append_message
    from app.core.local_mode import is_local
    from app.db.models import VentureDocument
    from core.clock import utcnow
    from app.chat import customers

    data = journey_db
    original = f"Find {old_audience} in {old_location}"
    request = f"Find 8 {audience} in {location}, only public groups with recent activity"
    view = {"state": "ready", "job_id": "saved_empty_job", "run_id": "saved_empty_run",
            "icp": {"who": old_audience, "location": old_location},
            "discovery_note": original}
    unchanged = customers.empty_search_reply(view | {"search_request": original})
    assert unchanged["body_md"].startswith("I searched public sources")
    own = conversations.create(data["vid"], idempotency_key="recovery-domain-counterexample")
    with conversations.bind_conversation(data["vid"], own["id"]):
        append_message(data["vid"], role="user", body_md=request)
        for failure in ({}, {"source_access": "unavailable", "error_code": "public_search_blocked"}):
            with data["factory"]() as session:
                row = session.get(VentureDocument, "changed_empty_search")
                if row is None:
                    row = VentureDocument(id="changed_empty_search", venture_id=data["vid"], kind="gtm_journey",
                        title="Customer search", body_md="", created_at=utcnow(), updated_at=utcnow())
                    session.add(row)
                row.provenance_json = json.dumps(view | failure)
                session.commit()
            updated = customers._saved_empty_reply(data["vid"])
            assert ("I haven't searched your updated request yet" if is_local() else
                "Previous attempt failed; current request not attempted") in updated["body_md"]
            assert request in updated["body_md"]
            assert old_audience not in str(updated) and old_location not in str(updated)
            assert "could not verify reachable contacts for" not in updated["body_md"]
            assert [action["action"] for action in updated["actions"]] == ["openworker_search", "openworker_search", "contacts.preview"]
            assert all(request in action["payload"]["search_context"] for action in updated["actions"])
            assert updated["citations"] == [{"gtm_search_progress": {
                "run_id": "saved_empty_run", "job_id": "saved_empty_job", "state": "ready"}}]


@pytest.mark.parametrize("audience", ["School catering coordinators in Toronto", "Birdwatching beginners in Kyoto"])
def test_explicit_constraints_do_not_offer_library_ids_from_a_broader_saved_audience(journey_db, monkeypatch, audience):
    from app.chat import customers, next_moves

    data = journey_db
    icp = {"who": "Operations managers", "chat_confirmed": True}
    entry_ids = ["library_saved_scope"]
    items = [{"id": entry_ids[0], "name": "Saved scope match", "role": icp["who"],
              "company": "Business", "why": "Matches the saved role", "channels": ["LinkedIn"]}]
    monkeypatch.setattr("app.lead_library.service.match", lambda *args, **kwargs: {"total": 1, "items": items})
    view = {"state": "idle", "icp": icp, "can_prepare": True}
    state = {**_saved_state(audience), "idea": {"confirmed": True}, "icp": icp,
             "research": {"present": True}, "page": True, "library": entry_ids}
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state.copy())
    request = {"value": ""}
    monkeypatch.setattr(customers, "saved_search_request", lambda *args, **kwargs: request["value"])
    baseline = customers.render(data["vid"], view)
    from app.core.local_mode import is_local
    if is_local():
        assert not any(action["action"].startswith("gtm.library_") for action in baseline["actions"])
    else:
        assert next(action["payload"] for action in baseline["actions"] if action["action"] == "gtm.library_claim") == {"entry_ids": entry_ids}
    baseline_moves = next_moves.compose(data["vid"])["actions"]
    if is_local():
        assert not any(action["action"].startswith("gtm.library_") for action in baseline_moves)
    else:
        assert next(action["payload"] for action in baseline_moves
                    if action["action"] == "gtm.library_claim") == {"entry_ids": entry_ids}

    request["value"] = f"Find me 8 {audience}, only public groups with recent activity"
    constrained = customers.render(data["vid"], view | {"search_request": request["value"]})
    assert not any(action["action"].startswith("gtm.library_") for action in constrained["actions"])
    assert "Saved scope match" not in constrained["body_md"]
    card = next(action for action in constrained["actions"] if action["action"] == "gtm.find")
    assert not card.get("confirmation") and card["payload"]["note"] == request["value"]
    offered = next_moves.compose(data["vid"])
    assert not any(action["action"] == "gtm.library_claim" for action in offered["actions"])
    assert next(action["payload"] for action in offered["actions"] if action["action"] == "gtm.find") == {
        "prepare": True, "note": request["value"]}


@pytest.mark.parametrize("audience", ["School catering coordinators in Toronto", "Birdwatching beginners in Kyoto"])
@pytest.mark.parametrize("action", ["openworker_search", "contacts.preview"])
def test_recovery_context_keeps_standing_grant_dispatch(journey_db, monkeypatch, audience, action):
    from app.analytics import chat_usage
    from app.chat import execute

    data = journey_db
    context = f"Your audience: 8 {audience}."
    payload = {"search_context": context}
    calls = []

    async def dispatch(venture_id, action_name, **kwargs):
        calls.append((venture_id, action_name, kwargs))
        return {"kind": "text", "body_md": "Nothing was posted or sent.", "actions": [], "citations": []}

    monkeypatch.setattr(execute, "_execute_action", dispatch)
    monkeypatch.setattr(chat_usage, "record_chat_action", lambda *args: None)
    monkeypatch.setenv("SUPYMEM_MODE", "local")

    result = asyncio.run(execute.execute_action(data["vid"], action, payload=payload, actor_subject=data["actor"],
                        confirmation_message_id="saved_confirmation", standing_grant_id="saved_grant"))

    assert calls == [(data["vid"], action, {"payload": payload, "actor_subject": data["actor"],
                     "confirmation_message_id": "saved_confirmation", "standing_grant_id": "saved_grant"})]
    assert result["body_md"] == "Nothing was posted or sent."


@pytest.mark.parametrize("idea", DOMAINS)
def test_emitted_kinds_distinguishes_suggestions_from_experiment_prose(tmp_path, idea):
    source = tmp_path / "suggestions.py"
    source.write_text(repr([
        {"hypothesis": idea, "action": f"Ask about recent needs for {idea}.", "success_measure": "Record recent behavior."},
        {"label": idea, "action": "chat.reply"},
        {"action": "experiment.list", "payload": {"hypothesis": idea}},
        {"label": idea, "action": "unregistered.action"},
    ]) + '\npermission = get_action("idea.answer").required_permission\n')
    assert _emitted_kinds(source) == {"chat.reply", "experiment.list", "unregistered.action"}


@pytest.mark.parametrize("idea", DOMAINS)
def test_every_next_move_and_proactive_action_dispatches(journey_db, monkeypatch, caplog, idea):
    from tests.unit.chat.test_next_moves import _finish_intake

    from app.chat import failed_jobs, next_moves, proactive
    from app.chat.actions import disabled_reason
    from app.chat.execute import execute_action
    from app.chat.navigation_map import _HIDDEN, in_chat
    from app.core.local_mode import is_local
    from app.db.models import Venture
    from app.gtm import journey
    from app.autopilot import outreach
    from app.experiments import exploration
    from app.ventures import next_step

    data = journey_db
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = idea
        session.commit()
    _finish_intake(data["vid"])
    monkeypatch.setattr(outreach, "_brief", lambda _: {"idea": {
        "audience": idea, "problem": idea, "confirmed": True}, "research": {}})
    monkeypatch.setattr(journey, "_icp_seed", outreach._brief)
    base = _saved_state(idea)
    selected = {"round_id": "selected_round", "option_id": "selected_option"}
    continued = []

    async def continue_exploration(venture_id, name, payload):
        assert venture_id == data["vid"] and name == "experiment.exploration.continue"
        continued.append(payload)
        return {"kind": "text", "body_md": "Continue with your selected idea.", "actions": [], "citations": []}

    monkeypatch.setattr(exploration, "act", continue_exploration)
    monkeypatch.setattr(next_moves, "experiments_autopilot_enabled", lambda: True)
    states = [base, {**base, "idea": {"idea_summary": idea, "confirmed": True}, "questions_pending": True},
              _saved_state(idea, later=True), {**_saved_state(idea, later=True), "drafts": []},
              {**_saved_state(idea, later=True), "library": []},
              {**base, "research": {"present": True}, "search_state": "running"},
              {**base, "page": True, "research": {"present": True},
              "social_leads": ["contact_1"]}, {**base, "paused": True}, {**base, "exploration": selected}]
    states += [{**base, "recommendation": {"id": name, "title": idea, "why": "Review your recorded results.",
                "action": name, "payload": {"prepare": True} if name == "pagebuilder.build" else {}}}
               for name in ("experiment.list", "experiment.beliefs.prepare", "experiment.recommendation.prepare", "pagebuilder.build")]
    if is_local():
        ready_page = {**base, "page": True, "page_build": "ready", "idea": {"idea_summary": idea, "confirmed": True}}
        states.extend([{**ready_page, "questions_pending": True}, {**ready_page, "research": {"present": True}}])
    states.append({**base, "goal_offer": {"label": "Book 3 customer calls this week", "action": "chat.reply",
                  "payload": {"text": "Book 3 customer calls this week"}}})
    states.append({**base, "first_question_pending": True, "questions_pending": False,
                   "idea": {"confirmed": False}})
    actions = []
    for state in states:
        monkeypatch.setattr(next_moves, "_snapshot", lambda _, state=state: deepcopy(state))
        monkeypatch.setattr(next_moves, "_fresh_recommendation", lambda _, state=state: state.get("recommendation"))
        monkeypatch.setattr(next_moves, "_customer_calls_offer", lambda _, snapshot: snapshot.get("goal_offer"))
        actions.extend(next_moves.compose(data["vid"])["actions"])
        if state.get("first_question_pending"):
            actions.extend(next_moves.compose(data["vid"], requested=True)["actions"])
    expected = _emitted_kinds(next_moves.__file__)
    from app.chat.actions import _local_blocked_reason
    if is_local():
        expected = {name for name in expected if not _local_blocked_reason(name)}
    assert {action["action"] for action in actions if action.get("action")} == expected
    actions += journey.draft_for_review(data["vid"])["actions"]
    actions += [target for _, target in _HIDDEN]
    actions += [{"label": name, "action": name} for name in sorted(_emitted_kinds(next_step.__file__))
                if name not in {action["action"] for action in actions}]
    actions += [proactive._ask("Choose the next step", "What should I do next?"),
                {"label": "Review customer search", "action": "gtm.status"}]
    for kind in failed_jobs.KINDS:
        actions.extend(in_chat(failed_jobs.failure_actions(data["vid"], kind, "clarification_invalid_output")))
    for action in actions:
        name = action.get("action")
        if not name:
            assert action["href"].startswith(f"/v/{data['vid']}/") or (is_local() and action["href"].startswith("/settings/local"))
            continue
        reason = disabled_reason(name)
        assert reason == (_local_blocked_reason(name) if is_local() else None), name
        if name == "chat.reply":
            assert action["payload"]["text"].strip()
            name, payload = "next_moves", {}
        else:
            payload = action.get("payload") or {}
        result = asyncio.run(execute_action(data["vid"], name, payload=payload, actor_subject=data["actor"]))
        assert result["kind"] and result["body_md"].strip(), (name, result)
        assert "cannot use this action" not in result["body_md"], (name, result)
    assert continued == [selected]
    assert not any("unhandled_chat_action" in record.message for record in caplog.records)


def test_every_emitted_suggestion_kind_has_a_dispatch_branch(journey_db):
    from app.chat import execute, next_moves
    from app.chat.actions import disabled_reason
    from app.ventures import journey_overrides, next_step

    root = Path(next_moves.__file__).parent
    kinds = set().union(*(_emitted_kinds(source) for source in root.rglob("*.py")))
    kinds |= _emitted_kinds(next_step.__file__)
    tree = ast.parse(Path(execute.__file__).read_text())
    dispatcher = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                      and node.name == "_execute_action")
    handled = set(journey_overrides.ACTIONS)
    prefixes = []
    for branch in dispatcher.body:
        if not isinstance(branch, ast.If):
            continue
        for condition in ast.walk(branch.test):
            if isinstance(condition, ast.Compare) and isinstance(condition.left, ast.Name) and condition.left.id == "action_name":
                handled.update(value.value for comparator in condition.comparators for value in ast.walk(comparator)
                               if isinstance(value, ast.Constant) and isinstance(value.value, str))
            if isinstance(condition, ast.Call) and isinstance(condition.func, ast.Attribute) and condition.func.attr == "startswith" \
                    and isinstance(condition.func.value, ast.Name) and condition.func.value.id == "action_name":
                prefixes.extend(value.value for argument in condition.args for value in ast.walk(argument)
                                if isinstance(value, ast.Constant) and isinstance(value.value, str))
    hook = (root.parents[3] / "apps/console/src/components/chat/useVentureChat.ts").read_text()
    client_kinds = {"chat.reply", "chat.prefill", "chat.stop", "chat.startFresh", "chat.new_conversation",
                    "referrals.show", "task.run", "mac.task.prepare", "gtm.route.confirm", "gtm.diagnosis.start"}
    from app.core.local_mode import is_local
    from app.chat.actions import _local_blocked_reason
    for kind in sorted(kinds):
        assert disabled_reason(kind) == (_local_blocked_reason(kind) if is_local() else None), kind
        if kind in client_kinds:
            assert f'"{kind}"' in hook, kind
        else:
            assert kind in handled or any(kind.startswith(prefix) for prefix in prefixes), kind


def test_disabled_page_builder_prepares_existing_build_path_without_starting_work(journey_db, monkeypatch):
    from fastapi import HTTPException
    from app.chat.execute import execute_action
    from app.db.models import Job, Venture
    from sqlalchemy import select

    data = journey_db
    monkeypatch.setenv("SUPYMEM_PAGEBUILDER_ENABLED", "0")
    from app.core.local_mode import is_local
    if is_local():
        from app.core.local_secrets import update_settings
        update_settings("capabilities", {"landing_pages": False}, {})
    for idea in DOMAINS:
        with data["factory"]() as session:
            session.get(Venture, data["vid"]).raw_idea = idea
            before = set(session.scalars(select(Job.id)))
            session.commit()
        result = asyncio.run(execute_action(data["vid"], "pagebuilder.build", payload={"prepare": True}, actor_subject=data["actor"]))
        assert result["body_md"].strip() and result["actions"]
        with data["factory"]() as session:
            assert set(session.scalars(select(Job.id))) == before
        if is_local():
            blocked = asyncio.run(execute_action(data["vid"], "pagebuilder.build", payload={}, actor_subject=data["actor"]))
            assert blocked["body_md"] == result["body_md"]
            with data["factory"]() as session:
                assert set(session.scalars(select(Job.id))) == before
        else:
            with pytest.raises(HTTPException) as error:
                asyncio.run(execute_action(data["vid"], "pagebuilder.build", payload={}, actor_subject=data["actor"]))
            assert error.value.status_code == 404 and error.value.detail == {"code": "pagebuilder_disabled"}


@pytest.mark.parametrize("idea", DOMAINS)
def test_choose_next_step_from_missing_memory_produces_moves_in_chat(journey_db, monkeypatch, idea):
    from app.chat import ground, next_moves
    from app.chat.execute import execute_action
    from app.db.models import Venture

    class EmptyMemory:
        async def answer(self, **kwargs):
            return None

    data = journey_db
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = idea
        session.commit()
    monkeypatch.setattr(ground, "get_memory", EmptyMemory)
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: _saved_state(idea))
    reply = asyncio.run(ground.answer_from_memory(data["vid"], "What did we learn last week?"))
    action = reply["actions"][0]
    assert action["label"] == "Choose your next step"
    assert action.get("action") == "next_moves" and "href" not in action
    result = asyncio.run(execute_action(data["vid"], action["action"], actor_subject=data["actor"]))
    assert result["kind"] == "next_moves" and result["body_md"].strip() and result["actions"]


