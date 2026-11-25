"""Money, authority, replay and durable artifact boundaries with provider I/O stubbed."""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from tests.company_os_harness import append_idea_revision
from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_auth_security import service_modules


def seed(factory, actor="pagebuilder-test-owner"):
    from app.authz.workspace import ensure_subject_person
    from app.credits.accounts import ensure_account
    from app.db.models import Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.workspaces.service import create_workspace
    from core.clock import utcnow
    from core.ids import new_id
    ensure_subject_person(actor)
    workspace = create_workspace(kind="company", name="Pagebuilder test", owner_id=actor)
    vid, sid, rid, now = new_id("ven"), new_id("ids"), new_id("idv"), utcnow()
    with factory() as session:
        session.add(Venture(id=vid, workspace_id=workspace.id, name="Nightshift", raw_idea="A concept for a shift handoff board for warehouse supervisors.",
            stage="draft", autonomy_level="L1", created_at=now, updated_at=now))
        session.flush()
        session.add(IdeaSession(id=sid, workspace_id=workspace.id, venture_id=vid, case="know", state="confirmed",
            confirmed_revision_id=rid, current_revision_id=rid, confirmed_by_person_id=actor, confirmed_at=now,
            created_at=now, updated_at=now))
        session.flush()
        session.add(IdeaRevision(id=rid, session_id=sid, workspace_id=workspace.id, venture_id=vid, rev=1, source="founder",
            summary="A concept for a shift handoff board for warehouse supervisors.", structured=json.dumps({"audience": "warehouse supervisors"}), created_at=now))
        session.commit()
    account = ensure_account(vid, workspace_id=workspace.id, owner_person_id=actor)
    return {"factory": factory, "actor": actor, "vid": vid, "wid": workspace.id, "rid": rid, "sid": sid, "account": account}


def connect_local_tool(monkeypatch):
    from app import coding_agents_consent
    from app.core.local_owner import ensure_owner
    from desktop_runtime import agent_status
    ensure_owner()
    monkeypatch.setattr(coding_agents_consent, "consents", lambda actor: {"codex": True})
    monkeypatch.setattr(agent_status, "statuses", lambda: [{"name": "codex", "installed": True,
        "signed_in": True, "available": True, "subscription_login": True}])


@pytest.mark.parametrize("idea,audience", [
    ("Confirm stock counts before a warehouse handoff.", "warehouse supervisors"),
    ("Remind home gardeners to water balcony plants.", "home gardeners"),
])
def test_local_build_with_oversized_research_keeps_founder_inputs(setup, monkeypatch, idea, audience):
    from app.conversion import waitlist
    from app.core.local_owner import ensure_owner
    from app.core.providers_local import save_settings
    from app.db.models import Job
    from app.db.supymem_models import IdeaRevision
    from app.pagebuilder import api, local_publish, tool
    from app.research import dossier

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    data = seed(setup["factory"], actor=ensure_owner().id)
    save_settings(provider="azure_openai", base_url="https://provider.example.invalid/v1",
        api_key="synthetic-local-test-key", models={"chat": "DeepSeek-V4-Flash"})
    monkeypatch.setattr(api, "require_enabled", lambda: None)
    monkeypatch.setattr(tool, "connected_provider", lambda actor: "codex")
    monkeypatch.setattr(tool, "prepare_job", lambda *args, **kwargs: None)
    answers = {"summary": idea, "audience": audience}
    with data["factory"]() as session:
        session.get(IdeaRevision, data["rid"]).structured = json.dumps(answers)
        session.commit()
    limitations = ["Only a few public sources discussed this problem."]
    markdown = "Evidence for " + audience + ': "résumé"\n' + ("🌱" * 16000)
    research = {"limitations": limitations, "deep_research": {"sources": [markdown]}}
    monkeypatch.setattr(dossier, "load_validation_dossier", lambda venture_id: (markdown, research))

    queued = api.build(data["vid"], data["actor"])
    assert queued["state"] == "queued"
    with data["factory"]() as session:
        job = json.loads(session.get(Job, queued["job_id"]).payload_json)
        assert job["coding_provider"] == "codex"
        inputs = job["_runtime"]["input"]
        assert api.snapshot(session, data["vid"], data["wid"])[1] == inputs
    assert len(api.dump(inputs).encode()) <= 48000
    assert inputs["idea"] == idea and inputs["clarified_answers"] == answers
    assert inputs["waitlist_url"] == waitlist.public_url(data["vid"])
    assert inputs["waitlist_consent_text"] == waitlist.consent_text(inputs["name"])
    assert inputs["waitlist_consent_version"] == waitlist.VERSION
    assert inputs["research"]["local_signup"] == local_publish.waitlist_destination(
        data["vid"], inputs["name"], deployed=False, verify=False)
    assert inputs["research"]["limitations"] == limitations
    assert markdown.startswith(inputs["research"]["dossier_markdown"]) and inputs["research"]["dossier_markdown"]
    assert research["deep_research"]["sources"] == [markdown]
    with data["factory"]() as session:
        session.get(IdeaRevision, data["rid"]).structured = json.dumps({**answers, "description": markdown}, ensure_ascii=False)
        session.flush()
        with pytest.raises(HTTPException, match="Idea screen, shorten your description and answers"):
            api.snapshot(session, data["vid"], data["wid"])


def test_new_page_version_preserves_ready_source_and_dedupes_repeated_click(setup, monkeypatch):
    from app.db.models import Job
    from app.pagebuilder import api
    from fastapi import HTTPException
    d = setup
    monkeypatch.setattr(api, "_candidate_current", lambda doc: True)
    original = api.build(d["vid"], d["actor"])["job_id"]
    with d["factory"]() as session:
        row = session.get(Job, original)
        doc = json.loads(row.payload_json)
        doc["_runtime"].update(state="ready", stage="ready", progress=1)
        row.status, row.payload_json = "succeeded", api.dump(doc)
        session.commit()
    newer = api.build(d["vid"], d["actor"], new_version_of=original)
    assert newer["job_id"] != original and newer["state"] == "queued"
    assert api.build(d["vid"], d["actor"], new_version_of=original)["job_id"] == newer["job_id"]
    with d["factory"]() as session:
        assert session.get(Job, original).status == "succeeded"
        doc = json.loads(session.get(Job, newer["job_id"]).payload_json)
        assert "rewrite" not in doc
    with pytest.raises(HTTPException) as refused:
        api.build(d["vid"], d["actor"], new_version_of="foreign-job")
    assert refused.value.status_code == 409
    with d["factory"]() as session:
        row = session.get(Job, newer["job_id"])
        doc = json.loads(row.payload_json)
        doc["_runtime"].update(state="ready", stage="ready", progress=1)
        row.status, row.payload_json = "succeeded", api.dump(doc)
        session.commit()
    final = api.build(d["vid"], d["actor"], new_version_of=newer["job_id"])
    with d["factory"]() as session:
        row = session.get(Job, final["job_id"])
        doc = json.loads(row.payload_json)
        doc["_runtime"].update(state="ready", stage="ready", progress=1)
        row.status, row.payload_json = "succeeded", api.dump(doc)
        session.commit()
    with pytest.raises(HTTPException) as capped:
        api.build(d["vid"], d["actor"], new_version_of=final["job_id"])
    assert capped.value.detail["code"] == "pagebuilder_build_limit"


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    for key, value in {"COMPANY_OS_DB_URL": "sqlite:///" + str(tmp_path / "pagebuilder.db"),
        "COMPANY_OS_DATA_DIR": str(tmp_path), "COMPANY_OS_WORKING_MEMORY_PATH": str(tmp_path / "memory.db"),
        "SUPYMEM_PAGEBUILDER_ENABLED": "1", "LLM_OFFLINE": "0", "LLM_API_KEY": "synthetic-test-only",
        "LLM_BASE_URL": "https://provider.example.invalid", "LLM_PROVIDER": "azure_foundry", "LLM_DEPLOYMENT_PRO": "DeepSeek-V4-Pro",
        "PUBLIC_GATEWAY_URL": "https://gateway.supymem.com", "APP_ENV": "local", "COMPANY_OS_REQUIRE_SIGNED_CALLER": "0"}.items():
        monkeypatch.setenv(key, value)
    with service_modules("company_os"):
        from app.state import db
        yield seed(db())


def test_confirmation_without_research_unlocks_pagebuilder(setup):
    from sqlalchemy import select
    from core.clock import utcnow
    from app.db.models import CreditReservation, Job
    from app.db.supymem_models import IdeaSession, ValidationRun
    from app.idea.service import confirm
    from app.idea.validation import METHODS
    from app.pagebuilder import api
    d = setup
    with d["factory"]() as session:
        idea = session.get(IdeaSession, d["sid"])
        idea.state, idea.confirmed_revision_id = "validating", None
        for method in METHODS:
            session.add(ValidationRun(id=method, session_id=d["sid"], revision_id=d["rid"],
                workspace_id=d["wid"], venture_id=d["vid"], method=method, state="complete",
                report="{}", created_at=utcnow()))
        session.commit()
        with pytest.raises(HTTPException) as blocked:
            api.snapshot(session, d["vid"], d["wid"])
        assert blocked.value.detail["code"] == "pagebuilder_confirmed_revision_required"
    result = confirm(d["sid"], revision_id=d["rid"], person_id=d["actor"],
                     start_research=False, accept_validation=True)
    assert result["dossier_job_id"] is None
    with d["factory"]() as session:
        assert api.snapshot(session, d["vid"], d["wid"])[0] == d["rid"]
        assert not session.scalars(select(Job)).all()
        assert not session.scalars(select(CreditReservation)).all()


@pytest.mark.parametrize("explicit", [False, True])
def test_failed_new_version_can_retry_without_discarding_older_ready_page(setup, monkeypatch, explicit):
    from app.db.models import Job
    from app.pagebuilder import api
    d = setup
    monkeypatch.setattr(api, "_candidate_current", lambda doc: True)
    original = api.build(d["vid"], d["actor"])["job_id"]
    with d["factory"]() as session:
        row = session.get(Job, original)
        doc = json.loads(row.payload_json)
        doc["_runtime"].update(state="ready", stage="ready", progress=1)
        row.status, row.payload_json = "succeeded", api.dump(doc)
        session.commit()
    newer = api.build(d["vid"], d["actor"], new_version_of=original)["job_id"]
    with d["factory"]() as session:
        row = session.get(Job, newer)
        doc = json.loads(row.payload_json)
        doc["_runtime"].update(state="failed", error="pagebuilder_generation_failed")
        row.status, row.payload_json = "failed_dead", api.dump(doc)
        session.commit()
    retry = api.build(d["vid"], d["actor"], new_version_of=newer if explicit else None)
    assert retry["state"] == "queued" and retry["job_id"] not in {original, newer}
    with d["factory"]() as session:
        assert session.get(Job, original).status == "succeeded"


@pytest.mark.parametrize("code,runtime_state", [("pagebuilder_renderer_unavailable", "failed"),
    ("pagebuilder_candidate_invalid", "ready"), ("pagebuilder_plan_rejected", "failed")])
def test_explicit_failed_candidate_retry_requeues_only_proven_free_failure_otherwise_creates_successor(setup, monkeypatch, code, runtime_state):
    from app.api.pagebuilder_api import BuildBody, build
    from app.db.models import Job
    from app.pagebuilder import api
    d = setup
    monkeypatch.setattr(api, "_candidate_current", lambda _doc: True)
    original = api.build(d["vid"], d["actor"])["job_id"]
    with d["factory"]() as session:
        row = session.get(Job, original)
        doc = json.loads(row.payload_json)
        doc["_runtime"].update(state=runtime_state, error=code if runtime_state == "failed" else None)
        row.status, row.last_error, row.payload_json = "failed_dead", code, api.dump(doc)
        session.commit()
    result = build(d["vid"], BuildBody(new_version_of=original), d["actor"])
    assert result["state"] == "queued"
    free = code == "pagebuilder_renderer_unavailable"
    assert (result["job_id"] == original and result["requeued"] is True) if free else result["job_id"] != original
    for _attempt in range(3):
        assert build(d["vid"], BuildBody(new_version_of=original), d["actor"])["job_id"] == result["job_id"]
    with d["factory"]() as session:
        assert session.get(Job, original).status == ("scheduled" if free else "failed_dead")


@pytest.mark.parametrize("delivered", [0, 3])
def test_explicit_free_retry_is_allowed_at_total_attempt_cap_but_not_ready_version_cap(setup, delivered):
    from app.db.models import Job
    from app.pagebuilder import api
    from sqlalchemy import select
    d = setup
    previous = None
    for _attempt in range(api.ATTEMPTS_PER_REVISION):
        result = api.build(d["vid"], d["actor"], new_version_of=previous)
        previous = result["job_id"]
        with d["factory"]() as session:
            row = session.get(Job, previous)
            doc = json.loads(row.payload_json)
            doc["_runtime"].update(state="failed", error="pagebuilder_plan_rejected")
            row.status, row.payload_json = "failed_dead", api.dump(doc)
            session.commit()
    with d["factory"]() as session:
        for index, row in enumerate(session.scalars(select(Job).where(Job.kind == api.KIND, Job.venture_id == d["vid"],
                Job.id != previous))):
            if index < delivered:
                doc = json.loads(row.payload_json)
                doc["result"]["ready_delivered"] = True
                row.payload_json = api.dump(doc)
        row = session.get(Job, previous)
        doc = json.loads(row.payload_json)
        doc["_runtime"]["error"] = "pagebuilder_renderer_unavailable"
        row.payload_json = api.dump(doc)
        session.commit()
    if delivered:
        with pytest.raises(HTTPException) as capped:
            api.build(d["vid"], d["actor"], new_version_of=previous)
        assert capped.value.detail["code"] == "pagebuilder_build_limit"
    else:
        result = api.build(d["vid"], d["actor"], new_version_of=previous)
        assert result["job_id"] == previous and result["state"] == "queued" and result["requeued"] is True
    with d["factory"]() as session:
        assert len(session.scalars(select(Job).where(Job.kind == api.KIND, Job.venture_id == d["vid"])).all()) == 8


@pytest.mark.parametrize("explicit", [False, True])
def test_four_platform_failures_do_not_exhaust_ready_versions_and_eight_attempts_stop_new_spend(setup, monkeypatch, explicit):
    from app.db.models import Job
    from app.pagebuilder import api
    from sqlalchemy import select
    d = setup
    monkeypatch.setattr(api, "_candidate_current", lambda _doc: True)
    codes = ["pagebuilder_provider_rejected", "handler_failed:ValueError", "pagebuilder_candidate_invalid", "qa_runtime_failed"]
    previous = None
    for attempt in range(api.ATTEMPTS_PER_REVISION):
        result = api.build(d["vid"], d["actor"], new_version_of=previous if explicit else None)
        assert result["state"] == "queued" and result["job_id"] != previous
        if previous:
            assert api.build(d["vid"], d["actor"], new_version_of=previous if explicit else None)["job_id"] == result["job_id"]
        previous = result["job_id"]
        code = codes[attempt % len(codes)]
        with d["factory"]() as session:
            row = session.get(Job, previous)
            doc = json.loads(row.payload_json)
            doc["_runtime"].update(state="ready" if code == "pagebuilder_candidate_invalid" else "failed",
                error=None if code == "pagebuilder_candidate_invalid" else code,
                calls_used=1, operations={"variant_0:code": {"state": "completed", "usage": {"credits_micro": 5}}})
            row.status, row.last_error, row.payload_json = "failed_dead", code, api.dump(doc)
            session.commit()
    with pytest.raises(HTTPException) as capped:
        api.build(d["vid"], d["actor"], new_version_of=previous if explicit else None)
    assert capped.value.status_code == 409 and capped.value.detail["code"] == "pagebuilder_attempt_limit"
    with d["factory"]() as session:
        assert len(session.scalars(select(Job).where(Job.kind == api.KIND, Job.venture_id == d["vid"])).all()) == 8


def test_delivered_ready_version_still_counts_after_later_platform_or_publication_failure(setup, monkeypatch):
    from app.db.models import Job
    from app.pagebuilder import api
    d = setup
    monkeypatch.setattr(api, "_candidate_current", lambda _doc: False)
    previous = None
    for code in ["pagebuilder_candidate_invalid", "qa_runtime_failed", "handler_failed:ValueError"]:
        result = api.build(d["vid"], d["actor"], new_version_of=previous)
        previous = result["job_id"]
        with d["factory"]() as session:
            row = session.get(Job, previous)
            doc = json.loads(row.payload_json)
            doc["result"]["ready_delivered"] = True
            doc["_runtime"].update(state="failed", error=code)
            row.status, row.last_error, row.payload_json = "failed_dead", code, api.dump(doc)
            session.commit()
    with pytest.raises(HTTPException) as capped:
        api.build(d["vid"], d["actor"], new_version_of=previous)
    assert capped.value.detail["code"] == "pagebuilder_build_limit"


