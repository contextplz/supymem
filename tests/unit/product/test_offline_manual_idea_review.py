"""A real offline worker receipt permits scoped founder review without model calls."""
import json

import pytest
from sqlalchemy import select

from tests.security.test_connector_oauth_boundary import client as client  # noqa: F401
from tests.security.test_durable_journey_boundary import request, run_queued


@pytest.fixture
def offline_idea(client, monkeypatch):
    """Replay legacy offline failures through real jobs; new offline workers now summarize."""
    from app.idea import runner
    from llm_gateway import sole

    def legacy_offline(run_id, payload):
        runner._receipt(run_id)
        runner._save(run_id, state="failed", error_code="clarification_provider_offline")
        raise runner.FatalError("clarification_provider_offline")

    monkeypatch.setattr(runner, "_perform", legacy_offline)
    monkeypatch.setattr(sole, "transmit", lambda *a, **kw: pytest.fail("Offline setup invoked a provider"))
    created = request(client, "POST", "/ventures", json={"name": "Dinner Tip QA",
        "raw_idea": "A tip splitter that helps friends split the bill at group dinners, with a small health API", "start": False})
    assert created.status_code == 201, created.text
    vid = created.json()["id"]
    opened = request(client, "POST", f"/ventures/{vid}/idea/sessions")
    assert opened.status_code == 201, opened.text
    sid = opened.json()["id"]
    jid = run_queued("idea.clarify_turn")
    from app.db.models import Job
    from app.db.supymem_models import IdeaClarificationRun
    from app.state import db
    with db()() as session:
        job = session.get(Job, jid)
        run = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == jid))
        assert job.status == "failed_dead" and job.last_error == "clarification_provider_offline"
        assert run.state == "failed" and run.response is None and run.usage == "{}"
        run_id = run.id
    return {"vid": vid, "sid": sid, "jid": jid, "run_id": run_id, "base": f"/ventures/{vid}/idea/sessions/{sid}"}


def test_genuine_offline_receipt_manual_review_and_revision_bound_confirmation(client, offline_idea):
    d = offline_idea
    saved = request(client, "GET", d["base"]).json()
    assert saved["manual_review_available"] is True
    assert saved["clarification_retry_available"] is False
    assert saved["clarification_invalid_output_seen"] is False
    rid = saved["current_revision"]["id"]
    body = {"revision_id": rid, "patch": {"problem": "Split the bill and tip correctly",
        "customer": "Friends at group dinners", "offer": "An offline-friendly tip splitter with /api/health"}}
    assert request(client, "POST", d["base"] + "/manual-review", "usr_bob", json=body).status_code in {403, 404}
    reviewed = request(client, "POST", d["base"] + "/manual-review", json=body)
    assert reviewed.status_code == 200, reviewed.text
    new = reviewed.json()
    assert new["state"] == "reviewing" and new["manual_review_available"] is False
    assert new["current_revision"]["structured"]["_manual_recovery"] is True
    assert request(client, "POST", d["base"] + "/confirm", json={"revision_id": rid}).status_code == 409
    confirmed = request(client, "POST", d["base"] + "/confirm", json={"revision_id": new["current_revision"]["id"]})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["session"]["state"] == "confirmed"


