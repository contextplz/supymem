import asyncio
import json

import pytest

from tests.company_os_harness import local_owner_clients
from tests.unit.test_autonomous_experiments import market, venture


def system_result(venture_id, workspace_id, hypothesis, *, outcome="refuted", direction=None, suffix="current"):
    from app.db.supymem_models import Experiment
    from app.experiments import ledger
    from app.state import db
    from core.clock import utcnow

    identifier = "exp_system_" + suffix
    with db()() as session:
        direction = ledger.current_direction(session, venture_id) if direction is None else direction
        session.add(Experiment(id=identifier, venture_id=venture_id, workspace_id=workspace_id,
            kind=ledger.KIND, hypothesis="", state="concluded", ended_at=utcnow(),
            context=json.dumps({"runner": "system", "outreach_state": "waiting_data",
                "spec": {"direction": direction, "hypothesis": hypothesis},
                "result": {"outcome": outcome, "insight": f"Recorded result for {hypothesis}",
                           "sources": [{"url": "https://source.example/record", "title": hypothesis}]}})))
        session.commit()
    return identifier


def test_loop_uses_current_system_and_founder_results_without_failing_unclear_tests(market, monkeypatch):
    from app.core.local_mode import is_local
    from core.clock import utcnow
    from app.credits import policy
    from app.db.supymem_models import Experiment
    from app.experiments import ledger
    from app.state import db

    _client, venture_id, workspace_id, view, *_rest = market
    hypothesis = f"{view['icp']['who']} need {view['icp']['problem']}"
    identifier = system_result(venture_id, workspace_id, hypothesis)
    system_result(venture_id, workspace_id, hypothesis, direction="old_revision", suffix="old")
    system_result("other_venture", workspace_id, hypothesis, suffix="foreign")
    founder_id = ledger.create_from_tests(venture_id, "chat", [{"hypothesis": "Try " + hypothesis}])[0]
    ledger.record_result(founder_id, venture_id, True, "I ran the test with five people. Results were inconclusive",
        "inconclusive", "Too early", sample_size=5, provenance={"source": "Your test notes",
        "method": "Customer interviews", "observed_at": utcnow().date().isoformat()})
    monkeypatch.setattr(policy, "get_policy", lambda _key: {})
    legacy = ledger.loop_state(venture_id)
    if is_local():
        assert {item["id"] for item in legacy["done"]} == {founder_id, identifier}
        assert legacy["ready"] and [item["id"] for item in legacy["failed"]] == [identifier]
    else:
        assert [item["id"] for item in legacy["rows"]] == [founder_id]
        assert not legacy["ready"] and not legacy["failed"]
    monkeypatch.setattr(policy, "get_policy", lambda _key: {"experiments_autopilot_v1": True})
    state = ledger.loop_state(venture_id)
    assert {item["id"] for item in state["done"]} == {founder_id, identifier}
    assert [item["id"] for item in state["failed"]] == [identifier]
    assert state["failed"][0]["hypothesis"] == hypothesis
    assert state["failed"][0]["learning"] == f"Recorded result for {hypothesis}"
    assert state["ready"]
    with db()() as session:
        session.get(Experiment, identifier).outcome = "inconclusive"
        session.commit()
    assert not ledger.loop_state(venture_id)["failed"] and not ledger.loop_state(venture_id)["ready"]
    with db()() as session:
        session.get(Experiment, identifier).outcome = "supported"
        session.commit()
    assert ledger.loop_state(venture_id)["supported"] and not ledger.loop_state(venture_id)["ready"]


