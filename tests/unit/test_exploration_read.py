"""The rounds feed reads saved experiments and current confirmations in two markets."""
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from tests.unit.test_exploration import exploration_market, market, venture


def test_autopilot_rounds_payload_is_saved_scoped_current_and_flagged(exploration_market, monkeypatch, caplog):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    from app.api import exploration_read
    from app.chat import experiments, next_moves
    from app.credits import policy
    from app.db.models import ChatConversation, ChatMessage, Job, VentureDocument
    from app.db.supymem_models import EntryChatProposal, Experiment
    from app.experiments import autonomous, ledger
    from app.state import db

    client, venture_id, workspace_id, view, *_rest = exploration_market
    who, problem = view["icp"]["who"], view["icp"]["problem"]
    flags = {}
    monkeypatch.setattr(policy, "get_policy", lambda _key, default=None: flags)
    monkeypatch.setattr(next_moves, "compose", lambda _vid: None)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("A read must not plan, contact people or call a provider")

    monkeypatch.setattr(autonomous, "available", forbidden)
    monkeypatch.setattr(autonomous, "_provider", forbidden)
    monkeypatch.setattr(experiments, "_llm_json", forbidden)
    original = client.get(f"/ventures/{venture_id}/experiments/rounds")
    assert original.status_code == 200
    assert "experiments_autopilot_v1" not in original.json()
    started = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
    with db()() as session:
        direction = ledger.current_direction(session, venture_id)
        base = {"venture_id": venture_id, "direction": direction, "title": f"You could help {who}",
                "hypothesis": f"You could solve {problem} for {who}", "runner": "scan", "scan": "demand",
                "metric": "positive_responses", "min_sample": 20, "max_duration_days": 7,
                "founder_minutes": 10, "ceiling_micro": 50000, "approval": "once",
                "success_threshold": {"op": ">=", "value": 3, "min_events": 20},
                "failure_threshold": {"op": "<", "value": 1, "min_events": 20},
                "runner_params": {"private_note": "Never show internal planning or recipient data"}}
        for identifier, state, events, outcome, extra in [
            ("exp_failed_1", "concluded", 20, "refuted", {}),
            ("exp_failed_2", "concluded", 20, "refuted", {}),
            ("exp_running", "running", 7.0, None, {}),
            ("exp_pending", "proposed", None, None, {"claimed": True}),
            ("exp_sensible", "proposed", None, None, {}),
            ("exp_red", "proposed", None, None, {"scan": "redteam", "approach": "red-pill"}),
            ("exp_outreach", "proposed", None, None, {"runner": "outreach_split", "approval": "each_send"}),
        ]:
            spec = {**base, "id": identifier, **extra}
            result = {"measured": {"events": events}, "outcome": outcome,
                      "sources": [{"url": "https://source.example/record", "title": f"Your result for {who}"}],
                      "private_note": "Never show raw execution details"} if events is not None else None
            session.add(Experiment(id=identifier, venture_id=venture_id, workspace_id=workspace_id,
                kind=ledger.KIND, hypothesis=spec["hypothesis"], state=state, started_at=started if state != "proposed" else None,
                ended_at=started + timedelta(days=7) if state == "concluded" else None,
                outcome=outcome, learnings=f"You collected {events} responses about {problem}" if outcome else "",
                context=json.dumps({"runner": "system", "spec": spec, "result": result,
                                    **({"claimed": True} if extra.get("claimed") else {})})))
        for identifier, vid, wid, spec in [
            ("exp_other_venture", "other_venture", workspace_id, base),
            ("exp_other_workspace", venture_id, "other_workspace", base),
            ("exp_foreign_spec", venture_id, workspace_id, {**base, "venture_id": "other_venture"}),
            ("exp_wrong_id", venture_id, workspace_id, {**base, "id": "other_experiment"}),
            ("exp_old_direction", venture_id, workspace_id, {**base, "direction": "earlier_revision"}),
        ]:
            session.add(Experiment(id=identifier, venture_id=vid, workspace_id=wid, kind=ledger.KIND,
                                   state="proposed", context=json.dumps({"spec": spec})))
        session.add(Experiment(id="exp_corrupt", venture_id=venture_id, workspace_id=workspace_id,
                               kind=ledger.KIND, context="not-json"))
        session.add(Experiment(id="exp_manual", venture_id=venture_id, workspace_id=workspace_id,
            kind=ledger.KIND, state="running", hypothesis=f"You could help {who} with {problem}",
            context=json.dumps({"direction": direction})))
        session.commit()
    for value in (None, False, "true", 1):
        flags["experiments_autopilot_v1"] = value
        assert client.get(f"/ventures/{venture_id}/experiments/rounds").content == original.content
    flags["experiments_autopilot_v1"] = True
    first = client.get(f"/ventures/{venture_id}/experiments/rounds")
    assert first.status_code == 200, first.text
    feed = first.json()["experiments_autopilot_v1"]
    assert feed["venture_id"] == venture_id and feed["decision_recommendation"] is None
    running = next(item for item in feed["experiments"] if item["id"] == "exp_running")
    assert running["collected"] == running["result"]["measured"]["events"] == 7
    assert running["n_min"] == running["spec"]["min_sample"] == 20
    assert running["spec"]["success_threshold"] == base["success_threshold"]
    assert running["spec"]["failure_threshold"] == base["failure_threshold"]
    assert running["started_at"] == started.isoformat()
    assert running["review_at"] == (started + timedelta(days=7)).isoformat()
    assert running["outcome"] is None
    pending = next(item for item in feed["experiments"] if item["id"] == "exp_pending")
    assert pending["state"] == "running" and pending["collected"] is None and pending["result"] is None
    assert pending["review_at"] is None
    manual = next(item for item in feed["experiments"] if item["id"] == "exp_manual")
    assert manual["hypothesis"] == f"You could help {who} with {problem}"
    assert manual["collected"] is None and manual["n_min"] is None and manual["review_at"] is None
    assert all(item["outcome"] == "refuted" for item in feed["experiments"] if item["id"].startswith("exp_failed"))
    proposals = {item["id"]: item for item in feed["proposed_experiments"]}
    assert set(proposals) == {"exp_sensible", "exp_red", "exp_outreach"}
    assert proposals["exp_sensible"]["approach"] == "sensible"
    assert proposals["exp_red"]["approach"] == "red-pill"
    assert proposals["exp_outreach"]["contacts_people"] is True
    assert proposals["exp_sensible"]["contacts_people"] is False
    assert proposals["exp_sensible"]["max_duration_days"] == 7
    assert proposals["exp_sensible"]["founder_minutes"] == 10
    assert proposals["exp_sensible"]["ceiling_micro"] == 50000
    assert "private_note" not in first.text and "runner_params" not in first.text

    state = ledger.loop_state(venture_id)
    proposed_direction = {"kind": "narrower_wedge", "text": f"Focus on {problem}",
                          "idea": f"You could help {who} by focusing on {problem}"}
    cards = [experiments._decision_card(venture_id, name, state, "You choose your next step",
        ["You review the change before anything happens."],
        proposed_direction if name in {"experiment.pivot", "experiment.spinoff"} else None)
        for name in ("experiment.persevere", "experiment.pivot", "experiment.spinoff", "experiment.kill")]
    with db()() as session:
        session.add(ChatMessage(id="review_saved", venture_id=venture_id, workspace_id=workspace_id,
            role="assistant", kind="confirmation", body_md="Internal notes must not appear in the feed",
            actions_json=json.dumps(cards), created_at=started))
        session.add(ChatConversation(id="private_other", venture_id=venture_id, workspace_id=workspace_id,
            visibility="private", creator_person_id="another_founder", title="Private", creation_key="private_other",
            created_at=started, updated_at=started))
        session.add(ChatMessage(id="review_private", venture_id=venture_id, workspace_id=workspace_id,
            conversation_id="private_other", role="assistant", kind="confirmation", body_md="Private",
            actions_json=json.dumps([{**card, "label": "Private choice"} for card in cards]),
            created_at=started + timedelta(hours=1)))
        session.add(ChatMessage(id="review_foreign", venture_id="other_venture", workspace_id=workspace_id,
            role="assistant", kind="confirmation", body_md="Other venture", actions_json=json.dumps(cards),
            created_at=started + timedelta(hours=2)))
        session.commit()

    models = (Experiment, ChatMessage, ChatConversation, Job, VentureDocument, EntryChatProposal)
    with db()() as session:
        before = [session.scalar(select(func.count()).select_from(model)) for model in models]
        saved_contexts = {row.id: row.context for row in session.scalars(select(Experiment))}
    for _repeat in range(2):
        response = client.get(f"/ventures/{venture_id}/experiments/rounds")
        assert response.status_code == 200, response.text
        recommendation = response.json()["experiments_autopilot_v1"]["decision_recommendation"]
        assert recommendation["actions"] == cards
        assert "Did not work:" in recommendation["evidence_line"] and problem in recommendation["evidence_line"]
        assert "Internal notes" not in response.text and "Private choice" not in response.text
    with db()() as session:
        assert [session.scalar(select(func.count()).select_from(model)) for model in models] == before
        assert {row.id: row.context for row in session.scalars(select(Experiment))} == saved_contexts
    assert client.get(f"/ventures/{venture_id}/experiments/rounds", headers={"X-Subject": "another_founder"}).status_code == 404
    assert client.get("/ventures/missing/experiments/rounds").status_code == 404

    with db()() as session:
        row = session.get(Experiment, "exp_failed_1")
        row.learnings = f"You recorded a new result about {problem}"
        session.commit()
    assert client.get(f"/ventures/{venture_id}/experiments/rounds").json()["experiments_autopilot_v1"]["decision_recommendation"] is None

    with db()() as session:
        row = session.get(Experiment, "exp_running")
        context = json.loads(row.context)
        context["result"]["measured"]["events"] = True
        context["spec"]["success_threshold"]["value"] = float("inf")
        context["spec"]["max_duration_days"] = 1e100
        row.context = json.dumps(context)
        session.commit()
    malformed = client.get(f"/ventures/{venture_id}/experiments/rounds")
    assert malformed.status_code == 200, malformed.text
    running = next(item for item in malformed.json()["experiments_autopilot_v1"]["experiments"] if item["id"] == "exp_running")
    assert running["collected"] is None and running["review_at"] is None
    assert running["result"]["measured"] == {}
    assert "success_threshold" not in running["spec"]
    assert "exploration_autopilot_unavailable" not in caplog.text
    monkeypatch.setattr(exploration_read, "autopilot_view", forbidden)
    flags["experiments_autopilot_v1"] = False
    assert client.get(f"/ventures/{venture_id}/experiments/rounds").content == original.content
    flags["experiments_autopilot_v1"] = True
    unavailable = client.get(f"/ventures/{venture_id}/experiments/rounds")
    assert unavailable.status_code == 200 and unavailable.json()["experiments_autopilot_v1"] is None
    assert "exploration_autopilot_unavailable" in caplog.text