@pytest.mark.parametrize("fork", ["direction", "answer"])
def test_correction_of_manual_review_can_recover_its_own_offline_failure(client, offline_idea, fork):
    from app.db.models import Job
    from app.db.supymem_models import ClarifierTurn, IdeaClarificationRun, IdeaRevision, IdeaSession
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    d = offline_idea
    saved = request(client, "GET", d["base"]).json()
    question_id = None
    if fork == "answer":
        # Seed only the owned question; its founder answer and failed worker
        # receipt go through the ordinary signed API and job execution. A
        # non-core field: offline mode answers offer/customer follow-ups itself.
        question_id = new_id("ict")
        with db()() as session:
            session.add(ClarifierTurn(id=question_id, session_id=d["sid"], venture_id=d["vid"],
                workspace_id=saved["workspace_id"], turn=max(turn["turn"] for turn in saved["turns"]) + 1,
                role="agent", kind="question", text="Who will try it first?",
                options=json.dumps({"field": "first_user_path", "options": ["Friends", "Teachers"]}), created_at=utcnow()))
            session.commit()
        answer = request(client, "POST", d["base"] + "/messages", json={"text": "Friends at group dinners"})
        assert answer.status_code == 200, answer.text
        run_queued("idea.clarify_turn", through_job_id=answer.json()["job_id"])
        saved = request(client, "GET", d["base"]).json()
    patch = {"problem": "Split the bill and tip correctly", "customer": "Friends at group dinners",
        "offer": "An offline-friendly tip splitter with /api/health"}
    manual = request(client, "POST", d["base"] + "/manual-review", json={
        "revision_id": saved["current_revision"]["id"], "patch": patch})
    assert manual.status_code == 200, manual.text
    old_revision = manual.json()["current_revision"]["id"]
    confirmed = request(client, "POST", d["base"] + "/confirm", json={"revision_id": old_revision})
    assert confirmed.status_code == 200, confirmed.text
    with db()() as session:
        old_structured = session.get(IdeaRevision, old_revision).structured
        assert json.loads(old_structured)["_manual_recovery"] is True
    if fork == "direction":
        corrected = request(client, "POST", d["base"] + "/correct-direction", json={
            "revision_id": old_revision, "case": "know"})
    else:
        corrected = request(client, "POST", d["base"] + "/correct-answer", json={
            "revision_id": old_revision, "question_id": question_id,
            "text": "Independent teachers", "patch": {"first_user_path": "Independent teachers"}})
    assert corrected.status_code == 201, corrected.text
    fresh = corrected.json()
    assert fresh["id"] != d["sid"]
    assert fresh["current_revision"]["structured"]["_corrects_revision_id"] == old_revision
    if fork == "answer":
        assert "Independent teachers" in fresh["current_revision"]["structured"]["_answer_corrections"].values()
    base = f"/ventures/{d['vid']}/idea/sessions/{fresh['id']}"
    assert request(client, "GET", base).json()["manual_review_available"] is False
    answer = request(client, "POST", base + "/messages", json={"text": "Actually this should help independent teachers split shared costs."})
    assert answer.status_code == 200, answer.text
    job_id = run_queued("idea.clarify_turn", through_job_id=answer.json()["job_id"])
    failed = request(client, "GET", base).json()
    with db()() as session:
        job = session.get(Job, job_id)
        run = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == job_id))
        assert job.status == "failed_dead" and job.attempts == 1 and job.last_error == "clarification_provider_offline"
        assert (run.session_id, run.revision_id) == (fresh["id"], failed["current_revision"]["id"])
        assert run.state == "failed" and run.response is None and run.usage == "{}"
    assert failed["manual_review_available"] is True
    assert failed["clarification_retry_available"] is False
    assert "_manual_recovery" not in fresh["current_revision"]["structured"]
    assert request(client, "POST", base + "/manual-review", json={
        "revision_id": fresh["current_revision"]["id"], "patch": patch}).status_code == 409
    revised = {**patch, "customer": "Independent teachers"}
    reviewed = request(client, "POST", base + "/manual-review", json={
        "revision_id": failed["current_revision"]["id"], "patch": revised})
    assert reviewed.status_code == 200, reviewed.text
    latest = reviewed.json()
    assert latest["current_revision"]["structured"]["_manual_recovery"] is True
    assert latest["current_revision"]["structured"]["_corrects_revision_id"] == old_revision
    assert latest["manual_review_available"] is False
    assert request(client, "POST", base + "/confirm", json={"revision_id": old_revision}).status_code == 404
    assert request(client, "POST", base + "/confirm", json={"revision_id": failed["current_revision"]["id"]}).status_code == 409
    confirmed = request(client, "POST", base + "/confirm", json={"revision_id": latest["current_revision"]["id"]})
    assert confirmed.status_code == 200 and confirmed.json()["session"]["state"] == "confirmed"
    with db()() as session:
        assert session.get(IdeaRevision, old_revision).structured == old_structured
        original = session.get(IdeaSession, d["sid"])
        assert original.state == "confirmed" and original.confirmed_revision_id == old_revision


