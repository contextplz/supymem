"""Round-three chat commands stay tied to saved venture work."""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.company_os_harness import company_os_client as TestClient, configure_local_test_model, load_company_os_app


@pytest.fixture()
def venture(tmp_path, monkeypatch, request):
    if request.node.get_closest_marker("hosted_only"):
        monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/chat.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "memory.sqlite3"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    main = load_company_os_app()
    configure_local_test_model()
    with TestClient(main.create_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        from app.core.local_mode import is_local
        if is_local():
            from core.local_security import OWNER_COOKIE, _proxy_secret
            client.cookies.set(OWNER_COOKIE, _proxy_secret().decode("ascii"))
        created = client.post("/ventures", json={"raw_idea": getattr(request, "param", "A shared calendar for neighborhood volunteers")})
        assert created.status_code == 201, created.text
        yield client, created.json()["id"], created.json()["workspace_id"]


@pytest.mark.parametrize("venture", ["Inventory alerts for warehouse operators", "Walking groups for neighborhood families"], indirect=True)
def test_five_matrix_messages_route_without_generic_task(venture):
    client, venture_id, _workspace_id = venture
    from app.chat.intent import classify_intent
    from app.chat.journey import asks_for_guidance
    from app.db.models import Job, PlannedTask
    from app.org.characters import parse_speaker
    from app.state import db

    expected = [
        ("What is happening, and what should I do next?", "assistant", "text", [("blocker.explain", "ghost")]),
        ("Build my app.", "start_product_build", "navigation", [("idea.review", "primary")]),
        ("The job is stuck. Check its saved status.", "assistant", "text", [("blocker.explain", "ghost")]),
        ("Start getting users.", "gtm_review", "text", [("chat.reply", "secondary")]),
        ("Start getting users. First tell me the exact next step and what needs "
         "my approval. Do not send mail, publish, or contact anyone.",
         "gtm_review", "text", [("chat.reply", "secondary")]),
        ("I already have a product. What should I do next to get users? "
         "Do not start paid work or send outreach.",
         "gtm_review", "text", [("chat.reply", "secondary")]),
        ("Show my latest error.", "latest_error", "navigation", [("status", "primary")]),
    ]
    assert asks_for_guidance(expected[2][0])
    from app.core.local_mode import is_local
    if is_local():
        expected[1] = ("Build my app.", "build.tool.start", "text", [(None, "primary")])
        expected[3] = ("Start getting users.", "gtm_review", "gtm_diagnosis_pending", [])
        expected[4] = (*expected[4][:3], [])
        expected[5] = (*expected[5][:3], [])
    with db()() as session:
        previous_jobs = {row.id for row in session.query(Job).filter_by(venture_id=venture_id)}
    pending_ids = []
    for message, intent, kind, actions in expected:
        assert classify_intent(message) == intent
        response = client.post(f"/ventures/{venture_id}/chat", json={"message": message})
        assert response.status_code == 200, response.text
        reply = response.json()
        assert reply["kind"] == kind, (message, parse_speaker(reply["body_md"])[1])
        assert [(action.get("action"), action.get("style")) for action in reply["actions"]] == actions, (message, parse_speaker(reply["body_md"])[1])
        if intent == "gtm_review":
            if is_local() and kind == "gtm_diagnosis_pending":
                assert parse_speaker(reply["body_md"])[1] == "Your diagnosis is queued. I will show its saved result here when it is ready."
                pending_ids.append(reply["id"])
            elif is_local() and "First tell me" in message:
                assert "show your saved audience" in reply["body_md"] and "No search started" in reply["body_md"]
            elif is_local() and "Do not start paid work" in message:
                assert "Nothing is sent." in reply["body_md"]
            else:
                assert "Who are you trying to reach" in reply["body_md"]
            assert not any(a.get("href") for a in reply.get("actions") or [])
        if intent == "build.tool.start" and is_local():
            assert "Coding tools" in reply["body_md"]
            assert reply["actions"][0]["href"] == "/settings/local#coding-tools"
        assert reply["kind"] != "next_up"
        assert "Queued **" not in reply["body_md"]
    with db()() as session:
        assert not session.query(PlannedTask).filter_by(venture_id=venture_id).count()
        if is_local():
            assert len(set(pending_ids)) == 1
            assert [row.kind for row in session.query(Job).filter_by(venture_id=venture_id)
                    if row.id not in previous_jobs] == ["gtm.god.run"]


def test_latest_error_is_scoped_and_uses_saved_job_receipt(venture):
    client, venture_id, workspace_id = venture
    from app.core.local_mode import is_local
    from app.db.models import Job
    from app.state import db
    from core.clock import utcnow

    with db()() as session:
        session.add(Job(id="job_foreign", kind="research.deep", venture_id="ven_foreign",
                        workspace_id=workspace_id, payload_json="{}", status="failed",
                        last_error="private foreign error", created_at=utcnow()))
        session.commit()
    empty = client.post(f"/ventures/{venture_id}/chat", json={"message": "Show my latest error."}).json()
    assert "No error is recorded" in empty["body_md"]
    assert "private foreign error" not in empty["body_md"]

    with db()() as session:
        session.add(Job(id="job_ours", kind="research.deep", venture_id=venture_id,
                        workspace_id=workspace_id, payload_json="{}", status="failed",
                        last_error="research provider timed out; token=sk_live_" + "a" * 32,
                        created_at=utcnow()))
        session.commit()
    found = client.post(f"/ventures/{venture_id}/chat", json={"message": "Show my latest error."}).json()
    assert "research service took too long" in found["body_md"]
    assert "sk_live_" not in found["body_md"]
    assert "token=" not in found["body_md"] and "[redacted]" not in found["body_md"].lower()
    assert "job_ours" not in found["body_md"] and "job_foreign" not in found["body_md"]
    if is_local():
        assert found["actions"][:1] == [{"label": "Review result", "href": f"/v/{venture_id}/research", "style": "primary"}]
    else:
        assert found["actions"][:1] == [{"label": "Show research here", "action": "research", "style": "primary"}]

    from app.db.supymem_models import BuildRun, FounderApp
    with db()() as session:
        session.add(FounderApp(id="app_ours", workspace_id=workspace_id, venture_id=venture_id,
                               name="Calendar", slug="calendar-ours", kind="web",
                               created_at=utcnow(), updated_at=utcnow()))
        session.flush()
        session.add(BuildRun(id="bld_ours", app_id="app_ours", workspace_id=workspace_id,
                             venture_id=venture_id, trigger="test", agent="managed", state="failed",
                             spec="{}", error_code="qa_failed", exit_summary="QA failed after build",
                             runtime_version=1, created_at=utcnow() + timedelta(minutes=1)))
        session.commit()
    newer = client.post(f"/ventures/{venture_id}/chat", json={"message": "Show my latest error."}).json()
    assert "build did not pass its final checks" in newer["body_md"]
    assert "research service took too long" not in newer["body_md"]
    if is_local():
        assert newer["actions"][0] == {"label": "Review build", "href": f"/v/{venture_id}/builds", "style": "primary"}
    else:
        assert newer["actions"][0]["action"] == "status"


def test_latest_error_recovery_uses_cancelled_review_and_scoped_status_link(venture, monkeypatch):
    client, venture_id, _workspace_id = venture
    from app.chat.intent import classify_intent, split_speech_acts
    from app.db.models import PlannedTask
    from app.state import db
    from app.ventures import run_status, state_brief

    question = ("Show my latest error and tell me the next recovery action. "
                "Answer only; do not start or prepare any task.")
    assert classify_intent(question) == "latest_error"
    assert split_speech_acts(question) == [("latest_error", question)]
    assert classify_intent(question.replace("do not start", "start")) != "latest_error"

    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "builds": {"last": None},
        "device_work": {"recent": [{"id": "dwo_cancelled", "status": "cancelled",
                                    "approved": False, "executed": False}]},
    })
    monkeypatch.setattr(run_status, "snapshot", lambda *_args, **_kwargs: {
        "state": "needs_you", "action": {"label": "Review local build",
                                         "href": f"/v/{venture_id}/idea#journey-action"},
    })

    response = client.post(f"/ventures/{venture_id}/chat", json={"message": question})
    assert response.status_code == 200, response.text
    reply = response.json()
    assert "No error is recorded" in reply["body_md"]
    assert "cancelled before approval" in reply["body_md"]
    assert "did not run" in reply["body_md"]
    assert "not a product failure" in reply["body_md"]
    assert "No task was started" in reply["body_md"]
    assert reply["actions"] == []
    with db()() as session:
        assert session.query(PlannedTask).filter_by(venture_id=venture_id).count() == 0


