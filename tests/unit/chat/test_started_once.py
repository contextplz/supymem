"""A started message appears once per job, before its result, and a consumed card replays its receipt."""
import json

import pytest
from fastapi import HTTPException

from tests.unit.chat.test_chat_request_status import chat_request  # noqa: F401
from tests.unit.chat.test_confirmation_replay import _confirm_saved_card, _save_confirmation_cards
from tests.unit.chat.test_next_moves import DOMAINS
from tests.unit.chat.test_site_actions import env  # noqa: F401 (fixture)


def test_started_receipts_precede_results_and_replay_once_for_every_job_kind(env, monkeypatch):
    import asyncio
    from datetime import datetime, timedelta, timezone
    from app.chat import execute, store
    from app.chat.store import append_message, idempotent_message, list_messages
    from app.db.models import Job, Venture
    from app.state import db

    _, venture_id, workspace_id = env
    accepted = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    now = accepted
    monkeypatch.setattr(store, "utcnow", lambda: now)
    monkeypatch.setattr("app.analytics.chat_usage.record_chat_action", lambda *args: None)
    for domain_index, domain in enumerate(("Inventory planning for factories", "Weekend walks for families")):
        with db()() as session:
            venture = session.get(Venture, venture_id)
            venture.name, venture.raw_idea = domain, domain
            session.commit()
        for kind in ("gtm.journey.prepare", "research", "pagebuilder.build", "browse.web", "gtm.god.run", "build.run", "task.run", "plan.compile", "deploy.run", "idea.clarify_turn", "idea.validate"):
            job_id = f"job_{domain_index}_{kind}"
            with db()() as session:
                session.add(Job(id=job_id, venture_id=venture_id, workspace_id=workspace_id,
                    kind=kind, status="running", payload_json=json.dumps({"task_id": "planned_" + job_id}) if kind == "task.run" else "{}", created_at=accepted))
                session.commit()
            now = accepted + timedelta(minutes=5)
            result = append_message(venture_id, role="assistant", kind="text",
                body_md=f"Your work for {domain} is ready. Review your saved result.")
            now = accepted + timedelta(minutes=10)
            async def dispatch(*args, **kwargs):
                return {"kind": "text", "action_started": True, "run_id": job_id,
                    "body_md": f"Your work for {domain} is starting. You can keep chatting."}
            monkeypatch.setattr(execute, "_execute_action", dispatch)
            reply = asyncio.run(execute.execute_action(venture_id, "research.start"))
            citations = reply["citations"]
            if kind == "gtm.journey.prepare":
                citations = [{"gtm_search_progress": {"job_id": job_id, "run_id": job_id, "state": "running"}},
                             {"job_started": {"job_id": job_id}}]
            elif kind == "browse.web":
                citations = [{"browse_progress": {"job_id": job_id, "state": "queued"}}]
            elif kind == "pagebuilder.build":
                citations = [{"confirmed_action": {"message_id": result["id"], "action": "pagebuilder.build", "run_id": job_id, "state": "started"}}]
            elif kind == "gtm.god.run":
                citations = [{"job_started": {"job_id": job_id}}, {"confirmed_action": {"run_id": "diagnosis_" + job_id, "state": "started"}}]
            message_kind = {"gtm.journey.prepare": "gtm_search_progress", "browse.web": "browse_progress"}.get(kind, "text")
            started = append_message(venture_id, role="assistant", kind=message_kind,
                body_md=f"Your work for {domain} is starting. You can keep chatting.", citations=[*citations, {"client_request_id": f"first-{job_id}"}],
                idempotency_key=f"first-{job_id}")
            receipt = idempotent_message(venture_id, "assistant", f"first-{job_id}")
            assert receipt and receipt["id"] == started["id"]
            replay = append_message(venture_id, role="assistant", kind=started["kind"],
                body_md=f"Your work for {domain} is running. You can keep chatting.", citations=[*citations, {"client_request_id": f"retry-{job_id}"}],
                idempotency_key=f"retry-{job_id}")
            assert replay["id"] == started["id"]
            messages = list_messages(venture_id, limit=100)
            ids = [message["id"] for message in messages]
            assert ids.index(started["id"]) < ids.index(result["id"])
            assert started["created_at"] == accepted.isoformat().replace("+00:00", "Z")