def test_legacy_succeeded_but_invalid_candidate_does_not_spend_a_ready_version(setup, monkeypatch):
    from app.db.models import Job
    from app.pagebuilder import api
    d = setup
    monkeypatch.setattr(api, "_candidate_current", lambda _doc: False)
    previous = None
    for _attempt in range(api.BUILDS_PER_REVISION):
        result = api.build(d["vid"], d["actor"], new_version_of=previous)
        previous = result["job_id"]
        with d["factory"]() as session:
            row = session.get(Job, previous)
            doc = json.loads(row.payload_json)
            doc["_runtime"].update(state="ready", stage="ready", progress=1)
            row.status, row.payload_json = "succeeded", api.dump(doc)
            session.commit()
    result = api.build(d["vid"], d["actor"], new_version_of=previous)
    assert result["state"] == "queued" and result["job_id"] != previous


def test_explicit_retry_does_not_repeat_uncertain_paid_candidate(setup):
    from app.db.models import Job
    from app.pagebuilder import api
    d = setup
    original = api.build(d["vid"], d["actor"])["job_id"]
    with d["factory"]() as session:
        row = session.get(Job, original)
        doc = json.loads(row.payload_json)
        doc["_runtime"].update(state="failed", error="pagebuilder_outcome_uncertain", operations={"variant_0:brief_direction_plan": {"state": "uncertain"}})
        row.status, row.payload_json = "failed_dead", api.dump(doc)
        session.commit()
    with pytest.raises(HTTPException) as refused:
        api.build(d["vid"], d["actor"], new_version_of=original)
    assert refused.value.detail["code"] == "pagebuilder_outcome_uncertain"


def test_build_refuses_a_leased_but_invalid_candidate_instead_of_successful_noop(setup, caplog):
    from app.api.pagebuilder_api import BuildBody, build
    from app.db.models import Job
    from app.pagebuilder import api
    d = setup
    original = api.build(d["vid"], d["actor"])["job_id"]
    with d["factory"]() as session:
        row = session.get(Job, original)
        doc = json.loads(row.payload_json)
        doc["_runtime"].update(state="failed", error="pagebuilder_candidate_invalid")
        row.status, row.payload_json = "leased", api.dump(doc)
        session.commit()
    with pytest.raises(HTTPException) as refused:
        build(d["vid"], BuildBody(new_version_of=original), d["actor"])
    assert refused.value.detail["code"] == "pagebuilder_candidate_invalid"
    assert any(record.message == "pagebuilder_build_request_failed" and record.error_code == "pagebuilder_candidate_invalid" for record in caplog.records)


@pytest.mark.parametrize("active_state", ["running", "ready"])
def test_new_version_preserves_an_older_active_job_before_retrying_other_slots(setup, monkeypatch, active_state):
    from unittest.mock import Mock
    from app.db.models import Job
    from app.pagebuilder import api
    d = setup
    ids = _legacy_failed_slots(d)
    monkeypatch.setattr(api, "_candidate_current", lambda doc: True)
    with d["factory"]() as session:
        for job_id, state, status in [(ids[0], active_state, "leased"), (ids[1], "ready", "succeeded")]:
            row = session.get(Job, job_id)
            doc = json.loads(row.payload_json)
            doc["_runtime"]["state"] = state
            row.status, row.payload_json = status, api.dump(doc)
        session.commit()
        untouched = session.get(Job, ids[2]).payload_json
    wake = Mock()
    monkeypatch.setattr(api, "publish_wake", wake)
    with pytest.raises(HTTPException) as refused:
        api.build(d["vid"], d["actor"], new_version_of=ids[1])
    assert refused.value.detail["code"] == "pagebuilder_new_version_unavailable"
    wake.assert_not_called()
    with d["factory"]() as session:
        assert session.get(Job, ids[2]).status == "failed_dead"
        assert session.get(Job, ids[2]).payload_json == untouched


def claimed(d):
    from app.core.jobs import lease
    from app.pagebuilder import api
    started = api.start(d["vid"], d["actor"])
    with d["factory"]() as session:
        job = lease(session, "test-pagebuilder-worker", kinds=[api.KIND], n=1)[0]
        session.commit()
    return started, {**job.payload, "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts}


def test_new_version_never_requeues_an_older_free_renderer_failure(setup, monkeypatch):
    from app.db.models import Job
    from app.pagebuilder import api
    d = setup
    monkeypatch.setattr(api, "_candidate_current", lambda doc: True)
    first = api.build(d["vid"], d["actor"])["job_id"]
    with d["factory"]() as session:
        row = session.get(Job, first)
        doc = json.loads(row.payload_json)
        doc["_runtime"].update(state="failed", error="pagebuilder_plan_rejected")
        row.status, row.payload_json = "failed_dead", api.dump(doc)
        session.commit()
    ready = api.build(d["vid"], d["actor"])["job_id"]
    with d["factory"]() as session:
        row = session.get(Job, first)
        doc = json.loads(row.payload_json)
        doc["_runtime"]["error"] = "pagebuilder_renderer_unavailable"
        row.payload_json = api.dump(doc)
        row = session.get(Job, ready)
        doc = json.loads(row.payload_json)
        doc["_runtime"].update(state="ready", stage="ready", progress=1)
        row.status, row.payload_json = "succeeded", api.dump(doc)
        session.commit()
    requested = api.build(d["vid"], d["actor"], new_version_of=ready)
    assert requested["job_id"] not in {first, ready}
    with d["factory"]() as session:
        assert session.get(Job, first).status == "failed_dead"


def test_terminal_job_error_is_not_hidden_by_missing_runtime_error(setup):
    from app.db.models import Job
    from app.pagebuilder import api
    d = setup
    job_id = api.build(d["vid"], d["actor"])["job_id"]
    with d["factory"]() as session:
        row = session.get(Job, job_id)
        row.status, row.last_error = "failed_dead", "pagebuilder_reference_unavailable"
        session.commit()
    view = api.status(d["vid"], d["actor"], job_id)
    assert view["error"] == "pagebuilder_reference_unavailable"
    assert "safely saved" in view["error_reason"]


def test_ready_preview_with_terminal_invalid_source_is_not_reported_ready(setup, monkeypatch):
    from app.db.models import Job
    from app.pagebuilder import api
    d = setup
    job_id = api.build(d["vid"], d["actor"])["job_id"]
    with d["factory"]() as session:
        row = session.get(Job, job_id)
        doc = json.loads(row.payload_json)
        doc["_runtime"]["state"] = "ready"
        row.payload_json = api.dump(doc)
        row.status, row.last_error = "failed_dead", "pagebuilder_candidate_invalid"
        session.commit()
    monkeypatch.setattr(api, "_candidate_current", lambda _doc: True)
    view = api.status(d["vid"], d["actor"], job_id)
    assert view["state"] == "failed" and "preview_url" not in view
    assert view["error"] == "pagebuilder_candidate_invalid"
    assert "source could not be verified" in view["error_reason"]
    assert api.build(d["vid"], d["actor"])["job_id"] != job_id


@pytest.mark.parametrize("idea", ["Invoice reconciliation for wholesale buyers", "Weekend pottery lessons for beginners"])
@pytest.mark.parametrize("outcome", ["failed", "retry", "uncertain", "cancelled", "lease_lost"])
def test_failed_page_job_posts_one_plain_scoped_notice(setup, monkeypatch, idea, outcome):
    import asyncio

    from app.core.jobs import RetryableError, lease
    from app.db.models import ChatConversation, ChatMessage, Job, Venture
    from app.db.supymem_models import IdeaRevision
    from app.pagebuilder import api, jobs, worker
    from sqlalchemy import select

    d = setup
    with d["factory"]() as session:
        session.get(Venture, d["vid"]).raw_idea = idea
        d["rid"] = append_idea_revision(session, d["rid"], summary=idea).id
        session.commit()
    job_id = api.build(d["vid"], d["actor"])["job_id"]
    with d["factory"]() as session:
        job = lease(session, "failure-notice-worker", kinds=[api.KIND], n=1)[0]
        session.commit()

    def fail(_payload):
        if outcome == "retry":
            raise RetryableError("pagebuilder_provider_rate_limited")
        if outcome == "uncertain":
            raise ValueError("pagebuilder_operation_in_progress")
        if outcome in {"cancelled", "lease_lost"}:
            with d["factory"]() as session:
                row = session.get(Job, job_id)
                if outcome == "cancelled":
                    row.cancel_requested = True
                else:
                    row.locked_by = "replacement-worker"
                session.commit()
        raise ValueError("pagebuilder_renderer_unavailable: private diagnostic")

    monkeypatch.setattr(jobs, "run", fail)
    asyncio.run(worker.run_one(d["factory"], job))
    asyncio.run(worker.run_one(d["factory"], job))
    with d["factory"]() as session:
        notices = list(session.scalars(select(ChatMessage).where(ChatMessage.role == "assistant")))
        row = session.get(Job, job_id)
        assert row.status == {"failed": "failed_dead", "retry": "retry_wait", "uncertain": "leased",
                              "cancelled": "cancelled", "lease_lost": "leased"}[outcome]
        if notices:
            assert session.get(ChatConversation, notices[0].conversation_id).venture_id == d["vid"]
    if outcome != "failed":
        assert notices == []
        return
    assert len(notices) == 1
    notice = notices[0]
    assert (notice.venture_id, notice.workspace_id) == (d["vid"], d["wid"])
    assert notice.kind == "text" and not notice.actions_json
    assert "could not" in notice.body_md and "Try building again later" in notice.body_md
    assert "Your" in notice.body_md
    assert all(word not in notice.body_md for word in ("private diagnostic", "pagebuilder_", "credits", "—"))
    worker._post_failure(job)
    with d["factory"]() as session:
        assert len(list(session.scalars(select(ChatMessage).where(ChatMessage.role == "assistant")))) == 1


def test_dedicated_worker_preserves_known_source_verification_error(setup, monkeypatch, caplog):
    import asyncio
    from app.core.jobs import lease
    from app.db.models import Job
    from app.pagebuilder import api, jobs, worker
    d = setup
    job_id = api.build(d["vid"], d["actor"])["job_id"]
    with d["factory"]() as session:
        job = lease(session, "test-pagebuilder-worker", kinds=[api.KIND], n=1)[0]
        session.commit()
    def invalid(_payload):
        raise ValueError("pagebuilder_candidate_invalid: private provider detail")
    monkeypatch.setattr(jobs, "run", invalid)
    asyncio.run(worker.run_one(d["factory"], job))
    with d["factory"]() as session:
        assert session.get(Job, job_id).last_error == "pagebuilder_candidate_invalid"
    assert any(getattr(record, "error_code", None) == "pagebuilder_candidate_invalid" for record in caplog.records)


def response(text="model-produced-content"):
    return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=300, total_tokens=1300),
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=text))])


def test_idempotent_start_and_auth_scope(setup, monkeypatch):
    from app.pagebuilder import api
    d = setup
    quote = api.estimate(d["vid"], d["actor"])
    assert quote["ready"] and 0 < quote["credits"] <= 25
    assert api.CALLS_PER_VARIANT == 10
    assert api.MAX_PAGE_MICRO == 250000
    assert quote["credits"] == 22.084
    assert quote["ceiling_micro"] == api.resources(d["wid"], d["vid"])["call_ceiling_micro"] * 10
    assert quote["seconds"] == 300
    first = api.start(d["vid"], d["actor"], variants=2)
    assert api.start(d["vid"], d["actor"], variants=1)["job_id"] == first["job_id"]
    assert first["variant_count"] == 2
    from app.db.models import Job
    with d["factory"]() as session:
        assert session.get(Job, first["job_id"]).timeout_s == 2 * (api.CALLS_PER_VARIANT * api.CALL_TIMEOUT_SECONDS + 120) == 6240
    with pytest.raises(HTTPException) as hidden:
        api.status(d["vid"], "different-owner", first["job_id"])
    assert hidden.value.status_code == 404
    other = seed(d["factory"], actor=d["actor"])
    with pytest.raises(HTTPException) as sibling:
        api.status(other["vid"], d["actor"], first["job_id"])
    assert sibling.value.status_code == 404
    monkeypatch.setenv("SUPYMEM_PAGEBUILDER_ENABLED", "0")
    with pytest.raises(HTTPException) as disabled:
        api.estimate(d["vid"], d["actor"])
    assert disabled.value.status_code == 404


def test_viewer_cannot_start_and_missing_confirmation(setup):
    from app.authz.workspace import ensure_subject_person
    from app.db.models import WorkspaceMember
    from app.db.supymem_models import IdeaSession
    from app.pagebuilder import api
    from core.clock import utcnow
    d = setup
    ensure_subject_person("pagebuilder-viewer")
    with d["factory"]() as session:
        session.add(WorkspaceMember(id="pagebuilder-viewer-member", workspace_id=d["wid"], person_id="pagebuilder-viewer", role="viewer", can_approve=0, created_at=utcnow()))
        session.commit()
    with pytest.raises(HTTPException) as viewer:
        api.start(d["vid"], "pagebuilder-viewer")
    assert viewer.value.status_code == 403
    with d["factory"]() as session:
        session.get(IdeaSession, d["sid"]).confirmed_revision_id = None
        session.commit()
    with pytest.raises(HTTPException) as unconfirmed:
        api.start(d["vid"], d["actor"])
    assert unconfirmed.value.status_code == 409


@pytest.mark.parametrize("stage", ["code", "repair_1", "repair_2"])
def test_meter_settles_actual_and_replays_without_provider(setup, monkeypatch, stage):
    from app.credits.accounts import get_account
    from app.pagebuilder import runtime
    from llm_gateway import sole
    d = setup
    started, payload = claimed(d)
    count = []
    async def transmit(**kwargs):
        count.append(kwargs)
        assert kwargs["single_attempt"] is True and kwargs["use_workspace_override"] is False
        assert kwargs["config"].reasoning_effort == "none"
        assert kwargs["config"].timeout_seconds == (180 if stage.startswith("repair_") else 300)
        assert get_account(d["vid"]).reserved_micro > 0
        return response()
    monkeypatch.setattr(sole, "transmit", transmit)
    call = runtime.job_model_call(started["job_id"], payload)
    assert call(stage, "Generate the page") == "model-produced-content"
    assert call(stage, "Generate the page") == "model-produced-content"
    account = get_account(d["vid"])
    assert len(count) == 1 and account.spent_micro > 0 and account.reserved_micro == 0
    operation = runtime.journal.operation(started["job_id"], "variant_0:" + stage)
    assert operation["state"] == "completed" and operation["usage"]["settled"]


