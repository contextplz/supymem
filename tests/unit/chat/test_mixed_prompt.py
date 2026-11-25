"""Questions never enqueue. Mixed prompts split: lookup + apply work."""

from __future__ import annotations

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app
from tests.company_os_harness import configure_local_test_model
from tests.unit.chat.test_steer_site import _saved_page


@pytest.fixture(params=["Equipment handovers for factory supervisors", "Weekend outings for families"])
def env(tmp_path, monkeypatch, request):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/mixed.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FIRSTLOOK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "mem.sqlite3"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("FIRSTLOOK_COPY_MODE", "fallback")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("CODING_AGENT", "inhouse_stub")
    main = load_company_os_app()
    configure_local_test_model()
    from app import state as state_mod
    from app.memory import reset_memory_for_tests

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    reset_memory_for_tests(tmp_path / "mem.sqlite3")
    with TestClient(main.create_app()) as client:
        created = client.post(
            "/ventures",
            json={"raw_idea": request.param},
        )
        assert created.status_code == 201, created.text
        yield client, created.json()["id"], created.json()["workspace_id"], tmp_path


def _chat_ids(vid: str) -> set[str]:
    from app.planning.store import list_tasks

    return {
        row["id"]
        for row in list_tasks(vid, statuses=("open", "running", "done", "dismissed"))
        if row.get("source") == "chat"
    }


def _site_sha(workspace_id: str) -> str | None:
    from app.firstlook.versions import get_latest

    latest = get_latest(workspace_id)
    return None if latest is None else latest.sha256


def test_gtm_question_with_answer_format_and_listed_prohibitions_does_not_queue(env):
    client, vid, _wid, _tmp = env
    from app.chat.intent import classify_intent, split_speech_acts

    message = (
        "For LabHire, what is the next smallest founder-approved GTM step to get two "
        "consenting pilot users, based only on saved evidence? Distinguish a connected "
        "channel from verified delivery. Do not send, publish, or start a task."
    )
    before_ids = _chat_ids(vid)
    assert classify_intent(message) == "assistant"
    assert split_speech_acts(message) == [("assistant", message)]
    reply = client.post(f"/ventures/{vid}/chat", json={"message": message})
    assert reply.status_code == 200, reply.text
    assert "queued" not in reply.json()["body_md"].lower()
    assert _chat_ids(vid) == before_ids

    mixed = "What is our GTM status? Draft an invitation for review. Do not send it."
    assert [intent for intent, _part in split_speech_acts(mixed)] == ["assistant", "change"]


@pytest.mark.asyncio
async def test_pure_question_does_not_enqueue_or_mutate_site(env):
    client, vid, wid, tmp_path = env
    first = _saved_page(wid, vid, tmp_path, {"headline": "Your current page"})
    before_ids = _chat_ids(vid)
    before_sha = first.sha256

    reply = client.post(
        f"/ventures/{vid}/chat",
        json={"message": "How many waitlist signups do we have?"},
    )
    assert reply.status_code == 200, reply.text
    body = reply.json()["body_md"]
    assert "Queued" not in body

    assert _chat_ids(vid) == before_ids
    assert _site_sha(wid) == before_sha


@pytest.mark.asyncio
async def test_mixed_question_and_rewrite_splits(env):
    from app.firstlook.service import CONNECT_STEP

    client, vid, wid, tmp_path = env
    first = _saved_page(wid, vid, tmp_path, {"headline": "Your current page"})
    before_ids = _chat_ids(vid)

    reply = client.post(
        f"/ventures/{vid}/chat",
        json={
            "message": (
                "How many waitlist signups do we have? "
                "Rewrite the headline to Stop waiting on the lobby"
            )
        },
    )
    assert reply.status_code == 200, reply.text
    body = reply.json()["body_md"]
    assert "Queued" not in body
    assert CONNECT_STEP in body
    assert any(item.get("href") == "/settings/local" for item in reply.json()["citations"])
    assert "Send the change as its own message" in body

    after = _chat_ids(vid)
    assert after == before_ids
    sha = _site_sha(wid)
    assert sha is not None
    assert sha == first.sha256


def test_status_questions_do_not_enqueue(env):
    client, vid, _wid, _tmp = env
    before_ids = _chat_ids(vid)

    for message in ("who is working?", "what's the status?"):
        reply = client.post(f"/ventures/{vid}/chat", json={"message": message})
        assert reply.status_code == 200, reply.text
        assert "Queued" not in reply.json()["body_md"]

    assert _chat_ids(vid) == before_ids


def test_plain_lead_and_email_requests_do_not_get_mixed_question_notes(env, monkeypatch):
    import asyncio
    from app.chat import router
    from app.db.models import Venture
    from app.state import db

    _client, vid, _wid, _tmp = env
    cases = [
        ("a warehouse maintenance tracker", "show leads and draft emails for warehouse supervisors"),
        ("a family hiking planner", "show leads and draft emails for hiking families"),
    ]
    for raw, message in cases:
        with db()() as session:
            session.get(Venture, vid).raw_idea = raw
            session.commit()
        monkeypatch.setattr(router, "split_speech_acts", lambda text: [("leads", "show leads"), ("outreach_draft", "draft emails")])

        async def dispatch(venture_id, context, intent, clause, speaker, **kwargs):
            if intent == "leads":
                return {"kind": "text", "body_md": "Your leads are ready.", "actions": [], "citations": []}
            return {"kind": "text", "body_md": "Your drafts are ready. Review them before asking to send.", "actions": [], "citations": []}

        monkeypatch.setattr(router, "_dispatch_intent", dispatch)
        reply = asyncio.run(router.handle_message(vid, message))
        assert not any(action.get("confirmation") for action in reply["actions"])
        assert "I lined these up" not in reply["body_md"]
        assert "I only answered the question" not in reply["body_md"]
        assert "Send the change as its own message" not in reply["body_md"]
        monkeypatch.setattr(router, "split_speech_acts", lambda text: [("status", "How many leads do we have?"), ("change", "Draft an email")])
        mixed = asyncio.run(router.handle_message(vid, "How many leads do we have? Draft an email."))
        assert "I lined these up" not in mixed["body_md"]
        assert "I only answered the question" in mixed["body_md"]


def test_no_delivery_constraint_stays_attached_and_queues_one_safe_task(env):
    client, vid, _wid, _tmp = env
    from app.chat.intent import split_speech_acts
    from app.chat.steer import leftover_after_steer, parse_steer
    from app.planning.store import list_tasks

    message = (
        "Prepare a founder-reviewed LabHire GTM outreach channel decision brief, planning only. "
        "Do not contact anyone, send mail, post, or publish anything."
    )
    assert split_speech_acts(message) == [("change", message)]
    assert parse_steer(message) == []
    assert leftover_after_steer(message) == message

    reply = client.post(f"/ventures/{vid}/chat", json={"message": message})
    assert reply.status_code == 200, reply.text
    body = reply.json()["body_md"]
    assert "Queued" in body
    assert "Drafted an email" not in body

    tasks = [
        row
        for row in list_tasks(vid, statuses=("open",))
        if row.get("source") == "chat" and row.get("detail") == message
    ]
    assert len(tasks) == 1
    assert tasks[0]["runnable"] is True