@pytest.mark.parametrize("mutation", ["active", "uncertain", "response", "usage", "revision", "wrong_error"])
def test_offline_manual_review_refuses_unconfirmed_or_stale_receipts(client, offline_idea, mutation):
    from app.db.models import Job
    from app.db.supymem_models import IdeaClarificationRun
    from app.state import db
    d = offline_idea
    with db()() as session:
        job = session.get(Job, d["jid"])
        run = session.get(IdeaClarificationRun, d["run_id"])
        if mutation == "active":
            job.status = "running"
        elif mutation == "uncertain":
            run.state = "uncertain"
        elif mutation == "response":
            run.response = "A response exists"
        elif mutation == "usage":
            run.usage = json.dumps({"unconfirmed": True})
        elif mutation == "wrong_error":
            run.error_code = job.last_error = "clarification_outcome_uncertain"
        session.commit()
    if mutation == "revision":
        changed = request(client, "PATCH", d["base"] + "/revision", json={"patch": {"problem": "Changed problem"}})
        assert changed.status_code == 200
    saved = request(client, "GET", d["base"]).json()
    assert saved["manual_review_available"] is False
    denied = request(client, "POST", d["base"] + "/manual-review", json={
        "revision_id": saved["current_revision"]["id"],
        "patch": {"problem": "Split tips", "customer": "Friends", "offer": "Tip splitter"},
    })
    assert denied.status_code == 409


def test_new_statement_after_unconfirmed_manual_review_can_recover_its_own_offline_failure(client, offline_idea):
    from app.db.models import Job
    from app.db.supymem_models import IdeaClarificationRun, IdeaRevision, IdeaSession
    from app.state import db

    d = offline_idea
    saved = request(client, "GET", d["base"]).json()
    patch = {"problem": "Split the bill and tip correctly", "customer": "Friends at group dinners",
        "offer": "An offline-friendly tip splitter with /api/health"}
    manual = request(client, "POST", d["base"] + "/manual-review", json={
        "revision_id": saved["current_revision"]["id"], "patch": patch})
    assert manual.status_code == 200, manual.text
    reviewed = manual.json()
    old_revision = reviewed["current_revision"]["id"]
    assert reviewed["state"] == "reviewing"
    assert reviewed["current_revision"]["structured"]["_manual_recovery"] is True
    with db()() as session:
        old_structured = session.get(IdeaRevision, old_revision).structured
        assert session.get(IdeaSession, d["sid"]).confirmed_revision_id is None
    posted = request(client, "POST", d["base"] + "/messages", json={
        "text": "Actually this should help independent teachers split shared classroom costs."})
    assert posted.status_code == 200, posted.text
    job_id = posted.json()["job_id"]
    assert job_id and job_id != d["jid"]
    assert run_queued("idea.clarify_turn", through_job_id=job_id) == job_id
    failed = request(client, "GET", d["base"]).json()
    revision_id = failed["current_revision"]["id"]
    assert revision_id != old_revision
    with db()() as session:
        job = session.get(Job, job_id)
        run = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == job_id))
        assert job.status == "failed_dead" and job.attempts == 1 and job.last_error == "clarification_provider_offline"
        assert (job.workspace_id, job.venture_id) == (reviewed["workspace_id"], d["vid"])
        assert (run.session_id, run.revision_id, run.workspace_id, run.venture_id) == (
            d["sid"], revision_id, reviewed["workspace_id"], d["vid"])
        assert run.state == "failed" and run.error_code == "clarification_provider_offline"
        assert run.response is None and run.usage == "{}"
        assert session.get(IdeaRevision, old_revision).structured == old_structured
        assert json.loads(old_structured)["_manual_recovery"] is True
        assert session.get(IdeaSession, d["sid"]).confirmed_revision_id is None
    assert failed["manual_review_available"] is True
    assert failed["clarification_retry_available"] is False
    assert "_manual_recovery" not in failed["current_revision"]["structured"]
    recovered = request(client, "POST", d["base"] + "/manual-review", json={
        "revision_id": revision_id, "patch": {**patch, "customer": "Independent teachers"}})
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["state"] == "reviewing"