def test_a_request_note_is_not_taken_for_its_receipt(env):
    from app.chat.store import append_message, idempotent_message

    _, venture_id, _ = env
    for domain in ("Inventory planning for factories", "Weekend walks for families"):
        append_message(venture_id, role="assistant", body_md=f"A note about {domain}.", citations=[{"client_request_id": domain}])
        assert idempotent_message(venture_id, "assistant", domain) is None


def test_job_retry_and_browse_updates_still_post_their_own_rows(env, monkeypatch):
    import asyncio
    from datetime import datetime, timezone
    from app.chat import execute
    from app.chat.store import append_message
    from app.db.models import Job
    from app.state import db

    _, venture_id, workspace_id = env
    monkeypatch.setattr("app.analytics.chat_usage.record_chat_action", lambda *args: None)
    for domain in ("Inventory planning for factories", "Weekend walks for families"):
        job_id = "job_" + str(abs(hash(domain)))
        with db()() as session:
            session.add(Job(id=job_id, venture_id=venture_id, workspace_id=workspace_id, kind="browse.web", status="running", payload_json="{}", created_at=datetime.now(timezone.utc)))
            session.commit()

        def browse(state, percent):
            return append_message(venture_id, role="assistant", kind="browse_progress", body_md=f"{domain}: {percent}%",
                citations=[{"browse_progress": {"job_id": job_id, "state": state, "percent": percent}}], idempotency_key=f"browse:{job_id}:{percent}")

        assert browse("queued", 0)["id"] != browse("running", 50)["id"]

        async def dispatch(*args, **kwargs):
            return {"kind": "text", "action_started": True, "run_id": job_id, "citations": [], "body_md": f"{domain} was restarted."}
        monkeypatch.setattr(execute, "_execute_action", dispatch)
        assert asyncio.run(execute.execute_action(venture_id, "job.retry"))["citations"] == []
        assert asyncio.run(execute.execute_action(venture_id, "research.start"))["citations"] == [{"job_started": {"job_id": job_id}}]


def test_consumed_card_replays_receipt_after_its_saved_inputs_change(chat_request, monkeypatch):
    from sqlalchemy import select
    from app.db.models import ChatMessage
    from app.db.supymem_models import FounderRequest

    chat = chat_request
    calls = []

    async def execute(venture_id, action, **kwargs):
        calls.append(kwargs["payload"])
        return {"body_md": "Your work started. Review its saved progress.", "run_id": f"job_{len(calls)}"}

    monkeypatch.setattr(chat["api"], "execute_action", execute)
    for domain in DOMAINS:
        for outcome in ("completed", "unknown"):
            state = {"audience": domain}
            card = _save_confirmation_cards(chat, domain, "gtm.find", [state])
            first = _confirm_saved_card(chat, card, "gtm.find", state)
            with chat["factory"]() as session:
                row = session.get(ChatMessage, card["id"])
                actions = json.loads(row.actions_json)
                actions[0]["confirmation"]["resulting_state"] = {"audience": domain, "ready": True}
                row.actions_json = json.dumps(actions)
                if outcome == "unknown":
                    request = session.scalar(select(FounderRequest).where(FounderRequest.text.contains(first["id"], autoescape=True)))
                    envelope = json.loads(request.text)
                    envelope.pop("response")
                    request.text, request.outcome = json.dumps(envelope), "unknown"
                session.commit()
            replay = _confirm_saved_card(chat, card, "gtm.find", state, idempotency_key="second-click")
            assert replay == first
            with pytest.raises(HTTPException) as changed:
                _confirm_saved_card(chat, card, "gtm.find", {"audience": "someone else"}, idempotency_key="third-click")
            assert changed.value.status_code == 409
    assert len(calls) == len(DOMAINS) * 2