@pytest.mark.parametrize("stages", [
    ("brief_direction_plan", "plan_repair_1", "capability_review_1", "capability_review_2", "plan_repair_2", "capability_review_3", "capability_review_4", "code", "repair_1", "repair_2"),
    ("brief_direction_plan", "plan_repair_1", "capability_review_1", "plan_repair_2", "capability_review_2", "plan_repair_3", "capability_review_5", "capability_review_6", "code", "repair_1"),
    ("brief_direction_plan", "plan_repair_1", "capability_review_1", "capability_review_2", "plan_repair_2", "plan_repair_3", "capability_review_3", "plan_repair_4", "capability_review_4", "code"),
    ("brief_direction_plan", "plan_repair_1", "plan_repair_2", "plan_repair_3", "plan_repair_4", "plan_repair_5", "capability_review_1", "capability_review_2", "code", "repair_1"),
])
def test_ten_metered_stages_capture_effort_and_block_eleventh_call(setup, monkeypatch, stages):
    from app.pagebuilder import runtime
    from llm_gateway import sole
    d = setup
    started, payload = claimed(d)
    quote = json.loads(runtime.read(started["job_id"]).payload_json)["_runtime"]["quote"]
    assert quote["request_policy"] == "scoped_grounding_v19"
    assert quote["reasoning_policy"] == {"planning": "low", "reviewer": "low", "generation": "none"}
    monkeypatch.setenv("LLM_REASONING_EFFORT", "max")
    configs = []
    async def transmit(**kwargs):
        configs.append((kwargs["purpose"], kwargs["config"].reasoning_effort))
        stage = kwargs["purpose"].partition(".")[2]
        assert kwargs["max_tokens"] == (8192 if stage.startswith("plan_repair_") else quote["max_output_tokens"])
        assert kwargs["config"].timeout_seconds == (180 if stage.startswith(("plan_repair_", "repair_")) else quote["call_timeout_seconds"])
        return response()
    monkeypatch.setattr(sole, "transmit", transmit)
    call = runtime.job_model_call(started["job_id"], payload)
    for stage in stages:
        call(stage, "Execute this bounded stage")
    assert [effort for _, effort in configs] == ["none" if stage in {"code", "repair_1", "repair_2"} else "low" for stage in stages]
    assert json.loads(runtime.read(started["job_id"]).payload_json)["_runtime"]["calls_used"] == quote["call_budget"] == 10
    # A completed stage replays; an eleventh distinct operation cannot spend again.
    assert call(stages[-1], "Execute this bounded stage") == "model-produced-content"
    from app.credits.accounts import get_account
    spent = get_account(d["vid"]).spent_micro
    from app.core.jobs import FatalError
    with pytest.raises(FatalError, match="pagebuilder_stage_invalid"):
        call("capability_review_7", "No seventh review stage allowed")
    with pytest.raises(FatalError, match="pagebuilder_stage_invalid"):
        call("plan_repair_6", "No sixth plan correction stage allowed")
    with pytest.raises(FatalError, match="pagebuilder_call_budget_exhausted"):
        runtime.job_model_call(started["job_id"], payload, variant=1)("code", "No additional call allowed")
    assert len(configs) == 10
    account = get_account(d["vid"])
    assert account.spent_micro == spent and account.reserved_micro == 0


@pytest.mark.parametrize("uncertain", [False, True])
def test_failure_refund_and_uncertain_outcome_never_redial(setup, monkeypatch, uncertain):
    from app.core.jobs import FatalError
    from app.credits.accounts import get_account
    from app.pagebuilder import runtime
    from llm_gateway import sole
    from llm_gateway.errors import ProviderCallError
    d = setup
    started, payload = claimed(d)
    calls = []
    async def rejected(**kwargs):
        calls.append(1)
        raise ProviderCallError("provider_error" if uncertain else "bad_api_key", "synthetic test failure", status=None if uncertain else 403)
    monkeypatch.setattr(sole, "transmit", rejected)
    call = runtime.job_model_call(started["job_id"], payload)
    with pytest.raises(FatalError):
        call("code", "Generate the page")
    account = get_account(d["vid"])
    assert account.spent_micro == 0
    assert (account.reserved_micro > 0) is uncertain
    from app.pagebuilder import api
    receipt = api.status(d["vid"], d["actor"], started["job_id"])["receipt"]
    assert (receipt["uncertain_outcomes"] == 1) is uncertain
    assert (receipt["unresolved_ceiling_usd"] > 0) is uncertain
    with pytest.raises(FatalError):
        call("code", "Generate the page")
    assert len(calls) == 1


def test_old_worker_cannot_call_after_lease_loss(setup, monkeypatch):
    from app.core.jobs import FatalError
    from app.db.models import Job
    from app.pagebuilder import runtime
    d = setup
    started, payload = claimed(d)
    with d["factory"]() as session:
        session.get(Job, started["job_id"]).locked_by = "replacement-worker"
        session.commit()
    with pytest.raises(FatalError, match="pagebuilder_lease_lost"):
        runtime.job_model_call(started["job_id"], payload)("code", "Generate the page")


@pytest.mark.parametrize("model", ["DeepSeek-V4-Pro", "gpt-6-luna"])
def test_other_provider_retains_its_reasoning_configuration(setup, monkeypatch, model):
    import os
    from app.credits.accounts import get_account
    from app.pagebuilder import runtime
    from llm_gateway import sole
    d = setup
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("LLM_DEPLOYMENT_PRO", model)
    monkeypatch.setenv("LLM_DEPLOYMENT_FLASH", model)
    monkeypatch.setenv("LLM_REASONING_EFFORT", "low")
    started, payload = claimed(d)
    quote = json.loads(runtime.read(started["job_id"]).payload_json)["_runtime"]["quote"]
    assert (quote["provider"], quote["model"]) == ("openai_compatible", model)
    assert quote["reasoning_policy"] == {"planning": None, "reviewer": None, "generation": None}
    assert quote["max_output_tokens"] == 16384 and quote["call_timeout_seconds"] == 300
    if model == "gpt-6-luna":
        assert quote["ceiling_micro"] == 99920
    configs = []
    async def transmit(**kwargs):
        assert kwargs["single_attempt"] is True and kwargs["use_workspace_override"] is False
        assert kwargs["tier"] == "pro" and kwargs["max_tokens"] == 16384
        assert kwargs["config"].api_key.get_secret_value() == os.environ["LLM_API_KEY"]
        assert kwargs["config"].provider == "openai_compatible" and kwargs["config"].deployment_pro == model
        assert kwargs["config"].base_url == "https://api.openai.com/v1" and kwargs["config"].source == "platform_default"
        expected_timeout = 180 if kwargs["purpose"].startswith("pagebuilder.repair_") else 300
        assert kwargs["config"].timeout_seconds == expected_timeout and get_account(d["vid"]).reserved_micro > 0
        configs.append(kwargs["config"])
        return sole._mark_provider(response(), "foundry")
    monkeypatch.setattr(sole, "transmit", transmit)
    call = runtime.job_model_call(started["job_id"], payload)
    for stage in ("brief_direction_plan", "capability_review_1", "code", "repair_1", "repair_2"):
        assert call(stage, "Generate the page") == "model-produced-content"
        operation = runtime.journal.operation(started["job_id"], "variant_0:" + stage)
        assert operation["state"] == "completed" and operation["usage"]["settled"]
        usage = operation["usage"]
        assert (usage["provider"], usage["model"], usage["provider_identity"], usage["account_id"], usage["source"]) == (
            quote["provider"], quote["model"], quote["provider_identity"], quote["account_id"], "platform")
    assert all(config.reasoning_effort == "low" for config in configs)
    assert len(configs) == 5 and get_account(d["vid"]).reserved_micro == 0
    from app.core.jobs import FatalError
    spent = get_account(d["vid"]).spent_micro
    monkeypatch.setenv("LLM_BASE_URL", "https://changed.example.invalid")
    with pytest.raises(FatalError, match="pagebuilder_resources_changed"):
        call("plan_repair_1", "The captured endpoint cannot change")
    assert len(configs) == 5 and get_account(d["vid"]).spent_micro == spent and get_account(d["vid"]).reserved_micro == 0


@pytest.fixture(params=[
    ("An inspection checklist for machine repair crews.", "machine repair crews"),
    ("A scrapbook for families recording weekend hikes.", "families recording weekend hikes"),
])
def edit_domain(setup, request):
    from app.db.models import Venture
    from app.db.supymem_models import IdeaRevision
    with setup["factory"]() as session:
        session.get(Venture, setup["vid"]).raw_idea = request.param[0]
        setup["rid"] = append_idea_revision(session, setup["rid"], summary=request.param[0],
            structured=json.dumps({"audience": request.param[1]})).id
        session.commit()
    return setup


def ready_edit_source(d, monkeypatch):
    from app.builder.artifact_store import S3ArtifactStore
    from app.db.models import Job
    from app.pagebuilder import jobs, runtime

    from tests.unit.pagebuilder.test_native_reference import Store
    monkeypatch.setattr(S3ArtifactStore, "configured", lambda: Store())
    monkeypatch.setattr("app.chat.site_actions.page_built", lambda _: None)
    install_pipeline_stub(monkeypatch)
    started, payload = claimed(d)
    jobs.run(payload)
    with d["factory"]() as session:
        session.get(Job, started["job_id"]).status = "succeeded"
        session.commit()
    return started["job_id"], json.loads(runtime.read(started["job_id"]).payload_json)