def test_requested_decisions_keep_evidence_quotas_and_spinoff_retry_safety(market, monkeypatch):
    from app.core.local_mode import is_local
    from app.core.local_secrets import update_settings
    from app.analytics.feature_catalog import ACTION_SLUG
    from app.chat import actions, execute, experiments
    from app.credits import policy
    from app.db.models import Venture, VentureDocument, Workspace
    from app.db.supymem_models import EntryChatProposal, Experiment
    from app.entry import service
    from app.experiments import ledger
    from app.state import db
    from app.ventures import quota, state_brief
    from app.workspaces import service as workspaces
    from core.clock import utcnow

    _client, venture_id, workspace_id, view, *_rest = market
    if is_local():
        update_settings("composio", {"auth_configs": {"stripe": "ac_stripe", "hubspot": "ac_hubspot"}},
                        {"api_key": "test-only-composio-key"})
        update_settings("capabilities", {"connected_apps": True, "run_for_me_experiments": True}, {})
    hypothesis = f"{view['icp']['who']} need {view['icp']['problem']}"
    identifier = system_result(venture_id, workspace_id, hypothesis)
    monkeypatch.setattr(policy, "get_policy", lambda _key: {"experiments_autopilot_v1": True})
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda _venture: {
        "idea": {"raw_idea": hypothesis, "audience": view["icp"]["who"]}})
    prompts, creation, quota_calls, revised, paused = [], [], [], [], []

    async def directions(_system, prompt, _venture):
        prompts.append(prompt)
        return {"directions": [{"kind": "narrower_wedge", "title": "Try the urgent task\u2014privately",
                                "text": "Focus on " + view["icp"]["problem"] + "\u2014nothing is sent."}]}

    monkeypatch.setattr(experiments, "_llm_json", directions)
    review = asyncio.run(experiments.review_reply(venture_id))
    assert identifier in prompts[0] and "https://source.example/record" in prompts[0]
    assert view["icp"]["who"] in review["body_md"]
    assert "\u2014" not in json.dumps(review, ensure_ascii=False)
    cards = {item["action"]: item["payload"] if is_local() else item["confirmation"]["resulting_state"]
        for item in review["actions"] if item["action"].startswith("experiment.")}
    if is_local():
        assert review["kind"] != "confirmation" and not any(item.get("confirmation") for item in review["actions"])
    assert set(cards) == {"experiment.persevere", "experiment.pivot", "experiment.spinoff", "experiment.kill"}
    assert [{"action": item["action"], "label": item["label"]} for item in review["actions"] if item["action"] not in cards] == [
        {"action": "connections.connect", "label": "Connect Stripe"},
        {"action": "connections.connect", "label": "Connect HubSpot"}]
    for name, payload in cards.items():
        assert actions.get_action(name).consequential and ACTION_SLUG[name] == "experiments"
        assert payload["evidence"][0]["experiment_id"] == identifier
    assert [source for source in review["citations"] if "url" in source] == cards["experiment.spinoff"]["evidence"][0]["sources"]
    with db()() as session:
        assert session.query(EntryChatProposal).count() == 0
        parent_owner = session.get(Workspace, workspace_id).owner_id
    workspaces.merge_settings(workspace_id, {"outbound_approvals": ["parent_only"], "standing_budget": 5})

    async def create_idea(text, *, options, actor_id, surface):
        creation.append((text, options, actor_id, surface))
        workspace = workspaces.create_workspace(kind="venture", name="Separate test", entry_mode="idea", owner_id=actor_id)
        with db()() as session:
            session.add(Venture(id="ven_separate", workspace_id=workspace.id, name="Separate test", raw_idea=text,
                                stage="draft", autonomy_level="L1", created_at=utcnow(), updated_at=utcnow()))
            service._link_chat_proposal(session, options, "ven_separate", actor_id)
            session.commit()
        raise RuntimeError("interrupted_after_venture_commit")

    def quota_denied(actor):
        quota_calls.append(actor)
        raise quota.VentureLimitReached()

    monkeypatch.setattr(service, "_commit_idea", create_idea)
    monkeypatch.setattr(quota, "assert_can_create_venture", quota_denied)
    payload = cards["experiment.spinoff"]
    with pytest.raises(ValueError, match="experiment_decision_invalid"):
        asyncio.run(experiments.decision_run(venture_id, "experiment.spinoff", {**payload, "evidence": []}, parent_owner))
    assert not creation
    denied = asyncio.run(experiments.decision_run(venture_id, "experiment.spinoff", payload, parent_owner))
    assert "could not be created" in denied["body_md"] and not creation and quota_calls == [parent_owner]
    monkeypatch.setattr(quota, "assert_can_create_venture", lambda actor: quota_calls.append(actor))
    with pytest.raises(RuntimeError, match="interrupted_after_venture_commit"):
        asyncio.run(experiments.decision_run(venture_id, "experiment.spinoff", payload, parent_owner))
    with db()() as session:
        session.get(Experiment, identifier).outcome = "supported"
        session.commit()
    separate = asyncio.run(execute.execute_action(venture_id, "experiment.spinoff", payload=payload,
                                                actor_subject=parent_owner))
    assert separate["kind"] == "experiment_spinoff" and len(creation) == 1 and len(quota_calls) == 2
    assert asyncio.run(experiments.decision_run(venture_id, "experiment.spinoff", payload, parent_owner)) == separate
    with pytest.raises(ValueError, match="experiment_decision_invalid"):
        asyncio.run(experiments.decision_run(venture_id, "experiment.kill", payload, parent_owner))
    assert len(creation) == 1
    with db()() as session:
        session.get(Experiment, identifier).outcome = "refuted"
        session.commit()
    assert creation[0][0] == payload["idea"] and creation[0][1]["parent_evidence"] == payload["evidence"]
    assert creation[0][1]["idempotency_key"] == payload["idempotency_key"]
    with db()() as session:
        child = session.get(Venture, "ven_separate")
        settings = json.loads(session.get(Workspace, child.workspace_id).settings_json)
        assert settings["experiment_origin"]["parent_venture_id"] == venture_id
        assert settings["experiment_origin"]["evidence"] == payload["evidence"]
        assert "outbound_approvals" not in settings and "standing_budget" not in settings
        assert session.get(Venture, venture_id).raw_idea == view["icp"]["problem"]

    async def revise(received_venture, received_payload, actor):
        revised.append((received_venture, received_payload, actor))
        return experiments._reply("Started an idea revision with that direction.")

    async def pause(received_venture):
        paused.append(received_venture)
        return experiments._reply("Paused", [{"label": "Resume", "action": "resume"}])

    monkeypatch.setattr(experiments, "revise_run", revise)
    monkeypatch.setattr(execute, "_pause", pause)
    for name in ("experiment.persevere", "experiment.pivot", "experiment.kill"):
        reply = asyncio.run(experiments.decision_run(venture_id, name, cards[name], parent_owner))
        assert reply["citations"] == [source for source in review["citations"] if "url" in source]
        assert asyncio.run(experiments.decision_run(venture_id, name, cards[name], parent_owner)) == reply
    assert len(revised) == len(paused) == 1 and revised[0][1]["text"] == cards["experiment.pivot"]["text"]
    with db()() as session:
        saved = session.query(VentureDocument).filter_by(venture_id=venture_id, kind="experiment_decision").all()
        assert len(saved) == 4
        assert {json.loads(item.provenance_json)["action"] for item in saved} == set(cards)
    uncertain_calls = []

    async def interrupted_creation(text, **_kwargs):
        uncertain_calls.append(text)
        raise RuntimeError("creation_outcome_unknown")

    monkeypatch.setattr(service, "_commit_idea", interrupted_creation)
    with db()() as session:
        session.get(Experiment, identifier).outcome = "supported"
        session.commit()
    uncertain_payload = experiments._decision_payload(venture_id, "experiment.spinoff", ledger.loop_state(venture_id),
        {"kind": "different_customer", "text": "Test another group", "idea": hypothesis + ". Test another group."})
    with pytest.raises(RuntimeError, match="creation_outcome_unknown"):
        asyncio.run(experiments.decision_run(venture_id, "experiment.spinoff", uncertain_payload, parent_owner))
    uncertain = asyncio.run(experiments.decision_run(venture_id, "experiment.spinoff", uncertain_payload, parent_owner))
    assert "until this is checked" in uncertain["body_md"] and len(uncertain_calls) == 1
    monkeypatch.setattr(policy, "get_policy", lambda _key: {})
    if is_local():
        update_settings("capabilities", {"run_for_me_experiments": False}, {})
    quota_count = len(quota_calls)
    off = asyncio.run(experiments.decision_run(venture_id, "experiment.spinoff", payload, parent_owner))
    if is_local():
        assert off == separate and len(quota_calls) == quota_count
    else:
        assert "not available yet" in off["body_md"]
    assert len(creation) == 1


@pytest.mark.parametrize("action", ["experiment.kill", "experiment.pivot", "experiment.spinoff", "experiment.persevere"])
def test_decisions_reject_forged_or_missing_evidence(market, action):
    from app.core.local_mode import is_local

    client, venture_id = market[0], market[1]
    response = client.post(f"/ventures/{venture_id}/chat/confirm",
                           json={"action": action, "payload": {"parent_venture_id": venture_id}})
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error"]["code"] == ("experiment_decision_invalid" if is_local() else "confirmation_card_required")
    if is_local():
        assert "review your saved results" in response.json()["detail"]["error"]["message"]
