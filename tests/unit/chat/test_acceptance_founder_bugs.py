"""Founder bug repros using existing service and chat action boundaries."""

import asyncio

import pytest

from tests.unit.chat.test_next_moves import DOMAINS, _saved_state
from tests.unit.chat.test_proactive import NOON, _job, env


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.xfail(strict=False, reason="acceptance: BUG-04")
def test_choose_your_next_step_dispatches_a_reply(idea, env, monkeypatch):
    from app.chat import next_moves, refuse
    from app.chat.actions import REGISTRY
    from app.chat.context import ChatContext
    from app.chat.execute import execute_action
    from app.chat.handlers import explain

    client, _, _ = env
    made = client.post("/ventures", json={"raw_idea": idea, "start": False})
    assert made.status_code == 201, made.text
    venture_id = made.json()["id"]
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: _saved_state(idea))
    context = ChatContext(venture={"id": venture_id})
    for card in (explain.handle(context, "Why did you choose that?"), refuse.refuse_unknown(context)):
        action = next(action for action in card["actions"] if action["label"] == "Choose your next step")
        assert action.get("action") in REGISTRY, "A link to the current venture does not dispatch a reply"
        assert "href" not in action
        reply = asyncio.run(execute_action(venture_id, action["action"], payload=action.get("payload", {})))
        assert reply["kind"] == "next_moves" and reply["body_md"].strip()
        assert reply["actions"]


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.xfail(strict=False, reason="acceptance: BUG-05")
def test_status_copy_avoids_internal_job_words(idea, env, monkeypatch):
    from app.chat import next_moves
    from app.chat.context import ChatContext
    from app.chat.handlers import status

    monkeypatch.setattr(next_moves, "_snapshot", lambda _: {
        **_saved_state(idea), "research_running": True, "search_state": "queued",
    })
    reply = status.handle(ChatContext(venture={"id": env[1], "raw_idea": idea}), "What is happening?")
    assert reply["kind"] == "next_moves" and reply["body_md"].strip()
    for jargon in ("runner", "company job", "market read"):
        assert jargon not in reply["body_md"].lower()
    assert "you" in reply["body_md"].lower() and "\u2014" not in reply["body_md"]


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.xfail(strict=False, reason="acceptance: BUG-07")
def test_next_up_does_not_repeat_your_raw_goal_as_an_action(idea, env, monkeypatch):
    from app.chat import next_moves

    state = _saved_state(idea)
    state["goal"] = {"text": idea}
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    card = next_moves.compose(env[1])
    assert card["kind"] == "next_moves" and card["actions"]
    assert all(action["label"].strip().casefold() != idea.casefold() for action in card["actions"])
    assert f"1. {idea}" not in card["body_md"]


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.xfail(strict=False, reason="acceptance: BUG-09")
def test_initial_next_moves_use_positive_idea_copy(idea, env, monkeypatch):
    from app.chat import next_moves

    monkeypatch.setattr(next_moves, "_snapshot", lambda _: _saved_state(idea))
    card = next_moves.compose(env[1])
    visible = " ".join([card["body_md"], *[action["label"] for action in card["actions"]]]).lower()
    for old_copy in ("pressure-test your idea", "ai assessment", "biggest risk"):
        assert old_copy not in visible
    assert card["actions"] and "you" in visible


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.xfail(strict=False, reason="acceptance: BUG-10")
def test_running_research_offers_two_or_three_parallel_actions(idea, env, monkeypatch):
    from app.chat import next_moves
    from app.chat.actions import REGISTRY

    monkeypatch.setattr(next_moves, "_snapshot", lambda _: {
        **_saved_state(idea), "research_running": True, "search_state": "running",
    })
    card = next_moves.compose(env[1])
    assert "while i" in card["body_md"].lower() and "you can" in card["body_md"].lower()
    parallel = [action for action in card["actions"] if action["action"] != "journey.skip_step"]
    assert 2 <= len(parallel) <= 3
    assert all(action["action"] in REGISTRY for action in parallel)
    assert not any(action["action"].startswith("research.") for action in parallel)


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.xfail(strict=False, reason="acceptance: BUG-11")
def test_what_next_replies_without_waiting_for_or_cancelling_running_research(idea, env, monkeypatch):
    from app.chat import assistant, next_moves
    from app.db.models import Job
    from app.state import db

    client, _, _ = env
    made = client.post("/ventures", json={"raw_idea": idea, "start": False})
    assert made.status_code == 201, made.text
    venture_id = made.json()["id"]
    job_id = _job(venture_id, "research.deep", "running", created=NOON)
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: {
        **_saved_state(idea), "research_running": True, "search_state": "running",
    })

    async def no_model(*args, **kwargs):
        pytest.fail("A next-moves reply must not wait on the chat model")

    monkeypatch.setattr(assistant, "_complete", no_model)
    with db()() as session:
        job = session.get(Job, job_id)
        before = (job.status, job.started_at, job.attempts, job.cancel_requested, job.finished_at)
    # shortcut: saved job only, add a held-worker-lock check before closing BUG-11.
    reply = client.post(f"/ventures/{venture_id}/chat", json={"message": "What should I do next?"})
    assert reply.status_code == 200, reply.text
    assert reply.json()["kind"] == "next_moves" and reply.json()["actions"]
    with db()() as session:
        job = session.get(Job, job_id)
        assert (job.status, job.started_at, job.attempts, job.cancel_requested, job.finished_at) == before