def claim_edit(d, source):
    from app.core.jobs import lease
    from app.pagebuilder import api
    edited = api.rewrite(d["vid"], d["actor"], new_version_of=source,
                         instruction='Change the headline to "Keep your work organized", keep the design.')
    with d["factory"]() as session:
        job = lease(session, "edit-worker", kinds=[api.KIND], n=1)[0]
        session.commit()
    return edited["job_id"], {**job.payload, "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts}


@pytest.mark.parametrize("outcome", ["unchanged", "failed", "uncertain"])
def test_unsuccessful_edit_refunds_settled_calls_without_delivering_a_version(edit_domain, monkeypatch, outcome):
    from app.core.jobs import FatalError
    from app.credits.accounts import get_account
    from app.pagebuilder import api, jobs, pipeline, runtime
    d = edit_domain
    source, original = ready_edit_source(d, monkeypatch)
    if outcome == "uncertain":
        from llm_gateway import sole
        from llm_gateway.errors import ProviderCallError
        transmit = sole.transmit
        async def unknown(**kwargs):
            if kwargs["purpose"] == "pagebuilder.code":
                raise ProviderCallError("provider_error", "Synthetic unknown outcome", status=None)
            return await transmit(**kwargs)
        monkeypatch.setattr(sole, "transmit", unknown)
    account = get_account(d["vid"])
    available = account.granted_micro - account.spent_micro
    generate = pipeline.generate_page
    def unsuccessful(inputs, call, **kwargs):
        def unchanged(stage, prompt):
            value = call(stage, prompt)
            return "model-produced-content" if stage == "brief_direction_plan" else value
        page = generate(inputs, unchanged, **kwargs)
        if outcome == "failed":
            raise FatalError("pagebuilder_generation_failed")
        return page
    monkeypatch.setattr(pipeline, "generate_page", unsuccessful)
    edited, payload = claim_edit(d, source)
    try:
        jobs.run(payload)
    except FatalError:
        pass
    doc = json.loads(runtime.read(edited).payload_json)
    assert doc["_runtime"]["state"] == "failed"
    assert doc.get("result", {}).get("ready_delivered") is not True
    assert api._view(runtime.read(edited))["state"] == "failed"
    account = get_account(d["vid"])
    assert account.granted_micro - account.spent_micro == available
    assert (account.reserved_micro > 0) is (outcome == "uncertain")
    assert doc["_runtime"]["calls_used"] == 3
    jobs._refund_failed_edit(edited, payload)
    account = get_account(d["vid"])
    assert account.granted_micro - account.spent_micro == available
    if outcome == "uncertain":
        from app.db.models import Job
        assert api._view(runtime.read(edited))["receipt"]["uncertain_outcomes"] == 1
        with d["factory"]() as session:
            session.get(Job, edited).status = "failed_dead"
            session.commit()
        with pytest.raises(HTTPException) as blocked:
            api.rewrite(d["vid"], d["actor"], new_version_of=source, instruction="Change the headline")
        assert blocked.value.detail["code"] == "pagebuilder_outcome_uncertain"
        with pytest.raises(FatalError, match="pagebuilder_lease_lost"):
            jobs._refund_failed_edit(edited, payload)
    else:
        assert all(op["usage"]["settled"] for op in doc["_runtime"]["operations"].values())
    assert api._view(runtime.read(edited))["receipt"]["credits_micro"] == 0
    assert json.loads(runtime.read(source).payload_json) == original


def test_generation_history_never_reads_another_venture_in_the_workspace(edit_domain, monkeypatch):
    from app.db.models import Job, Venture
    from app.pagebuilder import api, jobs, pipeline
    from core.clock import utcnow
    d = edit_domain
    source, original = ready_edit_source(d, monkeypatch)
    foreign = json.loads(json.dumps(original))
    foreign["result"]["bundles"][0]["direction"]["layout_archetype"] = "Private composition from another venture"
    with d["factory"]() as session:
        session.add(Venture(id="other-venture", workspace_id=d["wid"], name="Other concept", raw_idea="Private other idea",
                            stage="draft", autonomy_level="L1", created_at=utcnow(), updated_at=utcnow()))
        session.flush()
        session.add(Job(id="other-page", kind=api.KIND, workspace_id=d["wid"], venture_id="other-venture",
                        status="succeeded", payload_json=json.dumps(foreign), created_at=utcnow()))
        session.commit()
    captured = []
    generate = pipeline.generate_page
    def tracked(inputs, call, **kwargs):
        captured.extend(kwargs["recent_directions"])
        return generate(inputs, call, **kwargs)
    monkeypatch.setattr(pipeline, "generate_page", tracked)
    _, payload = claim_edit(d, source)
    jobs.run(payload)
    assert "Private composition from another venture" not in json.dumps(captured)
    history = jobs.recent_directions(d["wid"], venture_id=d["vid"])
    assert history and "Private composition from another venture" not in json.dumps(history)


def test_headline_edit_preserves_design_without_similarity_rejection(edit_domain, monkeypatch):
    import base64
    from html import escape

    from app.api.waitlist_api import public_waitlist
    from app.conversion import waitlist
    from app.pagebuilder import api, jobs, pipeline, runtime
    d = edit_domain
    source, original = ready_edit_source(d, monkeypatch)
    generate = pipeline.generate_page
    def validated(inputs, call, **kwargs):
        page = generate(inputs, call, **kwargs)
        pipeline._validate_direction(page.direction, kwargs["recent_directions"])
        return page
    monkeypatch.setattr(pipeline, "generate_page", validated)
    edited, payload = claim_edit(d, source)
    instruction = 'Change the headline to "Keep your work organized", keep the design.'
    assert json.loads(runtime.read(edited).payload_json)["rewrite"] == {"source_job_id": source, "instruction": instruction}
    assert api.rewrite(d["vid"], d["actor"], new_version_of=source, instruction=instruction)["job_id"] == edited
    with pytest.raises(HTTPException) as conflict:
        api.rewrite(d["vid"], d["actor"], new_version_of=source, instruction="Use a different headline.")
    assert conflict.value.status_code == 409
    jobs.run(payload)
    assert api.result(d["vid"], d["actor"], edited)["state"] == "ready"
    saved = json.loads(runtime.read(edited).payload_json)["result"]["bundles"][0]
    prior = original["result"]["bundles"][0]
    assert saved["direction"] == prior["direction"] and saved["plan"]["assets"] == prior["plan"]["assets"]
    assert "<h1>Keep your work organized</h1>" in base64.b64decode(saved["files"]["index.html"]).decode()
    details = waitlist.public_details(d["vid"])
    for bundle in (prior, saved):
        assert details["url"] in base64.b64decode(bundle["files"]["index.html"]).decode()
    form = public_waitlist(d["vid"])
    assert form.status_code == 200
    assert '<form method="post"' in form.body.decode()
    assert '<input type="checkbox" name="consent" value="yes" required>' in form.body.decode()
    assert escape(details["consent"]) in form.body.decode()
    assert details["consent"] == original["_runtime"]["input"]["waitlist_consent_text"]


def install_pipeline_stub(monkeypatch, *, review_stage="capability_review_1"):
    """Only page/model/render seams are stubbed; jobs, credits and artifact checks run."""
    from app.pagebuilder import pipeline, rendering
    monkeypatch.setattr(rendering, "require_renderer", lambda: None)
    from app.pagebuilder.models import CapabilityReview
    from tests.unit.pagebuilder.test_pipeline import fixture_plan
    from llm_gateway import sole
    async def transmit(**kwargs):
        if kwargs["purpose"].startswith("pagebuilder.capability_review_"):
            prompt = kwargs["messages"][-1]["content"]
            facts = json.loads(prompt.split("CONFIRMED_FACTS:\n", 1)[1].split("\nPUBLIC_COPY_BY_FIELD:\n", 1)[0])
            public = json.loads(prompt.split("PUBLIC_COPY_BY_FIELD:\n", 1)[1].split("\nSANITIZED_SVG_BY_ASSET:\n", 1)[0])
            artwork = json.loads(prompt.split("SANITIZED_SVG_BY_ASSET:\n", 1)[1].split("\nVERIFIED_IMAGE_ORIGIN_BY_ALT_FIELD:\n", 1)[0])
            fields = []
            for field in public:
                editorial = field == "brief.cta" or field.startswith("sections[2].")
                quotes = (re.findall(r"<[^>]+>", artwork[field.removesuffix(".alt")])[:32]
                    if field.endswith(".alt") else [facts["idea"]])
                fields.append({"field_id": field, "classification": "editorial" if editorial else "supported",
                    "reason": "This is a neutral signup invitation." if editorial else "The exact supplied source supports this concept or geometry.",
                    "source_quotes": [] if editorial else quotes})
            return response(json.dumps({"approved": True, "field_reviews": fields, "unsupported_claims": []}))
        if kwargs["purpose"] == "pagebuilder.brief_direction_plan" and "COPY EDIT REQUEST:\n" in kwargs["messages"][-1]["content"]:
            prompt = kwargs["messages"][-1]["content"]
            instruction = json.loads(prompt.split("COPY EDIT REQUEST:\n", 1)[1].split("\nPREVIOUS REVIEWED PLAN:\n", 1)[0])
            return response(re.search(r'headline to "([^"]+)"', instruction)[1])
        return response()
    monkeypatch.setattr(sole, "transmit", transmit)
    def generate(inputs, call, **kwargs):
        data = fixture_plan()
        planned = call("brief_direction_plan", "Design this venture")
        data["brief"].update(promise=inputs.idea, concept_disclosure="A concept for a shift handoff board.",
            proof_points=["A shift handoff board is the concept.", "Warehouse supervisors are the intended audience.", "The idea concerns shift handoffs."])
        data["sections"][0].update(headline="Explore a shift handoff concept", body=inputs.idea)
        data["sections"][1].update(headline="For warehouse supervisors", body="The concept is a shift handoff board for warehouse supervisors.")
        data["sections"][2].update(headline="Follow early access", body="Review the consent form and leave your details.")
        data["assets"] = [{**{key: asset[key] for key in ("id", "role", "section_id", "subject", "prompt")},
            "scene": {"objects": [{"kind": "note_board" if index == 0 else "filmstrip", "x": 600, "y": 450,
                "scale": 1.4, "rotation": index * 8}]}}
            for index, asset in enumerate(data["assets"])]
        if planned != "model-produced-content":
            data["sections"][0]["headline"] = planned
        plan = pipeline.compile_draft_plan(inputs, data)
        review = CapabilityReview.model_validate(json.loads(call(review_stage, pipeline._grounding_prompt(plan, inputs)))).model_dump(mode="json")
        call("code", "Generate this page")
        images = "".join(f'<img src="{asset.path}" alt="{asset.alt}">' for asset in plan.assets)
        headline = inputs.name + " concept" if planned == "model-produced-content" else plan.sections[0].headline
        return SimpleNamespace(files={"index.html": '<!doctype html><html><head><title>Concept</title></head><body><main><h1>' + headline + '</h1>' + images + '<a href="' + inputs.waitlist_url + '">Join the waitlist</a></main></body></html>',
            **{asset.path: asset.svg for asset in plan.assets}},
            direction=plan.direction,
            plan=plan, manifest={"qa_contract": {}, "image_provenance": "model_scene_native_svg",
                "image_compiler_version": "native_scene_v2"}, qa={"passed": True,
                "render": {"passed": True, "browser_rendered": True, "failures": [], "observations": {
                    viewport: {"checks": {check: True for check in ("contrast", "image_clipping", "hero_cta_initial")},
                        "contrastSkipped": 0, "contrastUnresolved": [], "imageClippingFailures": [],
                        "initialHeroCta": {"visible": True, "withinViewport": True, "hitCoverage": True}}
                    for viewport in ("desktop", "mobile")}},
                "capability_grounding": {"policy": "semantic_capability_v4", "passed": True,
                    "plan_sha256": pipeline.plan_digest(plan), "scope": "generation_only", "independent_qa_attestation": False,
                    "reviews": [{"stage": review_stage, "plan_sha256": pipeline.plan_digest(plan),
                        "review_sha256": pipeline.review_digest(CapabilityReview.model_validate(review)), "review": review}]}},
            screenshots={"desktop.png": b"synthetic-desktop", "mobile.png": b"synthetic-mobile"})
    monkeypatch.setattr(pipeline, "generate_page", generate)
    monkeypatch.setattr(pipeline, "build_spec", lambda plan, inputs, **_: "# Approved Nightshift concept and original assets")


@pytest.mark.parametrize("review_stage", ["capability_review_3", "capability_review_4", "capability_review_5", "capability_review_6"])
def test_later_final_review_requires_actual_completed_settled_typed_response(setup, monkeypatch, review_stage):
    from app.builder.artifact_store import S3ArtifactStore
    from tests.unit.pagebuilder.test_native_reference import Store
    store = Store()
    monkeypatch.setattr(S3ArtifactStore, "configured", lambda: store)
    from app.pagebuilder import api, jobs, runtime
    d = setup
    install_pipeline_stub(monkeypatch, review_stage=review_stage)
    started, payload = claimed(d)
    jobs.run(payload)
    assert api.result(d["vid"], d["actor"], started["job_id"])["state"] == "ready"
    original = json.loads(runtime.read(started["job_id"]).payload_json)
    assert original["_runtime"]["quote"]["request_policy"] == "scoped_grounding_v19"
    assert api._candidate_current(original)
    for change in ("unfinished", "unsettled", "invalid_response", "incomplete_review", "invalid_stage"):
        altered = json.loads(json.dumps(original))
        operation = altered["_runtime"]["operations"]["variant_0:" + review_stage]
        if change == "unfinished":
            operation["state"] = "responded"
        elif change == "unsettled":
            operation["usage"]["settled"] = False
        elif change == "invalid_response":
            operation["response"]["content"] = '{"approved":true,"unsupported_claims":[]}'
        elif change == "invalid_stage":
            altered["result"]["bundles"][0]["qa"]["capability_grounding"]["reviews"][-1]["stage"] = "capability_review_7"
            altered["_runtime"]["operations"]["variant_0:capability_review_7"] = operation
        else:
            reviewed = altered["result"]["bundles"][0]["qa"]["capability_grounding"]["reviews"][-1]
            reviewed["review"]["field_reviews"].pop()
            reviewed["review_sha256"] = api.digest(reviewed["review"])
            operation["response"]["content"] = json.dumps(reviewed["review"])
        assert api._candidate_current(altered) is False


def test_durable_worker_result_source_and_sandbox_preview(setup, monkeypatch):
    from app.builder.artifact_store import S3ArtifactStore
    from tests.unit.pagebuilder.test_native_reference import Store
    store = Store()
    monkeypatch.setattr(S3ArtifactStore, "configured", lambda: store)
    import asyncio
    import base64
    import hashlib
    import io
    import zipfile
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.pagebuilder_api import router
    from app.api.waitlist_api import router as waitlist_router
    from app.core.jobs import lease
    from app.db.models import Job
    from app.pagebuilder import api
    from app.pagebuilder.worker import run_one
    d = setup
    install_pipeline_stub(monkeypatch)
    started = api.start(d["vid"], d["actor"])
    with d["factory"]() as session:
        job = lease(session, "durable-test-worker", kinds=[api.KIND], n=1)[0]
        session.commit()
    asyncio.run(run_one(d["factory"], job))
    view = api.result(d["vid"], d["actor"], started["job_id"])
    assert view["state"] == "ready" and view["progress"] == 1 and view["receipt"]["calls"] == 3
    assert view["receipt"]["credits_micro"] > 0 and view["publish_ready"] is False
    with d["factory"]() as session:
        assert session.get(Job, job.id).status == "succeeded"
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.include_router(waitlist_router)
    with TestClient(app) as client:
        headers = {"X-Subject": d["actor"]}
        prefix = f"/api/ventures/{d['vid']}/pagebuilder/jobs/{job.id}"
        preview = client.get(prefix + "/preview", headers=headers)
        assert preview.status_code == 200 and "data:image/svg+xml;base64," in preview.text
        assert 'http-equiv="Content-Security-Policy"' in preview.text
        assert "script-src 'none'" in preview.headers["content-security-policy"]
        assert client.get(prefix + "/source", headers={"X-Subject": "other-founder"}).status_code == 404
        source = client.get(prefix + "/source", headers=headers)
        assert source.status_code == 200
        capture = view["manifest"]["artifact_capture"]
        assert capture["manifest"] == view["manifest"]["manifest"]
        assert capture["manifest_digest"] == view["manifest"]["manifest_digest"]
        assert hashlib.sha256(source.content).hexdigest() == capture["sha256"]
        with zipfile.ZipFile(io.BytesIO(source.content)) as archive:
            assert set(archive.namelist()) == {"index.html", "assets/garden_hero.svg", "assets/planting_art.svg"}
            html = archive.read("index.html").decode()
            assert html.count('data-supymem-analytics="1"') == 1
            from app.apps.keys import ensure_ingest_key
            from app.infra.apps import get_app
            from app.product_analytics.beacon import script_tag
            beacon = view["manifest"]["analytics_beacon"]
            app_binding = get_app(beacon["app_id"])
            assert app_binding["venture_id"] == d["vid"] and app_binding["workspace_id"] == d["wid"]
            tag = script_tag(key=ensure_ingest_key(beacon["app_id"])["secret"], endpoint=beacon["endpoint"])
            assert tag in html and hashlib.sha256(tag.encode()).hexdigest() == beacon["script_sha256"]
            from app.pagebuilder import api
            assert beacon["script_sha256"] in api.build_spec_for_coding_tool(d["vid"], d["actor"])
            index = next(item for item in view["manifest"]["manifest"] if item["path"] == "index.html")
            assert index["sha256"] == hashlib.sha256(archive.read("index.html")).hexdigest()
        assert client.get(prefix + "/assets/../../private", headers=headers).status_code == 404
        assert client.get(prefix + "/preview?variant=1", headers=headers).status_code == 404
        # Exercise the existing real consent backend; test rows trigger no outreach.
        from app.conversion import waitlist
        from app.db.supymem_models import VentureWaitlist, VentureWaitlistEntry
        from core.clock import utcnow
        from datetime import timedelta
        details = waitlist.public_details(d["vid"])
        assert client.get("/public/waitlists/" + d["vid"]).status_code == 200
        token = "synthetic-pagebuilder-consent-probe"
        with d["factory"]() as session:
            row = session.get(VentureWaitlist, d["vid"])
            row.probe_hash = hashlib.sha256(token.encode()).hexdigest()
            row.probe_expires_at = utcnow() + timedelta(seconds=60)
            session.commit()
        consent = client.post("/public/waitlists/" + d["vid"], json={"email": "builder-check@qa.invalid", "consent": "yes",
            "consent_version": details["consent_version"], "consent_digest": hashlib.sha256(details["consent"].encode()).hexdigest(),
            "test_token": token}, headers={"Accept": "application/json"})
        assert consent.status_code == 201 and consent.json()["received"] is True
        with d["factory"]() as session:
            from sqlalchemy import select
            entry = session.scalar(select(VentureWaitlistEntry).where(VentureWaitlistEntry.venture_id == d["vid"]))
            assert entry.is_test and entry.consent_text == details["consent"] and entry.workspace_id == d["wid"]
        # Historical policy, changed plan, and unsettled review cannot expose a
        # candidate through any download/preview/spec seam or mutate its history.
        with d["factory"]() as session:
            original = json.loads(session.get(Job, job.id).payload_json)
        for change in ("old_policy", "old_review_policy", "changed_call_budget", "changed_deadline", "changed_plan", "unsettled_review", "changed_reasoning", "changed_planning", "changed_response", "incomplete_review", "forged_source_quote", "legacy_images", "changed_scene", "changed_palette", "changed_scene_path", "changed_asset_file", "changed_zip_asset", "missing_render", "missing_mobile_render", "skipped_contrast", "unresolved_contrast", "clipped_image", "below_fold_cta", "covered_cta", "failed_render_check"):
            altered = json.loads(json.dumps(original))
            if change == "missing_render":
                del altered["result"]["bundles"][0]["qa"]["render"]
            elif change == "missing_mobile_render":
                del altered["result"]["bundles"][0]["qa"]["render"]["observations"]["mobile"]
            elif change in {"skipped_contrast", "unresolved_contrast", "clipped_image", "below_fold_cta", "covered_cta", "failed_render_check"}:
                observation = altered["result"]["bundles"][0]["qa"]["render"]["observations"]["mobile"]
                if change == "skipped_contrast":
                    observation["contrastSkipped"] = 15
                elif change == "unresolved_contrast":
                    observation["contrastUnresolved"] = [{"id": "concept", "reason": "background_image"}]
                elif change == "clipped_image":
                    observation["imageClippingFailures"] = [{"id": "hero_art"}]
                elif change == "below_fold_cta":
                    observation["initialHeroCta"]["withinViewport"] = False
                elif change == "covered_cta":
                    observation["initialHeroCta"]["hitCoverage"] = False
                else:
                    observation["checks"]["hero_cta_initial"] = False
            elif change == "old_policy":
                altered["_runtime"]["quote"]["request_policy"] = "scoped_grounding_v18"
            elif change == "old_review_policy":
                altered["result"]["bundles"][0]["qa"]["capability_grounding"]["policy"] = "semantic_capability_v3"
            elif change == "changed_call_budget":
                altered["_runtime"]["quote"]["call_budget"] = 8
            elif change == "changed_deadline":
                altered["_runtime"]["quote"]["call_timeout_seconds"] = 180
            elif change == "changed_plan":
                altered["result"]["bundles"][0]["plan"]["brief"]["promise"] = "Invented role-based permission controls."
            elif change == "unsettled_review":
                altered["_runtime"]["operations"]["variant_0:capability_review_1"]["usage"]["settled"] = False
            elif change in {"changed_reasoning", "changed_planning"}:
                altered["_runtime"]["quote"]["reasoning_policy"]["planning" if change == "changed_planning" else "reviewer"] = "high" if change == "changed_planning" else "none"
            elif change == "changed_response":
                altered["_runtime"]["operations"]["variant_0:capability_review_1"]["response"]["content"] = '{"approved":true,"unsupported_claims":[]}'
            elif change == "legacy_images":
                altered["result"]["bundles"][0]["manifest"]["image_provenance"] = "original_model_svg"
            elif change in {"changed_scene", "changed_palette", "changed_scene_path"}:
                from app.pagebuilder.pipeline import plan_digest
                bundle = altered["result"]["bundles"][0]
                if change == "changed_scene":
                    bundle["plan"]["assets"][0]["scene"]["objects"][0]["scale"] = 1.3
                elif change == "changed_palette":
                    bundle["plan"]["direction"]["palette"]["accent"] = "#000000"
                else:
                    bundle["plan"]["assets"][0]["path"] = "assets/unbound.svg"
                revised_digest = plan_digest(bundle["plan"])
                bundle["qa"]["capability_grounding"]["plan_sha256"] = revised_digest
                bundle["qa"]["capability_grounding"]["reviews"][-1]["plan_sha256"] = revised_digest
            elif change in {"changed_asset_file", "changed_zip_asset"}:
                bundle = altered["result"]["bundles"][0]
                asset = bundle["plan"]["assets"][0]
                changed_svg = asset["svg"].replace('stroke-width="8"', 'stroke-width="7"', 1).encode()
                assert changed_svg != asset["svg"].encode()
                capture = bundle["manifest"]["artifact_capture"]
                if change == "changed_asset_file":
                    bundle["files"][asset["path"]] = base64.b64encode(changed_svg).decode()
                    entry = next(item for item in capture["manifest"] if item["path"] == asset["path"])
                    entry.update(sha256=hashlib.sha256(changed_svg).hexdigest(), size_bytes=len(changed_svg))
                else:
                    output = io.BytesIO()
                    with zipfile.ZipFile(io.BytesIO(base64.b64decode(bundle["source_zip"]))) as source, zipfile.ZipFile(output, "w") as changed:
                        for name in source.namelist():
                            changed.writestr(name, changed_svg if name == asset["path"] else source.read(name))
                    body = output.getvalue()
                    bundle["source_zip"] = base64.b64encode(body).decode()
                    capture.update(sha256=hashlib.sha256(body).hexdigest(), size_bytes=len(body))
            else:
                reviewed = altered["result"]["bundles"][0]["qa"]["capability_grounding"]["reviews"][-1]
                if change == "incomplete_review":
                    reviewed["review"]["field_reviews"].pop()
                else:
                    reviewed["review"]["field_reviews"][0]["source_quotes"] = ["Invented authoritative founder quotation."]
                reviewed["review_sha256"] = api.digest(reviewed["review"])
                altered["_runtime"]["operations"]["variant_0:capability_review_1"]["response"]["content"] = json.dumps(reviewed["review"])
            frozen = json.dumps(altered)
            with d["factory"]() as session:
                session.get(Job, job.id).payload_json = frozen
                session.commit()
            for suffix in ("/result", "/preview", "/source", "/assets/garden_hero.svg", "/screenshots/desktop.png"):
                blocked = client.get(prefix + suffix, headers=headers)
                assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "pagebuilder_candidate_policy_obsolete"
            blocked_spec = client.get(f"/api/ventures/{d['vid']}/pagebuilder/spec", params={"job_id": job.id}, headers=headers)
            assert blocked_spec.status_code == 409 and blocked_spec.json()["detail"]["code"] == "pagebuilder_candidate_policy_obsolete"
            with pytest.raises(HTTPException, match="pagebuilder_candidate_policy_obsolete"):
                api.build_spec_for_coding_tool(d["vid"], d["actor"])
            assert api.status(d["vid"], d["actor"], job.id)["error"] == "pagebuilder_candidate_policy_obsolete"
            with d["factory"]() as session:
                assert session.get(Job, job.id).payload_json == frozen


    from app.pagebuilder import runtime
    with d["factory"]() as session:
        session.get(Job, job.id).payload_json = json.dumps(original)
        session.commit()
    prompts = []
    original_factory = runtime.job_model_call
    def tracked_call(*args, **kwargs):
        call = original_factory(*args, **kwargs)
        def tracked(stage, prompt):
            prompts.append((stage, prompt))
            return call(stage, prompt)
        return tracked
    monkeypatch.setattr(runtime, "job_model_call", tracked_call)
    instruction = 'Change the headline to "Keep your work organized".'
    edited = api.rewrite(d["vid"], d["actor"], new_version_of=job.id, instruction=instruction)
    with d["factory"]() as session:
        editing = lease(session, "rewrite-test-worker", kinds=[api.KIND], n=1)[0]
        session.commit()
    asyncio.run(run_one(d["factory"], editing))
    assert api.result(d["vid"], d["actor"], edited["job_id"])["state"] == "ready"
    assert json.dumps(instruction) in next(prompt for stage, prompt in prompts if stage == "brief_direction_plan")
    assert all("COPY EDIT REQUEST:" not in prompt for stage, prompt in prompts if stage.startswith("capability_review_"))
    with d["factory"]() as session:
        unchanged = json.loads(session.get(Job, job.id).payload_json)
        saved = json.loads(session.get(Job, edited["job_id"]).payload_json)
    assert unchanged == original
    assert saved["_runtime"]["input"] == original["_runtime"]["input"]
    saved_html = base64.b64decode(saved["result"]["bundles"][0]["files"]["index.html"]).decode()
    assert "<h1>Keep your work organized</h1>" in saved_html
    assert waitlist.public_details(d["vid"])["url"] in saved_html
    assert waitlist.public_details(d["vid"])["consent"] == details["consent"]


def test_explicit_disabled_waitlist_stops_queue_and_calls_without_reenable(setup, monkeypatch):
    from app.conversion import waitlist
    from app.core.jobs import FatalError
    from app.credits.accounts import get_account
    from app.pagebuilder import api, jobs, runtime
    from llm_gateway import sole
    d = setup
    started, payload = claimed(d)
    waitlist.configure(d["vid"], d["wid"], enabled=False, actor=d["actor"])
    quote = api.estimate(d["vid"], d["actor"])
    assert quote["ready"] is False and quote["reason"] == "pagebuilder_waitlist_disabled"
    with pytest.raises(HTTPException, match="pagebuilder_waitlist_disabled"):
        api.start(d["vid"], d["actor"])
    with pytest.raises(FatalError, match="pagebuilder_waitlist_disabled"):
        jobs.ensure_waitlist(started["job_id"], payload)
    async def forbidden(**kwargs):
        raise AssertionError("Provider must not be called")
    monkeypatch.setattr(sole, "transmit", forbidden)
    with pytest.raises(FatalError, match="pagebuilder_waitlist_disabled"):
        runtime.job_model_call(started["job_id"], payload)("code", "Generate this page")
    assert waitlist.status(d["vid"], d["wid"])["enabled"] is False
    account = get_account(d["vid"])
    assert account.spent_micro == 0 and account.reserved_micro == 0


def test_local_alias_uses_canonical_actor_for_public_app_bootstrap(setup, monkeypatch):
    from app.builder.artifact_store import S3ArtifactStore
    from tests.unit.pagebuilder.test_native_reference import Store
    store = Store()
    monkeypatch.setattr(S3ArtifactStore, "configured", lambda: store)
    import asyncio
    from app.core.jobs import lease
    from app.pagebuilder import api
    from app.pagebuilder.worker import run_one
    from app.tenancy import bind
    from app.workspaces.resolve import LOCAL_PERSON_ID
    d = seed(setup["factory"], actor=LOCAL_PERSON_ID)
    install_pipeline_stub(monkeypatch)
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor="founder"):
        started = api.start(d["vid"], "founder")
    with d["factory"]() as session:
        job = lease(session, "local-alias-worker", kinds=[api.KIND], n=1)[0]
        session.commit()
    assert job.payload["_actor_subject"] == LOCAL_PERSON_ID
    asyncio.run(run_one(d["factory"], job))
    assert api.result(d["vid"], "founder", started["job_id"])["state"] == "ready"


