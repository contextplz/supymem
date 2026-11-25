"""The Draft outreach emails card posts the drafts in the reply and withdraws stale posting gates."""

from __future__ import annotations

import asyncio

import pytest

from tests.company_os_harness import load_company_os_app
from tests.unit.chat.test_round3_matrix import venture  # noqa: F401

load_company_os_app()


@pytest.mark.parametrize("venture", [
    "A maintenance scheduling tool for factory operators",
    "A neighborhood walking group app for adults",
], indirect=True)
def test_requested_interview_email_falls_back_to_a_saved_compliant_draft(venture, monkeypatch):
    from app.chat import assistant, offers
    from app.core.local_mode import is_local
    from app.db.models import PlannedTask, VentureDocument
    from app.gtm.relationships.service import _settings
    from app.state import db

    _, venture_id, workspace_id = venture
    address = "24 Market Street, Boston, MA 02110"
    if is_local():
        from app.core.local_secrets import update_settings
        update_settings("smtp", {"postal_address": address}, {})
    with db()() as session:
        _settings(session, workspace_id).sender_postal_address = address
        session.commit()

    async def empty(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(assistant, "_complete", empty)
    monkeypatch.setattr(offers, "_save_requested_email", empty)
    request = "Write one interview email to Morgan at morgan@example.test about my saved idea, about 120 words. Draft only. Do not send anything."
    from app.chat.intent import single_interview_email_request
    assert single_interview_email_request(request)
    reply = asyncio.run(offers.draft_run(venture_id, request=request))
    assert reply["body_md"].count("Subject:") == 1, reply
    assert "Hi Morgan," in reply["body_md"], reply
    assert address in reply["body_md"] and 'reply "no thanks"' in reply["body_md"], reply
    assert not any(action.get("confirmation") for action in reply["actions"])
    with db()() as session:
        task = session.query(PlannedTask).filter_by(venture_id=venture_id).one()
        assert task.status == "done", task.last_error
        saved = session.get(VentureDocument, "task-note:" + task.id)
        assert saved and "Subject:" in saved.body_md


@pytest.fixture(autouse=True)
def local_draft_model(venture, monkeypatch):
    from app.core.local_mode import is_local
    from tests.idea_fixtures import install_provider

    if is_local():
        with monkeypatch.context() as provider_patch:
            install_provider(provider_patch)


@pytest.mark.parametrize("venture", [
    "A procurement reconciliation tool for warehouse operators",
    "A meal planning app for households",
], indirect=True)
def test_repeated_request_retries_only_a_verified_draft_failure(venture, monkeypatch):
    from app.chat import offers
    from app.db.models import PlannedTask
    from app.planning import execute
    from app.planning.store import enqueue_task, get_task
    from app.state import db

    _, venture_id, workspace_id = venture
    from app.gtm.relationships.service import _settings
    with db()() as session:
        _settings(session, workspace_id).sender_postal_address = "24 Market Street, Boston, MA 02110"
        session.commit()
    request = "Write one outreach email asking for a short conversation about the problem."
    calls = []
    async def compose(*args):
        calls.append(args)
        return "" if len(calls) == 1 else "Draft 1\nSubject: A conversation\n\nCould we talk about this problem?"
    monkeypatch.setattr(execute, "_review_drafts", compose)
    assert "could not be prepared" in asyncio.run(offers.draft_run(venture_id, request=request))["body_md"]
    with db()() as session:
        failed = session.query(PlannedTask).filter_by(venture_id=venture_id).one()
        task_id, fingerprint = failed.id, failed.fingerprint
        assert failed.last_error == "incomplete:review_draft_request_unverified"
    held = enqueue_task(venture_id, {"title": "Draft one outreach email for me to review", "fingerprint": fingerprint}, source="autopilot")
    assert held["id"] == task_id and held["runnable"] is False
    reply = asyncio.run(offers.draft_run(venture_id, request=request))
    assert "Subject: A conversation" in reply["body_md"], reply
    assert "save your sending address" not in reply["body_md"].lower()
    assert len(calls) == 2
    assert get_task(venture_id, task_id)["status"] == "done"
    spec = {"title": "Draft one outreach email for me to review", "fingerprint": fingerprint}
    for status, error in (("running", None), ("open", "needs_you:review_draft_provider_outcome_unknown")):
        with db()() as session:
            failed = session.get(PlannedTask, task_id)
            failed.status, failed.last_error, failed.runnable = status, error, 0
            session.commit()
        reused = enqueue_task(venture_id, spec)
        assert reused["id"] == task_id and reused["runnable"] is False
        assert reused["status"] == status and reused["last_error"] == error
        reply = asyncio.run(offers.draft_run(venture_id, request=request))
        assert ("still being prepared" in reply["body_md"] if status == "running" else reply["kind"] == "needs_you")
        assert len(calls) == 2


@pytest.mark.parametrize("venture", [
    "An enterprise payment reconciliation platform for finance operations teams",
    "A personal medication reminder app for adults managing prescriptions",
], indirect=True)
def test_saved_review_emails_are_retrieved_without_becoming_sendable(venture, monkeypatch):
    from app.autopilot import outreach
    from app.chat import assistant, offers
    from app.db.models import PlannedTask, VentureDocument
    from app.gtm.relationships.email_compliance import OPT_OUT
    from app.planning.store import enqueue_task
    from app.state import db

    client, venture_id, _ = venture
    async def offline(*_args, **_kwargs):
        return ""
    monkeypatch.setattr(assistant, "_complete", offline)
    written = asyncio.run(offers.draft_run(venture_id))
    assert written["body_md"].count("Subject:") == 3
    unrelated = client.post("/ventures", json={"raw_idea": "A spacecraft thermal simulation service"}).json()["id"]
    unfinished = enqueue_task(venture_id, {"title": "Draft outreach emails for me to review", "runnable": True})
    with db()() as session:
        completed = session.query(PlannedTask).filter_by(venture_id=venture_id, status="done").one()
        note = session.get(VentureDocument, "task-note:" + completed.id)
        note.body_md = note.body_md.replace(OPT_OUT, "")
        now = session.get(PlannedTask, unfinished["id"]).created_at
        session.add(VentureDocument(id="task-note:" + unfinished["id"], venture_id=venture_id, kind="task_note",
            title=unfinished["title"], body_md="Draft 1\nSubject: Not finished\n\nNot a completed draft.",
            created_at=now, updated_at=now))
        session.commit()
    response = client.post(f"/ventures/{venture_id}/chat", json={"message": "Show my drafts"})
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["body_md"].count("Subject:") == 3, saved
    assert "Not finished" not in saved["body_md"]
    assert "Nothing was sent" in saved["body_md"]
    assert saved["body_md"].count(OPT_OUT) == 3
    assert "before sending" in saved["body_md"]
    assert all(action.get("action") not in {"gtm.send", "gtm.approve"} for action in saved["actions"])
    assert not outreach.drafts(venture_id)
    other = client.post(f"/ventures/{unrelated}/chat", json={"message": "Show my drafts"}).json()
    assert "Subject:" not in other["body_md"]
    outreach._save_draft(venture_id, {"to": "recipient@example.com", "subject": "A saved recipient email",
        "body": "Your recipient-specific draft stays saved.", "state": "manual"})
    mixed = client.post(f"/ventures/{venture_id}/chat", json={"message": "Show my drafts"}).json()
    assert mixed["body_md"].count("Subject:") == 1
    assert "A saved recipient email" in mixed["body_md"]


def test_draft_run_posts_drafts_and_withdraws_stale_posting_gate(monkeypatch, venture):
    _, vid, _ = venture
    from app.chat import offers
    from app.concierge.human import ensure_human_task, list_open_human_tasks
    from app.db.models import PlannedTask
    from app.planning import execute
    from app.planning.store import enqueue_task
    from app.state import db

    old = enqueue_task(vid, {"title": "Draft outreach emails for gym owners", "detail": "", "why_now": "x",
                             "tags": [], "est_minutes": 5, "runnable": True}, source="chat")
    with db()() as s:
        s.get(PlannedTask, old["id"]).last_error = "needs_you:social"
        s.commit()
    ensure_human_task(vid, title="Connect posting", why="x", provider="social", category="credential")

    async def fake_run(*_a, **_k):
        return {"status": "done", "draft_text": "Draft 1\nSubject: A\n\nBody\n\nDraft 2\nSubject: B\n\nBody"}

    monkeypatch.setattr(execute, "execute_planned_task", fake_run)
    reply = asyncio.run(offers.draft_run(vid))
    assert reply["body_md"].count("```text") == 2 and "Subject: B" in reply["body_md"]
    assert not any(t.get("provider") == "social" for t in list_open_human_tasks(vid))

    async def fail_run(*_a, **_k):
        return {}

    monkeypatch.setattr(execute, "execute_planned_task", fail_run)
    assert "Here are the drafts" not in asyncio.run(offers.draft_run(vid))["body_md"]


@pytest.mark.parametrize("inline", [True, False])
@pytest.mark.parametrize("venture", [
    "A supplier invoice reconciliation tool for purchasing teams",
    "A hiking route journal for weekend walkers",
], indirect=True)
def test_real_task_drafts_have_one_chat_result_and_work_link(venture, monkeypatch, inline):
    client, vid, _ = venture
    from app.chat.store import list_messages
    from app.db.models import PlannedTask, VentureDocument
    from app.planning import execute
    from app.planning.store import enqueue_task
    from app.state import db

    drafts = "\n\n".join(f"Draft {number}\nSubject: Trail Notebook {number}\n\nReview-only body {number}" for number in range(1, 4))
    async def compose(*_args, **_kwargs):
        return drafts
    monkeypatch.setattr(execute, "_review_drafts", compose)
    if inline:
        response = client.post(f"/ventures/{vid}/chat/confirm", json={"action": "outreach.draft", "payload": {"run": True}})
        assert response.status_code == 200, response.text
    else:
        task = enqueue_task(vid, {"title": "Draft the outreach emails for me to review",
            "detail": "Write three email drafts. Do not send anything.", "runnable": True}, source="chat")
        result = asyncio.run(execute.execute_planned_task(vid, task["id"]))
        assert result["status"] == "done"
    results = [message for message in list_messages(vid, limit=100) if "Subject: Trail Notebook 1" in message["body_md"]]
    assert len(results) == 1
    assert results[0]["body_md"].count("```text") == 3
    assert sum(action.get("action") == "status" for action in results[0]["actions"]) == 1  # in chat, not the hidden Work page
    recipients = client.post(f"/ventures/{vid}/chat", json={"message": "whom should I send them to"})
    assert recipients.status_code == 200, recipients.text
    assert "recipients for your outreach drafts" in recipients.json()["body_md"]
    assert not any(action.get("href") for action in recipients.json()["actions"])
    with db()() as session:
        task = session.query(PlannedTask).filter_by(venture_id=vid, status="done").one()
        task_id = task.id
        assert drafts in session.get(VentureDocument, f"task-note:{task.id}").body_md
    replay = asyncio.run(execute.execute_planned_task(vid, task_id))
    assert replay["already_done"]
    assert len([message for message in list_messages(vid, limit=100) if "Subject: Trail Notebook 1" in message["body_md"]]) == 1
