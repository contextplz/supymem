import asyncio
from types import SimpleNamespace

from tests.unit.chat.test_next_moves import DOMAINS, _saved_state
from tests.unit.chat.test_proactive import env, NOON, _job


def test_local_priority_has_one_plain_instruction_and_button(env, monkeypatch):
    from app.chat import build_moves, next_moves

    _, vid, _ = env
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr("app.ventures.stage.get_stage", lambda _: {"label": "Your next step"})
    monkeypatch.setattr(build_moves, "recommendation", lambda *_: None)
    for domain in DOMAINS:
        detail = f"Open Settings to name the website for {domain}."
        priority = {"stage": "Your priority", "title": "Set up your website", "detail": detail,
                    "cta_label": "Open Settings", "href": "/settings/local"}
        state = {**_saved_state(domain), "page_build": None, "priority_step": priority}
        monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
        reply = next_moves.compose(vid)
        assert reply["body_md"] == f"Set up your website. {detail} No new work was started."
        assert reply["actions"] == [{"label": "Open Settings", "href": "/settings/local", "style": "primary"}]


def test_choose_next_step_dispatches_a_reply_for_every_fallback(env, monkeypatch):
    from app.chat import ground, next_moves
    from app.chat.context import ChatContext
    from app.chat.handlers import explain
    from app.chat.navigation_map import in_chat_reply
    from app.chat.refuse import refuse_unknown

    client, _, _ = env

    async def no_memory(**kwargs):
        return None

    monkeypatch.setattr(ground, "get_memory", lambda: SimpleNamespace(answer=no_memory))
    for domain in DOMAINS:
        vid = client.post("/ventures", json={"raw_idea": domain, "start": False}).json()["id"]
        monkeypatch.setattr(next_moves, "_snapshot", lambda _: _saved_state(domain))
        ctx = ChatContext(venture={"id": vid, "raw_idea": domain})
        fallbacks = [explain.handle(ctx, "Why that choice?"), refuse_unknown(ctx),
                     asyncio.run(ground.answer_from_memory(vid, "Why that choice?"))]
        for href in (f"/v/{vid}", f"https://app.supymem.com/v/{vid}/"):
            legacy = {"label": "Choose your next step", "href": href}
            served = in_chat_reply({"role": "assistant", "body_md": domain, "actions": [legacy]})
            assert served["actions"][0]["action"] == "next_moves"
            assert not served["actions"][0].get("href")
            assert legacy["href"] == href
            for protected in ({**legacy, "kind": "recovery_link"}, {**legacy, "confirmation": {"action": "approve"}},
                              {**legacy, "action": "status"}, {**legacy, "label": "Open your venture"},
                              {**legacy, "href": "https://external.example/v/other"}):
                assert in_chat_reply({"actions": [protected]})["actions"] == [protected]
        for fallback in fallbacks:
            action = next(item for item in fallback["actions"] if item["label"] == "Choose your next step")
            assert action.get("action") == "next_moves"
            assert not action.get("href")
            for _ in range(2):
                response = client.post(f"/ventures/{vid}/chat/confirm", json={"action": action["action"]})
                assert response.status_code == 200, response.text
                reply = response.json()
                assert reply["kind"] == "next_moves"
                assert reply["body_md"].strip() and len(reply["actions"]) >= 2


def test_status_uses_next_moves_with_plain_waiting_and_running_copy(env, monkeypatch):
    from app.chat import next_moves
    from app.chat.context import ChatContext
    from app.chat.handlers import status

    client, _, _ = env
    snapshot = next_moves._snapshot
    for domain in DOMAINS:
        vid = client.post("/ventures", json={"raw_idea": domain, "start": False}).json()["id"]
        job_id = _job(vid, "tasks.execute", "pending", created=NOON)
        assert (job_id, "pending") in snapshot(vid)["jobs"]
        for job_status, wording in [(state, "Your work is waiting to start") for state in
                                    ("queued", "pending", "scheduled", "retry_wait")] + [
                                    ("leased", "Your work is underway"), ("running", "Your work is underway")]:
            state = {**_saved_state(domain), "jobs": [("job_1", job_status)]}
            monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
            reply = status.handle(ChatContext(venture={"id": vid}, limits=["No market read yet."]), "Where do I stand?")
            assert reply == next_moves.compose(vid)
            assert wording in reply["body_md"]
            assert len(reply["actions"]) >= 2
            assert not any(term in reply["body_md"].lower() for term in
                           ("company job", "runner", "market read", "in motion:", "\u2014"))


def test_handoff_next_moves_do_not_repeat_the_founder_goal(env, monkeypatch):
    from app.chat import next_moves
    from app.chat import store
    from app.core.local_mode import is_local
    from app.onboarding.steps import handoff

    _, vid, _ = env
    posted = []
    monkeypatch.setattr(handoff, "append_message", lambda *args, **kwargs: posted.append(kwargs))
    if is_local():
        store.append_message(vid, role="assistant", kind="text", body_md="Your setup is saved.")
        monkeypatch.setattr(store, "append_message", lambda *args, **kwargs: posted.append(kwargs) or True)
    for domain in DOMAINS:
        state = _saved_state(domain)
        monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
        ctx = SimpleNamespace(venture=SimpleNamespace(id=vid, raw_idea=domain), company="Your venture",
                              run=SimpleNamespace(id="run_1"), autonomy_word="Suggest", flags=lambda: {},
                              out={"handoff.plan_next_tasks": {"headline": domain, "tasks": [{"title": domain}]}})
        result = asyncio.run(handoff.done(ctx))
        reply = posted[-1]
        assert reply["kind"] == "next_moves"
        assert domain not in reply["body_md"]
        assert "**Next up:" not in reply["body_md"]
        assert not any(line.startswith("1. " + domain) for line in reply["body_md"].splitlines())
        assert next_moves.compose(vid)["body_md"] in reply["body_md"]
        assert reply["actions"] == next_moves.compose(vid)["actions"]
        assert result.output["recap"] is True