def test_owned_failure_code_is_safe_and_offline_quote_is_unavailable(setup, monkeypatch):
    from app.pagebuilder import api, runtime
    d = setup
    started, payload = claimed(d)
    runtime.fail(started["job_id"], payload, ValueError("pagebuilder_qa_failed:untrusted source detail"))
    status = api.status(d["vid"], d["actor"], started["job_id"])
    assert status["error"] == "pagebuilder_qa_failed" and "untrusted" not in api.dump(status)
    monkeypatch.setenv("LLM_OFFLINE", "1")
    assert api.estimate(d["vid"], d["actor"])["ready"] is False


def test_deepseek_byok_uses_reviewed_gateway_with_captured_phase_policy(setup, monkeypatch):
    from app.llm import founder_provider
    from app.pagebuilder import api
    d = setup
    for provider in ("deepseek_direct", "azure_foundry"):
        config = founder_provider.ResolvedLLMConfig(workspace_id=d["wid"], provider=provider,
            base_url="https://provider.example.invalid", model="DeepSeek-V4-Pro",
            api_key_ref="synthetic-key-ref", source="founder_override")
        monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda _: config)
        quote = api.estimate(d["vid"], d["actor"])
        assert quote["ready"] is True and quote["payer"] == "founder"
        captured = api.resources(d["wid"], d["vid"])
        assert captured["reasoning_policy"] == {"planning": "low", "reviewer": "low", "generation": "none"}
        assert captured["call_timeout_seconds"] == 300 and captured["max_output_tokens"] == 16384


def test_storage_retry_after_ready_never_repeats_paid_generation(setup, monkeypatch):
    from app.core.jobs import RetryableError
    from app.credits.accounts import get_account
    from app.pagebuilder import jobs, native_reference, pipeline, runtime
    d = setup
    install_pipeline_stub(monkeypatch)
    started, payload = claimed(d)
    attempts = []
    def persist(job_id, ownership):
        saved = json.loads(runtime.read(job_id).payload_json)
        assert saved["_runtime"]["state"] == "ready" and saved["result"]["publish_ready"] is False
        attempts.append(saved)
        if len(attempts) == 1:
            raise RetryableError("pagebuilder_reference_storage_uncertain")
        return []
    monkeypatch.setattr(native_reference, "persist_job", persist)
    with pytest.raises(RetryableError, match="pagebuilder_reference_storage_uncertain"):
        jobs.run(payload)
    before = json.loads(runtime.read(started["job_id"]).payload_json)
    spend = get_account(d["vid"]).spent_micro
    assert before["_runtime"]["calls_used"] == 3 and spend > 0
    monkeypatch.setattr(pipeline, "generate_page", lambda *_args, **_kwargs: pytest.fail("saved ready source must never regenerate"))
    jobs.run(payload)
    after = json.loads(runtime.read(started["job_id"]).payload_json)
    assert after == before and len(attempts) == 2
    assert get_account(d["vid"]).spent_micro == spend


def test_missing_worker_renderer_stops_before_any_model_charge(setup, monkeypatch):
    from app.core.jobs import FatalError
    from app.credits.accounts import get_account
    from app.pagebuilder import jobs, rendering, runtime
    from llm_gateway import sole
    d = setup
    started, payload = claimed(d)
    from app.db.models import Job
    # Exhausting the bounded native startup retries records a closed fatal reason.
    with d["factory"]() as session:
        session.get(Job, started["job_id"]).attempts = 3
        session.commit()
    payload["_job_attempt"] = 3
    def unavailable():
        raise ValueError("pagebuilder_renderer_unavailable")
    monkeypatch.setattr(rendering, "require_renderer", unavailable)
    monkeypatch.setattr(sole, "transmit", lambda **_kwargs: pytest.fail("renderer must be verified before paid dispatch"))
    with pytest.raises(FatalError, match="pagebuilder_renderer_unavailable"):
        jobs.run(payload)
    saved = json.loads(runtime.read(started["job_id"]).payload_json)["_runtime"]
    account = get_account(d["vid"])
    assert saved["state"] == "failed" and saved["error"] == "pagebuilder_renderer_unavailable"
    assert saved.get("calls_used", 0) == 0 and account.spent_micro == account.reserved_micro == 0


def test_profile_counts_failed_builds_for_current_revision_and_resets_on_new_work(setup):
    from app.api.pagebuilder_api import profile
    from app.db.models import Job
    from app.pagebuilder import api
    d = setup
    for expected in (1, 2):
        started = api.build(d["vid"], d["actor"])
        with d["factory"]() as session:
            row = session.get(Job, started["job_id"])
            doc = json.loads(row.payload_json)
            doc["_runtime"].update(state="failed", error="pagebuilder_qa_failed", calls_used=1,
                operations={"variant_0:code": {"state": "completed", "usage": {"credits_micro": 5}}})
            row.payload_json, row.status = json.dumps(doc), "failed_dead"
            session.commit()
        current = profile(d["vid"], d["actor"])["current"]
        assert current["state"] == "failed" and current["consecutive_failures"] == expected
    api.build(d["vid"], d["actor"])
    assert profile(d["vid"], d["actor"])["current"]["consecutive_failures"] == 0


def test_native_preview_document_is_size_bounded(setup, monkeypatch):
    from app.api import pagebuilder_api
    from fastapi.responses import HTMLResponse
    monkeypatch.setattr(pagebuilder_api, "preview", lambda *_args: HTMLResponse("x" * (pagebuilder_api.PREVIEW_DOCUMENT_MAX_BYTES + 1)))
    with pytest.raises(HTTPException) as bounded:
        pagebuilder_api.preview_document(setup["vid"], "job-test", 0, setup["actor"])
    assert bounded.value.status_code == 409 and bounded.value.detail["code"] == "pagebuilder_preview_too_large"


@pytest.mark.parametrize("domain", ["industrial-inspection", "home-gardening"])
def test_native_preview_uses_preview_policy_and_embedded_assets(monkeypatch, domain):
    import base64
    import hashlib

    def svg(shape):
        return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 900" role="img">{shape}</svg>'.encode()

    hero = svg('<path d="M20 450h1160" stroke="navy"/>')
    detail = svg('<circle id="detail" cx="600" cy="450" r="160"/>')
    hero_path, detail_path = ("assets/blade_view.svg", "assets/inspection_lines.svg") if domain == "industrial-inspection" else (
        "assets/garden view.svg", "assets/seedlings.svg")

    def uri(body):
        return "data:image/svg+xml;base64," + base64.b64encode(body).decode()

    if domain == "industrial-inspection":
        alt = "A wind-turbine blade silhouette beside concentric ultrasound waves"
        head = '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src \'self\'">'
        body = f'<img id="hero" src="{hero_path}" alt="{alt}" width="1200" height="900"><img src="{detail_path}">'
    else:
        alt = "Seed trays on a kitchen windowsill"
        head = (f'<link rel=icon href=./{detail_path}>'
                f'<meta property="og:image" content="{detail_path}">'
                f'<style>.hero {{ background: u\\72l( "{detail_path}#detail" ); }}</style>')
        body = (f'<picture><source id="wide" srcset="{detail_path} 2x, assets/missing.svg 3x">'
                f'<img id="hero" src=assets/garden%20view.svg?v=2 alt="{alt}" '
                f'srcset="./assets/garden%20view.svg 1x, {uri(detail)} 2x" '
                f'style="background: url(\'{detail_path}\')"></picture>')

    before_head = '<!-- <head> -->' if domain == "home-gardening" else ""
    html = f'<!doctype html>{before_head}<html><head>{head}</head><body>{body}</body></html>'
    files = {"index.html": html.encode(), hero_path: hero, detail_path: detail}
    bundle = {"files": {path: base64.b64encode(body).decode() for path, body in files.items()},
        "manifest": {"artifact_capture": {"manifest": [{"path": path, "sha256": hashlib.sha256(body).hexdigest(),
            "size_bytes": len(body)} for path, body in files.items()]}}}
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    with service_modules("company_os"):
        from app.api import pagebuilder_api
        from app.pagebuilder.qa import PageParser

        monkeypatch.setattr(pagebuilder_api, "bundle", lambda *_args: bundle)
        document = pagebuilder_api.preview_document("venture", "result", 0, "owner")["html"]
    parser = PageParser()
    parser.feed(document)
    image = next(attrs for tag, attrs, _ in parser.elements if tag == "img" and attrs.get("id") == "hero")
    assert image["src"] == uri(hero) and image["alt"] == alt
    policies = [attrs["content"] for tag, attrs, _ in parser.elements if tag == "meta"
                and (attrs.get("http-equiv") or "").lower() == "content-security-policy"]
    assert len(policies) == 1
    assert all(directive in policies[0] for directive in ("img-src data:", "script-src 'none'", "connect-src 'none'",
        "base-uri 'none'", "form-action 'none'"))
    if domain == "industrial-inspection":
        assert [attrs["src"] for tag, attrs, _ in parser.elements if tag == "img"] == [uri(hero), uri(detail)]
    else:
        assert image["srcset"] == f"{uri(hero)} 1x, {uri(detail)} 2x"
        assert uri(detail) in image["style"] and uri(detail) + "#detail" in parser.styles[0]
        assert next(attrs for tag, attrs, _ in parser.elements if tag == "source")["srcset"] == f"{uri(detail)} 2x"
        assert next(attrs for tag, attrs, _ in parser.elements if tag == "link")["href"] == uri(detail)
        assert "assets/" not in document


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
def test_generated_preview_cta_reaches_a_persisted_waitlist(request, tmp_path, monkeypatch, domain):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    import base64
    import hashlib
    import os
    import secrets
    from datetime import timedelta
    from urllib.parse import urljoin, urlsplit

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select
    from sqlalchemy.orm import sessionmaker
    from tests.unit.pagebuilder.test_pipeline import fixture_draft, fixture_input, nightshift_draft

    db_url, engine = (request.getfixturevalue("pg_database") if os.environ.get("TEST_POSTGRES_URL")
        else ("sqlite:///" + str(tmp_path / "preview.db"), None))
    for key, value in {"COMPANY_OS_DB_URL": db_url, "COMPANY_OS_DATA_DIR": str(tmp_path),
        "COMPANY_OS_WORKING_MEMORY_PATH": str(tmp_path / "memory.db"),
        "SUPYMEM_PAGEBUILDER_ENABLED": "1", "PUBLIC_GATEWAY_URL": "https://gateway.supymem.com",
        "APP_ENV": "local", "COMPANY_OS_REQUIRE_SIGNED_CALLER": "0", "LLM_OFFLINE": "1",
        "MARKETING_EXECUTION_ENABLED": "0", "MARKETING_OUTBOUND_ENABLED": "0", "CHANNELS_DRY_RUN": "true"}.items():
        monkeypatch.setenv(key, value)
    with service_modules("company_os"):
        from app.api import pagebuilder_api, waitlist_api
        from app.conversion import waitlist
        from app.credits.policy import WALLET_PHASE_KEY, person_wallet_enabled
        from app.db.models import CreditPolicy, Venture
        from app.db.supymem_models import VentureWaitlist, VentureWaitlistEntry
        from app.pagebuilder import api, models, pipeline, qa
        from app.state import db, set_factory
        from core.clock import utcnow
        from core.ids import new_id

        factory = sessionmaker(engine, expire_on_commit=False) if engine is not None else db()
        set_factory(factory)
        with factory() as session:
            session.merge(CreditPolicy(key=WALLET_PHASE_KEY, value_json='"person"', updated_at=utcnow()))
            session.commit()
            assert person_wallet_enabled(session)
        data = seed(factory, actor=new_id("usr"))
        raw_input, draft = nightshift_draft() if domain == "b2b" else (fixture_input(), fixture_draft())
        with factory() as session:
            venture = session.get(Venture, data["vid"])
            venture.name, venture.raw_idea = raw_input["name"], raw_input["idea"]
            session.commit()
        waitlist.configure(data["vid"], data["wid"], enabled=True, actor=data["actor"])
        inputs = models.PageInput.model_validate({**raw_input, "venture_id": data["vid"],
            "waitlist_url": waitlist.public_url(data["vid"])})
        plan = pipeline.compile_draft_plan(inputs, draft)
        published = pipeline.fallback_html(plan, inputs)
        preview_html = published.replace('href="#waitlist"', 'href=&#35;waitlist target="_blank"') if domain == "consumer" else published
        files = {"index.html": preview_html.encode(), **{asset.path: asset.svg.encode() for asset in plan.assets}}
        captured = {"files": {path: base64.b64encode(body).decode() for path, body in files.items()},
            "manifest": {"qa_contract": qa.qa_contract(inputs, plan).model_dump(mode="json"),
                "artifact_capture": {"manifest": [{"path": path, "sha256": hashlib.sha256(body).hexdigest(),
                    "size_bytes": len(body)} for path, body in files.items()]}}}

        def saved_bundle(venture_id, caller, job_id, variant):
            api.authority(venture_id, caller)
            assert (venture_id, job_id, variant) == (data["vid"], "saved-page", 0)
            return captured

        monkeypatch.setattr(pagebuilder_api, "bundle", saved_bundle)
        app = FastAPI()
        app.include_router(pagebuilder_api.router, prefix="/api")
        app.include_router(waitlist_api.router)
        prefix = f"/api/ventures/{data['vid']}/pagebuilder/jobs/saved-page"
        with TestClient(app) as client:
            document = client.get(prefix + "/preview-document", headers={"X-Subject": data["actor"]})
            assert document.status_code == 200
            parser = qa.PageParser()
            parser.feed(document.json()["html"])
            hero = next(attrs for tag, attrs, _ in parser.elements if tag == "a" and attrs.get("id") == "hero_cta")
            assert urljoin("https://console.supymem.com/v/" + data["vid"] + "/site", hero["href"]) == "about:srcdoc#waitlist"
            assert hero["target"] == "_self"
            assert any(attrs.get("id") == "waitlist" for _, attrs, _ in parser.elements)
            served = client.get(prefix + "/preview", headers={"X-Subject": data["actor"]})
            policy = served.headers["content-security-policy"]
            assert "sandbox allow-popups allow-popups-to-escape-sandbox;" in policy
            assert all(directive in policy for directive in ("script-src 'none'", "connect-src 'none'", "form-action 'none'", "base-uri 'none'"))
            assert all(permission not in policy for permission in ("allow-scripts", "allow-same-origin", "allow-forms", "allow-top-navigation"))
            assert client.get(prefix + "/preview-document", headers={"X-Subject": new_id("usr")}).status_code == 404
            for origin, markup in (("preview", document.json()["html"]), ("published", published)):
                parser = qa.PageParser()
                parser.feed(markup)
                cta = next(attrs for tag, attrs, _ in parser.elements if tag == "a" and attrs.get("id") == "waitlist_cta")
                assert cta["href"] == inputs.waitlist_url and cta["target"] == "_blank"
                assert {"noopener", "noreferrer"} <= set(cta["rel"].split())
                form_page = client.get(urlsplit(cta["href"]).path)
                assert form_page.status_code == 200
                parser = qa.PageParser()
                parser.feed(form_page.text)
                form = next(attrs for tag, attrs, _ in parser.elements if tag == "form")
                fields = {attrs["name"]: attrs.get("value", "") for tag, attrs, _ in parser.elements if tag == "input"}
                token = secrets.token_urlsafe()
                with factory() as session:
                    row = session.get(VentureWaitlist, data["vid"])
                    row.probe_hash = hashlib.sha256(token.encode()).hexdigest()
                    row.probe_expires_at = utcnow() + timedelta(minutes=1)
                    session.commit()
                fields.update(email=origin + "@qa.invalid", consent="", test_token=token)
                target = urlsplit(form["action"]).path
                assert client.post(target, data=fields).status_code == 422
                fields["consent"] = "yes"
                assert client.post(target, data=fields).status_code == 201
                with factory() as session:
                    entry = session.scalar(select(VentureWaitlistEntry).where(
                        VentureWaitlistEntry.venture_id == data["vid"], VentureWaitlistEntry.email == fields["email"]))
                    assert entry and entry.is_test and entry.workspace_id == data["wid"]
                    assert entry.consent_text == waitlist.consent_text(inputs.name)
            with factory() as session:
                assert session.scalar(select(func.count()).select_from(VentureWaitlistEntry).where(
                    VentureWaitlistEntry.venture_id == data["vid"])) == 2