def test_latest_error_recovery_rejects_foreign_review_link(venture, monkeypatch):
    client, venture_id, _workspace_id = venture
    from app.ventures import run_status, state_brief

    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "builds": {"last": None}, "device_work": {"recent": []},
    })
    monkeypatch.setattr(run_status, "snapshot", lambda *_args, **_kwargs: {
        "state": "needs_you", "action": {"label": "Review local build",
                                         "href": "/v/ven_foreign/idea#journey-action"},
    })
    reply = client.post(f"/ventures/{venture_id}/chat", json={
        "message": "Show my latest error and tell me the next recovery action.",
    }).json()
    assert reply["actions"] == [{"label": "Show status here", "action": "status", "style": "primary"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("audience", ["Freight depot coordinators", "Weekend sketching groups"])
async def test_customer_request_opens_journey_not_saved_waitlist(monkeypatch, audience):
    load_company_os_app()
    from app.chat.context import ChatContext
    from app.chat.router import _dispatch_intent
    from app.ventures import state_brief

    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "idea": {"project_intent": "personal"},
        "gtm": {"present": True},
        "website": {"waitlist_count": 2},
    })
    reply = await _dispatch_intent("ven_personal", ChatContext(venture={"id": "ven_personal", "raw_idea": audience}),
                                   "gtm_review", "Start getting users.", None)
    assert "book a first call" in reply["body_md"]
    assert "waitlist" not in reply["body_md"]
    assert reply["actions"] == [{"label": "Find customers", "action": "gtm.status", "style": "primary"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("audience", ["Fleet parts purchasers", "Home quilting groups"])
async def test_saved_customer_work_does_not_require_new_search_setup(monkeypatch, audience):
    load_company_os_app()
    from app.chat import customers

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    view = {"icp": {"who": audience}, "drafts": [{"id": "saved-draft"}], "can_prepare": False}
    monkeypatch.setattr(customers, "_view", lambda _: view)
    monkeypatch.setattr(customers, "saved_search_request", lambda *args, **kwargs: "")
    monkeypatch.setattr(customers, "_search_setup_reply", lambda: {
        "kind": "text", "body_md": "In Settings > Capabilities, turn on research.", "actions": []})

    async def shown(venture_id, saved):
        assert saved["drafts"] == view["drafts"]
        return {"kind": "text", "body_md": f"Your saved drafts for {audience}.", "actions": []}

    monkeypatch.setattr(customers, "_shown", shown)
    reply = await customers.find_reply("venture", "whom should I send them to")
    assert reply["body_md"].endswith(f"Your saved drafts for {audience}.")
    assert "recipients for your outreach drafts" in reply["body_md"]


@pytest.mark.asyncio
async def test_blocker_question_with_no_approval_constraint_only_guides(monkeypatch):
    load_company_os_app()
    from app.chat.context import ChatContext
    from app.chat.intent import split_speech_acts
    from app.chat.router import _dispatch_intent
    from app.ventures import run_status, state_brief

    question = (
        "Why can’t I proceed? Tell me the single next action. "
        "Do not approve this plan or start a build."
    )
    assert split_speech_acts(question) == [("assistant", question)]
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "idea": {"name": "ShiftLens"}, "plan": {"present": True, "state": "review"},
    })
    monkeypatch.setattr(run_status, "snapshot", lambda *_args, **_kwargs: {
        "state": "needs_you", "activity": "The plan is ready for review.",
        "next_step": "Review the saved plan before approving work.",
        "action": {"label": "Review execution plan", "href": "/v/ven_shift/plan#plan-review"},
    })
    reply = await _dispatch_intent(
        "ven_shift", ChatContext(venture={"id": "ven_shift", "workspace_id": "wsp_shift"}),
        "assistant", question, None,
    )
    assert reply["kind"] == "text"
    assert reply["actions"] == [{"label": "Review execution plan",
                                  "href": "/v/ven_shift/plan#plan-review", "style": "primary"}]
    assert "review" in reply["body_md"].lower()
