import json
from datetime import timedelta

import pytest

from tests.company_os_harness import company_os_test_client, load_company_os_app


@pytest.fixture(params=["Invoice reconciliation for freight dispatchers", "Neighborhood garden tool sharing"])
def venture(tmp_path, monkeypatch, request):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/chat.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SUPYMEM_LOCAL_PROXY_SECRET_FILE", str(tmp_path / "owner-session.secret"))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "memory.sqlite3"))
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    main = load_company_os_app()
    with company_os_test_client(main.create_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        from app.core.local_mode import is_local
        if is_local():
            selected = client.post("/local/providers/sample", json={})
            assert selected.status_code == 200, selected.text
        created = client.post("/ventures", json={"raw_idea": request.param})
        assert created.status_code == 201, created.text
        wakes = []
        monkeypatch.setattr("app.chat.failed_jobs.publish_wake", lambda: wakes.append(True))
        yield client, created.json()["id"], created.json()["workspace_id"], wakes


def save_job(venture, *, kind="experiment.exploration.prepare", error="exploration_direction_changed",
             payload=None, status="failed_dead", age=0, workspace_id=None, venture_id=None):
    from app.db.models import Job
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    _, saved_venture_id, saved_workspace_id, _ = venture
    job_id = new_id("job")
    with db()() as session:
        session.add(Job(id=job_id, kind=kind, venture_id=venture_id or saved_venture_id,
                        workspace_id=workspace_id or saved_workspace_id, payload_json=json.dumps(payload or {}),
                        status=status, last_error=error, attempts=3, max_attempts=3,
                        finished_at=utcnow(), created_at=utcnow() + timedelta(seconds=age)))
        session.commit()
    return job_id


def retry(venture, job_id):
    client, venture_id, _, _ = venture
    response = client.post(f"/ventures/{venture_id}/chat", json={
        "message": "Help me safely retry my failed task. Review its saved result before starting anything.",
        "job_id": job_id})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("kind", ["pagebuilder.build", "pagebuilder.publish"])
def test_local_private_page_retry_passes_saved_attempt_but_publication_stays_fenced(venture, monkeypatch, kind):
    from app.chat import execute
    from app.core import local_capabilities
    from app.core.local_mode import is_local
    from app.db.models import Job
    from app.state import db

    if not is_local():
        pytest.skip("Private-page retries use the local desktop owner boundary")
    enabled = local_capabilities.enabled
    monkeypatch.setattr(local_capabilities, "enabled", lambda name: name == "landing_pages" or enabled(name))

    payload = {"_runtime": {"operations": {"paid": {"state": "uncertain"}}}}
    if kind == "pagebuilder.build":
        payload["variant_count"] = 1
        payload["_runtime"].update(state="failed", stage="failed", progress=0.5, calls_used=1,
                                   quote={"ceiling_micro": 10000, "payer": "founder"})
    job_id = save_job(venture, kind=kind, payload=payload)
    calls = []

    def build(venture_id, request, actor):
        calls.append((venture_id, request, actor))
        return {"kind": "text", "body_md": "Building your private preview. Open Website to review it.",
            "actions": [], "citations": [], "run_id": "fresh-preview"}

    monkeypatch.setattr(execute, "_build_landing_page", build)
    reply = retry(venture, job_id)
    if kind == "pagebuilder.build":
        assert len(calls) == 1 and calls[0][:2] == (venture[1], {"retry_job_id": job_id}), reply
        assert calls[0][2] and "Building your private preview" in reply["body_md"]
    else:
        assert calls == [] and "did not restart" in reply["body_md"]
    with db()() as session:
        saved = session.get(Job, job_id)
        assert saved.status == "failed_dead" and json.loads(saved.payload_json) == payload
    assert venture[3] == []


def test_clarification_handler_failure_has_one_plain_recovery(venture):
    from app.chat.failed_jobs import failure_reply

    reply = failure_reply(venture[1], {"kind": "idea.clarify_turn", "code": "handler_failed:AttributeError"})
    copy = reply["body_md"]
    assert copy.count("saved work") == 1
    assert copy.count("did not finish") + copy.count("could not finish") == 1
    assert "nothing went public" in copy and "Open" in copy
    assert not any(word in copy for word in ("Clarification", "handler_failed", "AttributeError", "credits", "Your task", "\u2014"))
    assert "\n" not in copy
    assert copy.count(".") == 2
    assert any(action.get("href") == f"/v/{venture[1]}/idea" for action in reply["actions"])
    assert not any(action.get("action") == "job.retry" for action in reply["actions"])


@pytest.mark.parametrize("kind", ["experiment.exploration.prepare", "estimate.compute"])
def test_exact_retry_requeues_saved_idempotent_work_once(venture, kind, monkeypatch):
    from app.db.models import Job
    from app.state import db

    payload = {"saved_input": "Keep this input", "workspace_id": venture[2], "venture_id": venture[1]}
    job_id = save_job(venture, kind=kind, payload=payload)
    with monkeypatch.context() as patch:
        async def unavailable(*args, **kwargs):
            raise RuntimeError("saved status temporarily unavailable")
        patch.setattr("app.api.chat_api.handle_message", unavailable)
        error = retry(venture, job_id)
        assert any(action.get("payload", {}).get("job_id") == job_id for action in error["actions"])
    first = retry(venture, job_id)
    assert "restarted" in first["body_md"]
    assert "already queued or running" in retry(venture, job_id)["body_md"]
    with db()() as session:
        saved = session.get(Job, job_id)
        assert saved.status == "scheduled" and saved.attempts == 0
        assert json.loads(saved.payload_json) == payload
        assert saved.finished_at is None and saved.locked_by is None
    assert venture[3] == [True]


@pytest.mark.parametrize("kind,error,payload", [
    ("experiment.exploration.prepare", "handler_failed", {"_runtime": {"operations": {"paid": {"state": "calling"}}}}),
    ("experiment.exploration.prepare", "handler_failed", {"_runtime": {"operations": {"paid": {"state": "uncertain"}}}}),
    ("experiment.exploration.prepare", "handler_failed", {"_runtime": {"operations": []}}),
    ("research.deep", "research_outcome_uncertain", {}),
    ("email.send", "handler_failed", {}),
])
def test_unsafe_retry_keeps_failure_and_offers_review_then_a_new_round(venture, kind, error, payload):
    from app.db.models import Job
    from app.state import db

    job_id = save_job(venture, kind=kind, error=error, payload=payload)
    reply = retry(venture, job_id)
    assert "did not restart" in reply["body_md"] and "saved result first" in reply["body_md"]
    assert any(action["label"] == "Start a new round" for action in reply["actions"])
    assert any(action.get("action") == "jobs.status" for action in reply["actions"])
    assert "no failed" not in reply["body_md"] and error not in reply["body_md"]
    with db()() as session:
        assert session.get(Job, job_id).status == "failed_dead"
    assert venture[3] == []


@pytest.mark.parametrize("paid_state", ["active", "reconciling", "calling", "completed", "foreign-venture", "foreign-workspace"])
def test_exploration_retry_checks_scoped_billing_receipts_even_without_a_job_journal(venture, paid_state):
    from app.db.models import CreditReservation, EventLog
    from app.state import db
    from core.clock import utcnow

    job_id = save_job(venture)
    with db()() as session:
        if paid_state in {"active", "reconciling"}:
            session.add(CreditReservation(id="pending-model", account_id="account", amount_micro=100,
                workspace_id=venture[2], venture_id=venture[1], purpose="experiment.beliefs", status=paid_state,
                created_at=utcnow(), expires_at=utcnow() + timedelta(hours=1)))
        else:
            scope = {"venture_id": "another-venture" if paid_state == "foreign-venture" else venture[1],
                     "workspace_id": "another-workspace" if paid_state == "foreign-workspace" else venture[2]}
            session.add(EventLog(id="model:begin", topic="credits.model.receipt.begin", target_ref="model",
                payload_json="{}", actor="credits.model", ts=utcnow(), **scope))
            if paid_state == "completed":
                session.add(EventLog(id="model:completed", topic="credits.model.receipt.completed", target_ref="model",
                    payload_json="{}", actor="credits.model", ts=utcnow(), **scope))
        session.commit()
    reply = retry(venture, job_id)
    if paid_state in {"active", "reconciling", "calling"}:
        assert "did not restart" in reply["body_md"]
        assert any(action["label"] == "Start a new round" for action in reply["actions"])
        assert venture[3] == []
    else:
        assert "restarted" in reply["body_md"]
        assert venture[3] == [True]


@pytest.mark.parametrize("paid_state", ["pending", "unknown", "confirmed", "unspent", "foreign-venture", "foreign-workspace", "unavailable"])
def test_local_retry_checks_saved_spending_outcomes_without_a_job_journal(venture, monkeypatch, paid_state):
    from app.core.jobs import retry_is_safe
    from app.credits import metered
    from app.db.models import Job
    from app.state import db

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    job_id = save_job(venture)
    workspace_id = "another-workspace" if paid_state == "foreign-workspace" else venture[2]
    venture_id = "another-venture" if paid_state == "foreign-venture" else venture[1]

    def safe():
        with db()() as session:
            return retry_is_safe(session, session.get(Job, job_id))

    with metered.charge_sync(workspace_id=workspace_id, venture_id=venture_id,
            purpose="experiment.exploration.prepare", request_id=job_id + ":paid", estimate_micro=10) as meter:
        meter.protect_outcome()
        if paid_state == "pending":
            assert not safe()
        if paid_state == "confirmed":
            meter.record_micro(1)
        elif paid_state == "unspent":
            meter.confirm_unspent()
    if paid_state == "unavailable":
        from app.credits import local_meter
        from app.credits.errors import CreditsUnavailable

        def unavailable():
            raise CreditsUnavailable()

        monkeypatch.setattr(local_meter, "_database", unavailable)
    assert safe() is (paid_state in {"confirmed", "unspent", "foreign-venture", "foreign-workspace"})


@pytest.mark.parametrize("foreign_scope", ["venture", "workspace", "missing"])
def test_retry_refuses_work_outside_the_exact_scope_without_exposing_it(venture, foreign_scope):
    from app.db.models import Job
    from app.state import db

    client, venture_id, _, _ = venture
    foreign = client.post("/ventures", json={"raw_idea": "An unrelated private project"}).json()
    job_id = save_job(venture, venture_id=foreign["id"] if foreign_scope == "venture" else venture_id,
                      error="private diagnostic not for this venture")
    if foreign_scope == "workspace":
        with db()() as session:
            session.get(Job, job_id).workspace_id = "another-workspace"
            session.commit()
    reply = retry(venture, "not-a-saved-task" if foreign_scope == "missing" else job_id)
    assert "only retry work saved in this venture" in reply["body_md"]
    assert "private diagnostic" not in reply["body_md"]
    with db()() as session:
        assert session.get(Job, job_id).status == "failed_dead"
    assert venture[3] == []


def test_chat_lists_real_failed_and_running_work_without_internal_details(venture):
    client, venture_id, _, _ = venture
    failed_id = save_job(venture)
    save_job(venture, kind="plan.compile", status="running", error=None)
    save_job(venture, venture_id="unrelated-venture", error="private other venture diagnostic")
    failed = client.post(f"/ventures/{venture_id}/chat", json={"message": "what failed?"})
    assert failed.status_code == 200, failed.text
    reply = failed.json()
    assert "Exploring your idea" in reply["body_md"]
    assert "Your idea changed while this round was being prepared" in reply["body_md"]
    assert "exploration_direction_changed" not in reply["body_md"]
    assert "private other venture" not in reply["body_md"]
    assert any(action.get("payload", {}).get("job_id") == failed_id for action in reply["actions"])
    running = client.post(f"/ventures/{venture_id}/chat", json={"message": "what is running?"})
    assert running.status_code == 200, running.text
    assert "Planning: Running" in running.json()["body_md"]
    assert venture[3] == []


def test_retry_without_an_id_finds_failures_outside_the_old_journey_kinds(venture):
    from app.db.models import Job
    from app.state import db

    client, venture_id, _, _ = venture
    job_id = save_job(venture, age=10)
    reply = client.post(f"/ventures/{venture_id}/chat", json={"message": "try again"})
    assert reply.status_code == 200, reply.text
    assert "restarted" in reply.json()["body_md"]
    with db()() as session:
        assert session.get(Job, job_id).status == "scheduled"


def test_successful_replacement_resolves_only_its_scoped_kind_and_leaves_other_failures_visible(venture):
    from app.chat.failed_jobs import latest_failed
    from app.db.models import Job
    from app.state import db

    client, venture_id, workspace_id, _ = venture
    old_id = save_job(venture)
    other_id = save_job(venture, kind="estimate.compute", age=-10)
    save_job(venture, status="succeeded", age=10, error=None)
    assert latest_failed(venture_id, workspace_id)["job_id"] == other_id
    assert "already finished successfully" in retry(venture, old_id)["body_md"]
    active = client.get(f"/workspaces/{workspace_id}/jobs", params={"venture_id": venture_id, "active_only": "true"})
    assert active.status_code == 200, active.text
    assert old_id in active.json()["resolved_ids"] and other_id not in active.json()["resolved_ids"]
    failed = client.post(f"/ventures/{venture_id}/chat", json={"message": "what failed?"}).json()
    assert "Exploring your idea" not in failed["body_md"] and "Resource estimate" in failed["body_md"]
    with db()() as session:
        assert session.get(Job, old_id).status == "failed_dead"