@pytest.mark.parametrize("provider", ["azure_foundry", "deepseek_direct", "openai_compatible"])
def test_byok_quote_runtime_uses_captured_phase_policy_on_actual_sdk_wire(setup, monkeypatch, provider):
    from dataclasses import replace
    from openai import AsyncOpenAI, _base_client
    from pydantic import SecretStr
    from app.core.jobs import FatalError
    from app.credits.accounts import get_account
    from app.llm import founder_provider
    from app.pagebuilder import api, runtime
    from llm_gateway import sole
    from llm_gateway.provider_foundry import ProviderConfig
    httpx = getattr(_base_client, "httpx", None) or _base_client.httpx2
    d, requests, clients = setup, [], []
    resolved = founder_provider.ResolvedLLMConfig(workspace_id=d["wid"], provider=provider,
        base_url="https://offline.invalid/v1", model="DeepSeek-V4-Pro", api_key_ref="synthetic-key-ref", source="founder_override")
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda _: resolved)
    def captured_config(workspace_id, *, resolved_config):
        assert workspace_id == resolved_config.workspace_id == d["wid"]
        return ProviderConfig(provider=provider, base_url=resolved_config.base_url, api_key=SecretStr("offline"),
            deployment_pro=resolved_config.model, deployment_flash=resolved_config.model, source="founder_override", reasoning_effort="high")
    monkeypatch.setattr(founder_provider, "to_provider_config", captured_config)
    def open_client(config):
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"id": "offline", "object": "chat.completion", "model": config.deployment_pro,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "known offline output"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}})
        client = AsyncOpenAI(api_key="offline", base_url=config.base_url, timeout=config.timeout_seconds,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)), max_retries=2)
        clients.append(client)
        return client, config
    monkeypatch.setattr(sole, "create_async_client_from_config", open_client)
    monkeypatch.setattr(sole, "_primary_chat", None)
    monkeypatch.setattr(sole, "_fallback_chat", lambda *_args, **_kwargs: pytest.fail("BYOK must not fall back"))
    monkeypatch.setenv("LLM_REASONING_EFFORT", "max")
    quote = api.estimate(d["vid"], d["actor"])
    assert quote["ready"] and quote["payer"] == "founder"
    started, payload = claimed(d)
    frozen = json.loads(runtime.read(started["job_id"]).payload_json)["_runtime"]["quote"]
    assert frozen["reasoning_policy"] == ({"planning": "low", "reviewer": "low", "generation": "none"}
        if provider != "openai_compatible" else {"planning": None, "reviewer": None, "generation": None})
    call = runtime.job_model_call(started["job_id"], payload)
    for stage, effort in [("brief_direction_plan", "low"), ("capability_review_1", "low"), ("code", "none")]:
        assert call(stage, "Bounded offline goal") == call(stage, "Bounded offline goal") == "known offline output"
        operation = runtime.journal.operation(started["job_id"], "variant_0:" + stage)
        assert operation["state"] == "completed" and operation["usage"]["settled"]
        assert operation["usage"]["source"] == "byok" and operation["usage"]["credits_micro"] == 0
        request = requests[-1]
        body = json.loads(request.content)
        assert body["model"] == resolved.model and body["max_tokens"] == api.MAX_OUTPUT_TOKENS
        assert "max_completion_tokens" not in body and "thinking" not in body
        assert body.get("reasoning_effort") == (effort if provider != "openai_compatible" else None)
        assert request.headers["x-stainless-retry-count"] == "0" and request.extensions["timeout"]["read"] == frozen["call_timeout_seconds"] == 300
        assert str(request.url) == resolved.base_url + "/chat/completions" and clients[-1].is_closed()
    assert len(requests) == 3
    assert call("plan_repair_2", "Bounded repair") == call("plan_repair_2", "Bounded repair") == "known offline output"
    repair_body = json.loads(requests[-1].content)
    assert len(requests) == 4 and repair_body["max_tokens"] == 8192
    assert requests[-1].extensions["timeout"]["read"] == 180
    assert repair_body.get("thinking") == ({"type": "disabled"} if provider != "openai_compatible" else None)
    assert "reasoning_effort" not in repair_body
    account = get_account(d["vid"])
    assert account.spent_micro == account.reserved_micro == 0
    # A changed captured endpoint is refused before another SDK request.
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda _: replace(resolved, base_url="https://other.invalid/v1"))
    with pytest.raises(FatalError, match="pagebuilder_resources_changed"):
        call("repair_1", "No changed-provider request")
    assert len(requests) == 4
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda _: resolved)
    from app.credits import estimate
    original_price = estimate._lookup_model_row
    def updated_price(model):
        row = original_price(model)
        return {**row, "output_per_mtok": float(row["output_per_mtok"]) * 1.01} if row else None
    monkeypatch.setattr(estimate, "_lookup_model_row", updated_price)
    with pytest.raises(FatalError, match="pagebuilder_resources_changed"):
        call("repair_2", "No changed-price request")
    assert len(requests) == 4


def test_provider_call_with_active_renderer_loop_preserves_scope_and_settles_once(setup, monkeypatch):
    import asyncio
    import threading
    from app import tenancy
    from app.credits.accounts import get_account
    from app.pagebuilder import runtime
    from llm_gateway import sole
    d = setup
    started, payload = claimed(d)
    parent_thread = threading.get_ident()
    calls = []

    async def transmit(**kwargs):
        scope = tenancy.current()
        assert (scope.workspace_id, scope.venture_id, scope.actor) == (d["wid"], d["vid"], d["actor"])
        assert threading.get_ident() != parent_thread
        assert get_account(d["vid"]).reserved_micro > 0
        assert kwargs["single_attempt"] is True
        calls.append(kwargs["purpose"])
        return response()

    monkeypatch.setattr(sole, "transmit", transmit)
    call = runtime.job_model_call(started["job_id"], payload)

    async def running_renderer_loop():
        with tenancy.bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
            assert call("code", "Generate the page") == "model-produced-content"
            assert call("code", "Generate the page") == "model-produced-content"

    asyncio.run(running_renderer_loop())
    assert calls == ["pagebuilder.code"]
    account = get_account(d["vid"])
    assert account.spent_micro > 0 and account.reserved_micro == 0
    operation = runtime.journal.operation(started["job_id"], "variant_0:code")
    assert operation["state"] == "completed" and operation["usage"]["settled"]


@pytest.mark.parametrize("paid", [False, True])
def test_three_undelivered_failures_leave_version_allowance_available(setup, monkeypatch, paid):
    from app.db.models import Job
    from app.pagebuilder import api
    from core.clock import utcnow
    from sqlalchemy import select
    d = setup
    ids = []
    for _ in range(3):
        started = api.build(d["vid"], d["actor"])
        ids.append(started["job_id"])
        with d["factory"]() as session:
            row = session.get(Job, started["job_id"])
            doc = json.loads(row.payload_json)
            doc["_runtime"].update(state="failed", stage="failed", progress=.3, error="pagebuilder_qa_failed" if paid else "pagebuilder_renderer_unavailable",
                calls_used=int(paid), operations={"variant_0:code": {"state": "completed", "usage": {"credits_micro": 5}}} if paid else {})
            row.payload_json, row.status, row.attempts = api.dump(doc), "failed_dead", 3
            row.locked_by, row.locked_at, row.started_at, row.finished_at, row.last_error = "old-worker", utcnow(), utcnow(), utcnow(), "old-failure"
            session.commit()
    last = api.build(d["vid"], d["actor"])
    assert last["state"] == "queued"
    assert (last["job_id"] not in ids) if paid else (last["job_id"] == ids[-1])
    assert len(set(ids)) == (3 if paid else 1)
    with d["factory"]() as session:
        rows = session.scalars(select(Job).where(Job.kind == api.KIND, Job.venture_id == d["vid"])).all()
        assert len(rows) == (4 if paid else 1)
        if not paid:
            row = rows[0]
            assert row.attempts == 0 and row.status == "scheduled" and row.scheduled_for is not None
            assert row.locked_by is row.locked_at is row.started_at is row.finished_at is row.last_error is None
            doc = json.loads(row.payload_json)
            assert doc["_runtime"]["state"] == doc["_runtime"]["stage"] == "queued" and doc["_runtime"]["progress"] == 0
            assert "error" not in doc["_runtime"] and doc["_runtime"]["operations"] == {}


def _legacy_failed_slots(d, *, paid_latest=False):
    """Seed the distinct retry rows created before free failures reused a slot."""
    from datetime import timedelta
    from app.db.models import Job
    from app.pagebuilder import api
    from core.clock import utcnow
    from core.ids import new_id
    first = api.start(d["vid"], d["actor"])
    ids = []
    with d["factory"]() as session:
        original = session.get(Job, first["job_id"])
        doc = json.loads(original.payload_json)
        base_key, created = original.dedupe_key, utcnow() - timedelta(minutes=5)
        for attempt in range(1, 4):
            key = base_key if attempt == 1 else f"{base_key}:{attempt}"
            row = original if attempt == 1 else Job(id=new_id("job"), kind=api.KIND,
                workspace_id=d["wid"], venture_id=d["vid"], dedupe_key=key, effect_key=key,
                max_attempts=3, timeout_s=original.timeout_s)
            paid = paid_latest and attempt == 3
            doc["_runtime"].update(state="failed", stage="failed", progress=.3,
                error="pagebuilder_qa_failed" if paid else "pagebuilder_renderer_unavailable",
                calls_used=int(paid), operations={"variant_0:code": {"state": "completed",
                    "usage": {"credits_micro": 5}}} if paid else {})
            row.created_at = created + timedelta(seconds=attempt)
            row.scheduled_for = row.started_at = row.finished_at = row.created_at
            row.payload_json, row.status, row.attempts = api.dump(doc), "failed_dead", 3
            row.locked_by, row.locked_at, row.last_error = "old-worker", row.created_at, doc["_runtime"]["error"]
            session.add(row)
            ids.append(row.id)
        session.commit()
    return ids


@pytest.mark.parametrize("paid_latest", [False, True])
def test_legacy_free_retry_stays_current_through_ready_without_new_job(setup, monkeypatch, paid_latest):
    from unittest.mock import Mock
    from sqlalchemy import select
    from app.api.pagebuilder_api import profile
    from app.core.jobs import complete_job, lease
    from app.db.models import Job
    from app.pagebuilder import api, jobs, native_reference, rendering
    from app.pagebuilder_admission import inputs, publication
    d = setup
    ids = _legacy_failed_slots(d, paid_latest=paid_latest)
    selected = ids[1] if paid_latest else ids[2]
    with d["factory"]() as session:
        before = {row.id: (row.created_at, row.payload_json) for row in session.scalars(
            select(Job).where(Job.id.in_(ids))).all()}
    wake = Mock()
    monkeypatch.setattr(api, "publish_wake", wake)
    retried = api.build(d["vid"], d["actor"])
    assert retried["job_id"] == selected and retried["state"] == "queued"
    current = profile(d["vid"], d["actor"])["current"]
    assert current["job_id"] == selected and current["state"] == "queued" and current["queue_state"] == "scheduled"
    assert current["consecutive_failures"] == 0 and current["original_creator_required"] is False
    assert api.build(d["vid"], d["actor"])["job_id"] == selected and wake.call_count == 1
    with d["factory"]() as session:
        rows = session.scalars(select(Job).where(Job.kind == api.KIND, Job.venture_id == d["vid"])).all()
        assert {row.id for row in rows} == set(ids)
        assert all(row.created_at == before[row.id][0] for row in rows)
        assert all(row.payload_json == before[row.id][1] for row in rows if row.id != selected)
        job = lease(session, "legacy-recovery-worker", kinds=[api.KIND], n=1)[0]
        assert job.id == selected
        session.commit()
    install_pipeline_stub(monkeypatch)
    monkeypatch.setattr(rendering, "require_renderer", lambda: None)
    monkeypatch.setattr(native_reference, "persist_job", lambda *_: [])
    jobs.run({**job.payload, "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts})
    with d["factory"]() as session:
        complete_job(session, job.id, worker_id=job.worker_id, attempt=job.attempts)
        session.commit()
    monkeypatch.setattr(native_reference, "resolve_candidate", lambda *_: {})
    monkeypatch.setattr(publication, "current", lambda *_: None)
    def no_publication(*_args, **_kwargs):
        raise ValueError("pagebuilder_admission_landing_required")
    monkeypatch.setattr(inputs, "current", no_publication)
    current = profile(d["vid"], d["actor"])["current"]
    assert current["job_id"] == selected and current["state"] == "ready" and current["queue_state"] == "succeeded"
    assert current["native_ready"] and current["candidates"] and not current["server_publish_ready"]
    assert current["preview_url"] and current["consecutive_failures"] == 0


def test_legacy_free_retry_never_requeues_another_slot_while_an_older_job_is_active(setup, monkeypatch):
    from unittest.mock import Mock
    from app.api.pagebuilder_api import profile
    from app.db.models import Job
    from app.pagebuilder import api
    d = setup
    ids = _legacy_failed_slots(d)
    with d["factory"]() as session:
        row = session.get(Job, ids[0])
        doc = json.loads(row.payload_json)
        doc["_runtime"].update(state="running", stage="code", progress=.7)
        row.status, row.payload_json = "leased", api.dump(doc)
        session.commit()
        before = {job_id: session.get(Job, job_id).payload_json for job_id in ids}
    wake = Mock()
    monkeypatch.setattr(api, "publish_wake", wake)
    assert api.build(d["vid"], d["actor"])["job_id"] == ids[0]
    current = profile(d["vid"], d["actor"])["current"]
    assert current["job_id"] == ids[0] and current["state"] == "running" and current["queue_state"] == "leased"
    wake.assert_not_called()
    with d["factory"]() as session:
        assert all(session.get(Job, job_id).payload_json == before[job_id] for job_id in ids)
        assert all(session.get(Job, job_id).status == "failed_dead" for job_id in ids[1:])


@pytest.mark.parametrize("scheduled", [False, True])
def test_profile_admits_old_recovered_job_before_bounded_newer_revisions(setup, monkeypatch, scheduled):
    from datetime import timedelta
    from app.api.pagebuilder_api import profile
    from app.db.models import Job
    from app.pagebuilder import api
    from core.clock import utcnow
    from core.ids import new_id
    d = setup
    started = api.build(d["vid"], d["actor"])
    with d["factory"]() as session:
        row = session.get(Job, started["job_id"])
        doc = json.loads(row.payload_json)
        doc["_runtime"].update(state="failed", error="pagebuilder_renderer_unavailable")
        old_created = utcnow() - timedelta(days=1)
        row.created_at = row.scheduled_for = old_created
        row.payload_json, row.status, row.attempts = api.dump(doc), "failed_dead", 3
        for index in range(20):
            newer = utcnow() - timedelta(hours=1) + timedelta(seconds=index)
            other = {**doc, "revision_id": "revision_other_" + str(index)}
            key = "other_revision:" + new_id("job")
            session.add(Job(id=new_id("job"), kind=api.KIND, workspace_id=d["wid"], venture_id=d["vid"],
                dedupe_key=key, effect_key=key, payload_json=api.dump(other), status="failed_dead",
                attempts=3, created_at=newer, scheduled_for=newer if scheduled else None))
        session.commit()
        created_before = session.get(Job, row.id).created_at
    monkeypatch.setattr(api, "publish_wake", lambda: None)
    recovered = api.build(d["vid"], d["actor"])
    assert recovered["job_id"] == started["job_id"] and recovered["state"] == "queued"
    current = profile(d["vid"], d["actor"])["current"]
    assert current["job_id"] == started["job_id"] and current["queue_state"] == "scheduled"
    assert current["consecutive_failures"] == 0
    with d["factory"]() as session:
        assert session.get(Job, started["job_id"]).created_at == created_before


def test_free_failure_cannot_be_requeued_for_another_actor(setup, monkeypatch):
    from app.db.models import Job
    from app.pagebuilder import api
    d = setup
    started = api.build(d["vid"], d["actor"])
    with d["factory"]() as session:
        row = session.get(Job, started["job_id"])
        doc = json.loads(row.payload_json)
        doc["_runtime"].update(state="failed", error="pagebuilder_renderer_unavailable")
        row.payload_json, row.status, row.attempts = api.dump(doc), "failed_dead", 3
        session.commit()
    monkeypatch.setattr(api, "authority", lambda *_args, **_kwargs: (d["wid"], "another-founder"))
    found = api.build(d["vid"], "another-founder")
    assert found["job_id"] == started["job_id"] and found["state"] == "failed"
    with d["factory"]() as session:
        assert session.get(Job, started["job_id"]).attempts == 3


@pytest.mark.parametrize("attempt", [1, 2])
def test_free_renderer_outage_waits_for_bounded_job_retry_without_spend(setup, monkeypatch, attempt):
    from app.db.models import Job
    from app.core.jobs import RetryableError
    from app.credits.accounts import get_account
    from app.pagebuilder import jobs, rendering, runtime
    from llm_gateway import sole
    d = setup
    started, payload = claimed(d)
    with d["factory"]() as session:
        session.get(Job, started["job_id"]).attempts = attempt
        session.commit()
    payload["_job_attempt"] = attempt
    def unavailable():
        raise ValueError("pagebuilder_renderer_unavailable")
    monkeypatch.setattr(rendering, "require_renderer", unavailable)
    monkeypatch.setattr(sole, "transmit", lambda **_: pytest.fail("a renderer outage cannot dispatch a paid call"))
    with pytest.raises(RetryableError, match="^pagebuilder_renderer_unavailable$"):
        jobs.run(payload)
    saved = json.loads(runtime.read(started["job_id"]).payload_json)["_runtime"]
    assert saved["state"] == saved["stage"] == "queued" and "error" not in saved
    assert saved["calls_used"] == 0 and saved["operations"] == {}
    account = get_account(d["vid"])
    assert account.spent_micro == account.reserved_micro == 0


@pytest.mark.parametrize("fault", ["actor", "revision", "same_revision_inputs", "resources", "missing_ops", "possibly_sent", "noninfra", "cancelled", "reservation", "effect"])
def test_free_requeue_requires_unchanged_approval_and_proven_no_dispatch(setup, fault):
    from app.db.models import Job
    from app.pagebuilder import api
    from app.credits.accounts import get_account
    from app.credits.reserve import reserve
    from core.clock import utcnow
    from core.ids import new_id
    d = setup
    started = api.build(d["vid"], d["actor"])
    held = None
    if fault == "reservation":
        held = reserve(account_id=d["account"].id, amount_micro=10, purpose="pagebuilder.code", model="DeepSeek-V4-Pro",
            provider="azure_foundry", workspace_id=d["wid"], request_id=started["job_id"] + ":variant_0:code:1")
    before = get_account(d["vid"])
    with d["factory"]() as session:
        row = session.get(Job, started["job_id"])
        doc = json.loads(row.payload_json)
        doc["_runtime"].update(state="failed", error="pagebuilder_renderer_unavailable")
        row.status, row.attempts = "failed_dead", 3
        if fault == "actor":
            doc["_actor_subject"] = "another-actor"
        elif fault == "revision":
            doc["revision_id"] = "another-revision"
        elif fault == "same_revision_inputs":
            doc["_runtime"]["input"]["idea"] += " Changed data without a new revision."
        elif fault == "resources":
            doc["_runtime"]["quote"]["resource_digest"] = "changed-approval"
        elif fault == "missing_ops":
            doc["_runtime"].pop("operations")
        elif fault == "possibly_sent":
            doc["_runtime"]["operations"] = {"variant_0:code": {"state": "calling"}}
        elif fault == "noninfra":
            doc["_runtime"]["error"] = "pagebuilder_inputs_changed"
        elif fault == "cancelled":
            row.status = "cancelled"
        elif fault == "effect":
            session.add(Job(id=new_id("job"), kind=api.KIND, workspace_id=d["wid"], venture_id=d["vid"],
                payload_json=api.dump(doc), status="succeeded", effect_key=row.effect_key,
                dedupe_key=row.dedupe_key + ":other", created_at=utcnow()))
            session.flush()
        row.payload_json = api.dump(doc)
        snapshot = row.payload_json
        _revision, current_inputs = api.snapshot(session, d["vid"], d["wid"])
        q = api.resources(d["wid"], d["vid"])
        assert not api._requeue_free_failure(session, row, actor=d["actor"], revision_id=d["rid"], inputs=current_inputs, resource_digest=q["resource_digest"])
        assert row.payload_json == snapshot and row.attempts == 3 and row.status == ("cancelled" if fault == "cancelled" else "failed_dead")
        session.commit()
    after = get_account(d["vid"])
    assert (after.spent_micro, after.reserved_micro) == (before.spent_micro, before.reserved_micro)
    if held:
        from app.db.models import CreditReservation
        with d["factory"]() as session:
            assert session.get(CreditReservation, held.reservation_id).status == "active"


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
@pytest.mark.parametrize("via_chat", [False, True])
def test_local_job_replays_saved_planning_model_to_preview_and_export(setup, monkeypatch, domain, via_chat):
    from app.build import device_orders
    from app.pagebuilder import tool
    from core.artifact_snapshot import manifest_digest
    from app import coding_agents_consent
    from desktop_runtime import agent_status
    monkeypatch.setattr(coding_agents_consent, "consents", lambda actor: {"codex": True})
    monkeypatch.setattr(agent_status, "statuses", lambda: [{"name": "codex", "installed": True,
        "signed_in": True, "available": True, "subscription_login": True}])
    orders = {}
    started_orders = []
    def start_order(venture_id, order):
        assert venture_id == data["vid"] and order["state"] == "approved"
        started_orders.append(order["id"])
        order["state"] = "running_coding"
    monkeypatch.setattr(tool, "_start_order", start_order)
    prepare_landing = device_orders.prepare_landing_page_from_job
    def prepare_page(venture_id, caller, job_id, instruction, *, provider):
        order = prepare_landing(venture_id, caller, job_id, instruction, provider=provider)
        assert order["state"] == "approved" and order["requested_build"]["request_sha256"]
        assert order["manifest"]["coding_loop_verification"]["profile"] == "static_landing_v1"
        assert order["task_snapshot"]["pagebuilder_job_id"] == job_id
        orders[order["id"]] = order
        assert provider == "codex" and "no backend" in instruction and "light theme" in instruction
        return order
    monkeypatch.setattr(device_orders, "prepare_landing_page_from_job", prepare_page, raising=False)
    monkeypatch.setattr(device_orders, "retrieve", lambda venture, caller, order_id: orders[order_id])
    def complete_page(job_id):
        saved = runtime.read(job_id)
        doc = json.loads(saved.payload_json)
        inputs = models.PageInput.model_validate(doc["_runtime"]["input"])
        reviewed = pipeline.read_tool_plan(doc["tool_build"]["plan"])
        from app.pagebuilder import images
        from app.pagebuilder.local_publish import signup_page
        selected = pipeline.scene_draft(reviewed).model_dump(mode="json") if reviewed is not None else json.loads(json.dumps(draft))
        if reviewed is None and doc.get("rewrite"):
            selected["sections"][0]["headline"] += "?"
        plan = final_plan({"models": models, "pipeline": pipeline, "images": images}, inputs, selected)
        files = {asset.path: asset.svg.encode() for asset in plan.assets}
        files["page-plan.txt"] = json.dumps(selected).encode()
        if signup := inputs.research.get("local_signup"):
            files["waitlist.html"] = signup_page(inputs.venture_id, inputs.name, signup).encode()
        files["index.html"] = fixture_html(plan, inputs).encode()
        from tests.unit.pagebuilder.test_tool_build import _with_social_preview
        files = _with_social_preview(files, inputs)
        candidate = tool.bundle(files, inputs, reviewed, doc["tool_build"]["grounding"])
        order = orders[doc["tool_build"]["order_id"]]
        order.update(workspace_id=saved.workspace_id, actor_id=doc["_actor_subject"], state="stopped",
            execution_id="dex_page",
            native_stop={"execution_id": "dex_page", "processes_stopped": True, "task_verified": True, "verification_kind": "workspace_loop_v1"},
            iterations=[{"outcome": "pass", "file_manifest_sha256": manifest_digest(candidate["manifest"]["manifest"])}])
        with data["factory"]() as session:
            session.get(VentureDocument, order["id"]).provenance_json = api.dump(order)
            session.commit()
        return tool.finish(saved.venture_id, doc["_actor_subject"], job_id, order["id"], "dex_page",
            base64.b64decode(candidate["source_zip"]))
    import base64
    import io
    import zipfile
    from fastapi import FastAPI
    from tests.unit.test_local_mode import _owner_client
    from sqlalchemy import select
    from app.api import pagebuilder_api
    from app.core.jobs import complete_job, lease
    from app.core.providers_local import save_settings
    from app.credits import accounts
    from app.db.models import CreditReservation, Job, Venture, VentureDocument
    from app.db.supymem_models import IdeaRevision
    from app.pagebuilder import api, jobs, models, pipeline, rendering, runtime
    from tests.unit.pagebuilder.test_pipeline import customer_draft, final_plan, fixture_html, review_payload, stub_render

    data = setup
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
    monkeypatch.delenv("SUPYMEM_PAGEBUILDER_ENABLED")
    from app.core.local_owner import ensure_owner
    data = seed(setup["factory"], actor=ensure_owner().id)
    save_settings(provider="azure_openai", base_url="https://provider.example.invalid/v1",
        api_key="synthetic-local-test-key", models={"chat": "DeepSeek-V4-Flash", "planning": "DeepSeek-V4-Pro"})
    monkeypatch.setenv("SUPYMEM_MODEL_PLANNING", "stale-environment-model")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "synthetic-stale-environment-key")
    monkeypatch.setattr(accounts, "get_account", lambda *_: pytest.fail("Local pages must not require a credit account"))
    monkeypatch.setattr(rendering, "_browser", lambda: pytest.fail("Local preview must not launch a browser"))
    preflights = []
    monkeypatch.setattr(rendering, "require_renderer", lambda: preflights.append(True))
    def render(html, files):
        from app.pagebuilder.qa import PageParser

        parser = PageParser()
        parser.feed(html)
        controls = [{"id": attributes.get("id"), "role": attributes.get("role", "link"),
            "name": attributes.get("aria-label") or " ".join(attributes.get("_label", "").split()),
            "target": attributes.get("href"), "enabled": "hidden" not in attributes and "disabled" not in attributes}
            for tag, attributes, _parents in parser.elements if tag == "a"]
        result = stub_render(html, files)
        for observation in result["observations"].values():
            observation.update(visibleControls=controls,
                checks={check: True for check in ("contrast", "image_clipping", "hero_cta_initial")},
                contrastSkipped=0, contrastUnresolved=[], imageClippingFailures=[],
                initialHeroCta={"visible": True, "withinViewport": True, "hitCoverage": True})
        return result
    monkeypatch.setattr(rendering, "render_page", render)
    raw, draft = customer_draft(domain)
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.name, venture.raw_idea = raw["name"], raw["idea"]
        revision = session.get(IdeaRevision, data["rid"])
        revision.summary, revision.structured = raw["idea"], json.dumps({"audience": draft["brief"]["audience"]})
        session.commit()
    transmitted = []

    def replay(dial, timeout):
        assert not preflights
        stage = dial["purpose"].split(".", 1)[1]
        transmitted.append(dial["trace_id"])
        assert dial["config"].deployment_pro == "DeepSeek-V4-Pro"
        assert dial["config"].source == "founder_override"
        assert dial["config"].api_key.get_secret_value() == "synthetic-local-test-key"
        prompt = dial["messages"][-1]["content"]
        selected = json.loads(json.dumps(draft))
        if ":variant_1:" in dial["trace_id"]:
            selected["direction"]["layout_archetype"] = "An editorial split composition"
        row = runtime.read(dial["trace_id"].split(":", 1)[0])
        saved = json.loads(row.payload_json)
        if saved.get("rewrite"):
            selected["sections"][0]["headline"] += "?"
        if stage == "brief_direction_plan":
            content = json.dumps(selected)
        elif stage.startswith("capability_review_"):
            content = json.dumps(review_payload(prompt))
        else:
            inputs = models.PageInput.model_validate(saved["_runtime"]["input"])
            content = pipeline.fallback_html(pipeline.compile_draft_plan(inputs, selected), inputs)
        return SimpleNamespace(model="DeepSeek-V4-Pro", usage={"prompt_tokens": 40, "completion_tokens": 100, "total_tokens": 140},
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))])

    monkeypatch.setattr(runtime, "_provider_response", replay)
    from app.core.local_owner import LocalOwnerMiddleware

    app = FastAPI()
    app.add_middleware(LocalOwnerMiddleware)
    app.include_router(pagebuilder_api.router, prefix="/api")
    with _owner_client(app) as client:
        prefix = f"/api/ventures/{data['vid']}/pagebuilder"
        variants = 1
        if via_chat:
            import asyncio
            from app.chat.execute import execute_action
            started = asyncio.run(execute_action(data["vid"], "pagebuilder.build",
                payload={"prepare": True}, actor_subject=data["actor"], founder_initiated=True))
            assert started["kind"] != "confirmation"
            assert not any(word in started["body_md"].lower() for word in ("credit", "ceiling", "quote"))
            started = {"job_id": started["run_id"]}
        else:
            response = client.post(prefix + "/start", headers={"X-Subject": data["actor"]}, json={"variants": variants})
            assert response.status_code == 202, response.text
            started = response.json()
        with data["factory"]() as session:
            job = lease(session, "local-page-worker", kinds=[api.KIND], n=1)[0]
            assert job.id == started["job_id"]
            session.commit()
        payload = {**job.payload, "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts}
        from app.tenancy import bind
        with bind(workspace_id=data["wid"], venture_id=data["vid"], actor=data["actor"]):
            jobs.run(payload)
            calls = len(transmitted)
            jobs.run(payload)
            assert len(transmitted) == calls
            assert started_orders == list(orders) and len(started_orders) == 1
        with data["factory"]() as session:
            complete_job(session, job.id, worker_id=job.worker_id, attempt=job.attempts)
            session.commit()
            complete_page(job.id)
            assert not session.scalars(select(CreditReservation)).all()
            references = session.scalars(select(VentureDocument).where(VentureDocument.kind == "pagebuilder_native_reference_v1")).all()
            assert len(references) == variants
            assert all(json.loads(row.provenance_json)["receipt"]["storage"] == "local" for row in references)
            forged = json.loads(session.get(Job, job.id).payload_json)
            forged["result"]["bundles"][0]["qa"]["render"]["browser_rendered"] = False
            forged["result"]["bundles"][0]["manifest"].pop("preview_method", None)
            assert api._candidate_current(forged) is False
        result = api.result(data["vid"], data["actor"], job.id)
        assert result["state"] == "ready" and len(result["variants"]) == variants
        from copy import deepcopy
        source = json.loads(runtime.read(job.id).payload_json)
        assert api._candidate_current(source)
        with monkeypatch.context() as updated:
            updated.setattr(api, "REQUEST_POLICY", api.REQUEST_POLICY + "_updated")
            updated.setattr(api, "CALL_TIMEOUT_SECONDS", api.CALL_TIMEOUT_SECONDS + 1)
            updated.setattr(api, "MAX_OUTPUT_TOKENS", api.MAX_OUTPUT_TOKENS + 1)
            updated.setattr(api, "CALLS_PER_VARIANT", api.CALLS_PER_VARIANT + 1)
            updated.setattr(api, "reasoning_policy", lambda *_: {"planning": None, "reviewer": None, "generation": None})
            data["factory"].kw["bind"].dispose()
            restored = client.get(prefix + "/profile", params={"job_id": job.id}).json()["current"]
            assert restored["state"] == "ready" and restored["candidates"]
            assert client.get(prefix + f"/jobs/{job.id}/preview-document").status_code == 200
            assert client.get(prefix + f"/jobs/{job.id}/source").status_code == 200
            from core.clock import utcnow
            from app.core.jobs import reclaim_stale
            with data["factory"]() as session:
                row = session.get(Job, job.id)
                row.status, row.locked_by = "leased", "stopped-page-worker"
                row.attempts = row.max_attempts
                row.locked_at = utcnow()
                session.commit()
            with data["factory"]() as session:
                assert reclaim_stale(session) == 1
                session.commit()
                assert session.get(Job, job.id).status == "succeeded"
                assert reclaim_stale(session) == 0
                row = session.get(Job, job.id)
                row.status, row.last_error = "failed_dead", "pagebuilder_candidate_invalid"
                historical = json.loads(row.payload_json)
                assert historical["result"]["ready_delivered"] is True
                historical["_runtime"].update(state="failed", error="pagebuilder_candidate_policy_obsolete")
                row.payload_json = api.dump(historical)
                session.commit()
            with data["factory"]() as session:
                assert reclaim_stale(session) == 1
                session.commit()
                assert session.get(Job, job.id).status == "succeeded"
                assert reclaim_stale(session) == 0
            assert client.get(prefix + "/profile", params={"job_id": job.id}).json()["current"]["state"] == "ready"
        tampered = deepcopy(source)
        tampered["result"]["bundles"][0]["files"]["index.html"] = base64.b64encode(b"<script>unsafe()</script>").decode()
        assert not api._candidate_current(tampered)
        assert source["tool_build"]["provider"] == "codex"
        assert all(":code:" not in trace and ":repair_" not in trace for trace in transmitted)
        bundles = [pagebuilder_api.bundle(data["vid"], data["actor"], job.id, variant) for variant in range(variants)]
        assert "credits" not in result["receipt"] and "ceiling_micro" not in result["receipt"]
        for variant in range(variants):
            url = prefix + f"/jobs/{job.id}"
            preview = client.get(url + f"/preview?variant={variant}", headers={"X-Subject": data["actor"]})
            assert preview.status_code == 200 and raw["name"] in preview.text
            assert "connect-src 'none'" in preview.headers["content-security-policy"]
            exported = client.get(url + f"/source?variant={variant}", headers={"X-Subject": data["actor"]})
            assert exported.status_code == 200
            with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
                assert archive.read("index.html") == base64.b64decode(bundles[variant]["files"]["index.html"])
        assert client.get(f"/api/ventures/not-owned/pagebuilder/jobs/{job.id}/source").status_code == 404
        with pytest.raises(HTTPException) as refused:
            api.result(data["vid"], "another-owner", job.id)
        assert refused.value.status_code == 404
        headline = draft["sections"][0]["headline"]
        edited = api.rewrite(data["vid"], data["actor"], new_version_of=job.id,
            instruction=f'Replace "{headline}" with "{headline}?"')
        with data["factory"]() as session:
            edit = lease(session, "local-edit-worker", kinds=[api.KIND], n=1)[0]
            session.commit()
        edit_payload = {**edit.payload, "_job_id": edit.id, "_worker_id": edit.worker_id, "_job_attempt": edit.attempts}
        with bind(workspace_id=data["wid"], venture_id=data["vid"], actor=data["actor"]):
            jobs.run(edit_payload)
        with data["factory"]() as session:
            complete_job(session, edit.id, worker_id=edit.worker_id, attempt=edit.attempts)
            session.commit()
            complete_page(edit.id)
        revised = api.result(data["vid"], data["actor"], edited["job_id"])
        assert revised["state"] == "ready"
        changed = pagebuilder_api.bundle(data["vid"], data["actor"], edit.id, 0)
        assert headline + "?" in base64.b64decode(changed["files"]["index.html"]).decode()
        assert pagebuilder_api.bundle(data["vid"], data["actor"], job.id, 0)["source_zip"] == bundles[0]["source_zip"]
        assert started_orders == list(orders) and len(started_orders) == 2


@pytest.mark.parametrize("idea", ["Inventory planning for independent grocers", "Family bicycle repair lessons"])
@pytest.mark.parametrize("in_flight", [False, True])
def test_local_interrupted_page_is_not_restarted_and_posts_one_retry_notice(setup, monkeypatch, idea, in_flight):
    from datetime import timedelta
    from core.clock import utcnow
    from sqlalchemy import select
    from app.core.jobs import lease, reclaim_stale
    from app.db.models import ChatMessage, Job, Venture
    from app.db.supymem_models import IdeaRevision
    from app.pagebuilder import api

    data = setup
    job_id = api.build(data["vid"], data["actor"])["job_id"]
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = idea
        session.get(IdeaRevision, data["rid"]).summary = idea
        lease(session, "stopped-page-worker", kinds=[api.KIND])
        row = session.get(Job, job_id)
        row.locked_at = utcnow() - timedelta(seconds=120)
        doc = json.loads(row.payload_json)
        doc["_runtime"].update(state="running", stage="generating")
        if in_flight:
            doc["_runtime"]["operations"]["variant_0:code"] = {"state": "calling"}
        row.payload_json = api.dump(doc)
        session.commit()
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    connect_local_tool(monkeypatch)
    from app.core.providers_local import save_settings
    save_settings(provider="azure_openai", base_url="https://provider.example.invalid/v1",
        api_key="synthetic-local-test-key", models={"chat": "DeepSeek-V4-Flash", "planning": "DeepSeek-V4-Pro"})
    with data["factory"]() as session:
        assert reclaim_stale(session) == 1
        session.commit()
        assert session.get(Job, job_id).status == "failed_dead"
        assert reclaim_stale(session) == 0
        assert lease(session, "new-page-worker", kinds=[api.KIND]) == []
        session.commit()
        notices = [row for row in session.scalars(select(ChatMessage)).all()
            if "interrupted" in row.body_md.lower()]
        assert len(notices) == 1
        assert "Website" in notices[0].body_md
        assert ("Refresh status" if in_flight else "Retry") in notices[0].body_md
        actions = json.loads(notices[0].actions_json or "[]")
        assert [action["payload"] for action in actions if action.get("label") == "Retry"] == (
            [] if in_flight else [{"retry_job_id": job_id}])
    saved = api.status(data["vid"], data["actor"], job_id)
    assert saved["state"] == "failed" and saved["error"] == "pagebuilder_interrupted"
    assert "preview_url" not in saved
    assert ("retry" in saved) is not in_flight


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
@pytest.mark.parametrize("build_state", ["ready", "off", "needs_setup"])
def test_local_landing_readiness_requires_your_connected_build_tool(setup, monkeypatch, domain, build_state):
    from app.core import local_capabilities
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setitem(local_capabilities.REGISTRY, "build_with_my_tool", ("Build with your coding tool", True))
    monkeypatch.setattr(local_capabilities, "read_settings", lambda section: {"values": {
        "landing_pages": True, "build_with_my_tool": build_state != "off"}})
    monkeypatch.setattr(local_capabilities, "_missing", lambda name: [("coding_tools", "Your coding tool")]
        if name == "build_with_my_tool" and build_state == "needs_setup" else [])
    landing = local_capabilities.capability("landing_pages")
    assert landing["state"] == ("ready" if build_state == "ready" else "needs_setup")
    if build_state != "ready":
        assert "Your connected coding tool" in landing["missing"]
        if build_state == "off":
            assert "Settings > Capabilities" in landing["next_step"]
        else:
            assert "Settings > Keys and connections > Coding tools" in landing["next_step"]


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
def test_local_failure_names_settings_and_has_retry_without_exposing_provider_text(setup, monkeypatch, domain):
    import httpx
    from app.core.jobs import FatalError
    from app.core.providers_local import save_settings
    from app.pagebuilder import api, runtime

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    connect_local_tool(monkeypatch)
    save_settings(provider="azure_openai", base_url="https://provider.example.invalid/v1",
        api_key="synthetic-local-failure-key", models={"chat": "DeepSeek-V4-Pro"})
    started, payload = claimed(setup)
    def rejected(*_args):
        request = httpx.Request("POST", "https://provider.example.invalid/v1/chat/completions")
        response = httpx.Response(401, request=request, text="private provider body " + domain)
        response.raise_for_status()
    monkeypatch.setattr(runtime, "_provider_response", rejected)
    from app.tenancy import bind
    with bind(workspace_id=setup["wid"], venture_id=setup["vid"], actor=setup["actor"]):
        with pytest.raises(FatalError, match="pagebuilder_provider_rejected") as failed:
            runtime.job_model_call(started["job_id"], payload)("brief_direction_plan", "Design your proposed landing page.")
        runtime.fail(started["job_id"], payload, failed.value)
    result = api.status(setup["vid"], setup["actor"], started["job_id"])
    assert result["state"] == "failed"
    assert "Settings > Keys and connections > Your AI provider" in result["error_reason"]
    assert "Retry" in result["error_reason"]
    assert result["retry"]["method"] == "POST" and result["retry"]["body"]["new_version_of"] == started["job_id"]
    assert "private provider body" not in json.dumps(result)
    operation = json.loads(runtime.read(started["job_id"]).payload_json)["_runtime"]["operations"]["variant_0:brief_direction_plan"]
    assert operation["state"] == "failed" and operation["usage"]["settled"] is True
    assert operation["usage"]["credits_micro"] == 0


@pytest.mark.parametrize("code,detail", [
    ("pagebuilder_provider_rejected", "Warehouse repair scheduling"),
    ("pagebuilder_byok_unavailable", "Family weekend walks"),
])
def test_local_page_failure_uses_settings_labels(code, detail):
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.pagebuilder.local import failure_reason

    message = failure_reason(f"{code}:{detail}")
    assert "service URL" in message and "model API key" in message
    assert "Settings > Keys and connections > Your AI provider" in message
    assert "Retry" in message
    assert detail not in message and code not in message


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
def test_local_spending_limit_names_the_setting_before_provider_dispatch(setup, monkeypatch, domain):
    from app.core.jobs import FatalError
    from app.core.local_secrets import update_settings
    from app.core.providers_local import save_settings
    from app.pagebuilder import api, runtime
    from app.tenancy import bind

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    connect_local_tool(monkeypatch)
    save_settings(provider="azure_openai", base_url="https://provider.example.invalid/v1",
        api_key="synthetic-spend-limit-key", models={"chat": "DeepSeek-V4-Pro"})
    update_settings("spend", {"monthly_spend_cap": 0}, {})
    started, payload = claimed(setup)
    monkeypatch.setattr(runtime, "_provider_response", lambda *_: pytest.fail("A spending limit must stop provider dispatch"))
    with bind(workspace_id=setup["wid"], venture_id=setup["vid"], actor=setup["actor"]):
        with pytest.raises(FatalError) as failed:
            runtime.job_model_call(started["job_id"], payload)("brief_direction_plan", "A proposed " + domain + " page")
        runtime.fail(started["job_id"], payload, failed.value)
    status = api.status(setup["vid"], setup["actor"], started["job_id"])
    assert status["error"] == "pagebuilder_spend_limit"
    assert "Monthly limit (USD)" in status["error_reason"] and "Retry" in status["error_reason"]


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
def test_local_model_key_is_not_saved_in_generated_output(setup, monkeypatch, domain):
    from app.core.jobs import FatalError
    from app.core.providers_local import save_settings
    from app.pagebuilder import runtime
    from app.tenancy import bind

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    connect_local_tool(monkeypatch)
    key = "synthetic-response-echo-key-" + domain
    save_settings(provider="azure_openai", base_url="https://provider.example.invalid/v1",
        api_key=key, models={"chat": "DeepSeek-V4-Pro"})
    started, payload = claimed(setup)
    def echo(dial, _timeout):
        assert dial["config"].api_key.get_secret_value() == key
        return SimpleNamespace(model="DeepSeek-V4-Pro",
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="/* " + key + " */"))])
    monkeypatch.setattr(runtime, "_provider_response", echo)
    with bind(workspace_id=setup["wid"], venture_id=setup["vid"], actor=setup["actor"]):
        with pytest.raises(FatalError, match="pagebuilder_invalid_output"):
            runtime.job_model_call(started["job_id"], payload)("brief_direction_plan", "Write your proposed page brief.")
    assert key not in runtime.read(started["job_id"]).payload_json
